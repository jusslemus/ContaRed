import logging
import json
from datetime import date
from decimal import Decimal
from itertools import combinations
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import HttpResponse
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.conciliaciones import (
    ImportStorageUnavailable,
    _delete_storage_object,
    _scope_for_reconciliation,
    _storage_request,
)
from contared_api.session_access import DatabaseUnavailable


logger = logging.getLogger(__name__)
REQUIRED_DEPOSIT_TABLES = {
    "casos_deposito": {
        "id", "cuenta_id", "conciliacion_origen_id", "clasificacion", "estado",
        "origen_correcto", "observacion", "responsable_id", "soporte_ruta",
        "conciliacion_resolucion_id", "fecha_efectiva", "motivo_resolucion",
        "creado_por", "creado_en", "actualizado_en",
    },
    "deposito_movimientos": {"caso_id", "cuenta_id", "movimiento_id", "rol"},
    "deposito_eventos": {
        "id", "caso_id", "tipo", "clasificacion_anterior", "clasificacion_nueva",
        "origen_correcto", "estado_anterior", "estado_nuevo", "fecha_efectiva", "registrado_por",
        "registrado_en", "observacion", "motivo", "soporte_ruta", "conciliacion_id",
    },
}
CLASSIFICATIONS = {"por_investigar", "deposito_transito", "deposito_mal_operado"}
SUPPORT_MAX_BYTES = 10 * 1024 * 1024
SUPPORT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_AMBIGUOUS_OPTIONS = 50


def _ambiguous_comparison_options(bank_items, book_items):
    def subsets(items):
        for size in range(1, min(3, len(items)) + 1):
            yield from combinations(items, size)

    def evidence_for(bank_group, book_group):
        movements = (*bank_group, *book_group)
        references = {item["referencia"] for item in movements}
        if len(references) == 1 and None not in references and "" not in references:
            return f"Referencia compartida: {next(iter(references))}."
        dates = {item["fecha"] for item in movements}
        if len(dates) == 1:
            return f"Fecha compartida: {next(iter(dates))}."
        return None

    options = []
    seen = set()
    truncated = False
    for bank_group in subsets(bank_items):
        for book_group in subsets(book_items):
            if len(bank_group) > 1 and len(book_group) > 1:
                continue
            bank_total = sum((Decimal(item["monto"]) for item in bank_group), Decimal(0))
            book_total = sum((Decimal(item["monto"]) for item in book_group), Decimal(0))
            if bank_total != book_total:
                continue
            evidence = evidence_for(bank_group, book_group)
            if evidence is None:
                continue
            key = (
                tuple(sorted(item["id"] for item in bank_group)),
                tuple(sorted(item["id"] for item in book_group)),
            )
            if key in seen:
                continue
            seen.add(key)
            if len(options) == MAX_AMBIGUOUS_OPTIONS:
                truncated = True
                break
            options.append({
                "banco": list(bank_group),
                "libros": list(book_group),
                "suma_banco": str(bank_total),
                "suma_libros": str(book_total),
                "evidencia": evidence,
            })
        if truncated:
            break
    return options, truncated


def _upload_support(upload, scope):
    if upload is None:
        return None
    content_type = upload.content_type or "application/octet-stream"
    extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}.get(content_type)
    if extension is None or upload.size > SUPPORT_MAX_BYTES:
        raise ValidationError({"soporte": "Adjunta un PDF o imagen PNG/JPEG de hasta 10 MiB."})
    object_path = (
        f"depositos/{scope['company_id']}/{scope['account_id']}/"
        f"{uuid4()}.{extension}"
    )
    try:
        _storage_request("POST", object_path, body=upload.read(), content_type=content_type)
    except ImportStorageUnavailable:
        raise
    return object_path


def _download_support(object_path):
    key = getattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
    base_url = getattr(settings, "SUPABASE_URL", "")
    if not key or not base_url:
        raise ImportStorageUnavailable()
    url = f"{base_url}/storage/v1/object/contared-documentos/{quote(object_path, safe='/')}"
    http_request = Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urlopen(http_request, timeout=30) as response:
            body = response.read(SUPPORT_MAX_BYTES + 1)
            content_type = response.headers.get("Content-Type", "application/octet-stream")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ImportStorageUnavailable() from exc
    if len(body) > SUPPORT_MAX_BYTES or content_type not in SUPPORT_TYPES:
        raise ImportStorageUnavailable()
    return HttpResponse(body, content_type=content_type, headers={
        "Content-Disposition": 'attachment; filename="soporte-deposito"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


class DepositMigrationRequired(APIException):
    status_code = 503
    default_detail = (
        "El seguimiento de depósitos requiere aplicar "
        "supabase/migrations/202610300002_seguimiento_depositos.sql."
    )
    default_code = "depositos_migration_required"


def _deposit_schema_missing():
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name,column_name
                FROM information_schema.columns
                WHERE table_schema='contared'
                  AND table_name IN ('casos_deposito','deposito_movimientos','deposito_eventos')
                """
            )
            columns = {}
            for table, column in cursor.fetchall():
                columns.setdefault(table, set()).add(column)
    except DatabaseError as exc:
        logger.error("Deposit schema diagnostic failed (error type: %s).", type(exc).__name__)
        return False
    return any(not fields.issubset(columns.get(table, set())) for table, fields in REQUIRED_DEPOSIT_TABLES.items())


def _db_error(exc):
    cause = exc.__cause__
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if sqlstate in ("42P01", "42703") and _deposit_schema_missing():
        logger.error("Deposit tracking schema is incomplete (SQLSTATE: %s).", sqlstate)
        raise DepositMigrationRequired() from exc
    logger.error(
        "Deposit tracking query failed (error type: %s, SQLSTATE: %s).",
        type(exc).__name__,
        sqlstate or "unavailable",
    )
    raise DatabaseUnavailable() from exc


def _role(scope, roles):
    if scope["role"] not in roles:
        raise PermissionDenied("Tu rol no permite esta operación.", code="role_not_permitted")


def _uuid_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError({"movimientos": "La selección de movimientos no es válida."}) from exc
    if not isinstance(value, list) or not value:
        raise ValidationError({"movimientos": "Selecciona al menos un movimiento."})
    try:
        result = [UUID(str(item)) for item in value]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({"movimientos": "La selección contiene un movimiento no válido."}) from exc
    if len(set(result)) != len(result):
        raise ValidationError({"movimientos": "No repitas movimientos en el mismo caso."})
    return result


def _iso_date(value, field):
    if not isinstance(value, str):
        raise ValidationError({field: "Indica una fecha válida con formato YYYY-MM-DD."})
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError({field: "Indica una fecha válida con formato YYYY-MM-DD."}) from exc


def _case_status_at_cutoff(state, effective_date, cutoff):
    if state == "resuelto" and effective_date and effective_date > cutoff:
        return "abierto"
    return state


def _case_json(row, cutoff):
    return {
        "id": row[0],
        "clasificacion": row[1],
        "estado": _case_status_at_cutoff(row[2], row[7], cutoff),
        "observacion": row[3],
        "responsable": row[4],
        "periodo_origen": row[5].isoformat(),
        "fecha_corte_origen": row[6].isoformat(),
        "fecha_efectiva": row[7].isoformat() if row[7] else None,
        "motivo_resolucion": row[8],
        "movimientos": row[9],
        "actualizado_en": row[10].isoformat() if row[10] else None,
        "soporte_disponible": row[11],
    }


class ReconciliationDepositView(APIView):
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """
                        SELECT d.id::text,d.clasificacion,d.estado,d.observacion,
                               COALESCE(u.nombre,'Sin asignar'),ro.periodo,ro.fecha_corte,
                               d.fecha_efectiva,d.motivo_resolucion,
                               COALESCE(jsonb_agg(jsonb_build_object(
                                 'id',m.id::text,'lado',m.lado,'fecha',m.fecha,
                                 'referencia',m.referencia,'descripcion',m.descripcion,
                                 'monto',m.monto::text
                               ) ORDER BY m.lado,m.fecha) FILTER (WHERE m.id IS NOT NULL),'[]'::jsonb),
                               d.actualizado_en,d.soporte_ruta IS NOT NULL
                        FROM contared.casos_deposito d
                        JOIN contared.conciliaciones ro ON ro.id=d.conciliacion_origen_id
                        LEFT JOIN contared.usuarios u ON u.id=d.responsable_id
                        LEFT JOIN contared.deposito_movimientos dm ON dm.caso_id=d.id
                        LEFT JOIN contared.movimientos m ON m.id=dm.movimiento_id
                        WHERE d.cuenta_id=%s AND ro.fecha_corte<=%s
                        GROUP BY d.id,u.nombre,ro.periodo,ro.fecha_corte
                        ORDER BY (d.estado='abierto') DESC,d.actualizado_en DESC
                        """,
                        [scope["account_id"], scope["cutoff"]],
                    )
                    cases = [_case_json(row, scope["cutoff"]) for row in cursor.fetchall()]
                    if cases:
                        case_ids = [UUID(item["id"]) for item in cases]
                        cursor.execute(
                            """
                            SELECT d.id::text,b.id::text,b.fecha,b.referencia,b.descripcion,
                                   b.monto::text,c.periodo
                            FROM contared.casos_deposito d
                            JOIN contared.conciliaciones ro ON ro.id=d.conciliacion_origen_id
                            JOIN contared.deposito_movimientos dm_l
                              ON dm_l.caso_id=d.id AND dm_l.rol='libros'
                            JOIN contared.movimientos l ON l.id=dm_l.movimiento_id
                            JOIN contared.movimientos b ON b.cuenta_id=d.cuenta_id
                              AND b.lado='banco' AND b.fecha>ro.fecha_corte
                              AND b.referencia IS NOT DISTINCT FROM l.referencia
                              AND b.monto=l.monto
                            JOIN contared.cargas cb ON cb.id=b.carga_id AND cb.estado='validada'
                            JOIN contared.conciliaciones c ON c.id=cb.conciliacion_id
                            WHERE d.id=ANY(%s::uuid[]) AND b.fecha<=%s
                            ORDER BY d.id,b.fecha,b.id
                            """,
                            [case_ids, scope["cutoff"]],
                        )
                        later = {}
                        for row in cursor.fetchall():
                            later.setdefault(row[0], []).append({
                                "id": row[1], "fecha": row[2].isoformat(),
                                "referencia": row[3], "descripcion": row[4],
                                "monto": row[5], "periodo": row[6].isoformat(),
                            })
                        for item in cases:
                            item["apariciones_posteriores"] = later.get(item["id"], [])
                    cursor.execute(
                        """
                        SELECT p.regla_codigo,p.estado,p.explicacion,
                               COALESCE(b.items,'[]'::jsonb),COALESCE(b.total,0)::text,
                               COALESCE(l.items,'[]'::jsonb),COALESCE(l.total,0)::text
                        FROM contared.propuestas_comparacion p
                        LEFT JOIN LATERAL (
                          SELECT jsonb_agg(jsonb_build_object(
                            'id',m.id::text,'lado',m.lado,'fecha',m.fecha,
                            'referencia',m.referencia,'descripcion',m.descripcion,
                            'monto',m.monto::text) ORDER BY m.fecha,m.id) AS items,
                            sum(m.monto) AS total
                          FROM jsonb_array_elements_text(p.banco_ids) x(id)
                          JOIN contared.movimientos m ON m.id=x.id::uuid
                        ) b ON true
                        LEFT JOIN LATERAL (
                          SELECT jsonb_agg(jsonb_build_object(
                            'id',m.id::text,'lado',m.lado,'fecha',m.fecha,
                            'referencia',m.referencia,'descripcion',m.descripcion,
                            'monto',m.monto::text) ORDER BY m.fecha,m.id) AS items,
                            sum(m.monto) AS total
                          FROM jsonb_array_elements_text(p.libros_ids) x(id)
                          JOIN contared.movimientos m ON m.id=x.id::uuid
                        ) l ON true
                        WHERE p.conciliacion_id=%s AND p.estado<>'confirmada'
                          AND p.regla_codigo IN (
                            'reference_date_group','reference_amount_mismatch','ambiguous'
                          )
                        ORDER BY p.creada_en,p.id
                        """,
                        [scope["reconciliation_id"]],
                    )
                    comparison_groups = []
                    for row in cursor.fetchall():
                        bank_items = row[3]
                        book_items = row[5]
                        if isinstance(bank_items, str):
                            bank_items = json.loads(bank_items)
                        if isinstance(book_items, str):
                            book_items = json.loads(book_items)
                        bank_total = Decimal(row[4])
                        book_total = Decimal(row[6])
                        group = {
                            "regla": row[0],
                            "estado": "Descartada; movimientos pendientes" if row[1] == "descartada" else "Por revisar",
                            "explicacion": row[2],
                            "banco": bank_items,
                            "libros": book_items,
                            "suma_banco": str(bank_total),
                            "suma_libros": str(book_total),
                            "diferencia": str(abs(bank_total - book_total)),
                            "alternativas": [],
                            "alternativas_truncadas": False,
                        }
                        if row[0] == "ambiguous":
                            alternatives, truncated = _ambiguous_comparison_options(
                                bank_items, book_items
                            )
                            group.update({
                                "suma_banco": None,
                                "suma_libros": None,
                                "diferencia": None,
                                "alternativas": alternatives,
                                "alternativas_truncadas": truncated,
                            })
                        comparison_groups.append(group)
                    cursor.execute(
                        """
                        SELECT m.id::text,m.lado,m.fecha,m.referencia,m.descripcion,m.monto::text,
                               l.origen,c.periodo
                        FROM contared.movimientos m
                        JOIN contared.cargas l ON l.id=m.carga_id
                        JOIN contared.conciliaciones c ON c.id=l.conciliacion_id
                        WHERE m.cuenta_id=%s AND l.conciliacion_id=%s AND l.estado='validada'
                          AND m.fecha<=%s AND m.monto>0
                          AND NOT EXISTS (
                            SELECT 1 FROM contared.cruce_movimientos cm
                            JOIN contared.cruces x ON x.id=cm.cruce_id AND x.estado='confirmado'
                            WHERE cm.movimiento_id=m.id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM contared.deposito_movimientos dm
                            JOIN contared.casos_deposito d ON d.id=dm.caso_id AND d.estado='abierto'
                            WHERE dm.movimiento_id=m.id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM contared.propuestas_comparacion p
                            WHERE p.conciliacion_id=l.conciliacion_id
                              AND p.estado<>'confirmada'
                              AND EXISTS (
                                SELECT 1 FROM jsonb_array_elements_text(p.banco_ids||p.libros_ids) x(id)
                                WHERE x.id=m.id::text
                              )
                          )
                        ORDER BY m.fecha,m.id
                        """,
                        [scope["account_id"], scope["reconciliation_id"], scope["cutoff"]],
                    )
                    movement_rows = cursor.fetchall()
                    eligible = []
                    for row in movement_rows:
                        eligible.append({
                            "id": row[0], "lado": row[1], "fecha": row[2].isoformat(),
                            "referencia": row[3], "descripcion": row[4], "monto": row[5],
                            "origen": row[6], "periodo": row[7].isoformat(),
                        })
                    bank = [item for item in eligible if item["lado"] == "banco"]
                    books = [item for item in eligible if item["lado"] == "libros"]
                    mismatch_pairs = []
                    for book in books:
                        if not book["referencia"]:
                            continue
                        matches = [
                            item for item in bank
                            if item["referencia"] == book["referencia"] and item["monto"] != book["monto"]
                        ]
                        if len(matches) == 1:
                            mismatch_pairs.append({
                                "banco": matches[0],
                                "libros": book,
                                "diferencia": str(abs(Decimal(matches[0]["monto"]) - Decimal(book["monto"]))),
                            })
                    paired_ids = {pair[side]["id"] for pair in mismatch_pairs for side in ("banco", "libros")}
                    candidates = [
                        {"tipo": "diferencia_referencia",
                         "banco": [pair["banco"]], "libros": [pair["libros"]],
                         "diferencia": pair["diferencia"]}
                        for pair in mismatch_pairs
                    ]
                    candidates.extend(
                        {"tipo": "libros_sin_contraparte", "banco": [], "libros": [item]}
                        for item in books if item["id"] not in paired_ids
                    )
                    candidates.extend(
                        {"tipo": "banco_sin_contraparte", "banco": [item], "libros": []}
                        for item in bank if item["id"] not in paired_ids
                    )
        except DatabaseError as exc:
            _db_error(exc)
        return Response({
            "results": cases,
            "candidatos": candidates,
            "grupos_comparacion": comparison_groups,
            "cutoff": scope["cutoff"].isoformat(),
            "scope": {"account_id": scope["account_id"], "reconciliation_id": scope["reconciliation_id"]},
        })

    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _role(scope, ("administrador", "auxiliar"))
        classification = request.data.get("clasificacion")
        if classification not in CLASSIFICATIONS:
            raise ValidationError({"clasificacion": "Selecciona una clasificación válida."})
        movement_ids = _uuid_list(request.data.get("movimientos"))
        observation = request.data.get("observacion", "")
        if not isinstance(observation, str):
            raise ValidationError({"observacion": "La observación debe ser texto."})
        origin_correct = request.data.get("origen_correcto")
        if origin_correct is not None and origin_correct not in ("banco", "libros"):
            raise ValidationError({"origen_correcto": "Selecciona Banco o Libros."})
        if origin_correct and classification != "deposito_mal_operado":
            raise ValidationError({"origen_correcto": "Solo aplica a una diferencia de monto."})
        support_path = _upload_support(request.FILES.get("soporte"), scope)
        if origin_correct and not support_path:
            raise ValidationError({"soporte": "Adjunta evidencia privada para confirmar qué origen es correcto."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT r.id::text,r.cuenta_id::text,r.fecha_corte,cb.empresa_id::text,
                                  cb.pais_codigo
                           FROM contared.conciliaciones r
                           JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
                           WHERE r.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s
                           FOR UPDATE OF r""",
                        [reconciliation_id, scope["company_id"], scope["country_code"]],
                    )
                    recon = cursor.fetchone()
                    if recon is None or recon[1] != scope["account_id"]:
                        raise NotFound("La conciliación no existe o ya no pertenece al ámbito autorizado.")
                    cursor.execute("SELECT set_config('contared.actor_id',%s,true)", [scope["user_id"]])
                    cursor.execute(
                        """
                        SELECT m.id::text,m.lado,m.fecha,m.referencia,m.monto
                        FROM contared.movimientos m
                        JOIN contared.cargas l ON l.id=m.carga_id
                        WHERE m.id=ANY(%s::uuid[]) AND m.cuenta_id=%s
                          AND l.estado='validada' AND m.fecha<=%s AND m.monto>0
                          AND NOT EXISTS (SELECT 1 FROM contared.cruce_movimientos cm
                            JOIN contared.cruces x ON x.id=cm.cruce_id AND x.estado='confirmado'
                            WHERE cm.movimiento_id=m.id)
                          AND NOT EXISTS (SELECT 1 FROM contared.deposito_movimientos dm
                            JOIN contared.casos_deposito d ON d.id=dm.caso_id AND d.estado='abierto'
                            WHERE dm.movimiento_id=m.id)
                        FOR UPDATE OF m""",
                        [movement_ids, scope["account_id"], recon[2]],
                    )
                    rows = cursor.fetchall()
                    if len(rows) != len(movement_ids):
                        raise ValidationError({"movimientos": "Uno o más movimientos no son elegibles, están conciliados o ya tienen seguimiento abierto."})
                    sides = {row[1] for row in rows}
                    if classification == "deposito_transito":
                        books = [row for row in rows if row[1] == "libros"]
                        if not books or len(rows) != len(books):
                            raise ValidationError({"movimientos": "Un depósito en tránsito requiere ingreso de Libros sin reflejo en Banco."})
                        for book in books:
                            cursor.execute(
                                """SELECT 1 FROM contared.movimientos b
                                   JOIN contared.cargas l ON l.id=b.carga_id AND l.estado='validada'
                                   WHERE b.cuenta_id=%s AND b.lado='banco' AND b.monto>0
                                     AND b.fecha<=%s AND b.referencia IS NOT DISTINCT FROM %s
                                     AND b.monto=%s LIMIT 1""",
                                [scope["account_id"], recon[2], book[3], book[4]],
                            )
                            if cursor.fetchone():
                                raise ValidationError({"movimientos": "Ya existe un ingreso de Banco compatible; revisa la conciliación antes de clasificarlo en tránsito."})
                    elif classification == "deposito_mal_operado":
                        if sides != {"banco", "libros"} or len(rows) < 2:
                            raise ValidationError({"movimientos": "Una diferencia mal operada requiere movimientos de Banco y Libros."})
                        bank_rows = [row for row in rows if row[1] == "banco"]
                        book_rows = [row for row in rows if row[1] == "libros"]
                        if not any(
                            bank_row[3] and bank_row[3] == book_row[3]
                            for bank_row in bank_rows for book_row in book_rows
                        ):
                            raise ValidationError({"movimientos": "Los movimientos deben compartir una referencia para documentar la diferencia."})
                        bank_total = sum((Decimal(row[4]) for row in bank_rows), Decimal(0))
                        books_total = sum((Decimal(row[4]) for row in book_rows), Decimal(0))
                        if bank_total == books_total:
                            raise ValidationError({"movimientos": "Los importes agrupados son iguales; este caso no corresponde a una diferencia de monto."})
                    case_id = uuid4()
                    cursor.execute(
                        """INSERT INTO contared.casos_deposito
                           (id,cuenta_id,conciliacion_origen_id,clasificacion,
                            origen_correcto,observacion,responsable_id,soporte_ruta,creado_por)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [case_id, scope["account_id"], scope["reconciliation_id"], classification,
                         origin_correct, observation.strip(), scope["user_id"], support_path, scope["user_id"]],
                    )
                    cursor.executemany(
                        """INSERT INTO contared.deposito_movimientos
                           (caso_id,cuenta_id,movimiento_id,rol) VALUES (%s,%s,%s,%s)""",
                        [(case_id, scope["account_id"], row[0], row[1]) for row in rows],
                    )
                    cursor.execute(
                        """INSERT INTO contared.deposito_eventos
                           (caso_id,tipo,clasificacion_nueva,origen_correcto,estado_nuevo,registrado_por,
                            observacion,soporte_ruta,conciliacion_id)
                           VALUES (%s,'clasificado',%s,%s,'abierto',%s,%s,%s,%s)""",
                        [case_id, classification, origin_correct, scope["user_id"], observation.strip(),
                         support_path, scope["reconciliation_id"]],
                    )
        except IntegrityError as exc:
            if support_path:
                _delete_storage_object(support_path)
            raise ValidationError({"movimientos": "Algún movimiento ya está relacionado con otro caso de seguimiento."}) from exc
        except ValidationError:
            if support_path:
                _delete_storage_object(support_path)
            raise
        except APIException:
            if support_path:
                _delete_storage_object(support_path)
            raise
        except DatabaseError as exc:
            if support_path:
                _delete_storage_object(support_path)
            _db_error(exc)
        return Response({"id": str(case_id), "estado": "abierto"}, status=201)


class DepositCaseActionView(APIView):
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def patch(self, request, reconciliation_id, case_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        action = request.data.get("accion")
        if action not in ("documentar", "resolver"):
            raise ValidationError({"accion": "Selecciona una acción válida."})
        if action == "documentar":
            _role(scope, ("administrador", "auxiliar"))
            observation = request.data.get("observacion")
            if not isinstance(observation, str):
                raise ValidationError({"observacion": "Indica una observación."})
            classification = request.data.get("clasificacion")
            if classification is not None and classification not in CLASSIFICATIONS:
                raise ValidationError({"clasificacion": "Selecciona una clasificación válida."})
            origin_correct = request.data.get("origen_correcto")
            if origin_correct is not None and origin_correct not in ("banco", "libros"):
                raise ValidationError({"origen_correcto": "Selecciona Banco o Libros."})
            if origin_correct and classification not in (None, "deposito_mal_operado"):
                raise ValidationError({"origen_correcto": "Solo aplica a diferencias de monto."})
        else:
            _role(scope, ("administrador", "revisor"))
            effective = _iso_date(request.data.get("fecha_efectiva"), "fecha_efectiva")
            reason = request.data.get("motivo")
            if not isinstance(reason, str) or not reason.strip():
                raise ValidationError({"motivo": "Explica la evidencia de resolución."})
            resolving_movement = request.data.get("movimiento_resolucion_id")
            try:
                resolving_movement = UUID(str(resolving_movement)) if resolving_movement else None
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError({"movimiento_resolucion_id": "El movimiento de resolución no es válido."}) from exc
        support_path = _upload_support(request.FILES.get("soporte"), scope)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('contared.actor_id',%s,true)", [scope["user_id"]])
                    cursor.execute(
                        """SELECT d.clasificacion,d.estado,d.conciliacion_origen_id::text,
                                  d.soporte_ruta,ro.fecha_corte
                           FROM contared.casos_deposito d
                           JOIN contared.conciliaciones ro ON ro.id=d.conciliacion_origen_id
                           WHERE d.id=%s AND d.cuenta_id=%s FOR UPDATE OF d""",
                        [case_id, scope["account_id"]],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise NotFound("El caso de depósito no existe en esta cuenta.")
                    if row[2] != scope["reconciliation_id"] and action == "documentar":
                        raise ValidationError({"detail": "Documenta el caso desde su conciliación de origen."})
                    if row[1] != "abierto":
                        raise ValidationError({"detail": "El caso ya está resuelto."})
                    if action == "documentar":
                        new_classification = classification or row[0]
                        if origin_correct and new_classification != "deposito_mal_operado":
                            raise ValidationError({"origen_correcto": "Solo puede confirmarse el origen correcto de una diferencia de monto."})
                        if origin_correct and not (support_path or row[3]):
                            raise ValidationError({"soporte": "Adjunta evidencia privada para confirmar qué origen es correcto."})
                        cursor.execute(
                            """SELECT m.lado,m.referencia,m.monto
                               FROM contared.deposito_movimientos dm
                               JOIN contared.movimientos m ON m.id=dm.movimiento_id
                               WHERE dm.caso_id=%s AND dm.rol IN ('banco','libros')""",
                            [case_id],
                        )
                        related = cursor.fetchall()
                        if new_classification == "deposito_transito":
                            books = [item for item in related if item[0] == "libros"]
                            if not books or any(item[0] == "banco" for item in related):
                                raise ValidationError({"clasificacion": "La clasificación de tránsito requiere únicamente ingresos de Libros sin reflejo en Banco."})
                        if new_classification == "deposito_mal_operado":
                            bank = [item for item in related if item[0] == "banco"]
                            books = [item for item in related if item[0] == "libros"]
                            if not bank or not books or not any(
                                bank_item[1] and bank_item[1] == book_item[1]
                                for bank_item in bank for book_item in books
                            ):
                                raise ValidationError({"clasificacion": "Una diferencia mal operada requiere referencias coincidentes entre Banco y Libros."})
                            if sum((Decimal(item[2]) for item in bank), Decimal(0)) == sum(
                                (Decimal(item[2]) for item in books), Decimal(0)
                            ):
                                raise ValidationError({"clasificacion": "Los importes agrupados son iguales; no corresponde una diferencia de monto."})
                        cursor.execute(
                            """UPDATE contared.casos_deposito
                               SET clasificacion=%s,origen_correcto=%s,observacion=%s,
                                   responsable_id=%s,soporte_ruta=COALESCE(%s,soporte_ruta),
                                   actualizado_en=now()
                               WHERE id=%s""",
                            [new_classification, origin_correct, observation.strip(),
                             scope["user_id"], support_path, case_id],
                        )
                        cursor.execute(
                            """INSERT INTO contared.deposito_eventos
                               (caso_id,tipo,clasificacion_anterior,clasificacion_nueva,
                                origen_correcto,estado_anterior,estado_nuevo,registrado_por,
                                observacion,soporte_ruta,conciliacion_id)
                               VALUES (%s,'actualizado',%s,%s,%s,'abierto','abierto',%s,%s,%s,%s)""",
                            [case_id, row[0], new_classification, origin_correct, scope["user_id"],
                             observation.strip(), support_path, scope["reconciliation_id"]],
                        )
                    else:
                        if effective <= row[4]:
                            raise ValidationError({"fecha_efectiva": "La fecha efectiva debe ser posterior al corte de origen."})
                        if effective > scope["cutoff"]:
                            raise ValidationError({"fecha_efectiva": "La fecha efectiva debe estar dentro de la fecha de corte."})
                        cursor.execute(
                            """SELECT r.id::text FROM contared.conciliaciones r
                               WHERE r.id=%s AND r.cuenta_id=%s""",
                            [scope["reconciliation_id"], scope["account_id"]],
                        )
                        if cursor.fetchone() is None:
                            raise NotFound("La conciliación de resolución no existe.")
                        if resolving_movement:
                            cursor.execute(
                                """SELECT b.id::text,b.fecha,b.referencia,b.monto
                                   FROM contared.movimientos b
                                   JOIN contared.cargas l ON l.id=b.carga_id AND l.estado='validada'
                                   WHERE b.id=%s AND b.cuenta_id=%s AND b.lado='banco'
                                     AND b.monto>0 AND b.fecha=%s
                                     AND EXISTS (
                                       SELECT 1 FROM contared.deposito_movimientos dm
                                       JOIN contared.movimientos src ON src.id=dm.movimiento_id
                                       WHERE dm.caso_id=%s AND dm.rol='libros'
                                         AND src.referencia IS NOT DISTINCT FROM b.referencia
                                         AND src.monto=b.monto
                                     )
                                   FOR UPDATE OF b""",
                                [resolving_movement, scope["account_id"], effective, case_id],
                            )
                            if cursor.fetchone() is None:
                                raise ValidationError({"movimiento_resolucion_id": "El movimiento bancario no coincide exactamente con una partida de Libros del caso."})
                        cursor.execute(
                            """UPDATE contared.casos_deposito
                               SET estado='resuelto',conciliacion_resolucion_id=%s,
                                   fecha_efectiva=%s,motivo_resolucion=%s,
                                       soporte_ruta=COALESCE(%s,soporte_ruta),
                                       actualizado_en=now()
                                   WHERE id=%s""",
                                [scope["reconciliation_id"], effective, reason.strip(), support_path, case_id],
                        )
                        if resolving_movement:
                            cursor.execute(
                                """INSERT INTO contared.deposito_movimientos
                                   (caso_id,cuenta_id,movimiento_id,rol)
                                   VALUES (%s,%s,%s,'resolucion')""",
                                [case_id, scope["account_id"], resolving_movement],
                            )
                        cursor.execute(
                            """INSERT INTO contared.deposito_eventos
                               (caso_id,tipo,clasificacion_anterior,clasificacion_nueva,
                                origen_correcto,estado_anterior,estado_nuevo,fecha_efectiva,registrado_por,
                                observacion,motivo,soporte_ruta,conciliacion_id)
                               VALUES (%s,'resuelto',%s,%s,%s,'abierto','resuelto',%s,%s,%s,%s,%s,%s)""",
                            [case_id, row[0], row[0], None, effective, scope["user_id"],
                             "", reason.strip(), support_path, scope["reconciliation_id"]],
                        )
        except IntegrityError as exc:
            if support_path:
                _delete_storage_object(support_path)
            raise ValidationError({"movimiento_resolucion_id": "El movimiento ya está vinculado a otro seguimiento."}) from exc
        except ValidationError:
            if support_path:
                _delete_storage_object(support_path)
            raise
        except APIException:
            if support_path:
                _delete_storage_object(support_path)
            raise
        except DatabaseError as exc:
            if support_path:
                _delete_storage_object(support_path)
            _db_error(exc)
        return Response({"id": str(case_id), "estado": "abierto" if action == "documentar" else "resuelto"})


class DepositHistoryView(APIView):
    def get(self, request, reconciliation_id, case_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """SELECT e.id::text,e.tipo,e.clasificacion_anterior,
                                  e.clasificacion_nueva,e.origen_correcto,
                                  e.estado_anterior,e.estado_nuevo,
                                  e.fecha_efectiva,e.registrado_en,u.nombre,e.observacion,
                                  e.motivo,e.conciliacion_id::text
                           FROM contared.deposito_eventos e
                           JOIN contared.casos_deposito d ON d.id=e.caso_id
                           LEFT JOIN contared.usuarios u ON u.id=e.registrado_por
                           WHERE e.caso_id=%s AND d.cuenta_id=%s
                           ORDER BY e.registrado_en,e.id""",
                        [case_id, scope["account_id"]],
                    )
                    events = [{
                        "tipo": row[1], "clasificacion_anterior": row[2],
                        "clasificacion_nueva": row[3], "origen_correcto": row[4],
                        "estado_anterior": row[5], "estado_nuevo": row[6],
                        "fecha_efectiva": row[7].isoformat() if row[7] else None,
                        "registrado_en": row[8].isoformat(), "usuario": row[9],
                        "observacion": row[10], "motivo": row[11],
                        "conciliacion_id": row[12],
                    } for row in cursor.fetchall()]
        except DatabaseError as exc:
            _db_error(exc)
        return Response({"events": events})


class DepositSupportView(APIView):
    def get(self, request, reconciliation_id, case_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """SELECT soporte_ruta FROM contared.casos_deposito
                           WHERE id=%s AND cuenta_id=%s AND soporte_ruta IS NOT NULL""",
                        [case_id, scope["account_id"]],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise NotFound("El soporte no existe.")
                    object_path = row[0]
            return _download_support(object_path)
        except DatabaseError as exc:
            _db_error(exc)
