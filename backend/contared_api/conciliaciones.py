import hashlib
import json
import logging
import re
import shutil
from calendar import monthrange
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.banking import paginate, scope_from_request
from contared_api.imports import (
    BALANCE_FIELDS,
    bind_preview,
    cleanup_expired_previews,
    create_preview,
    get_preview,
    inspect_workbook_sheet,
    parse_decimal,
    validate_workbook,
    workbook_preview,
)
from contared_api.session_access import (
    DatabaseUnavailable,
    database_error_details,
    load_session_access,
)


logger = logging.getLogger(__name__)

BANCO_INDUSTRIAL_TRANSACTION_TYPES = {
    "CQ": "Pago de Cheque",
    "NC": "Nota de crédito",
    "ND": "Nota de débito",
    "DE": "Depósito",
}


class ImportStorageUnavailable(APIException):
    status_code = 503
    default_detail = "El almacenamiento privado de documentos no está disponible."
    default_code = "document_storage_unavailable"


class ImportConflict(APIException):
    status_code = 409
    default_detail = "El archivo ya fue importado para esta cuenta y origen."
    default_code = "duplicate_import"


class ReconciliationConflict(APIException):
    status_code = 409
    default_code = "reconciliation_conflict"


class ArchiveMigrationRequired(APIException):
    status_code = 503
    default_detail = (
        "Las acciones de archivo requieren aplicar "
        "supabase/migrations/202610300003_archivo_conciliaciones.sql."
    )
    default_code = "reconciliation_archive_migration_required"


ARCHIVE_REFERENCE_SPECS = (
    ("cargas", "SELECT EXISTS(SELECT 1 FROM contared.cargas WHERE conciliacion_id=%s)"),
    ("movimientos", """SELECT EXISTS(SELECT 1 FROM contared.cargas c
        JOIN contared.movimientos m ON m.carga_id=c.id WHERE c.conciliacion_id=%s)"""),
    ("cruces", "SELECT EXISTS(SELECT 1 FROM contared.cruces WHERE conciliacion_id=%s)"),
    ("cruce_movimientos", """SELECT EXISTS(SELECT 1 FROM contared.cruces x
        JOIN contared.cruce_movimientos cm ON cm.cruce_id=x.id WHERE x.conciliacion_id=%s)"""),
    ("pendientes", """SELECT EXISTS(SELECT 1 FROM contared.pendientes
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s)"""),
    ("ajustes", "SELECT EXISTS(SELECT 1 FROM contared.ajustes WHERE conciliacion_id=%s)"),
    ("reportes", "SELECT EXISTS(SELECT 1 FROM contared.reportes WHERE conciliacion_id=%s)"),
    ("ejecuciones_comparacion", """SELECT EXISTS(SELECT 1 FROM contared.ejecuciones_comparacion
        WHERE conciliacion_id=%s)"""),
    ("propuestas_comparacion", """SELECT EXISTS(SELECT 1 FROM contared.propuestas_comparacion
        WHERE conciliacion_id=%s)"""),
    ("ejecucion_propuestas", """SELECT EXISTS(SELECT 1 FROM contared.ejecucion_propuestas
        WHERE conciliacion_id=%s)"""),
    ("cheques", """SELECT EXISTS(SELECT 1 FROM contared.cheques
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s)"""),
    ("cheque_eventos", "SELECT EXISTS(SELECT 1 FROM contared.cheque_eventos WHERE conciliacion_id=%s)"),
    ("casos_deposito", """SELECT EXISTS(SELECT 1 FROM contared.casos_deposito
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s)"""),
    ("deposito_movimientos", """SELECT EXISTS(SELECT 1 FROM contared.casos_deposito d
        JOIN contared.deposito_movimientos dm ON dm.caso_id=d.id
        WHERE d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s)"""),
    ("deposito_eventos", """SELECT EXISTS(SELECT 1 FROM contared.deposito_eventos e
        JOIN contared.casos_deposito d ON d.id=e.caso_id
        WHERE e.conciliacion_id=%s OR d.conciliacion_origen_id=%s
           OR d.conciliacion_resolucion_id=%s)"""),
    ("eventos_archivo_conciliacion", """SELECT EXISTS(
        SELECT 1 FROM contared.eventos_archivo_conciliacion
        WHERE conciliacion_id=%s)"""),
)


def _archive_schema_tables():
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT table_name,column_name
               FROM information_schema.columns WHERE table_schema='contared'"""
        )
        columns = {}
        for table_name, column_name in cursor.fetchall():
            columns.setdefault(table_name, set()).add(column_name)
    if not {
        "archivada_en", "archivada_por", "motivo_archivo"
    }.issubset(columns.get("conciliaciones", set())) or not {
        "conciliacion_id", "accion", "actor_id", "ocurrido_en", "motivo"
    }.issubset(columns.get("eventos_archivo_conciliacion", set())):
        return None
    return columns


def _archive_reference_specs(available_tables):
    return [
        (table, sql)
        for table, sql in ARCHIVE_REFERENCE_SPECS
        if table in available_tables
    ]


def _has_reconciliation_references(cursor, reconciliation_id, available_tables):
    for _, sql in _archive_reference_specs(available_tables):
        cursor.execute(sql, [reconciliation_id] * sql.count("%s"))
        if cursor.fetchone()[0]:
            return True
    return False


def _reconciliation_archive_blockers(cursor, reconciliation_id, available_tables):
    blockers = []
    checks = (
        ("cargas", """SELECT EXISTS(SELECT 1 FROM contared.cargas
            WHERE conciliacion_id=%s AND estado='pendiente')""",
         "hay una carga pendiente de confirmar"),
        ("cruces", """SELECT EXISTS(SELECT 1 FROM contared.cruces
            WHERE conciliacion_id=%s AND estado='propuesto')""",
         "hay cruces por revisar"),
        ("propuestas_comparacion", """SELECT EXISTS(SELECT 1 FROM contared.propuestas_comparacion
            WHERE conciliacion_id=%s AND estado='propuesta')""",
         "hay propuestas de comparación por revisar"),
        ("pendientes", """SELECT EXISTS(SELECT 1 FROM contared.pendientes
            WHERE estado='abierto' AND (conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s))""",
         "hay pendientes abiertos"),
        ("cheques", """SELECT EXISTS(SELECT 1 FROM contared.cheques
            WHERE estado IN ('retenido','en_circulacion')
              AND (conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s))""",
         "hay cheques retenidos o en circulación"),
        ("casos_deposito", """SELECT EXISTS(SELECT 1 FROM contared.casos_deposito
            WHERE estado='abierto' AND (conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s))""",
         "hay seguimientos de depósitos abiertos"),
        ("pendientes", """SELECT EXISTS(SELECT 1 FROM contared.pendientes
            WHERE conciliacion_resolucion_id=%s AND conciliacion_origen_id<>%s)""",
         "hay pendientes de otros períodos que hacen referencia a esta conciliación"),
        ("cheques", """SELECT EXISTS(SELECT 1 FROM contared.cheques
            WHERE conciliacion_resolucion_id=%s AND conciliacion_origen_id<>%s)""",
         "hay cheques de otros períodos que hacen referencia a esta conciliación"),
        ("casos_deposito", """SELECT EXISTS(SELECT 1 FROM contared.casos_deposito
            WHERE conciliacion_resolucion_id=%s AND conciliacion_origen_id<>%s)""",
         "hay depósitos de otros períodos que hacen referencia a esta conciliación"),
        ("cheque_eventos", """SELECT EXISTS(SELECT 1 FROM contared.cheque_eventos ev
            JOIN contared.cheques ch ON ch.id=ev.cheque_id
            WHERE ev.conciliacion_id=%s AND ch.conciliacion_origen_id<>%s)""",
         "hay eventos de cheques de otros períodos que hacen referencia a esta conciliación"),
        ("deposito_eventos", """SELECT EXISTS(SELECT 1 FROM contared.deposito_eventos ev
            JOIN contared.casos_deposito d ON d.id=ev.caso_id
            WHERE ev.conciliacion_id=%s AND d.conciliacion_origen_id<>%s)""",
         "hay eventos de depósitos de otros períodos que hacen referencia a esta conciliación"),
    )
    for table, sql, message in checks:
        if table not in available_tables:
            continue
        cursor.execute(sql, [reconciliation_id] * sql.count("%s"))
        if cursor.fetchone()[0]:
            blockers.append(message)
    return blockers


def _normalize_original_data(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            logger.error("ContaRed movement original data is not valid JSON.")
            raise APIException(
                "No se pudieron leer los datos originales del movimiento.",
                code="invalid_original_data",
            ) from exc
    if value is None or isinstance(value, dict):
        return value
    logger.error("ContaRed movement original data is not a JSON object.")
    raise APIException(
        "Los datos originales del movimiento no tienen un formato válido.",
        code="invalid_original_data",
    )


def _is_banco_industrial(bank_name):
    normalized = re.sub(r"[^a-z0-9]", "", str(bank_name or "").casefold())
    return normalized in {"bancoindustrial", "bancoindustrialdeguatemala"}


def _valid_identifier(value):
    if value is None:
        return False
    normalized = str(value).strip()
    return bool(normalized) and not re.fullmatch(r"0+", normalized)


def _transaction_type_label(code, configuration, bank_name=None):
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except json.JSONDecodeError as exc:
            raise APIException(
                "No se pudo leer el catálogo del perfil de importación.",
                code="invalid_import_profile",
            ) from exc
    catalog = (
        configuration.get("catalogo_tipo_transaccion", {})
        if isinstance(configuration, dict)
        else {}
    )
    if isinstance(catalog, dict) and isinstance(code, str):
        label = catalog.get(code)
        if isinstance(label, str) and label.strip():
            return label.strip()
    if _is_banco_industrial(bank_name):
        return BANCO_INDUSTRIAL_TRANSACTION_TYPES.get(
            code,
            "Tipo de transacción sin identificar",
        )
    return "Tipo de transacción sin identificar"


def _apply_banco_industrial_fields(movements, *, origin, bank_name):
    if origin != "banco" or not _is_banco_industrial(bank_name):
        return
    for movement in movements:
        if (
            movement.get("codigo_tt") == "CQ"
            and not _valid_identifier(movement.get("numero_cheque"))
            and _valid_identifier(movement.get("referencia"))
        ):
            movement["numero_cheque"] = movement["referencia"]


def _writer(scope):
    if scope["role"] not in ("administrador", "auxiliar"):
        raise PermissionDenied(
            "Esta operación requiere el rol administrador o auxiliar.",
            code="writer_required",
        )
    if not scope.get("reconciliation_id"):
        return
    try:
        if _archive_schema_tables() is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT archivada_en FROM contared.conciliaciones
                       WHERE id=%s AND cuenta_id=%s""",
                    [scope["reconciliation_id"], scope["account_id"]],
                )
                row = cursor.fetchone()
            if row is None:
                raise NotFound("La conciliación no existe.")
            if row[0] is not None:
                raise ReconciliationConflict(
                    "La conciliación está archivada y solo permite consulta."
                )
    except DatabaseError as exc:
        logger.error(
            "Reconciliation write access check failed (%s).",
            database_error_details(exc),
        )
        raise DatabaseUnavailable() from exc


def _scope_for_reconciliation(request, reconciliation_id):
    session = load_session_access(request.user.auth_user_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    """
                    SELECT cb.empresa_id::text,cb.pais_codigo,r.id::text,
                           cb.id::text,cb.banco_id::text,r.periodo,r.fecha_corte,
                           r.estado,r.saldo_inicial_banco,r.saldo_inicial_libros,
                           r.saldo_final_banco,r.saldo_final_libros,cb.numero,
                           cb.moneda_codigo,b.nombre,cb.codigo_contable
                    FROM contared.conciliaciones AS r
                    JOIN contared.cuentas_bancarias AS cb ON cb.id=r.cuenta_id
                    JOIN contared.bancos AS b ON b.id=cb.banco_id
                    WHERE r.id=%s
                    """,
                    [reconciliation_id],
                )
                row = cursor.fetchone()
    except DatabaseError as exc:
        logger.error(
            "ContaRed reconciliation scope lookup failed (%s).",
            database_error_details(exc),
        )
        raise DatabaseUnavailable() from exc
    if row is None:
        raise NotFound("La conciliación no existe.")
    access = next(
        (
            item for item in session["accesos"]
            if item["empresa_id"] == row[0] and item["pais_codigo"] == row[1]
        ),
        None,
    )
    if access is None:
        raise NotFound("La conciliación no existe.")
    return {
        "user_id": session["usuario"]["id"],
        "user_name": session["usuario"]["nombre"],
        "company_id": row[0],
        "country_code": row[1],
        "role": access["rol"],
        "reconciliation_id": row[2],
        "account_id": row[3],
        "bank_id": row[4],
        "period": row[5],
        "cutoff": row[6],
        "state": row[7],
        "saldo_inicial_banco": row[8],
        "saldo_inicial_libros": row[9],
        "saldo_final_banco": row[10],
        "saldo_final_libros": row[11],
        "account_number": row[12],
        "currency_code": row[13],
        "bank_name": row[14],
        "accounting_code": row[15],
    }


def _scope_for_account(request, account_id):
    session = load_session_access(request.user.auth_user_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    """SELECT cb.empresa_id::text,cb.pais_codigo,cb.numero,
                              cb.moneda_codigo,b.nombre,cb.codigo_contable
                       FROM contared.cuentas_bancarias cb
                       JOIN contared.bancos b ON b.id=cb.banco_id
                       WHERE cb.id=%s""",
                    [account_id],
                )
                row = cursor.fetchone()
    except DatabaseError as exc:
        logger.error(
            "ContaRed account scope lookup failed (%s).",
            database_error_details(exc),
        )
        raise DatabaseUnavailable() from exc
    if row is None:
        raise NotFound("La cuenta no pertenece al ámbito autorizado.")
    access = next(
        (
            item for item in session["accesos"]
            if item["empresa_id"] == row[0] and item["pais_codigo"] == row[1]
        ),
        None,
    )
    if access is None:
        raise NotFound("La cuenta no pertenece al ámbito autorizado.")
    return {
        "user_id": session["usuario"]["id"],
        "company_id": row[0],
        "country_code": row[1],
        "role": access["rol"],
        "account_number": row[2],
        "currency_code": row[3],
        "bank_name": row[4],
        "accounting_code": row[5],
    }


def _reconciliation_row(row):
    return {
        "id": row[0],
        "cuenta_id": row[1],
        "cuenta": row[2],
        "banco": row[3],
        "moneda_codigo": row[4],
        "periodo": row[5].isoformat(),
        "fecha_corte": row[6].isoformat(),
        "estado": row[7],
        "saldo_inicial_banco": str(row[8]) if row[8] is not None else None,
        "saldo_inicial_libros": str(row[9]) if row[9] is not None else None,
        "saldo_final_banco": str(row[10]) if row[10] is not None else None,
        "saldo_final_libros": str(row[11]) if row[11] is not None else None,
        "creada_en": row[12].isoformat() if row[12] is not None else None,
    }


def _period(value):
    if not isinstance(value, str):
        raise ValidationError({"periodo": "periodo debe ser una fecha ISO del primer día del mes."})
    try:
        result = date.fromisoformat(f"{value}-01" if len(value) == 7 else value)
    except ValueError as exc:
        raise ValidationError({"periodo": "periodo debe ser una fecha ISO del primer día del mes."}) from exc
    if result.day != 1:
        raise ValidationError({"periodo": "periodo debe corresponder al primer día del mes."})
    return result


def _iso_date(value, field):
    if not isinstance(value, str):
        raise ValidationError({field: f"{field} debe tener formato YYYY-MM-DD."})
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError({field: f"{field} debe tener formato YYYY-MM-DD."}) from exc


def _balance(value, field):
    if value is None:
        return None
    try:
        return parse_decimal(value, field)
    except ValueError as exc:
        raise ValidationError({field: str(exc)}) from exc


def _paging(query):
    try:
        page = int(query.get("page", 1))
        page_size = int(query.get("page_size", 25))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"page": "page y page_size deben ser números enteros."}) from exc
    if page < 1:
        raise ValidationError({"page": "page debe ser mayor que cero."})
    if page_size < 1:
        raise ValidationError({"page_size": "page_size debe ser mayor que cero."})
    return page, min(page_size, 100)


class ReconciliationAccountListView(APIView):
    def get(self, request):
        scope = scope_from_request(request)
        page, page_size = _paging(request.query_params)
        query = (request.query_params.get("search") or "").strip()
        clauses = ["cb.empresa_id=%s", "cb.pais_codigo=%s"]
        params = [scope["company_id"], scope["country_code"]]
        if query:
            clauses.append("(cb.alias ILIKE %s OR cb.numero ILIKE %s OR b.nombre ILIKE %s)")
            params.extend([f"%{query}%"] * 3)
        where = " AND ".join(clauses)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    data = paginate(
                        cursor,
                        f"""SELECT COUNT(*) FROM contared.cuentas_bancarias cb
                            JOIN contared.bancos b ON b.id=cb.banco_id WHERE {where}""",
                        f"""SELECT cb.id::text,cb.alias,cb.numero,cb.moneda_codigo,cb.activa,
                                   b.nombre,b.activo,cb.codigo_contable
                            FROM contared.cuentas_bancarias cb
                            JOIN contared.bancos b ON b.id=cb.banco_id
                            WHERE {where}
                            ORDER BY b.nombre,cb.alias LIMIT %s OFFSET %s""",
                        params,
                        page,
                        page_size,
                    )
        except DatabaseError as exc:
            logger.error("ContaRed reconciliation account lookup failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            **{key: data[key] for key in ("count", "page", "page_size")},
            "results": [
                {
                    "id": row[0],
                    "alias": row[1],
                    "numero": row[2],
                    "moneda_codigo": row[3],
                    "activa": row[4],
                    "banco": row[5],
                    "banco_activo": row[6],
                    "codigo_contable": row[7],
                }
                for row in data["results"]
            ],
        })


class ReconciliationListCreateView(APIView):
    def get(self, request):
        scope = scope_from_request(request)
        page, page_size = _paging(request.query_params)
        clauses = ["cb.empresa_id=%s", "cb.pais_codigo=%s"]
        params = [scope["company_id"], scope["country_code"]]
        archive_filter = request.query_params.get("archivo", "activas")
        if archive_filter not in ("activas", "archivadas", "todas"):
            raise ValidationError({"archivo": "Usa activas, archivadas o todas."})
        account_id = request.query_params.get("cuenta_id")
        if account_id:
            clauses.append("r.cuenta_id=%s")
            params.append(account_id)
        counter_clauses = list(clauses)
        counter_params = list(params)
        query = """
            FROM contared.conciliaciones r
            JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
            JOIN contared.bancos b ON b.id=cb.banco_id
        """
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    archive_tables = _archive_schema_tables()
                    from contared_api.conciliacion_eliminacion import _deletion_schema_available

                    deletion_available = _deletion_schema_available()
                    if archive_tables is not None:
                        if archive_filter == "activas":
                            clauses.append("r.archivada_en IS NULL")
                        elif archive_filter == "archivadas":
                            clauses.append("r.archivada_en IS NOT NULL")
                        predicates = [
                            sql.replace("%s", "r.id")
                            for _, sql in _archive_reference_specs(archive_tables)
                        ]
                        has_information = (
                            " OR ".join(f"({predicate})" for predicate in predicates)
                            if predicates else "FALSE"
                        )
                        archive_projection = (
                            "r.archivada_en,r.motivo_archivo,"
                            f"CASE WHEN {has_information} THEN TRUE ELSE FALSE END"
                        )
                        cursor.execute(
                            f"""SELECT count(*) FILTER (WHERE r.archivada_en IS NULL),
                                       count(*) FILTER (WHERE r.archivada_en IS NOT NULL)
                                {query} WHERE {" AND ".join(counter_clauses)}""",
                            counter_params,
                        )
                        archive_counts = cursor.fetchone()
                    else:
                        archive_projection = "NULL::timestamptz,NULL::text,FALSE"
                        cursor.execute(
                            f"SELECT count(*) {query} WHERE {' AND '.join(counter_clauses)}",
                            counter_params,
                        )
                        archive_counts = (cursor.fetchone()[0], 0)
                        if archive_filter != "activas":
                            raise ArchiveMigrationRequired()
                    where = " AND ".join(clauses)
                    data = paginate(
                        cursor,
                        f"SELECT COUNT(*) {query} WHERE {where}",
                        f"""SELECT r.id::text,r.cuenta_id::text,cb.alias,b.nombre,
                                   cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,
                                   r.saldo_inicial_banco,r.saldo_inicial_libros,
                                   r.saldo_final_banco,r.saldo_final_libros,r.creada_en,
                                   {archive_projection}
                            {query} WHERE {where}
                            ORDER BY r.periodo DESC,b.nombre,cb.alias LIMIT %s OFFSET %s""",
                        params,
                        page,
                        page_size,
                    )
        except DatabaseError as exc:
            logger.error("ContaRed reconciliation list failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            **{key: data[key] for key in ("count", "page", "page_size")},
            "results": [
                {
                    **_reconciliation_row(row[:13]),
                    "archivada_en": row[13].isoformat() if row[13] else None,
                    "motivo_archivo": row[14],
                    "tiene_informacion": row[15],
                }
                for row in data["results"]
            ],
            "archivo_disponible": archive_tables is not None,
            "eliminacion_disponible": deletion_available,
            "conteos_archivo": {
                "activas": archive_counts[0],
                "archivadas": archive_counts[1],
            },
        })

    def post(self, request):
        account_id = request.data.get("cuenta_id")
        if not account_id:
            raise ValidationError({"cuenta_id": "La cuenta es obligatoria."})
        try:
            account_uuid = UUID(str(account_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"cuenta_id": "La cuenta no es válida."}) from exc
        scope = _scope_for_account(request, account_uuid)
        _writer(scope)
        if set(request.data) - {"cuenta_id", "periodo", "fecha_corte", *BALANCE_FIELDS}:
            raise ValidationError({"detail": "La solicitud contiene campos no permitidos."})
        period = _period(request.data.get("periodo"))
        cutoff = _iso_date(request.data.get("fecha_corte"), "fecha_corte")
        if cutoff < period or cutoff.year != period.year or cutoff.month != period.month:
            raise ValidationError({"fecha_corte": "La fecha de corte debe estar dentro del periodo."})
        balances = {field: _balance(request.data.get(field), field) for field in BALANCE_FIELDS}
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT cb.id::text FROM contared.cuentas_bancarias cb
                           JOIN contared.bancos b ON b.id=cb.banco_id
                           WHERE cb.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s
                             AND cb.activa IS TRUE AND b.activo IS TRUE""",
                        [account_uuid, scope["company_id"], scope["country_code"]],
                    )
                    if cursor.fetchone() is None:
                        raise ValidationError({"cuenta_id": "Selecciona una cuenta bancaria activa del ámbito autorizado."})
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    cursor.execute(
                        """INSERT INTO contared.conciliaciones
                           (cuenta_id,periodo,fecha_corte,saldo_inicial_banco,
                            saldo_inicial_libros,saldo_final_banco,saldo_final_libros,creada_por)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           RETURNING id::text""",
                        [
                            account_uuid, period, cutoff, balances["saldo_inicial_banco"],
                            balances["saldo_inicial_libros"], balances["saldo_final_banco"],
                            balances["saldo_final_libros"], scope["user_id"],
                        ],
                    )
                    reconciliation_id = cursor.fetchone()[0]
        except IntegrityError as exc:
            raise ValidationError({"periodo": "Ya existe una conciliación para esta cuenta y periodo."}) from exc
        except DatabaseError as exc:
            logger.error("ContaRed reconciliation creation failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({"id": reconciliation_id}, status=201)


class ReconciliationDetailView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        archive_available = False
        archive_history = []
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    archive_tables = _archive_schema_tables()
                    archive_available = archive_tables is not None
                    archive_columns = (
                        ",r.archivada_en,r.motivo_archivo,"
                        "COALESCE(usuario_archivo.nombre,'Usuario autorizado')"
                        if archive_available else ""
                    )
                    archive_join = (
                        "LEFT JOIN contared.usuarios usuario_archivo "
                        "ON usuario_archivo.id=r.archivada_por"
                        if archive_available else
                        "LEFT JOIN contared.usuarios usuario_archivo ON FALSE"
                    )
                    cursor.execute(
                        """SELECT r.id::text,r.cuenta_id::text,cb.alias,b.nombre,
                                  cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,
                                  r.saldo_inicial_banco,r.saldo_inicial_libros,
                                  r.saldo_final_banco,r.saldo_final_libros,r.creada_en,
                                  r.creada_por::text,r.notas"""
                        + archive_columns
                        + """,
                                  r.saldo_final_banco_detectado,r.saldo_final_banco_fuente,
                                  r.saldo_final_libros_detectado,r.saldo_final_libros_fuente"""
                        + """
                           FROM contared.conciliaciones r
                           JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
                           JOIN contared.bancos b ON b.id=cb.banco_id
                           """
                        + archive_join
                        + """
                           WHERE r.id=%s AND r.cuenta_id=%s""",
                        [reconciliation_id, scope["account_id"]],
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        if archive_available:
                            cursor.execute(
                                """SELECT ev.accion,ev.ocurrido_en,ev.motivo,u.nombre
                                   FROM contared.eventos_archivo_conciliacion ev
                                   LEFT JOIN contared.usuarios u ON u.id=ev.actor_id
                                   WHERE ev.conciliacion_id=%s
                                   ORDER BY ev.ocurrido_en,ev.id""",
                                [reconciliation_id],
                            )
                            archive_history = [
                                {
                                    "accion": event[0],
                                    "ocurrido_en": event[1].isoformat(),
                                    "motivo": event[2],
                                    "usuario": event[3],
                                }
                                for event in cursor.fetchall()
                            ]
                        cursor.execute(
                            """
                            SELECT
                                count(DISTINCT c.id),
                                count(m.id) FILTER (WHERE m.lado='banco'),
                                count(m.id) FILTER (WHERE m.lado='libros'),
                                sum(m.monto) FILTER (WHERE m.lado='banco' AND m.monto > 0),
                                sum(m.monto) FILTER (WHERE m.lado='banco' AND m.monto < 0),
                                sum(m.monto) FILTER (WHERE m.lado='libros' AND m.monto > 0),
                                sum(m.monto) FILTER (WHERE m.lado='libros' AND m.monto < 0)
                            FROM contared.cargas c
                            LEFT JOIN contared.movimientos m ON m.carga_id=c.id
                            WHERE c.conciliacion_id=%s
                            """,
                            [reconciliation_id],
                        )
                        summary = cursor.fetchone()
                        cursor.execute(
                            """
                            SELECT c.id::text,c.origen,c.metodo,c.nombre_archivo,c.sha256,
                                   c.estado,c.creada_en,count(m.id)
                            FROM contared.cargas c
                            LEFT JOIN contared.movimientos m ON m.carga_id=c.id
                            WHERE c.conciliacion_id=%s
                            GROUP BY c.id
                            ORDER BY c.creada_en,c.id
                            """,
                            [reconciliation_id],
                        )
                        loads = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("ContaRed reconciliation detail failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        if row is None:
            raise NotFound("La conciliación no existe.")
        summary_data = summary or (0, 0, 0, None, None, None, None)
        archived_at = row[15] if archive_available else None
        return Response({
            **_reconciliation_row(row[:13]),
            "creada_por": row[13],
            "notas": row[14],
            "archivo_disponible": archive_available,
            "archivada_en": archived_at.isoformat() if archived_at else None,
            "archivada_por_nombre": row[17] if archive_available else None,
            "motivo_archivo": row[16] if archive_available else None,
            "historial_archivo": archive_history,
            "resumen": {
                "cantidad_cargas": summary_data[0],
                "movimientos_banco": summary_data[1] or 0,
                "movimientos_libros": summary_data[2] or 0,
                "ingresos_banco": str(summary_data[3]) if summary_data[3] is not None else None,
                "salidas_banco": str(summary_data[4]) if summary_data[4] is not None else None,
                "ingresos_libros": str(summary_data[5]) if summary_data[5] is not None else None,
                "salidas_libros": str(summary_data[6]) if summary_data[6] is not None else None,
            },
            "balance_sources": {
                field: "manual" if row[index] is not None else None
                for field, index in (
                    ("saldo_inicial_banco", 8),
                    ("saldo_inicial_libros", 9),
                    ("saldo_final_banco", 10),
                    ("saldo_final_libros", 11),
                )
            },
            "saldos_finales_detectados": {
                "banco": {
                    "importe": str(row[-4]) if row[-4] is not None else None,
                    "fuente": row[-3],
                },
                "libros": {
                    "importe": str(row[-2]) if row[-2] is not None else None,
                    "fuente": row[-1],
                },
            },
            "cargas": [
                {
                    "id": load[0],
                    "origen": load[1],
                    "metodo": load[2],
                    "nombre_archivo": load[3],
                    "sha256": load[4],
                    "estado": load[5],
                    "creada_en": load[6].isoformat() if load[6] else None,
                    "movimientos": load[7],
                }
                for load in loads
            ],
        })

    def patch(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _writer(scope)
        allowed = BALANCE_FIELDS
        if not request.data or set(request.data) - allowed:
            raise ValidationError({"detail": "Solo se pueden editar saldos de una conciliación en borrador."})
        balances = {field: _balance(value, field) for field, value in request.data.items()}
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT estado FROM contared.conciliaciones WHERE id=%s AND cuenta_id=%s FOR UPDATE",
                        [reconciliation_id, scope["account_id"]],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise NotFound("La conciliación no existe.")
                    if row[0] != "borrador":
                        raise ValidationError({"estado": "Solo se pueden editar saldos en estado borrador."})
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    fields = list(balances)
                    cursor.execute(
                        f"UPDATE contared.conciliaciones SET {','.join(f'{field}=%s' for field in fields)} "
                        "WHERE id=%s AND cuenta_id=%s",
                        [*balances.values(), reconciliation_id, scope["account_id"]],
                    )
                    cursor.execute(
                        """SELECT r.id::text,r.cuenta_id::text,cb.alias,b.nombre,
                                  cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,
                                  r.saldo_inicial_banco,r.saldo_inicial_libros,
                                  r.saldo_final_banco,r.saldo_final_libros,r.creada_en
                           FROM contared.conciliaciones r
                           JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
                           JOIN contared.bancos b ON b.id=cb.banco_id
                           WHERE r.id=%s""",
                        [reconciliation_id],
                    )
                    updated = cursor.fetchone()
        except DatabaseError as exc:
            logger.error("ContaRed reconciliation update failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response(_reconciliation_row(updated))


class ReconciliationArchiveView(APIView):
    def delete(self, request, reconciliation_id):
        raise ValidationError({
            "detail": (
                "La eliminación requiere consultar primero la vista previa y confirmar "
                "la frase irreversible en /eliminacion/."
            ),
            "code": "deletion_preview_required",
        })

    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        if scope["role"] != "administrador":
            raise PermissionDenied("Solo un administrador puede archivar o restaurar conciliaciones.")
        try:
            available_tables = _archive_schema_tables()
        except DatabaseError as exc:
            logger.error("Reconciliation archive schema check failed (error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        if available_tables is None:
            raise ArchiveMigrationRequired()
        if set(request.data) - {"accion", "motivo", "confirmar"}:
            raise ValidationError({"detail": "La solicitud contiene campos no permitidos."})
        action = request.data.get("accion")
        if action not in ("archivar", "restaurar"):
            raise ValidationError({"accion": "Selecciona archivar o restaurar."})
        reason = request.data.get("motivo", "")
        if not isinstance(reason, str):
            raise ValidationError({"motivo": "Indica un motivo válido."})
        reason = reason.strip()
        if action == "archivar":
            if request.data.get("confirmar") is not True:
                raise ValidationError({"confirmar": "Confirma explícitamente que deseas archivar."})
            if not reason:
                raise ValidationError({"motivo": "El motivo de archivo es obligatorio."})
            if len(reason) > 1000:
                raise ValidationError({"motivo": "El motivo no puede superar 1,000 caracteres."})
        elif len(reason) > 1000:
            raise ValidationError({"motivo": "El motivo no puede superar 1,000 caracteres."})
        elif request.data.get("confirmar") is not True:
            raise ValidationError({"confirmar": "Confirma explícitamente que deseas restaurar."})
        elif not reason:
            reason = "Restauración por administrador"

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT estado,archivada_en
                           FROM contared.conciliaciones
                           WHERE id=%s AND cuenta_id=%s FOR UPDATE""",
                        [reconciliation_id, scope["account_id"]],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise NotFound("La conciliación no existe.")
                    if action == "archivar":
                        if row[1] is not None:
                            return Response({
                                "id": str(reconciliation_id),
                                "archivada": True,
                                "sin_cambios": True,
                            })
                        if not _has_reconciliation_references(
                            cursor, reconciliation_id, available_tables
                        ):
                            raise ReconciliationConflict(
                                "La conciliación no tiene información asociada. "
                                "Puedes eliminar el borrador vacío."
                            )
                        blockers = _reconciliation_archive_blockers(
                            cursor, reconciliation_id, available_tables
                        )
                        if blockers:
                            raise ReconciliationConflict(
                                "No se puede archivar todavía: " + "; ".join(blockers) + "."
                            )
                    elif row[1] is None:
                        return Response({
                            "id": str(reconciliation_id),
                            "archivada": False,
                            "sin_cambios": True,
                        })
                    cursor.execute(
                        "SELECT set_config('contared.actor_id',%s,true)",
                        [scope["user_id"]],
                    )
                    if action == "archivar":
                        cursor.execute(
                            """UPDATE contared.conciliaciones
                               SET archivada_en=now(),archivada_por=%s,motivo_archivo=%s
                               WHERE id=%s AND cuenta_id=%s""",
                            [
                                scope["user_id"], reason, reconciliation_id,
                                scope["account_id"],
                            ],
                        )
                        event_action = "archivada"
                    else:
                        cursor.execute(
                            """UPDATE contared.conciliaciones
                               SET archivada_en=NULL,archivada_por=NULL,motivo_archivo=NULL
                               WHERE id=%s AND cuenta_id=%s""",
                            [reconciliation_id, scope["account_id"]],
                        )
                        event_action = "restaurada"
                    cursor.execute(
                        """INSERT INTO contared.eventos_archivo_conciliacion
                           (conciliacion_id,accion,actor_id,motivo)
                           VALUES (%s,%s,%s,%s)""",
                        [
                            reconciliation_id, event_action, scope["user_id"], reason,
                        ],
                    )
        except DatabaseError as exc:
            logger.error(
                "Reconciliation archive action failed (error type: %s).",
                type(exc).__name__,
            )
            raise DatabaseUnavailable() from exc
        return Response({
            "id": str(reconciliation_id),
            "archivada": action == "archivar",
            "sin_cambios": False,
        })


class ReconciliationMovementsView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        side = request.query_params.get("lado")
        if side not in ("banco", "libros"):
            raise ValidationError({"lado": "lado debe ser banco o libros."})
        page, page_size = _paging(request.query_params)
        clauses = [
            "m.cuenta_id=%s",
            "m.lado=%s",
            "m.fecha >= %s",
            "m.fecha <= %s",
            "c.conciliacion_id=%s",
            "c.empresa_id=%s",
            "c.pais_codigo=%s",
        ]
        params = [
            scope["account_id"],
            side,
            scope["period"],
            scope["cutoff"],
            scope["reconciliation_id"],
            scope["company_id"],
            scope["country_code"],
        ]
        date_from = request.query_params.get("fecha_desde")
        date_to = request.query_params.get("fecha_hasta")
        if date_from:
            clauses.append("m.fecha >= %s")
            params.append(_iso_date(date_from, "fecha_desde"))
        if date_to:
            clauses.append("m.fecha <= %s")
            params.append(_iso_date(date_to, "fecha_hasta"))
        reference = (request.query_params.get("referencia") or "").strip()
        description = (request.query_params.get("descripcion") or "").strip()
        if reference:
            clauses.append("m.referencia ILIKE %s")
            params.append(f"%{reference}%")
        if description:
            clauses.append("m.descripcion ILIKE %s")
            params.append(f"%{description}%")
        where = " AND ".join(clauses)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    data = paginate(
                        cursor,
                        f"""SELECT COUNT(*)
                            FROM contared.movimientos m
                            JOIN contared.cargas c ON c.id=m.carga_id
                            WHERE {where}""",
                        f"""SELECT m.id::text,m.lado,m.fecha,m.fecha_valor,m.referencia,
                                   m.numero_transaccion,m.numero_cheque,m.codigo_tt,m.descripcion,
                                   m.monto,m.saldo_origen,m.hoja_origen,m.fila_origen,
                                   m.datos_originales,c.origen,c.metodo,c.nombre_archivo,
                                   c.estado,c.creada_en,u.nombre,p.configuracion
                            FROM contared.movimientos m
                            JOIN contared.cargas c ON c.id=m.carga_id
                            LEFT JOIN contared.perfiles_importacion p ON p.id=c.perfil_id
                            LEFT JOIN contared.usuarios u
                              ON u.id=c.creada_por AND u.activo IS TRUE
                             AND EXISTS (
                                 SELECT 1 FROM contared.accesos a
                                 WHERE a.usuario_id=u.id
                                   AND a.empresa_id=c.empresa_id
                                   AND a.pais_codigo=c.pais_codigo
                             )
                            WHERE {where}
                            ORDER BY m.fecha,m.id LIMIT %s OFFSET %s""",
                        params,
                        page,
                        page_size,
                    )
        except DatabaseError as exc:
            logger.error("ContaRed reconciliation movements failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            **{key: data[key] for key in ("count", "page", "page_size")},
            "results": [
                {
                    "id": row[0],
                    "lado": row[1],
                    "fecha": row[2].isoformat(),
                    "fecha_valor": row[3].isoformat() if row[3] else None,
                    "referencia": row[4],
                    "numero_transaccion": row[5],
                    "numero_cheque": (
                        row[6] if _valid_identifier(row[6])
                        else row[4]
                        if row[1] == "banco" and row[7] == "CQ"
                        and _is_banco_industrial(scope.get("bank_name"))
                        and _valid_identifier(row[4])
                        else None
                    ),
                    "codigo_tt": row[7],
                    "tipo_transaccion": _transaction_type_label(
                        row[7],
                        row[20],
                        scope.get("bank_name"),
                    ),
                    "descripcion": row[8],
                    "monto": str(row[9]),
                    "saldo_origen": str(row[10]) if row[10] is not None else None,
                    "hoja_origen": row[11],
                    "fila_origen": row[12],
                    "datos_originales": _normalize_original_data(row[13]),
                    "fuente": {
                        "origen": row[14],
                        "metodo": row[15],
                        "nombre_archivo": row[16],
                        "estado": row[17],
                        "creada_en": row[18].isoformat() if row[18] else None,
                        "creada_por_nombre": row[19],
                    },
                }
                for row in data["results"]
            ],
        })


def _validate_profile_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValidationError({"profile_name": "El nombre del perfil debe tener entre 1 y 120 caracteres."})
    return value.strip()


def _validate_transaction_catalog(value):
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError({
                "catalogo_tipo_transaccion": "El catálogo debe ser un objeto JSON de códigos y nombres."
            }) from exc
    if not isinstance(value, dict) or len(value) > 500:
        raise ValidationError({
            "catalogo_tipo_transaccion": "El catálogo debe ser un objeto con hasta 500 códigos."
        })
    catalog = {}
    for code, label in value.items():
        if (
            not isinstance(code, str)
            or not code.strip()
            or len(code) > 100
            or not isinstance(label, str)
            or not label.strip()
            or len(label.strip()) > 200
        ):
            raise ValidationError({
                "catalogo_tipo_transaccion": "Cada código y nombre debe ser texto válido."
            })
        catalog[code] = label.strip()
    return catalog


def _validate_options(request, extra_allowed=()):
    data = request.data
    allowed = {"sheet", "header_row", "mapping", "date_format"} | set(extra_allowed)
    if set(data) - allowed:
        raise ValidationError({"detail": "La solicitud de validación contiene campos no permitidos."})
    sheet_name = data.get("sheet")
    header_row = data.get("header_row")
    mapping = data.get("mapping")
    date_format = data.get("date_format")
    if isinstance(header_row, bool) or not (
        isinstance(header_row, int)
        or (isinstance(header_row, str) and header_row.isdigit())
    ):
        raise ValidationError({"header_row": "header_row debe ser un entero."})
    try:
        header_row = int(header_row)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"header_row": "header_row debe ser un entero."}) from exc
    return sheet_name, header_row, mapping, date_format


class ReconciliationImportProfileListView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        origin = request.query_params.get("origen")
        if origin not in ("banco", "libros"):
            raise ValidationError({"origen": "origen debe ser banco o libros."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """
                        SELECT DISTINCT ON (nombre)
                               id::text,nombre,version,origen,configuracion,creado_en
                        FROM contared.perfiles_importacion
                        WHERE empresa_id=%s AND pais_codigo=%s AND banco_id=%s AND origen=%s
                        ORDER BY nombre,version DESC,creado_en DESC
                        """,
                        [scope["company_id"], scope["country_code"], scope["bank_id"], origin],
                    )
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("ContaRed import profiles lookup failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            "results": [
                {
                    "id": row[0],
                    "nombre": row[1],
                    "version": row[2],
                    "origen": row[3],
                    "configuracion": row[4],
                    "creado_en": row[5].isoformat() if row[5] else None,
                }
                for row in rows
            ],
        })


class ReconciliationPreviewCreateView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _writer(scope)
        if scope["state"] != "borrador":
            raise ValidationError({"estado": "Solo se pueden importar movimientos en borrador."})
        origin = request.data.get("origen")
        if origin not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen debe ser banco o libros."})
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Adjunta un archivo .xlsx o .xls."})
        preview_id, metadata, path = create_preview(
            upload,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=reconciliation_id,
            origin=origin,
        )
        try:
            workbook_data = workbook_preview(path, origin=origin)
            metadata["contexto"] = workbook_data["reconocimiento"].get("contexto", {})
            saved_metadata = {key: value for key, value in metadata.items() if not key.startswith("_")}
            (path.parent / "metadata.json").write_text(json.dumps(saved_metadata), encoding="utf-8")
        except Exception:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise
        return Response({
            "preview_id": str(preview_id),
            "origen": origin,
            "nombre_archivo": metadata["nombre_archivo"],
            "sha256": metadata["sha256"],
            "expira_en": metadata["expira_en"],
            **workbook_data,
        }, status=201)


def _unbound_preview_scope(request):
    scope = scope_from_request(request)
    return {
        **scope,
        "account_id": None,
        "bank_id": None,
        "reconciliation_id": None,
    }


def _preview_coverage(data):
    month = _period(data.get("periodo"))
    first_day = month
    last_day = date(month.year, month.month, monthrange(month.year, month.month)[1])
    partial = data.get("cobertura_parcial", False)
    if not isinstance(partial, bool):
        raise ValidationError({"cobertura_parcial": "cobertura_parcial debe ser verdadero o falso."})
    if not partial:
        return first_day, last_day
    start = _iso_date(data.get("fecha_desde"), "fecha_desde")
    end = _iso_date(data.get("fecha_hasta"), "fecha_hasta")
    if start < first_day or end > last_day or start > end:
        raise ValidationError({"fecha_desde": "La cobertura parcial debe permanecer dentro del mes seleccionado."})
    return start, end


class UnboundReconciliationPreviewCreateView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        scope = _unbound_preview_scope(request)
        _writer(scope)
        if set(request.data) - {"empresa_id", "pais_codigo", "origen", "file"}:
            raise ValidationError({"detail": "La solicitud de inspección contiene campos no permitidos."})
        origin = request.data.get("origen", "banco")
        if origin not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen debe ser banco o libros."})
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Adjunta un archivo .xlsx o .xls."})
        preview_id, metadata, path = create_preview(
            upload,
            user_id=scope["user_id"],
            scope=scope,
            origin=origin,
        )
        try:
            workbook_data = workbook_preview(path, origin=origin)
            metadata["contexto"] = workbook_data["reconocimiento"].get("contexto", {})
            saved_metadata = {key: value for key, value in metadata.items() if not key.startswith("_")}
            (path.parent / "metadata.json").write_text(
                json.dumps(saved_metadata),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(path.parent, ignore_errors=True)
            raise
        return Response({
            "preview_id": str(preview_id),
            "origen": origin,
            "nombre_archivo": metadata["nombre_archivo"],
            "sha256": metadata["sha256"],
            "expira_en": metadata["expira_en"],
            **workbook_data,
        }, status=201)


class UnboundReconciliationPreviewInspectView(APIView):
    def post(self, request, preview_id):
        scope = _unbound_preview_scope(request)
        _writer(scope)
        if set(request.data) - {"empresa_id", "pais_codigo", "sheet", "header_row"}:
            raise ValidationError({"detail": "La solicitud de inspección contiene campos no permitidos."})
        metadata, path = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=None,
        )
        sheet, header_row, _, _ = _validate_options(request, {"empresa_id", "pais_codigo"})
        if not isinstance(sheet, str) or not sheet:
            raise ValidationError({"sheet": "Selecciona una hoja del archivo."})
        return Response(inspect_workbook_sheet(path, sheet_name=sheet, header_row=header_row))


class UnboundReconciliationPreviewDeleteView(APIView):
    def delete(self, request, preview_id):
        scope = _unbound_preview_scope(request)
        _writer(scope)
        metadata, _ = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=None,
            allow_unbound=True,
        )
        try:
            shutil.rmtree(Path(metadata["_source_path"]).parent)
        except OSError as exc:
            logger.exception("ContaRed temporary import preview cleanup failed.")
            raise ImportStorageUnavailable() from exc
        return Response(status=204)


class UnboundReconciliationPreviewValidateView(APIView):
    def post(self, request, preview_id):
        scope = _unbound_preview_scope(request)
        _writer(scope)
        allowed = {
            "empresa_id", "pais_codigo", "cuenta_id", "periodo", "cobertura_parcial",
            "fecha_desde", "fecha_hasta", "origen", "confirmar_normalizacion_fecha",
        }
        if set(request.data) - allowed - {"sheet", "header_row", "mapping", "date_format"}:
            raise ValidationError({"detail": "La solicitud de validación contiene campos no permitidos."})
        metadata, path = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=None,
        )
        sheet, header_row, mapping, date_format = _validate_options(request, allowed)
        origin = request.data.get("origen", metadata["origen"])
        if origin not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen debe ser banco o libros."})
        account_id = request.data.get("cuenta_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValidationError({"cuenta_id": "Selecciona la cuenta bancaria antes de validar el documento."})
        try:
            account_id = str(UUID(account_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"cuenta_id": "La cuenta seleccionada no es válida."}) from exc
        account_scope = _scope_for_account(request, account_id)
        if (
            account_scope["company_id"] != scope["company_id"]
            or account_scope["country_code"] != scope["country_code"]
        ):
            raise NotFound("La cuenta no pertenece a la empresa y país seleccionados.")
        _validate_workbook_account(metadata.get("contexto"), origin, account_scope)
        period, cutoff = _preview_coverage(request.data)
        result = validate_workbook(
            path,
            sheet_name=sheet,
            header_row=header_row,
            mapping=mapping,
            date_format=date_format,
            origin=origin,
            period=period,
            cutoff=cutoff,
            confirm_date_proposals=request.data.get("confirmar_normalizacion_fecha", False),
        )
        _apply_banco_industrial_fields(
            result.movimientos,
            origin=origin,
            bank_name=account_scope["bank_name"],
        )
        return Response({
            **_validation_response(result),
            "periodo": period.strftime("%Y-%m"),
            "fecha_desde": period.isoformat(),
            "fecha_hasta": cutoff.isoformat(),
            "cobertura_parcial": period.day != 1 or cutoff.day != monthrange(period.year, period.month)[1],
            "contexto": metadata.get("contexto", {}),
        })


class ReconciliationPreviewValidateView(APIView):
    def post(self, request, reconciliation_id, preview_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _writer(scope)
        allowed = {
            "sheet", "header_row", "mapping", "date_format",
            "excluded_rows", "save_profile", "profile_name", "origen",
            "confirmar_normalizacion_fecha",
        }
        if set(request.data) - allowed:
            raise ValidationError({"detail": "La solicitud de confirmación contiene campos no permitidos."})
        metadata, path = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=reconciliation_id,
            allow_unbound=True,
        )
        if scope["state"] != "borrador":
            raise ValidationError({"estado": "Solo se pueden importar movimientos en borrador."})
        origin = request.data.get("origen", metadata["origen"])
        if origin not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen debe ser banco o libros."})
        sheet, header_row, mapping, date_format = _validate_options(
            request,
            {"origen", "confirmar_normalizacion_fecha"},
        )
        result = validate_workbook(
            path,
            sheet_name=sheet,
            header_row=header_row,
            mapping=mapping,
            date_format=date_format,
            origin=origin,
            period=scope["period"],
            cutoff=scope["cutoff"],
            confirm_date_proposals=request.data.get("confirmar_normalizacion_fecha", False),
        )
        _apply_banco_industrial_fields(
            result.movimientos,
            origin=origin,
            bank_name=scope.get("bank_name"),
        )
        return Response(_validation_response(result))


class ReconciliationPreviewInspectView(APIView):
    def post(self, request, reconciliation_id, preview_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _writer(scope)
        metadata, path = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=reconciliation_id,
            allow_unbound=True,
        )
        if set(request.data) - {"sheet", "header_row"}:
            raise ValidationError({"detail": "La solicitud de inspección contiene campos no permitidos."})
        sheet, header_row, _, _ = _validate_options(request, {"sheet", "header_row"})
        if not isinstance(sheet, str) or not sheet:
            raise ValidationError({"sheet": "Selecciona una hoja del archivo."})
        if metadata["origen"] not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen guardado no es válido."})
        return Response(inspect_workbook_sheet(path, sheet_name=sheet, header_row=header_row))


def _validation_response(result):
    final_balance = next(
        (movement["saldo_origen"] for movement in reversed(result.movimientos)
         if movement["saldo_origen"] is not None),
        None,
    )
    observed_dates = [
        item["fecha"] for item in result.movimientos
    ] + [
        item["fecha"] for item in result.exclusiones_requeridas
    ]
    return {
        "convencion_signo": "salida negativa; entrada positiva; monto conserva el signo de origen.",
        "filas_analizadas": result.filas_analizadas,
        "movimientos_validos": len(result.movimientos),
        "saldo_final_origen": final_balance,
        "fecha_ultima_observada": max(observed_dates, default=None),
        "entradas": format(sum(
            (Decimal(item["monto"]) for item in result.movimientos if Decimal(item["monto"]) > 0),
            Decimal("0"),
        ), "f"),
        "salidas": format(abs(sum(
            (Decimal(item["monto"]) for item in result.movimientos if Decimal(item["monto"]) < 0),
            Decimal("0"),
        )), "f"),
        "errores": result.errores,
        "advertencias": result.advertencias,
        "exclusiones_requeridas": result.exclusiones_requeridas,
        "propuestas_normalizacion_fecha": result.propuestas_normalizacion_fecha,
        "normalizaciones_fecha_aplicadas": result.normalizaciones_fecha_aplicadas,
        "movimientos": [
            {
                "fila": item["fila_origen"],
                "fecha": item["fecha"],
                "monto": item["monto"],
                "direccion": "salida" if Decimal(item["monto"]) < 0 else "entrada",
                "referencia": item["referencia"][:500] if item["referencia"] else None,
                "numero_cheque": item["numero_cheque"],
                "descripcion": item["descripcion"][:500],
            }
            for item in result.movimientos
        ],
    }


def _validate_workbook_account(context, origin, scope):
    if not isinstance(context, dict):
        return
    if context.get("pais_codigo") and context["pais_codigo"] != scope["country_code"]:
        raise ValidationError({"cuenta_id": "El país del documento no coincide con la cuenta seleccionada."})
    detected_currency = context.get("moneda_codigo")
    if detected_currency and detected_currency != scope["currency_code"]:
        raise ValidationError({
            "cuenta_id": (
                f"El documento está en {detected_currency}, pero la cuenta seleccionada "
                f"está configurada en {scope['currency_code']}."
            )
        })
    if origin == "banco" and context.get("cuenta_bancaria"):
        if str(context["cuenta_bancaria"]).strip() != str(scope["account_number"]).strip():
            raise ValidationError({"cuenta_id": "El número de cuenta bancaria del documento no coincide con la cuenta seleccionada."})
    if origin == "libros" and context.get("codigo_contable"):
        configured_code = scope.get("accounting_code")
        if not configured_code:
            raise ValidationError({
                "cuenta_id": (
                    "El documento incluye un código contable, pero la relación con esta cuenta "
                    "bancaria aún no está configurada."
                )
            })
        if str(context["codigo_contable"]).strip() != str(configured_code).strip():
            raise ValidationError({"cuenta_id": "El código contable del documento no corresponde a la cuenta bancaria seleccionada."})
    detected_bank = context.get("banco")
    if detected_bank:
        normalized_detected = re.sub(r"[^a-z0-9]", "", str(detected_bank).casefold())
        normalized_configured = re.sub(r"[^a-z0-9]", "", str(scope["bank_name"]).casefold())
        if normalized_detected not in normalized_configured and normalized_configured not in normalized_detected:
            raise ValidationError({"cuenta_id": "El banco declarado en el documento no coincide con la cuenta seleccionada."})


def _storage_request(
    method, object_path, *, body=None, content_type=None,
    allow_conflict=False, allow_not_found=False,
):
    key = getattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
    base_url = getattr(settings, "SUPABASE_URL", "")
    if not key or not base_url:
        raise ImportStorageUnavailable()
    url = f"{base_url}/storage/v1/object/contared-documentos/{quote(object_path, safe='/')}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if content_type:
        headers["Content-Type"] = content_type
    if method == "POST":
        headers["x-upsert"] = "false"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            response.read(1024)
        return True
    except HTTPError as exc:
        if allow_conflict and exc.code == 409:
            return False
        if allow_not_found and exc.code == 404:
            return True
        raise ImportStorageUnavailable() from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ImportStorageUnavailable() from exc


def _upload_original(path, metadata, scope):
    source_path = Path(metadata.get("_source_path", path))
    extension = Path(metadata["nombre_archivo"]).suffix.lower()
    content_type = (
        "application/vnd.ms-excel"
        if extension == ".xls"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    object_path = (
        f"imports/{scope['company_id']}/{scope['account_id']}/"
        f"{uuid4()}-{metadata['sha256']}{extension}"
    )
    try:
        body = source_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != metadata["sha256"]:
            raise ValidationError({"archivo": "El archivo temporal cambió; vuelve a cargarlo."})
        try:
            _storage_request("POST", object_path, body=body,
                            content_type=content_type)
        except Exception:
            _delete_storage_object(object_path)
            raise
    except ImportStorageUnavailable:
        raise
    except OSError as exc:
        raise ValidationError({"archivo": "El archivo temporal no está disponible; vuelve a cargarlo."}) from exc
    return object_path


def _delete_storage_object(object_path):
    try:
        _storage_request("DELETE", object_path)
    except ImportStorageUnavailable:
        logger.error("ContaRed import orphan cleanup failed (storage request unavailable).")


def _validate_exclusions(raw_rows, required):
    if not isinstance(raw_rows, list) or any(isinstance(row, bool) or not isinstance(row, int) for row in raw_rows):
        raise ValidationError({"excluded_rows": "excluded_rows debe ser una lista de números de fila."})
    if len(set(raw_rows)) != len(raw_rows):
        raise ValidationError({"excluded_rows": "No se permiten filas de exclusión repetidas."})
    expected = {item["fila"] for item in required}
    actual = set(raw_rows)
    if actual != expected:
        raise ValidationError({
            "excluded_rows": "Debes excluir explícitamente todas y solo las filas fuera del periodo o fecha de corte.",
            "filas_requeridas": sorted(expected),
        })


def _manual_import_duplicates(cursor, account_id, movements):
    duplicates = []
    for movement in movements:
        number = movement.get("numero_cheque")
        if not number:
            continue
        cursor.execute(
            """
            SELECT m.id::text,m.numero_cheque,m.fecha,m.descripcion,
                   abs(m.monto)::text,c.metodo
            FROM contared.movimientos m
            JOIN contared.cargas c ON c.id=m.carga_id
            WHERE m.cuenta_id=%s AND m.lado='libros'
              AND c.origen='libros' AND c.metodo='manual'
              AND c.estado='validada'
              AND m.numero_cheque=%s AND m.fecha=%s
            FOR UPDATE OF m
            """,
            [account_id,number,movement["fecha"]],
        )
        duplicates.extend(
            {
                "fila_importacion":movement["fila_origen"],
                "movimiento_existente_id":row[0],
                "numero_cheque":row[1],
                "fecha_emision":row[2].isoformat(),
                "descripcion":row[3] or "",
                "monto":row[4],
                "metodo":row[5],
            }
            for row in cursor.fetchall()
        )
    return duplicates


def _existing_import_result(cursor, scope, reconciliation_id, origin, sha256, excluded_rows):
    cursor.execute(
        """SELECT id::text,conciliacion_id::text,estado,nombre_archivo,perfil_id::text
           FROM contared.cargas
           WHERE cuenta_id=%s AND origen=%s AND sha256=%s AND estado<>'rechazada'""",
        [scope["account_id"], origin, sha256],
    )
    existing = cursor.fetchone()
    if existing is None:
        return None
    if existing[1] != str(reconciliation_id) or existing[2] != "validada":
        raise ImportConflict()
    cursor.execute(
        "SELECT count(*) FROM contared.movimientos WHERE carga_id=%s",
        [existing[0]],
    )
    return {
        "carga_id": existing[0],
        "sha256": sha256,
        "nombre_archivo": existing[3],
        "origen": origin,
        "movimientos_insertados": cursor.fetchone()[0],
        "filas_excluidas": sorted(excluded_rows),
        "perfil_id": existing[4],
        "repetido": True,
    }


class ReconciliationPreviewConfirmView(APIView):
    def post(self, request, reconciliation_id, preview_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _writer(scope)
        if scope["state"] != "borrador":
            raise ValidationError({"estado": "Solo se pueden confirmar cargas en una conciliación borrador."})
        metadata, path = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=reconciliation_id,
            allow_unbound=True,
        )
        sheet, header_row, mapping, date_format = _validate_options(
            request,
            {
                "excluded_rows", "save_profile", "profile_name", "profile_id",
                "catalogo_tipo_transaccion", "origen", "cobertura_parcial",
                "fecha_desde", "fecha_hasta", "confirmar_normalizacion_fecha",
            },
        )
        origin = request.data.get("origen", metadata["origen"])
        if origin not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen debe ser banco o libros."})
        _validate_workbook_account(metadata.get("contexto"), origin, scope)
        partial = request.data.get("cobertura_parcial", False)
        if not isinstance(partial, bool):
            raise ValidationError({"cobertura_parcial": "cobertura_parcial debe ser verdadero o falso."})
        if partial:
            period, cutoff = _preview_coverage({
                **request.data,
                "periodo": scope["period"].strftime("%Y-%m"),
            })
        else:
            if request.data.get("fecha_desde") or request.data.get("fecha_hasta"):
                raise ValidationError({"cobertura_parcial": "Activa la cobertura parcial para indicar fechas de cobertura."})
            period, cutoff = scope["period"], scope["cutoff"]
        result = validate_workbook(
            path,
            sheet_name=sheet,
            header_row=header_row,
            mapping=mapping,
            date_format=date_format,
            origin=origin,
            period=period,
            cutoff=cutoff,
            confirm_date_proposals=request.data.get("confirmar_normalizacion_fecha", False),
        )
        _apply_banco_industrial_fields(
            result.movimientos,
            origin=origin,
            bank_name=            scope.get("bank_name"),
        )
        if result.errores:
            raise ValidationError({
                "detail": "La carga tiene errores; corrígelos antes de confirmar.",
                "errores": result.errores,
            })
        excluded_rows = request.data.get("excluded_rows", [])
        _validate_exclusions(excluded_rows, result.exclusiones_requeridas)
        proposed_rows = {
            proposal["fila"] for proposal in result.propuestas_normalizacion_fecha
        }
        if proposed_rows.intersection(excluded_rows):
            raise ValidationError({
                "excluded_rows": (
                    "Las filas con una propuesta específica no se pueden omitir; "
                    "confirma la normalización o revisa el documento."
                ),
            })
        if not result.movimientos:
            raise ValidationError({"detail": "No hay movimientos válidos para importar."})
        if not isinstance(request.data.get("save_profile", False), bool):
            raise ValidationError({"save_profile": "save_profile debe ser verdadero o falso."})
        save_profile = request.data.get("save_profile", False)
        profile_name = _validate_profile_name(request.data.get("profile_name")) if save_profile else None
        transaction_catalog = _validate_transaction_catalog(
            request.data.get("catalogo_tipo_transaccion")
        )
        profile_id = request.data.get("profile_id")
        if profile_id is not None:
            try:
                profile_id = UUID(str(profile_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError({"profile_id": "El perfil seleccionado no es válido."}) from exc
        if transaction_catalog and not save_profile and profile_id is None:
            raise ValidationError({
                "catalogo_tipo_transaccion": "Guarda un perfil para conservar el catálogo asociado a este banco."
            })
        # Reject malformed JSON option values before external storage is touched.
        config = {
            "sheet": sheet,
            "header_row": header_row,
            "mapping": mapping,
            "date_format": date_format,
            "catalogo_tipo_transaccion": transaction_catalog,
            "origen": origin,
            "signo_banco": (
                "salida_negativa_entrada_positiva"
                if {"salida", "entrada"} <= set(mapping)
                else "monto_firmado"
            ),
        }
        try:
            profile_json = json.dumps(config, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"mapping": "El mapeo no se puede guardar como perfil."}) from exc

        try:
            metadata, path = bind_preview(
                preview_id,
                user_id=scope["user_id"],
                scope=scope,
                reconciliation_id=reconciliation_id,
            )
        except OSError as exc:
            logger.exception("ContaRed preview binding failed.")
            raise ImportStorageUnavailable() from exc

        existing_import = None
        excluded_rows = request.data.get("excluded_rows", [])
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    if profile_id is not None:
                        cursor.execute(
                            """SELECT id::text FROM contared.perfiles_importacion
                               WHERE id=%s AND empresa_id=%s AND pais_codigo=%s
                                 AND banco_id=%s AND origen=%s""",
                            [
                                profile_id, scope["company_id"], scope["country_code"],
                                scope["bank_id"], origin,
                            ],
                        )
                        if cursor.fetchone() is None:
                            raise NotFound("El perfil no pertenece al banco y origen seleccionados.")
                    existing_import = _existing_import_result(
                        cursor,
                        scope,
                        reconciliation_id,
                        origin,
                        metadata["sha256"],
                        excluded_rows,
                    )
        except DatabaseError as exc:
            logger.error(
                "ContaRed duplicate import check failed (%s).",
                database_error_details(exc),
            )
            raise DatabaseUnavailable() from exc
        if existing_import is not None:
            cleanup_expired_previews()
            return Response(existing_import)

        object_path = _upload_original(path, metadata, scope)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT estado FROM contared.conciliaciones
                           WHERE id=%s AND cuenta_id=%s FOR UPDATE""",
                        [reconciliation_id, scope["account_id"]],
                    )
                    current = cursor.fetchone()
                    if current is None or current[0] != "borrador":
                        raise ValidationError({"estado": "La conciliación ya no está disponible para importar."})
                    if origin == "libros":
                        cursor.execute(
                            "SELECT id FROM contared.cuentas_bancarias WHERE id=%s FOR UPDATE",
                            [scope["account_id"]],
                        )
                        manual_duplicates = _manual_import_duplicates(
                            cursor,scope["account_id"],result.movimientos
                        )
                        if manual_duplicates:
                            raise ValidationError({
                                "duplicados_manuales":"La carga coincide con una emisión manual por número de cheque y fecha. Revisa las filas indicadas y los importes; no se importó ningún movimiento.",
                                "registros_relacionados":manual_duplicates,
                            })
                    cursor.execute(
                        """SELECT id FROM contared.cargas
                           WHERE cuenta_id=%s AND origen=%s AND sha256=%s AND estado<>'rechazada'""",
                        [scope["account_id"], origin, metadata["sha256"]],
                    )
                    if cursor.fetchone():
                        raise ImportConflict()
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    load_profile_id = profile_id
                    if save_profile:
                        cursor.execute(
                            "SELECT id FROM contared.empresas WHERE id=%s FOR UPDATE",
                            [scope["company_id"]],
                        )
                        cursor.execute(
                            """SELECT coalesce(max(version),0)+1
                               FROM contared.perfiles_importacion
                               WHERE empresa_id=%s AND pais_codigo=%s AND nombre=%s""",
                            [
                                scope["company_id"], scope["country_code"], profile_name,
                            ],
                        )
                        version = cursor.fetchone()[0]
                        cursor.execute(
                            """INSERT INTO contared.perfiles_importacion
                               (empresa_id,pais_codigo,banco_id,origen,nombre,version,configuracion)
                               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id::text""",
                            [
                                scope["company_id"], scope["country_code"], scope["bank_id"],
                                origin, profile_name, version, profile_json,
                            ],
                        )
                        load_profile_id = cursor.fetchone()[0]
                    cursor.execute(
                        """INSERT INTO contared.cargas
                           (cuenta_id,empresa_id,pais_codigo,conciliacion_id,origen,metodo,
                            perfil_id,nombre_archivo,storage_ruta,sha256,estado,creada_por)
                           VALUES (%s,%s,%s,%s,%s,'excel',%s,%s,%s,%s,'validada',%s)
                           RETURNING id::text""",
                        [
                            scope["account_id"], scope["company_id"], scope["country_code"],
                            reconciliation_id,                             origin, load_profile_id,
                            metadata["nombre_archivo"], object_path, metadata["sha256"],
                            scope["user_id"],
                        ],
                    )
                    load_id = cursor.fetchone()[0]
                    movement_rows = [
                        (
                            scope["account_id"], load_id, origin, movement["fecha"],
                            movement["fecha_valor"], movement["referencia"], movement["numero_transaccion"],
                            movement["numero_cheque"], movement["codigo_tt"], movement["descripcion"],
                            movement["monto"], movement["saldo_origen"], sheet, movement["fila_origen"],
                            json.dumps(movement["datos_originales"], ensure_ascii=False), scope["user_id"],
                        )
                        for movement in result.movimientos
                    ]
                    cursor.executemany(
                        """INSERT INTO contared.movimientos
                           (cuenta_id,carga_id,lado,fecha,fecha_valor,referencia,numero_transaccion,
                            numero_cheque,codigo_tt,descripcion,monto,saldo_origen,hoja_origen,
                            fila_origen,datos_originales,creado_por)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                        movement_rows,
                    )
                    balance_candidates = [
                        movement for movement in result.movimientos
                        if movement["saldo_origen"] is not None
                    ]
                    if balance_candidates:
                        detected = max(
                            balance_candidates,
                            key=lambda movement: movement["fila_origen"],
                        )
                        balance_source = {
                            "carga_id": load_id,
                            "archivo": metadata["nombre_archivo"],
                            "hoja": sheet,
                            "fila": detected["fila_origen"],
                            "metodo": (
                                "Última fila importada con saldo acumulado no vacío "
                                "en el campo mapeado saldo_origen."
                            ),
                            "campo": "saldo_origen",
                        }
                        detected_column = (
                            "saldo_final_banco_detectado"
                            if origin == "banco"
                            else "saldo_final_libros_detectado"
                        )
                        source_column = (
                            "saldo_final_banco_fuente"
                            if origin == "banco"
                            else "saldo_final_libros_fuente"
                        )
                        cursor.execute(
                            f"""UPDATE contared.conciliaciones
                                SET {detected_column}=%s,{source_column}=%s::jsonb
                                WHERE id=%s AND cuenta_id=%s AND estado='borrador'""",
                            [
                                detected["saldo_origen"],
                                json.dumps(balance_source, ensure_ascii=False),
                                reconciliation_id,
                                scope["account_id"],
                            ],
                        )
                        if cursor.rowcount != 1:
                            raise ValidationError({
                                "saldo_final": "No se pudo asociar el saldo detectado al borrador."
                            })
        except ImportConflict:
            _delete_storage_object(object_path)
            raise
        except IntegrityError as exc:
            _delete_storage_object(object_path)
            raise ImportConflict() from exc
        except DatabaseError as exc:
            _delete_storage_object(object_path)
            logger.error(
                "ContaRed import commit failed (%s).",
                database_error_details(exc),
            )
            raise DatabaseUnavailable() from exc
        except Exception:
            _delete_storage_object(object_path)
            raise
        cleanup_expired_previews()
        return Response({
            "carga_id": load_id,
            "sha256": metadata["sha256"],
            "nombre_archivo": metadata["nombre_archivo"],
            "origen": origin,
            "movimientos_insertados": len(result.movimientos),
            "filas_excluidas": sorted(request.data.get("excluded_rows", [])),
            "perfil_id": load_profile_id,
        }, status=201)
