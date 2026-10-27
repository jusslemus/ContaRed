import hashlib
import json
import logging
import shutil
from datetime import date
from decimal import Decimal
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
    load_session_access,
)


logger = logging.getLogger(__name__)


class ImportStorageUnavailable(APIException):
    status_code = 503
    default_detail = "El almacenamiento privado de documentos no está disponible."
    default_code = "document_storage_unavailable"


class ImportConflict(APIException):
    status_code = 409
    default_detail = "El archivo ya fue importado para esta cuenta y origen."
    default_code = "duplicate_import"


def _writer(scope):
    if scope["role"] not in ("administrador", "auxiliar"):
        raise PermissionDenied(
            "Esta operación requiere el rol administrador o auxiliar.",
            code="writer_required",
        )


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
                           r.saldo_final_banco,r.saldo_final_libros
                    FROM contared.conciliaciones AS r
                    JOIN contared.cuentas_bancarias AS cb ON cb.id=r.cuenta_id
                    WHERE r.id=%s
                    """,
                    [reconciliation_id],
                )
                row = cursor.fetchone()
    except DatabaseError as exc:
        logger.error("ContaRed reconciliation scope lookup failed (database error type: %s).", type(exc).__name__)
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
    }


def _scope_for_account(request, account_id):
    session = load_session_access(request.user.auth_user_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    """SELECT empresa_id::text,pais_codigo FROM contared.cuentas_bancarias
                       WHERE id=%s""",
                    [account_id],
                )
                row = cursor.fetchone()
    except DatabaseError as exc:
        logger.error("ContaRed account scope lookup failed (database error type: %s).", type(exc).__name__)
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
                                   b.nombre,b.activo
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
        account_id = request.query_params.get("cuenta_id")
        if account_id:
            clauses.append("r.cuenta_id=%s")
            params.append(account_id)
        where = " AND ".join(clauses)
        query = """
            FROM contared.conciliaciones r
            JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
            JOIN contared.bancos b ON b.id=cb.banco_id
        """
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    data = paginate(
                        cursor,
                        f"SELECT COUNT(*) {query} WHERE {where}",
                        f"""SELECT r.id::text,r.cuenta_id::text,cb.alias,b.nombre,
                                   cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,
                                   r.saldo_inicial_banco,r.saldo_inicial_libros,
                                   r.saldo_final_banco,r.saldo_final_libros,r.creada_en
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
            "results": [_reconciliation_row(row) for row in data["results"]],
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
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """SELECT r.id::text,r.cuenta_id::text,cb.alias,b.nombre,
                                  cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,
                                  r.saldo_inicial_banco,r.saldo_inicial_libros,
                                  r.saldo_final_banco,r.saldo_final_libros,r.creada_en,
                                  r.creada_por::text,r.notas
                           FROM contared.conciliaciones r
                           JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
                           JOIN contared.bancos b ON b.id=cb.banco_id
                           WHERE r.id=%s AND r.cuenta_id=%s""",
                        [reconciliation_id, scope["account_id"]],
                    )
                    row = cursor.fetchone()
                    if row is not None:
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
        return Response({
            **_reconciliation_row(row[:13]),
            "creada_por": row[13],
            "notas": row[14],
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


class ReconciliationMovementsView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        side = request.query_params.get("lado")
        if side not in ("banco", "libros"):
            raise ValidationError({"lado": "lado debe ser banco o libros."})
        page, page_size = _paging(request.query_params)
        clauses = ["m.cuenta_id=%s", "m.lado=%s", "m.fecha >= %s", "m.fecha <= %s"]
        params = [scope["account_id"], side, scope["period"], scope["cutoff"]]
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
                        f"SELECT COUNT(*) FROM contared.movimientos m WHERE {where}",
                        f"""SELECT m.id::text,m.lado,m.fecha,m.fecha_valor,m.referencia,
                                   m.numero_transaccion,m.numero_cheque,m.codigo_tt,m.descripcion,
                                   m.monto,m.saldo_origen,m.hoja_origen,m.fila_origen,
                                   m.datos_originales,c.id::text,c.origen,c.nombre_archivo,
                                   c.sha256,c.estado,c.creada_en,c.creada_por::text
                            FROM contared.movimientos m
                            JOIN contared.cargas c ON c.id=m.carga_id
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
                    "numero_cheque": row[6],
                    "codigo_tt": row[7],
                    "descripcion": row[8],
                    "monto": str(row[9]),
                    "saldo_origen": str(row[10]) if row[10] is not None else None,
                    "hoja_origen": row[11],
                    "fila_origen": row[12],
                    "datos_originales": row[13],
                    "fuente": {
                        "carga_id": row[14],
                        "origen": row[15],
                        "nombre_archivo": row[16],
                        "sha256": row[17],
                        "estado": row[18],
                        "creada_en": row[19].isoformat() if row[19] else None,
                        "creada_por": row[20],
                    },
                }
                for row in data["results"]
            ],
        })


def _validate_profile_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValidationError({"profile_name": "El nombre del perfil debe tener entre 1 y 120 caracteres."})
    return value.strip()


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
            raise ValidationError({"file": "Adjunta un archivo .xlsx."})
        preview_id, metadata, path = create_preview(
            upload,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=reconciliation_id,
            origin=origin,
        )
        try:
            workbook_data = workbook_preview(path)
        except Exception:
            path.parent.joinpath("original.xlsx").unlink(missing_ok=True)
            path.parent.joinpath("metadata.json").unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
            raise
        return Response({
            "preview_id": str(preview_id),
            "origen": origin,
            "nombre_archivo": metadata["nombre_archivo"],
            "sha256": metadata["sha256"],
            "expira_en": metadata["expira_en"],
            **workbook_data,
        }, status=201)


class ReconciliationPreviewValidateView(APIView):
    def post(self, request, reconciliation_id, preview_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _writer(scope)
        allowed = {
            "sheet", "header_row", "mapping", "date_format",
            "excluded_rows", "save_profile", "profile_name",
        }
        if set(request.data) - allowed:
            raise ValidationError({"detail": "La solicitud de confirmación contiene campos no permitidos."})
        metadata, path = get_preview(
            preview_id,
            user_id=scope["user_id"],
            scope=scope,
            reconciliation_id=reconciliation_id,
        )
        if scope["state"] != "borrador":
            raise ValidationError({"estado": "Solo se pueden importar movimientos en borrador."})
        if metadata["origen"] not in ("banco", "libros"):
            raise ValidationError({"origen": "El origen guardado no es válido."})
        sheet, header_row, mapping, date_format = _validate_options(request)
        result = validate_workbook(
            path,
            sheet_name=sheet,
            header_row=header_row,
            mapping=mapping,
            date_format=date_format,
            origin=metadata["origen"],
            period=scope["period"],
            cutoff=scope["cutoff"],
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
    return {
        "convencion_signo": "salida negativa; entrada positiva; monto conserva el signo de origen.",
        "filas_analizadas": result.filas_analizadas,
        "movimientos_validos": len(result.movimientos),
        "errores": result.errores,
        "advertencias": result.advertencias,
        "exclusiones_requeridas": result.exclusiones_requeridas,
        "movimientos": [
            {
                "fila": item["fila_origen"],
                "fecha": item["fecha"],
                "monto": item["monto"],
                "direccion": "salida" if Decimal(item["monto"]) < 0 else "entrada",
                "referencia": item["referencia"][:500] if item["referencia"] else None,
                "descripcion": item["descripcion"][:500],
            }
            for item in result.movimientos
        ],
    }


def _storage_request(method, object_path, *, body=None, content_type=None, allow_conflict=False):
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
        raise ImportStorageUnavailable() from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ImportStorageUnavailable() from exc


def _upload_original(path, metadata, scope):
    object_path = (
        f"imports/{scope['company_id']}/{scope['account_id']}/"
        f"{uuid4()}-{metadata['sha256']}.xlsx"
    )
    try:
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != metadata["sha256"]:
            raise ValidationError({"archivo": "El archivo temporal cambió; vuelve a cargarlo."})
        try:
            _storage_request("POST", object_path, body=body,
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
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
        )
        sheet, header_row, mapping, date_format = _validate_options(
            request,
            {"excluded_rows", "save_profile", "profile_name", "profile_id"},
        )
        result = validate_workbook(
            path,
            sheet_name=sheet,
            header_row=header_row,
            mapping=mapping,
            date_format=date_format,
            origin=metadata["origen"],
            period=scope["period"],
            cutoff=scope["cutoff"],
        )
        if result.errores:
            raise ValidationError({
                "detail": "La carga tiene errores; corrígelos antes de confirmar.",
                "errores": result.errores,
            })
        _validate_exclusions(request.data.get("excluded_rows", []), result.exclusiones_requeridas)
        if not result.movimientos:
            raise ValidationError({"detail": "No hay movimientos válidos para importar."})
        if not isinstance(request.data.get("save_profile", False), bool):
            raise ValidationError({"save_profile": "save_profile debe ser verdadero o falso."})
        save_profile = request.data.get("save_profile", False)
        profile_name = _validate_profile_name(request.data.get("profile_name")) if save_profile else None
        profile_id = request.data.get("profile_id")
        if profile_id is not None:
            try:
                profile_id = UUID(str(profile_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError({"profile_id": "El perfil seleccionado no es válido."}) from exc
        # Reject malformed JSON option values before external storage is touched.
        config = {
            "sheet": sheet,
            "header_row": header_row,
            "mapping": mapping,
            "date_format": date_format,
            "origen": metadata["origen"],
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
            with transaction.atomic():
                with connection.cursor() as cursor:
                    if profile_id is not None:
                        cursor.execute(
                            """SELECT id::text FROM contared.perfiles_importacion
                               WHERE id=%s AND empresa_id=%s AND pais_codigo=%s
                                 AND banco_id=%s AND origen=%s""",
                            [
                                profile_id, scope["company_id"], scope["country_code"],
                                scope["bank_id"], metadata["origen"],
                            ],
                        )
                        if cursor.fetchone() is None:
                            raise NotFound("El perfil no pertenece al banco y origen seleccionados.")
                    cursor.execute(
                        """SELECT id FROM contared.cargas
                           WHERE cuenta_id=%s AND origen=%s AND sha256=%s AND estado<>'rechazada'""",
                        [scope["account_id"], metadata["origen"], metadata["sha256"]],
                    )
                    if cursor.fetchone():
                        raise ImportConflict()
        except DatabaseError as exc:
            logger.error("ContaRed duplicate import check failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc

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
                    cursor.execute(
                        """SELECT id FROM contared.cargas
                           WHERE cuenta_id=%s AND origen=%s AND sha256=%s AND estado<>'rechazada'""",
                        [scope["account_id"], metadata["origen"], metadata["sha256"]],
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
                                metadata["origen"], profile_name, version, profile_json,
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
                            reconciliation_id,                             metadata["origen"], load_profile_id,
                            metadata["nombre_archivo"], object_path, metadata["sha256"],
                            scope["user_id"],
                        ],
                    )
                    load_id = cursor.fetchone()[0]
                    movement_rows = [
                        (
                            scope["account_id"], load_id, metadata["origen"], movement["fecha"],
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
        except ImportConflict:
            _delete_storage_object(object_path)
            raise
        except IntegrityError as exc:
            _delete_storage_object(object_path)
            raise ImportConflict() from exc
        except DatabaseError as exc:
            _delete_storage_object(object_path)
            logger.error("ContaRed import commit failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        except Exception:
            _delete_storage_object(object_path)
            raise
        cleanup_expired_previews()
        shutil.rmtree(path.parent, ignore_errors=True)
        return Response({
            "carga_id": load_id,
            "sha256": metadata["sha256"],
            "nombre_archivo": metadata["nombre_archivo"],
            "origen": metadata["origen"],
            "movimientos_insertados": len(result.movimientos),
            "filas_excluidas": sorted(request.data.get("excluded_rows", [])),
            "perfil_id": load_profile_id,
        }, status=201)
