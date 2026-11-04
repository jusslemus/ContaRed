import hashlib
import json
import logging
import re
import tempfile
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import HttpResponse
from openpyxl import Workbook, load_workbook
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.conciliaciones import (
    ImportStorageUnavailable,
    _delete_storage_object,
    _normalize_original_data,
    _scope_for_account,
    _scope_for_reconciliation,
    _storage_request,
)
from contared_api.imports import (
    MAX_DATA_ROWS,
    MAX_SHEETS,
    MAX_UPLOAD_BYTES,
    _check_archive,
    _file_format,
    _json_value,
    _normalize_xls,
    _normalized_header,
    parse_date,
    parse_decimal,
)
from contared_api.session_access import DatabaseUnavailable, database_error_details


logger = logging.getLogger(__name__)

_BANK_CHEQUE_NUMBER_SQL = """
CASE
  WHEN NULLIF(btrim(b.numero_cheque),'') IS NOT NULL
   AND btrim(b.numero_cheque) !~ '^0+$'
    THEN btrim(b.numero_cheque)
  WHEN b.codigo_tt='CQ'
   AND bank.nombre IN ('Banco Industrial','Banco Industrial de Guatemala')
   AND NULLIF(btrim(b.referencia),'') IS NOT NULL
   AND btrim(b.referencia) !~ '^0+$'
    THEN btrim(b.referencia)
END
"""
SUPPORT_MAX_BYTES = 10 * 1024 * 1024
READ_ROLES = ("administrador", "auxiliar", "revisor", "consulta")
CLASSIFY_ROLES = ("administrador", "auxiliar")
REVIEW_ROLES = ("administrador", "revisor")
REQUIRED_CHEQUE_SCHEMA = {
    "cheques": {
        "id", "cuenta_id", "conciliacion_origen_id", "movimiento_emision_id",
        "numero_cheque", "moneda_codigo", "fecha_emision", "descripcion", "referencia",
        "periodo_origen", "clave_fuente",
        "beneficiario",
        "monto", "estado", "fecha_entrega", "entregado_por",
        "observacion_entrega", "movimiento_cobro_id",
        "conciliacion_resolucion_id", "fecha_anulacion", "motivo_anulacion",
        "soporte_anulacion_ruta", "creado_por", "creado_en", "actualizado_en",
    },
    "cheque_eventos": {
        "id", "cheque_id", "estado_anterior", "estado_nuevo", "fecha_efectiva",
        "registrado_por", "registrado_en", "motivo", "observacion",
        "movimiento_banco_id", "conciliacion_id", "cruce_id", "soporte_ruta",
    },
    "cheque_importaciones": {
        "id", "cuenta_id", "tipo_documento", "fecha_corte", "version",
        "nombre_archivo", "sha256", "object_path", "perfil", "registros_total",
        "creado_por", "creado_en",
    },
    "cheque_importacion_detalles": {
        "id", "importacion_id", "cheque_id", "fila_origen", "numero_cheque",
        "fecha_emision", "beneficiario", "descripcion", "referencia", "monto",
        "estado", "datos_originales", "creado_en",
    },
}


class ChequesMigrationRequired(APIException):
    status_code = 503
    default_detail = (
        "El módulo de importación independiente de cheques requiere aplicar "
        "supabase/migrations/202611010001_cheques_independientes.sql después de las migraciones anteriores."
    )
    default_code = "cheques_migration_required"


def _cheque_schema_columns_missing(rows):
    columns_by_table = {}
    for table_name, column_name in rows:
        columns_by_table.setdefault(table_name, set()).add(column_name)
    return any(
        not required_columns.issubset(columns_by_table.get(table_name, set()))
        for table_name, required_columns in REQUIRED_CHEQUE_SCHEMA.items()
    )


def _required_cheque_schema_missing():
    table_names = tuple(REQUIRED_CHEQUE_SCHEMA)
    placeholders = ",".join(["%s"] * len(table_names))
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT table_name,column_name
                FROM information_schema.columns
                WHERE table_schema=%s AND table_name IN ({placeholders})
                """,
                ["contared", *table_names],
            )
            rows = cursor.fetchall()
    except DatabaseError as exc:
        logger.error(
            "ContaRed cheque schema diagnostic failed "
            "(%s).",
            database_error_details(exc),
        )
        return False
    return _cheque_schema_columns_missing(rows)


def _database_error(exc):
    cause = exc.__cause__
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if sqlstate in ("42P01", "42703") and _required_cheque_schema_missing():
        logger.error(
            "ContaRed cheque operation requires its database schema "
            "(SQLSTATE: %s).",
            sqlstate,
        )
        raise ChequesMigrationRequired() from exc
    logger.error(
        "ContaRed cheque database query failed (%s).",
        database_error_details(exc),
    )
    raise DatabaseUnavailable() from exc


def _parse_date(value, field):
    if not isinstance(value, str):
        raise ValidationError({field: "Indica una fecha válida con formato YYYY-MM-DD."})
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError({field: "Indica una fecha válida con formato YYYY-MM-DD."}) from exc


DEFAULT_CHEQUE_IMPORT_PROFILE = {
    "sheet": "Cheques",
    "header_row": 1,
    "date_format": "%Y-%m-%d",
    "mapping": {
        "numero_cheque": "numero_cheque",
        "fecha_emision": "fecha_emision",
        "beneficiario": "beneficiario",
        "monto": "monto",
        "estado": "estado_al_corte",
        "fecha_entrega": "fecha_entrega",
        "descripcion": "descripcion",
        "referencia": "referencia",
    },
}


def _cheque_import_profile(raw_profile):
    if raw_profile in (None, ""):
        profile = DEFAULT_CHEQUE_IMPORT_PROFILE
    elif isinstance(raw_profile, str):
        try:
            profile = json.loads(raw_profile)
        except json.JSONDecodeError as exc:
            raise ValidationError({"perfil": "El perfil debe ser JSON válido."}) from exc
    else:
        profile = raw_profile
    if not isinstance(profile, dict) or set(profile) - {
        "sheet", "header_row", "date_format", "mapping",
    }:
        raise ValidationError({"perfil": "El perfil solo admite hoja, header_row, date_format y mapping."})
    mapping = profile.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) - {
        "numero_cheque", "fecha_emision", "beneficiario", "monto", "estado",
        "fecha_entrega", "descripcion", "referencia",
    }:
        raise ValidationError({"perfil.mapping": "El mapeo contiene campos no permitidos."})
    for field in ("numero_cheque", "fecha_emision", "beneficiario", "monto", "estado"):
        if not mapping.get(field):
            raise ValidationError({"perfil.mapping": f"Configura la columna {field}."})
    row = profile.get("header_row", 1)
    if isinstance(row, bool) or not isinstance(row, int) or not 1 <= row <= 100:
        raise ValidationError({"perfil.header_row": "La fila de encabezados debe estar entre 1 y 100."})
    sheet = profile.get("sheet", "Cheques")
    if not isinstance(sheet, str) or not sheet.strip() or len(sheet) > 31:
        raise ValidationError({"perfil.sheet": "Indica el nombre de una hoja válido."})
    date_format = profile.get("date_format", "%Y-%m-%d")
    if not isinstance(date_format, str) or not date_format or len(date_format) > 50:
        raise ValidationError({"perfil.date_format": "Indica un formato de fecha válido."})
    return {
        "sheet": sheet.strip(),
        "header_row": row,
        "date_format": date_format,
        "mapping": mapping,
    }


def _cheque_column_index(headers, configured, field):
    if isinstance(configured, bool):
        raise ValidationError({"perfil.mapping": f"La columna de {field} no es válida."})
    if isinstance(configured, int) or (
        isinstance(configured, str) and configured.strip().isdigit()
    ):
        index = int(configured) - 1
    elif isinstance(configured, str):
        value = configured.strip()
        normalized = _normalized_header(value)
        matches = [
            index for index, header in enumerate(headers)
            if _normalized_header(header) == normalized
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValidationError({"perfil.mapping": f"La columna de {field} aparece más de una vez."})
        if not re.fullmatch(r"[A-Za-z]{1,3}", value):
            raise ValidationError({"perfil.mapping": f"No se encontró la columna configurada para {field}."})
        index = 0
        for character in value.upper():
            index = index * 26 + ord(character) - ord("A") + 1
        index -= 1
    else:
        raise ValidationError({"perfil.mapping": f"La columna de {field} no es válida."})
    if index < 0 or index >= len(headers):
        raise ValidationError({"perfil.mapping": f"La columna configurada para {field} no existe."})
    return index


def _cheque_source_key(number, issue_date):
    normalized = unicodedata.normalize("NFKC", number).strip().casefold()
    return f"{normalized}|{issue_date.isoformat()}"


def _parse_cheque_workbook(path, profile, cutoff):
    _check_archive(path)
    workbook = load_workbook(path, read_only=True, data_only=True)
    errors = []
    warnings = []
    parsed = []
    try:
        if len(workbook.sheetnames) > MAX_SHEETS:
            raise ValidationError({"archivo": "El libro supera el límite de hojas permitido."})
        if profile["sheet"] not in workbook.sheetnames:
            raise ValidationError({"perfil.sheet": "La hoja indicada no existe en el libro."})
        sheet = workbook[profile["sheet"]]
        if sheet.max_column and sheet.max_column > 200:
            raise ValidationError({"archivo": "La hoja supera el límite de columnas permitido."})
        header_cells = next(sheet.iter_rows(
            min_row=profile["header_row"],
            max_row=profile["header_row"],
        ), None)
        if not header_cells:
            raise ValidationError({"perfil.header_row": "La fila configurada no existe."})
        headers = [str(cell.value).strip() if cell.value is not None else "" for cell in header_cells]
        if not any(headers):
            raise ValidationError({"perfil.header_row": "La fila configurada no contiene encabezados."})
        indexes = {
            field: _cheque_column_index(headers, column, field)
            for field, column in profile["mapping"].items()
        }
        if len(set(indexes.values())) != len(indexes):
            raise ValidationError({"perfil.mapping": "Cada dato debe usar una columna distinta."})
        seen = set()
        for row_number, cells in enumerate(
            sheet.iter_rows(
                min_row=profile["header_row"] + 1,
                max_row=sheet.max_row,
            ),
            profile["header_row"] + 1,
        ):
            if row_number - profile["header_row"] > MAX_DATA_ROWS:
                raise ValidationError({"archivo": f"La hoja supera el máximo de {MAX_DATA_ROWS} filas."})
            values = [cell.value for cell in cells]
            if not any(value is not None and str(value).strip() for value in values):
                continue
            row_errors = []
            raw_number = values[indexes["numero_cheque"]]
            number_cell = cells[indexes["numero_cheque"]]
            if isinstance(raw_number, bool) or raw_number is None:
                number = ""
            elif isinstance(raw_number, (int, float)):
                number = str(int(raw_number)) if float(raw_number).is_integer() else str(raw_number)
                zeros = re.fullmatch(r"0+", number_cell.number_format or "")
                if zeros:
                    number = number.zfill(len(number_cell.number_format))
                else:
                    warnings.append({
                        "fila": row_number,
                        "mensaje": "El número de cheque es numérico; verifica que no se hayan perdido ceros iniciales.",
                    })
            else:
                number = str(raw_number).strip()
            if not number or len(number) > 100:
                row_errors.append("Número de cheque vacío o demasiado largo.")
            try:
                issue_date = parse_date(
                    values[indexes["fecha_emision"]],
                    profile["date_format"],
                    "fecha_emision",
                )
                if issue_date > cutoff:
                    row_errors.append("La fecha de emisión es posterior al corte seleccionado.")
            except ValueError as exc:
                issue_date = None
                row_errors.append(str(exc))
            try:
                amount = parse_decimal(values[indexes["monto"]], "monto")
                if amount is None or amount <= 0:
                    raise ValueError("El importe debe ser positivo.")
            except ValueError as exc:
                amount = None
                row_errors.append(str(exc))
            beneficiary = None
            if "beneficiario" in indexes:
                raw_beneficiary = values[indexes["beneficiario"]]
                beneficiary = str(raw_beneficiary).strip() if raw_beneficiary not in (None, "") else None
                if beneficiary and len(beneficiary) > 200:
                    row_errors.append("El beneficiario supera 200 caracteres.")
                if not beneficiary:
                    warnings.append({"fila": row_number, "mensaje": "No se informó beneficiario."})
            state_text = _normalized_header(values[indexes["estado"]] or "")
            states = {
                "retenido": "retenido",
                "retained": "retenido",
                "en circulacion": "en_circulacion",
                "en_circulacion": "en_circulacion",
                "circulating": "en_circulacion",
            }
            state = states.get(state_text)
            if state is None:
                row_errors.append("Estado al corte debe ser Retenido o En circulación.")
            delivery_date = None
            if state == "en_circulacion":
                if "fecha_entrega" not in indexes:
                    row_errors.append("Configura fecha_entrega para registrar cheques En circulación.")
                else:
                    try:
                        delivery_date = parse_date(
                            values[indexes["fecha_entrega"]],
                            profile["date_format"],
                            "fecha_entrega",
                        )
                        if issue_date and not issue_date <= delivery_date <= cutoff:
                            row_errors.append("La entrega debe estar entre la emisión y la fecha de corte.")
                    except ValueError as exc:
                        row_errors.append(str(exc))
            description_value = values[indexes["descripcion"]] if "descripcion" in indexes else None
            description = str(description_value).strip() if description_value not in (None, "") else f"Cheque {number}"
            reference_value = values[indexes["referencia"]] if "referencia" in indexes else None
            reference = str(reference_value).strip() if reference_value not in (None, "") else None
            if len(description) > 500:
                row_errors.append("La descripción supera 500 caracteres.")
            if reference and len(reference) > 200:
                row_errors.append("La referencia supera 200 caracteres.")
            key = _cheque_source_key(number, issue_date) if number and issue_date else ""
            if key and key in seen:
                row_errors.append("El número y fecha de emisión están repetidos en el documento.")
            if key:
                seen.add(key)
            if row_errors:
                errors.append({"fila": row_number, "errores": row_errors})
                continue
            parsed.append({
                "fila_origen": row_number,
                "clave_fuente": key,
                "numero_cheque": number,
                "fecha_emision": issue_date,
                "beneficiario": beneficiary,
                "descripcion": description,
                "referencia": reference,
                "monto": amount,
                "estado": state,
                "fecha_entrega": delivery_date,
                "datos_originales": {
                    headers[index] or f"columna_{index + 1}": _json_value(value)
                    for index, value in enumerate(values)
                    if index < len(headers)
                },
            })
    finally:
        workbook.close()
    if errors:
        raise ValidationError({
            "detail": "El documento contiene filas que requieren corrección; no se importó ningún cheque.",
            "errores": errors,
            "advertencias": warnings,
        })
    if not parsed:
        raise ValidationError({"archivo": "El documento no contiene cheques para importar."})
    return parsed, warnings


def _require_role(scope, allowed):
    if scope["role"] not in allowed:
        raise PermissionDenied("Tu rol no permite esta operación.", code="role_not_permitted")


def _locked_scope(cursor, request, reconciliation_id, *, lock=True):
    lock_clause = "FOR UPDATE OF r FOR SHARE OF u,a,e" if lock else ""
    cursor.execute(
        f"""
        SELECT r.id::text,r.cuenta_id::text,cb.empresa_id::text,cb.pais_codigo,
               cb.moneda_codigo,r.periodo,r.fecha_corte,u.id::text,a.rol,u.nombre,
               bank.nombre
        FROM contared.conciliaciones r
        JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
        JOIN contared.bancos bank ON bank.id=cb.banco_id
        JOIN contared.empresas e ON e.id=cb.empresa_id AND e.activa IS TRUE
        JOIN contared.accesos a
          ON a.empresa_id=cb.empresa_id AND a.pais_codigo=cb.pais_codigo
        JOIN contared.usuarios u
          ON u.id=a.usuario_id AND u.auth_user_id=%s AND u.activo IS TRUE
        WHERE r.id=%s
        {lock_clause}
        """,
        [request.user.auth_user_id, reconciliation_id],
    )
    row = cursor.fetchone()
    if row is None:
        raise NotFound("La conciliación no existe o no tienes acceso.")
    return {
        "reconciliation_id": row[0],
        "account_id": row[1],
        "company_id": row[2],
        "country_code": row[3],
        "currency_code": row[4],
        "period": row[5],
        "cutoff": row[6],
        "user_id": row[7],
        "role": row[8],
        "user_name": row[9],
        "bank_name": row[10],
    }


def _set_actor(cursor, scope):
    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])


def _related_issuances(cursor, scope, number):
    cursor.execute(
        """
        SELECT m.id::text AS movimiento_emision_id,m.numero_cheque,
               m.fecha AS fecha_emision,m.descripcion,abs(m.monto)::text,
               c.metodo,ch.id::text AS cheque_id,ch.estado
        FROM contared.movimientos m
        JOIN contared.cargas c ON c.id=m.carga_id
        JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
        LEFT JOIN contared.cheques ch ON ch.movimiento_emision_id=m.id
        WHERE m.cuenta_id=%s AND m.lado='libros' AND c.origen='libros'
          AND c.estado='validada' AND r.periodo<=%s AND m.fecha<=%s
          AND m.monto<0 AND m.numero_cheque=%s
        UNION ALL
        SELECT NULL::text,ch.numero_cheque,ch.fecha_emision,ch.descripcion,ch.monto::text,
               'independiente',ch.id::text,ch.estado
        FROM contared.cheques ch
        LEFT JOIN contared.conciliaciones r ON r.id=ch.conciliacion_origen_id
        WHERE ch.cuenta_id=%s AND ch.movimiento_emision_id IS NULL
          AND COALESCE(ch.periodo_origen,r.periodo)<=%s
          AND ch.fecha_emision<=%s AND ch.numero_cheque=%s
        ORDER BY fecha_emision DESC,movimiento_emision_id
        LIMIT 25
        """,
        [
            scope["account_id"],scope["period"],scope["cutoff"],number,
            scope["account_id"],scope["period"],scope["cutoff"],number,
        ],
    )
    return [
        {
            "movimiento_emision_id": row[0],
            "numero_cheque": row[1],
            "fecha_emision": row[2].isoformat(),
            "descripcion": row[3] or "",
            "monto": row[4],
            "metodo": row[5],
            "cheque_id": row[6],
            "estado": row[7],
        }
        for row in cursor.fetchall()
    ]


def _reflect_existing_cheque_cross(cursor, scope, book_movement_id):
    cursor.execute(
        """
        SELECT x.id::text,x.conciliacion_id::text,
               array_agg(CASE WHEN bank.lado='banco' THEN bank.id::text END)
                 FILTER (WHERE bank.lado='banco'),
               array_agg(CASE WHEN books.lado='libros' THEN books.id::text END)
                 FILTER (WHERE books.lado='libros')
        FROM contared.cruces x
        JOIN contared.cruce_movimientos cm_book ON cm_book.cruce_id=x.id
        JOIN contared.movimientos books ON books.id=cm_book.movimiento_id
        JOIN contared.cruce_movimientos cm_bank ON cm_bank.cruce_id=x.id
        JOIN contared.movimientos bank ON bank.id=cm_bank.movimiento_id
        WHERE x.cuenta_id=%s AND x.estado='confirmado'
          AND books.id=%s AND books.lado='libros' AND bank.lado='banco'
        GROUP BY x.id,x.conciliacion_id
        HAVING count(*) FILTER (WHERE bank.lado='banco')=1
           AND count(*) FILTER (WHERE books.lado='libros')=1
        """,
        [scope["account_id"],book_movement_id],
    )
    cross = cursor.fetchone()
    if cross is None:
        return False
    cross_scope = {**scope, "reconciliation_id": cross[1]}
    return ChequeCrossSync.reflect(cursor,cross_scope,cross[2],cross[3],cross[0])


def _cheque_counts(cursor, scope):
    cursor.execute(
        """
        SELECT state, count(*) FROM (
          SELECT ch.id,ev.estado_nuevo AS state
          FROM contared.cheques ch
          LEFT JOIN contared.conciliaciones origin
            ON origin.id=ch.conciliacion_origen_id
          LEFT JOIN LATERAL (
            SELECT e.estado_nuevo FROM contared.cheque_eventos e
            WHERE e.cheque_id=ch.id AND e.fecha_efectiva<=%s
            ORDER BY e.fecha_efectiva DESC,e.registrado_en DESC,e.id DESC
            LIMIT 1
          ) ev ON true
          WHERE ch.cuenta_id=%s AND COALESCE(ch.periodo_origen,origin.periodo)<=%s
            AND ch.fecha_emision<=%s
            AND ev.estado_nuevo IS NOT NULL
        ) statuses
        GROUP BY state
        """,
        [scope["cutoff"], scope["account_id"], scope["period"], scope["cutoff"]],
    )
    counts = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT count(*)
        FROM contared.movimientos m
        JOIN contared.cargas c ON c.id=m.carga_id
        JOIN contared.conciliaciones r
          ON r.id=c.conciliacion_id AND r.cuenta_id=c.cuenta_id
        WHERE m.cuenta_id=%s AND m.lado='libros' AND c.origen='libros'
          AND c.estado='validada' AND r.periodo<=%s AND m.fecha<=%s AND m.monto<0
          AND NULLIF(btrim(m.numero_cheque),'') IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM contared.cheques ch
            JOIN contared.cheque_eventos ev ON ev.cheque_id=ch.id
            WHERE ch.movimiento_emision_id=m.id AND ev.fecha_efectiva<=%s
          )
        """,
        [scope["account_id"], scope["period"], scope["cutoff"], scope["cutoff"]],
    )
    counts["candidato"] = cursor.fetchone()[0]
    return {
        "candidatos": counts.get("candidato", 0),
        "retenido": counts.get("retenido", 0),
        "en_circulacion": counts.get("en_circulacion", 0),
        "cobrado": counts.get("cobrado", 0),
        "anulado": counts.get("anulado", 0),
    }


def _fetch_cheques(cursor, scope, view, search, age_filter, page, page_size):
    state_map = {
        "retenidos": "retenido",
        "circulacion": "en_circulacion",
        "cobrados": "cobrado",
        "anulados": "anulado",
    }
    if view not in ("candidatos", "todos", *state_map):
        raise ValidationError({"vista": "La vista de cheques no es válida."})
    cursor.execute(
        """
        WITH items AS (
          SELECT ch.id::text AS id,'registrado'::text AS tipo,
                 ev.estado_nuevo AS estado,ch.movimiento_emision_id::text AS movimiento_emision_id,
                 ch.numero_cheque,ch.fecha_emision,
                 COALESCE(NULLIF(ch.descripcion,''),m.descripcion) AS descripcion,
                 COALESCE(NULLIF(ch.beneficiario,''),
                          NULLIF(m.datos_originales->>'beneficiario',''),
                          NULLIF(m.datos_originales->>'beneficiary','')) AS beneficiario,
                 ch.monto::text AS monto,ch.moneda_codigo,
                 COALESCE(ch.periodo_origen,origin.periodo) AS periodo_origen,
                 delivery.fecha_efectiva AS fecha_entrega,
                 ev.fecha_efectiva AS fecha_evento,ch.movimiento_cobro_id::text,
                 ch.conciliacion_resolucion_id::text,
                 CASE WHEN ev.estado_nuevo='anulado' THEN ev.fecha_efectiva END,
                 CASE WHEN ev.estado_nuevo='anulado' THEN ev.motivo END,
                 ev.registrado_en,ev.registrado_por::text,
                 ((SELECT count(*) FROM contared.movimientos duplicate
                  JOIN contared.cargas dc ON dc.id=duplicate.carga_id
                  WHERE duplicate.cuenta_id=ch.cuenta_id AND duplicate.lado='libros'
                    AND dc.estado='validada' AND duplicate.numero_cheque=ch.numero_cheque)
                  + (SELECT count(*) FROM contared.cheques duplicate
                     WHERE duplicate.cuenta_id=ch.cuenta_id
                       AND duplicate.movimiento_emision_id IS NULL
                       AND duplicate.numero_cheque=ch.numero_cheque)) > 1
                   AS ambiguo,
                 COALESCE(ch.referencia,m.referencia),m.numero_transaccion
          FROM contared.cheques ch
          LEFT JOIN contared.movimientos m ON m.id=ch.movimiento_emision_id
          LEFT JOIN contared.conciliaciones origin ON origin.id=ch.conciliacion_origen_id
          LEFT JOIN LATERAL (
            SELECT e.estado_nuevo,e.fecha_efectiva,e.registrado_en,
                   e.registrado_por,e.motivo
            FROM contared.cheque_eventos e
            WHERE e.cheque_id=ch.id AND e.fecha_efectiva<=%s
            ORDER BY e.fecha_efectiva DESC,e.registrado_en DESC,e.id DESC
            LIMIT 1
          ) ev ON true
          LEFT JOIN LATERAL (
            SELECT e.fecha_efectiva FROM contared.cheque_eventos e
            WHERE e.cheque_id=ch.id AND e.estado_nuevo='en_circulacion'
              AND e.fecha_efectiva<=%s
            ORDER BY e.fecha_efectiva DESC,e.registrado_en DESC,e.id DESC
            LIMIT 1
          ) delivery ON true
          WHERE ch.cuenta_id=%s
            AND COALESCE(ch.periodo_origen,origin.periodo)<=%s
            AND ch.fecha_emision<=%s
            AND ev.estado_nuevo IS NOT NULL
          UNION ALL
          SELECT m.id::text,'candidato'::text,'candidato'::text,m.id::text,
                 m.numero_cheque,m.fecha,m.descripcion,
                 COALESCE(NULLIF(m.datos_originales->>'beneficiario',''),
                          NULLIF(m.datos_originales->>'beneficiary','')),
                 abs(m.monto)::text,cb.moneda_codigo,r.periodo,NULL::date,
                 NULL::date,NULL::text,NULL::text,NULL::date,NULL::text,
                 NULL::timestamptz,NULL::text,
                 ((SELECT count(*) FROM contared.movimientos duplicate
                  JOIN contared.cargas dc ON dc.id=duplicate.carga_id
                  WHERE duplicate.cuenta_id=m.cuenta_id AND duplicate.lado='libros'
                    AND dc.estado='validada' AND duplicate.numero_cheque=m.numero_cheque)
                  + (SELECT count(*) FROM contared.cheques duplicate
                     WHERE duplicate.cuenta_id=m.cuenta_id
                       AND duplicate.movimiento_emision_id IS NULL
                       AND duplicate.numero_cheque=m.numero_cheque)) > 1,
                 m.referencia,m.numero_transaccion
          FROM contared.movimientos m
          JOIN contared.cargas c ON c.id=m.carga_id
          JOIN contared.conciliaciones r
            ON r.id=c.conciliacion_id AND r.cuenta_id=c.cuenta_id
          JOIN contared.cuentas_bancarias cb ON cb.id=m.cuenta_id
          WHERE m.cuenta_id=%s AND m.lado='libros' AND c.origen='libros'
            AND c.estado='validada' AND r.periodo<=%s AND m.fecha<=%s AND m.monto<0
            AND NULLIF(btrim(m.numero_cheque),'') IS NOT NULL
            AND NOT EXISTS (
              SELECT 1 FROM contared.cheques ch
              JOIN contared.cheque_eventos ev ON ev.cheque_id=ch.id
              WHERE ch.movimiento_emision_id=m.id AND ev.fecha_efectiva<=%s
            )
        )
        SELECT i.*,count(*) OVER() AS total
        FROM items i
        WHERE (%s='todos' OR (%s='candidatos' AND i.tipo='candidato')
          OR (%s='retenidos' AND i.estado='retenido')
          OR (%s='circulacion' AND i.estado='en_circulacion')
          OR (%s='cobrados' AND i.estado='cobrado')
          OR (%s='anulados' AND i.estado='anulado'))
          AND (%s='todos' OR (%s='anteriores' AND i.periodo_origen<%s)
            OR (%s='periodo' AND i.periodo_origen>=%s))
          AND (%s='' OR i.numero_cheque ILIKE %s OR COALESCE(i.descripcion,'') ILIKE %s
            OR COALESCE(i.beneficiario,'') ILIKE %s)
        ORDER BY i.fecha_emision DESC,i.numero_cheque,i.id
        LIMIT %s OFFSET %s
        """,
        [
            scope["cutoff"],scope["cutoff"],scope["account_id"],scope["period"],scope["cutoff"],
            scope["account_id"], scope["period"], scope["cutoff"],scope["cutoff"],
            view, view, view, view, view, view,
            age_filter,age_filter,scope["period"],age_filter,scope["period"],
            search, f"%{search}%", f"%{search}%", f"%{search}%",
            page_size, (page - 1) * page_size,
        ],
    )
    rows = cursor.fetchall()
    total = rows[0][-1] if rows else 0
    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "tipo": row[1],
            "estado": row[2],
            "movimiento_emision_id": row[3],
            "numero_cheque": row[4],
            "fecha_emision": row[5].isoformat(),
            "descripcion": row[6] or "",
            "beneficiario": row[7],
            "monto": row[8],
            "moneda_codigo": row[9],
            "periodo_origen": row[10].isoformat(),
            "fecha_entrega": row[11].isoformat() if row[11] else None,
            "fecha_ultimo_evento": row[12].isoformat() if row[12] else None,
            "movimiento_cobro_id": row[13],
            "conciliacion_resolucion_id": row[14],
            "fecha_anulacion": row[15].isoformat() if row[15] else None,
            "motivo_anulacion": row[16],
            "registrado_en": row[17].isoformat() if row[17] else None,
            "registrado_por_id": row[18],
            "ambiguo": bool(row[19]),
            "referencia": row[20],
            "numero_transaccion": row[21],
        })
    return results, total


class ChequeIssuanceCandidatesView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, CLASSIFY_ROLES)
        search = request.query_params.get("busqueda", "").strip()[:100]
        try:
            page = int(request.query_params.get("page", "1"))
            page_size = min(int(request.query_params.get("page_size", "25")),50)
        except ValueError as exc:
            raise ValidationError({"page": "La página y su tamaño deben ser números enteros."}) from exc
        if page<1 or page_size<1:
            raise ValidationError({"page":"La página y su tamaño deben ser positivos."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    current = _locked_scope(cursor, request, reconciliation_id, lock=False)
                    _require_role(current, CLASSIFY_ROLES)
                    cursor.execute(
                        """
                        SELECT m.id::text,m.numero_cheque,m.fecha,m.descripcion,
                               abs(m.monto)::text,cb.moneda_codigo,r.periodo,
                               COALESCE(NULLIF(ch.beneficiario,''),
                                        NULLIF(m.datos_originales->>'beneficiario',''),
                                        NULLIF(m.datos_originales->>'beneficiary','')),
                               c.metodo,ch.id::text,ch.estado,
                               (SELECT count(*) FROM contared.movimientos repeated
                                JOIN contared.cargas rc ON rc.id=repeated.carga_id
                                WHERE repeated.cuenta_id=m.cuenta_id
                                  AND repeated.lado='libros' AND rc.estado='validada'
                                  AND repeated.numero_cheque=m.numero_cheque)>1 AS ambiguo,
                               count(*) OVER() AS total
                        FROM contared.movimientos m
                        JOIN contared.cargas c ON c.id=m.carga_id
                        JOIN contared.conciliaciones r
                          ON r.id=c.conciliacion_id AND r.cuenta_id=c.cuenta_id
                        JOIN contared.cuentas_bancarias cb ON cb.id=m.cuenta_id
                        LEFT JOIN contared.cheques ch ON ch.movimiento_emision_id=m.id
                        WHERE m.cuenta_id=%s AND m.lado='libros' AND c.origen='libros'
                          AND c.estado='validada' AND c.empresa_id=%s AND c.pais_codigo=%s
                          AND r.periodo<=%s AND m.fecha<=%s AND m.monto<0
                          AND NULLIF(btrim(m.numero_cheque),'') IS NOT NULL
                          AND (%s='' OR m.numero_cheque ILIKE %s
                            OR COALESCE(m.descripcion,'') ILIKE %s)
                        ORDER BY r.periodo DESC,m.fecha DESC,m.id
                        LIMIT %s OFFSET %s
                        """,
                        [
                            current["account_id"],current["company_id"],
                            current["country_code"],current["period"],current["cutoff"],
                            search,f"%{search}%",f"%{search}%",page_size,(page-1)*page_size,
                        ],
                    )
                    rows = cursor.fetchall()
                    return Response({
                        "results": [
                            {
                                "movimiento_emision_id": row[0],
                                "numero_cheque": row[1],
                                "fecha_emision": row[2].isoformat(),
                                "descripcion": row[3] or "",
                                "monto": row[4],
                                "moneda_codigo": row[5],
                                "periodo_origen": row[6].isoformat(),
                                "beneficiario": row[7],
                                "metodo": row[8],
                                "cheque_id": row[9],
                                "cheque_estado": row[10],
                                "ambiguo": bool(row[11]),
                            }
                            for row in rows
                        ],
                        "count": rows[0][12] if rows else 0,
                        "page": page,
                        "page_size": page_size,
                    })
        except DatabaseError as exc:
            _database_error(exc)


class ReconciliationChequesView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, READ_ROLES)
        try:
            page = int(request.query_params.get("page", "1"))
            page_size = min(int(request.query_params.get("page_size", "50")), 100)
        except ValueError as exc:
            raise ValidationError({"page": "La página y su tamaño deben ser números enteros."}) from exc
        if page < 1 or page_size < 1:
            raise ValidationError({"page": "La página y su tamaño deben ser positivos."})
        view = request.query_params.get("vista", "todos")
        age_filter = request.query_params.get("origen_periodo", "todos")
        if age_filter not in ("todos", "periodo", "anteriores"):
            raise ValidationError({"origen_periodo": "El filtro de período no es válido."})
        search = request.query_params.get("busqueda", "").strip()[:100]
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    current = _locked_scope(cursor, request, reconciliation_id, lock=False)
                    _require_role(current, READ_ROLES)
                    results, total = _fetch_cheques(
                        cursor,current,view,search,age_filter,page,page_size
                    )
                    counts = _cheque_counts(cursor, current)
                    return Response({
                        "results": results,
                        "counts": counts,
                        "count_scope": "cuenta_hasta_corte_todos_los_periodos",
                        "count": total,
                        "page": page,
                        "page_size": page_size,
                        "pages": (total + page_size - 1) // page_size,
                    })
        except DatabaseError as exc:
            _database_error(exc)

    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, CLASSIFY_ROLES)
        allowed = {
            "movimiento_emision_id", "estado", "fecha_entrega",
            "confirmacion_entrega", "observacion", "revision_ambiguedad",
            "beneficiario",
        }
        if set(request.data) - allowed:
            raise ValidationError("La solicitud contiene campos no permitidos.")
        movement_id = _uuid(request.data.get("movimiento_emision_id"), "movimiento_emision_id")
        status = request.data.get("estado")
        if status not in ("retenido", "en_circulacion"):
            raise ValidationError({"estado": "El estado inicial debe ser Retenido o En circulación."})
        if status == "en_circulacion":
            effective_date = _parse_date(request.data.get("fecha_entrega"), "fecha_entrega")
            if request.data.get("confirmacion_entrega") is not True:
                raise ValidationError({"confirmacion_entrega": "Confirma que el cheque fue entregado."})
        else:
            effective_date = None
        observation = request.data.get("observacion", "")
        if not isinstance(observation, str) or len(observation) > 1000:
            raise ValidationError({"observacion": "La observación debe ser texto de hasta 1000 caracteres."})
        beneficiary = request.data.get("beneficiario")
        if beneficiary is not None and (
            not isinstance(beneficiary,str) or len(beneficiary.strip())>200
        ):
            raise ValidationError({"beneficiario": "El beneficiario debe ser texto de hasta 200 caracteres."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    current = _locked_scope(cursor, request, reconciliation_id)
                    _require_role(current, CLASSIFY_ROLES)
                    _set_actor(cursor, current)
                    cursor.execute(
                        """
                        SELECT m.id::text,m.numero_cheque,m.fecha,m.descripcion,m.monto::text,
                               m.cuenta_id::text,m.datos_originales,c.conciliacion_id::text,
                               cb.moneda_codigo,r.periodo
                        FROM contared.movimientos m
                        JOIN contared.cargas c ON c.id=m.carga_id
                        JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
                        JOIN contared.cuentas_bancarias cb ON cb.id=m.cuenta_id
                        WHERE m.id=%s AND m.cuenta_id=%s AND m.lado='libros'
                          AND c.origen='libros' AND c.estado='validada'
                          AND c.empresa_id=%s AND c.pais_codigo=%s
                          AND r.periodo<=%s AND m.fecha<=%s AND m.monto<0
                          AND NULLIF(btrim(m.numero_cheque),'') IS NOT NULL
                        FOR UPDATE OF m
                        """,
                        [
                            movement_id,current["account_id"],current["company_id"],
                            current["country_code"],current["period"],current["cutoff"],
                        ],
                    )
                    movement = cursor.fetchone()
                    if movement is None:
                        raise ValidationError("El movimiento no es un candidato válido de cheque para esta cuenta.")
                    if status == "en_circulacion" and (
                        effective_date < movement[2] or effective_date > current["cutoff"]
                    ):
                        raise ValidationError({"fecha_entrega": "La fecha de entrega debe estar entre la emisión y la fecha de corte."})
                    cursor.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM contared.movimientos m
                           JOIN contared.cargas c ON c.id=m.carga_id
                           WHERE m.cuenta_id=%s AND m.lado='libros'
                             AND c.estado='validada' AND m.numero_cheque=%s)
                          + (SELECT count(*) FROM contared.cheques ch
                             WHERE ch.cuenta_id=%s AND ch.movimiento_emision_id IS NULL
                               AND ch.numero_cheque=%s)
                        """,
                        [
                            current["account_id"],movement[1],
                            current["account_id"],movement[1],
                        ],
                    )
                    ambiguous = cursor.fetchone()[0] > 1
                    if ambiguous and request.data.get("revision_ambiguedad") is not True:
                        raise ValidationError({"revision_ambiguedad": "Hay movimientos con este número de cheque repetido. Revisa y confirma el candidato específico."})
                    cursor.execute(
                        "SELECT id::text,estado FROM contared.cheques WHERE movimiento_emision_id=%s FOR UPDATE",
                        [movement_id],
                    )
                    existing = cursor.fetchone()
                    if existing:
                        return Response({"id": existing[0], "estado": existing[1], "repetido": True})
                    cursor.execute(
                        """
                        SELECT id::text,estado FROM contared.cheques
                        WHERE cuenta_id=%s AND movimiento_emision_id IS NULL
                          AND numero_cheque=%s AND fecha_emision=%s
                          AND monto=abs(%s::numeric)
                        FOR UPDATE
                        """,
                        [current["account_id"],movement[1],movement[2],movement[4]],
                    )
                    standalone = cursor.fetchone()
                    if standalone:
                        cursor.execute(
                            """UPDATE contared.cheques
                               SET movimiento_emision_id=%s,
                                   conciliacion_origen_id=%s,periodo_origen=%s,
                                   actualizado_en=now()
                               WHERE id=%s AND movimiento_emision_id IS NULL""",
                            [movement_id,movement[7],movement[9],standalone[0]],
                        )
                        return Response({
                            "id":standalone[0],"estado":standalone[1],
                            "repetido":True,"asociado_a_movimiento":True,
                        })
                    original = _normalize_original_data(movement[6]) or {}
                    source_beneficiary = None
                    if isinstance(original, dict):
                        source_beneficiary = original.get("beneficiario") or original.get("beneficiary")
                    cheque_beneficiary = (
                        beneficiary.strip() if isinstance(beneficiary,str) and beneficiary.strip()
                        else source_beneficiary.strip()
                        if isinstance(source_beneficiary,str) and source_beneficiary.strip()
                        else None
                    )
                    cheque_id = str(uuid4())
                    cursor.execute(
                        """
                        INSERT INTO contared.cheques
                          (id,cuenta_id,conciliacion_origen_id,movimiento_emision_id,periodo_origen,
                           numero_cheque,moneda_codigo,fecha_emision,beneficiario,monto,
                           estado,fecha_entrega,entregado_por,observacion_entrega,
                           creado_por)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        [
                            cheque_id,current["account_id"],movement[7],movement_id,movement[9],
                            movement[1],movement[8],movement[2],
                            cheque_beneficiary,
                            abs(Decimal(movement[4])),status,effective_date,
                            current["user_id"] if effective_date else None,
                            observation.strip() or None,current["user_id"],
                        ],
                    )
                    cursor.execute(
                        """
                        INSERT INTO contared.cheque_eventos
                          (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,
                           registrado_por,motivo,observacion)
                        VALUES (%s,NULL,%s,%s,%s,%s,%s)
                        """,
                        [
                            cheque_id,status,effective_date or movement[2],current["user_id"],
                            "Clasificación inicial desde movimiento explícito de cheque",
                            observation.strip() or None,
                        ],
                    )
                    _reflect_existing_cheque_cross(cursor,current,movement_id)
                    cursor.execute(
                        "SELECT estado FROM contared.cheques WHERE id=%s",
                        [cheque_id],
                    )
                    resolved_status = cursor.fetchone()[0]
                    return Response({
                        "id":cheque_id,"estado":resolved_status,"repetido":False,
                    },status=201)
        except IntegrityError as exc:
            raise ValidationError("El movimiento ya fue registrado como cheque.") from exc
        except DatabaseError as exc:
            _database_error(exc)


class ManualChequeCreateView(APIView):
    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, CLASSIFY_ROLES)
        allowed = {
            "numero_cheque","fecha_emision","beneficiario","descripcion","monto",
            "referencia","estado","fecha_entrega","confirmacion_entrega",
            "observacion","revision_duplicidad","registro_independiente",
        }
        if set(request.data)-allowed:
            raise ValidationError("La solicitud contiene campos no permitidos.")
        independent = request.data.get("registro_independiente", False)
        if not isinstance(independent, bool):
            raise ValidationError({"registro_independiente":"Debe ser verdadero o falso."})
        number = request.data.get("numero_cheque")
        if not isinstance(number,str) or not number.strip() or len(number.strip())>100:
            raise ValidationError({"numero_cheque":"Indica el número de cheque como texto."})
        number = number.strip()
        issue_date = _parse_date(request.data.get("fecha_emision"),"fecha_emision")
        try:
            amount = Decimal(str(request.data.get("monto")))
            if (
                not amount.is_finite() or amount<=0
                or amount>=Decimal("10000000000000000")
                or amount!=amount.quantize(Decimal("0.0001"))
            ):
                raise InvalidOperation
        except (InvalidOperation,ValueError,TypeError):
            raise ValidationError({"monto":"Indica un importe positivo con hasta cuatro decimales."})
        description = request.data.get("descripcion")
        if not isinstance(description,str) or not description.strip() or len(description.strip())>500:
            raise ValidationError({"descripcion":"El concepto es obligatorio y admite hasta 500 caracteres."})
        beneficiary = request.data.get("beneficiario","")
        reference = request.data.get("referencia","")
        observation = request.data.get("observacion","")
        for field,value,max_length in (
            ("beneficiario",beneficiary,200),("referencia",reference,200),
            ("observacion",observation,1000),
        ):
            if value is None:
                continue
            if not isinstance(value,str) or len(value.strip())>max_length:
                raise ValidationError({field:f"El campo admite texto de hasta {max_length} caracteres."})
        beneficiary = beneficiary.strip() if isinstance(beneficiary,str) else None
        reference = reference.strip() if isinstance(reference,str) else None
        observation = observation.strip() if isinstance(observation,str) else None
        status = request.data.get("estado")
        if status not in ("retenido","en_circulacion"):
            raise ValidationError({"estado":"El estado inicial debe ser Retenido o En circulación."})
        delivery_date = None
        if status=="en_circulacion":
            delivery_date = _parse_date(request.data.get("fecha_entrega"),"fecha_entrega")
            if request.data.get("confirmacion_entrega") is not True:
                raise ValidationError({"confirmacion_entrega":"Confirma que el cheque fue entregado."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    current = _locked_scope(cursor,request,reconciliation_id)
                    _require_role(current,CLASSIFY_ROLES)
                    if current["state"]!="borrador":
                        raise ValidationError({"estado":"Solo se pueden registrar cheques manuales en una conciliación borrador."})
                    if (
                        issue_date.year!=current["period"].year
                        or issue_date.month!=current["period"].month
                        or issue_date>current["cutoff"]
                    ):
                        raise ValidationError({
                            "fecha_emision":"La fecha debe pertenecer al período de la conciliación y no superar su corte."
                        })
                    if delivery_date and (
                        delivery_date<issue_date or delivery_date>current["cutoff"]
                    ):
                        raise ValidationError({
                            "fecha_entrega":"La fecha debe estar entre la emisión y la fecha de corte."
                        })
                    _set_actor(cursor,current)
                    cursor.execute(
                        "SELECT id FROM contared.cuentas_bancarias WHERE id=%s FOR UPDATE",
                        [current["account_id"]],
                    )
                    related = _related_issuances(cursor,current,number)
                    duplicates = [
                        item for item in related
                        if item["fecha_emision"]==issue_date.isoformat()
                        and Decimal(item["monto"])==amount
                    ]
                    if duplicates:
                        raise ValidationError({
                            "duplicado":"Ya existe una emisión con el mismo número, fecha e importe en esta cuenta; revisa los movimientos relacionados.",
                            "registros_relacionados":duplicates,
                        })
                    if related and request.data.get("revision_duplicidad") is not True:
                        raise ValidationError({
                            "revision_duplicidad":"Hay movimientos o cheques con este número en la cuenta. Revísalos antes de continuar.",
                            "registros_relacionados":related,
                        })
                    load_id = movement_id = None
                    if not independent:
                        load_id, movement_id = str(uuid4()), str(uuid4())
                        original = {
                            "captura_manual_cheque":True,
                            "numero_cheque":number,
                            "fecha_emision":issue_date.isoformat(),
                            "beneficiario":beneficiary,
                            "descripcion":description.strip(),
                            "referencia":reference,
                            "moneda_codigo":current["currency_code"],
                            "monto":str(amount),
                        }
                        cursor.execute(
                            """
                            INSERT INTO contared.cargas
                              (id,cuenta_id,empresa_id,pais_codigo,conciliacion_id,
                               origen,metodo,nombre_archivo,estado,creada_por)
                            VALUES (%s,%s,%s,%s,%s,'libros','manual',%s,'validada',%s)
                            """,
                            [
                                load_id,current["account_id"],current["company_id"],
                                current["country_code"],current["reconciliation_id"],
                                f"Cheque manual {number}",current["user_id"],
                            ],
                        )
                        cursor.execute(
                            """
                            INSERT INTO contared.movimientos
                              (id,cuenta_id,carga_id,lado,fecha,referencia,numero_cheque,
                               descripcion,monto,hoja_origen,fila_origen,datos_originales,creado_por)
                            VALUES (%s,%s,%s,'libros',%s,%s,%s,%s,%s,'MANUAL',1,%s::jsonb,%s)
                            """,
                            [
                                movement_id,current["account_id"],load_id,issue_date,
                                reference,number,description.strip(),-amount,
                                json.dumps(original,ensure_ascii=False),current["user_id"],
                            ],
                        )
                    cheque_id = str(uuid4())
                    cursor.execute(
                        """
                        INSERT INTO contared.cheques
                          (id,cuenta_id,conciliacion_origen_id,movimiento_emision_id,periodo_origen,
                           numero_cheque,moneda_codigo,fecha_emision,descripcion,referencia,
                           beneficiario,monto,
                           estado,fecha_entrega,entregado_por,observacion_entrega,creado_por)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        [
                            cheque_id,current["account_id"],current["reconciliation_id"],
                            movement_id,current["period"],number,current["currency_code"],issue_date,
                            description.strip(),reference,beneficiary,amount,status,delivery_date,
                            current["user_id"] if delivery_date else None,
                            observation,current["user_id"],
                        ],
                    )
                    cursor.execute(
                        """
                        INSERT INTO contared.cheque_eventos
                          (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,
                           registrado_por,motivo,observacion)
                        VALUES (%s,NULL,%s,%s,%s,%s,%s)
                        """,
                        [
                            cheque_id,status,delivery_date or issue_date,current["user_id"],
                            "Registro manual de cheque en Libros",observation,
                        ],
                    )
                    return Response({
                        "id":cheque_id,"movimiento_emision_id":movement_id,
                        "carga_id":load_id,"estado":status,"repetido":False,
                    },status=201)
        except IntegrityError as exc:
            raise ValidationError("La emisión ya existe; revisa los movimientos antes de reintentar.") from exc
        except DatabaseError as exc:
            _database_error(exc)


def _uuid(value, field):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({field: "Indica un identificador válido."}) from exc


def _locked_cheque(cursor, scope, cheque_id, *, lock=True):
    lock_clause = "FOR UPDATE" if lock else ""
    cursor.execute(
        f"""
        SELECT ch.id::text,ch.estado,ch.cuenta_id::text,ch.movimiento_emision_id::text,
               ch.numero_cheque,ch.fecha_emision,ch.monto::text,ch.moneda_codigo,
               ch.conciliacion_origen_id::text,ch.movimiento_cobro_id::text,
               ch.conciliacion_resolucion_id::text,ch.fecha_entrega,
               ch.motivo_anulacion,ch.soporte_anulacion_ruta
        FROM contared.cheques ch
        WHERE ch.id=%s AND ch.cuenta_id=%s
        {lock_clause}
        """,
        [cheque_id, scope["account_id"]],
    )
    row = cursor.fetchone()
    if row is None:
        raise NotFound("El cheque no existe en esta cuenta.")
    return row


class ChequeActionView(APIView):
    parsers = (JSONParser,)
    action = ""

    def post(self, request, reconciliation_id, cheque_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        roles = CLASSIFY_ROLES if self.action == "entrega" else REVIEW_ROLES
        _require_role(scope, roles)
        allowed = {
            "entrega": {"fecha_entrega", "observacion"},
            "cobrar": {"movimiento_banco_id", "revision_ambiguedad"},
        }[self.action]
        if set(request.data) - allowed:
            raise ValidationError("La solicitud contiene campos no permitidos.")
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    current = _locked_scope(cursor, request, reconciliation_id)
                    _require_role(current, roles)
                    _set_actor(cursor, current)
                    cheque = _locked_cheque(cursor, current, cheque_id)
                    if self.action == "entrega":
                        return self._deliver(cursor, current, cheque, request.data)
                    return self._cash(cursor, current, cheque, request.data)
        except IntegrityError as exc:
            raise ValidationError("El movimiento bancario ya fue utilizado por otro cheque o cruce.") from exc
        except DatabaseError as exc:
            _database_error(exc)

    def _deliver(self, cursor, scope, cheque, data):
        delivery_date = _parse_date(data.get("fecha_entrega"), "fecha_entrega")
        if cheque[1] == "en_circulacion":
            if delivery_date != cheque[11]:
                raise ValidationError(
                    {"fecha_entrega": "El cheque ya tiene una fecha de entrega registrada."}
                )
            return Response({"id": cheque[0], "estado": cheque[1], "repetido": True})
        if cheque[1] != "retenido":
            raise ValidationError("Solo un cheque Retenido puede registrarse En circulación.")
        if delivery_date < cheque[5] or delivery_date > scope["cutoff"]:
            raise ValidationError({"fecha_entrega": "La fecha debe estar entre la emisión y el corte actual."})
        note = data.get("observacion", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValidationError({"observacion": "La observación debe ser texto de hasta 1000 caracteres."})
        cursor.execute(
            """
            UPDATE contared.cheques
            SET estado='en_circulacion',fecha_entrega=%s,entregado_por=%s,
                observacion_entrega=%s,actualizado_en=now()
            WHERE id=%s AND estado='retenido'
            """,
            [delivery_date,scope["user_id"],note.strip() or None,cheque[0]],
        )
        if cursor.rowcount != 1:
            raise ValidationError("El cheque cambió mientras se registraba la entrega.")
        cursor.execute(
            """
            INSERT INTO contared.cheque_eventos
              (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,
               registrado_por,motivo,observacion)
            VALUES (%s,'retenido','en_circulacion',%s,%s,'Entrega registrada',%s)
            """,
            [cheque[0],delivery_date,scope["user_id"],note.strip() or None],
        )
        return Response({"id": cheque[0], "estado": "en_circulacion", "repetido": False})

    def _cash(self, cursor, scope, cheque, data):
        bank_id = _uuid(data.get("movimiento_banco_id"), "movimiento_banco_id")
        if cheque[1] == "cobrado":
            if bank_id != cheque[9]:
                raise ValidationError(
                    "El cheque ya fue confirmado contra otro movimiento bancario."
                )
            return Response({
                "id": cheque[0], "estado": "cobrado", "repetido": True,
                "movimiento_cobro_id": cheque[9],
            })
        if cheque[1] == "anulado":
            raise ValidationError("Un cheque anulado no se puede registrar como cobrado.")
        cursor.execute(
            f"""
            SELECT b.id::text,{_BANK_CHEQUE_NUMBER_SQL},b.fecha,b.monto::text,
                   c.conciliacion_id::text,c.origen,c.estado,p.configuracion,
                   b.referencia,b.descripcion,b.datos_originales
            FROM contared.movimientos b
            JOIN contared.cargas c ON c.id=b.carga_id
            LEFT JOIN contared.perfiles_importacion p ON p.id=c.perfil_id
            JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
            JOIN contared.cuentas_bancarias cb ON cb.id=b.cuenta_id
            JOIN contared.bancos bank ON bank.id=cb.banco_id
            WHERE b.id=%s AND b.cuenta_id=%s AND b.lado='banco'
              AND c.origen='banco' AND c.estado='validada'
              AND c.conciliacion_id=%s AND c.empresa_id=%s AND c.pais_codigo=%s
              AND r.fecha_corte=%s AND b.fecha<=r.fecha_corte
            FOR UPDATE OF b,c
            """,
            [
                bank_id,scope["account_id"],scope["reconciliation_id"],
                scope["company_id"],scope["country_code"],scope["cutoff"],
            ],
        )
        bank = cursor.fetchone()
        if bank is None:
            raise ValidationError("Selecciona un movimiento bancario validado de esta conciliación.")
        bank_amount = Decimal(bank[3])
        if bank[1] != cheque[4] or bank_amount >= 0 or abs(bank_amount) != Decimal(cheque[6]):
            raise ValidationError("El movimiento debe ser una salida bancaria de importe exacto y con el mismo número de cheque.")
        if bank[2] < cheque[5]:
            raise ValidationError("La fecha del cobro no puede ser anterior a la emisión.")
        cursor.execute(
            f"""
            SELECT count(*) FROM contared.movimientos b
            JOIN contared.cargas c ON c.id=b.carga_id
            JOIN contared.cuentas_bancarias cb ON cb.id=b.cuenta_id
            JOIN contared.bancos bank ON bank.id=cb.banco_id
            WHERE b.cuenta_id=%s AND b.lado='banco' AND c.estado='validada'
              AND c.conciliacion_id=%s AND {_BANK_CHEQUE_NUMBER_SQL}=%s
              AND b.monto<0 AND abs(b.monto)=%s AND b.fecha>=%s
            """,
            [scope["account_id"],scope["reconciliation_id"],cheque[4],cheque[6],cheque[5]],
        )
        ambiguous = cursor.fetchone()[0] > 1
        if ambiguous and data.get("revision_ambiguedad") is not True:
            raise ValidationError({"revision_ambiguedad": "Hay varios movimientos bancarios candidatos. Revisa y confirma el movimiento seleccionado."})

        cursor.execute(
            """
            SELECT cm.cruce_id::text,x.estado,x.conciliacion_id::text,x.tipo,
                   (SELECT count(*) FROM contared.cruce_movimientos members
                    WHERE members.cruce_id=x.id)
            FROM contared.cruce_movimientos cm
            JOIN contared.cruces x ON x.id=cm.cruce_id
            WHERE cm.movimiento_id=ANY(%s::uuid[])
            FOR UPDATE OF x
            """,
            [[bank_id] if cheque[3] is None else [cheque[3],bank_id]],
        )
        existing_crosses = cursor.fetchall()
        cross_id = None
        if existing_crosses:
            if cheque[3] is None:
                raise ValidationError("El movimiento bancario ya participa en un cruce; no se puede asociar también a un cheque independiente.")
            if (
                len(existing_crosses) != 2
                or existing_crosses[0][0] != existing_crosses[1][0]
                or existing_crosses[0][1] != "confirmado"
                or existing_crosses[0][2] != scope["reconciliation_id"]
                or existing_crosses[0][3] != "coincidencia"
                or existing_crosses[0][4] != 2
            ):
                raise ValidationError("Uno de los movimientos ya participa en otro cruce; revisa ese cruce antes de cobrar el cheque.")
            cross_id = existing_crosses[0][0]
        elif cheque[3] is not None:
            cross_id = str(uuid4())
            cursor.execute(
                """
                INSERT INTO contared.cruces
                  (id,cuenta_id,conciliacion_id,tipo,estado,criterio,regla_codigo,
                   observacion,creado_por,confirmado_por,confirmado_en)
                VALUES (%s,%s,%s,'coincidencia','confirmado',
                        'Confirmación manual del cobro de cheque',
                        'check_number_amount',%s,%s,%s,now())
                """,
                [
                    cross_id,scope["account_id"],scope["reconciliation_id"],
                    f"Cheque {cheque[4]} confirmado contra movimiento bancario {bank_id}.",
                    scope["user_id"],scope["user_id"],
                ],
            )
            for movement_id in (cheque[3],bank_id):
                cursor.execute(
                    """
                    INSERT INTO contared.cruce_movimientos(cruce_id,cuenta_id,movimiento_id)
                    VALUES (%s,%s,%s)
                    """,
                    [cross_id,scope["account_id"],movement_id],
                )
        previous_status = cheque[1]
        cursor.execute(
            """
            UPDATE contared.cheques
            SET estado='cobrado',movimiento_cobro_id=%s,
                conciliacion_resolucion_id=%s,actualizado_en=now()
            WHERE id=%s AND estado IN ('retenido','en_circulacion')
            """,
            [bank_id,scope["reconciliation_id"],cheque[0]],
        )
        if cursor.rowcount != 1:
            raise ValidationError("El cheque cambió mientras se confirmaba su cobro.")
        inconsistency = previous_status == "retenido"
        reason = (
            "Cobro bancario comprobado mientras el cheque figuraba Retenido; "
            "la entrega no se infiere."
            if inconsistency else "Cobro confirmado contra movimiento bancario."
        )
        cursor.execute(
            """
            INSERT INTO contared.cheque_eventos
              (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,
               registrado_por,motivo,observacion,movimiento_banco_id,conciliacion_id,cruce_id)
            VALUES (%s,%s,'cobrado',%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                cheque[0],previous_status,bank[2],scope["user_id"],reason,
                reason if inconsistency else None,bank_id,scope["reconciliation_id"],cross_id,
            ],
        )
        return Response({
            "id": cheque[0], "estado": "cobrado", "cruce_id": cross_id,
            "movimiento_cobro_id": bank_id, "inconsistencia_retenido": inconsistency,
            "repetido": False,
        })


class ChequeDeliveryView(ChequeActionView):
    action = "entrega"


class ChequeCashView(ChequeActionView):
    action = "cobrar"


class ChequeCandidatesView(APIView):
    def get(self, request, reconciliation_id, cheque_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, READ_ROLES)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    current = _locked_scope(cursor, request, reconciliation_id, lock=False)
                    cheque = _locked_cheque(cursor, current, cheque_id, lock=False)
                    cursor.execute(
                        f"""
                        SELECT b.id::text,b.fecha,{_BANK_CHEQUE_NUMBER_SQL},b.referencia,
                               b.numero_transaccion,b.descripcion,b.monto::text,
                               b.codigo_tt,c.conciliacion_id::text,r.periodo,
                               (SELECT cm.cruce_id::text FROM contared.cruce_movimientos cm
                                JOIN contared.cruces x ON x.id=cm.cruce_id
                                WHERE cm.movimiento_id=b.id AND x.estado='confirmado'
                                LIMIT 1),
                               count(*) OVER() AS total
                        FROM contared.movimientos b
                        JOIN contared.cargas c ON c.id=b.carga_id
                        JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
                        JOIN contared.cuentas_bancarias cb ON cb.id=b.cuenta_id
                        JOIN contared.bancos bank ON bank.id=cb.banco_id
                        WHERE b.cuenta_id=%s AND b.lado='banco'
                          AND c.origen='banco' AND c.estado='validada'
                          AND c.conciliacion_id=%s AND b.fecha>=%s
                          AND b.fecha<=%s AND {_BANK_CHEQUE_NUMBER_SQL}=%s
                          AND b.monto<0 AND abs(b.monto)=%s
                        ORDER BY b.fecha,b.id
                        """,
                        [
                            current["account_id"],current["reconciliation_id"],
                            cheque[5],current["cutoff"],cheque[4],cheque[6],
                        ],
                    )
                    rows = cursor.fetchall()
                    return Response({
                        "results": [
                            {
                                "id": row[0], "fecha": row[1].isoformat(),
                                "numero_cheque": row[2], "referencia": row[3],
                                "numero_transaccion": row[4], "descripcion": row[5],
                                "monto": row[6], "codigo_tt": row[7],
                                "conciliacion_id": row[8],
                                "periodo": row[9].isoformat(),
                                "cruce_id": row[10],
                            }
                            for row in rows
                        ],
                        "ambiguo": bool(rows and rows[0][11] > 1),
                    })
        except DatabaseError as exc:
            _database_error(exc)


class ChequeAnnulmentView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, reconciliation_id, cheque_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, REVIEW_ROLES)
        if set(request.data) - {"motivo", "fecha_anulacion", "soporte"}:
            raise ValidationError("La solicitud contiene campos no permitidos.")
        reason = request.data.get("motivo")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValidationError({"motivo": "El motivo es obligatorio y admite hasta 1000 caracteres."})
        effective_date = _parse_date(request.data.get("fecha_anulacion"), "fecha_anulacion")
        support = request.FILES.get("soporte")
        if support is None:
            raise ValidationError({"soporte": "Adjunta soporte para la anulación."})
        if support.size > SUPPORT_MAX_BYTES:
            raise ValidationError({"soporte": "El soporte no puede superar 10 MiB."})
        extension = support.name.rsplit(".", 1)[-1].lower() if "." in support.name else ""
        content_types = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
        if extension not in content_types:
            raise ValidationError({"soporte": "Usa un soporte PDF, PNG o JPEG."})
        content_type = content_types[extension]
        if support.content_type not in (content_type, "application/octet-stream"):
            raise ValidationError({"soporte": "El tipo de archivo no coincide con su extensión."})
        cheque_uuid = _uuid(cheque_id, "cheque_id")
        object_path = (
            f"cheques/{scope['company_id']}/{scope['account_id']}/"
            f"{cheque_uuid}/{uuid4()}.{extension}"
        )
        uploaded = False
        try:
            _storage_request("POST", object_path, body=b"".join(support.chunks()), content_type=content_type)
            uploaded = True
            with transaction.atomic():
                with connection.cursor() as cursor:
                    current = _locked_scope(cursor, request, reconciliation_id)
                    _require_role(current, REVIEW_ROLES)
                    _set_actor(cursor, current)
                    cheque = _locked_cheque(cursor, current, cheque_uuid)
                    if cheque[1] == "cobrado":
                        raise ValidationError("Un cheque cobrado no se puede anular.")
                    if cheque[1] == "anulado":
                        return Response({"id": cheque[0], "estado": cheque[1], "repetido": True})
                    if effective_date < cheque[5] or effective_date > current["cutoff"]:
                        raise ValidationError({"fecha_anulacion": "La fecha debe estar entre la emisión y la fecha de corte."})
                    cursor.execute(
                        """
                        UPDATE contared.cheques
                        SET estado='anulado',fecha_anulacion=%s,motivo_anulacion=%s,
                            soporte_anulacion_ruta=%s,actualizado_en=now()
                        WHERE id=%s AND estado IN ('retenido','en_circulacion')
                        """,
                        [effective_date,reason.strip(),object_path,cheque[0]],
                    )
                    if cursor.rowcount != 1:
                        raise ValidationError("El cheque cambió mientras se registraba la anulación.")
                    cursor.execute(
                        """
                        INSERT INTO contared.cheque_eventos
                          (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,
                           registrado_por,motivo,soporte_ruta)
                        VALUES (%s,%s,'anulado',%s,%s,%s,%s)
                        """,
                        [
                            cheque[0],cheque[1],effective_date,current["user_id"],
                            reason.strip(),object_path,
                        ],
                    )
            uploaded = False
            return Response({
                "id": cheque_uuid,"estado":"anulado","ajuste_contable":"pendiente",
                "repetido":False,
            })
        except DatabaseError as exc:
            _database_error(exc)
        finally:
            if uploaded:
                _delete_storage_object(object_path)


class ChequeHistoryView(APIView):
    def get(self, request, reconciliation_id, cheque_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, READ_ROLES)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    current = _locked_scope(cursor, request, reconciliation_id, lock=False)
                    cheque = _locked_cheque(cursor, current, cheque_id, lock=False)
                    cursor.execute(
                        """
                        SELECT ev.id::text,ev.estado_anterior,ev.estado_nuevo,
                               ev.fecha_efectiva,ev.registrado_en,u.nombre,
                               ev.motivo,ev.observacion,ev.movimiento_banco_id::text,
                               ev.conciliacion_id::text,ev.cruce_id::text,ev.soporte_ruta
                        FROM contared.cheque_eventos ev
                        JOIN contared.usuarios u ON u.id=ev.registrado_por
                        WHERE ev.cheque_id=%s
                        ORDER BY ev.fecha_efectiva,ev.registrado_en,ev.id
                        """,
                        [cheque[0]],
                    )
                    events = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT ch.movimiento_emision_id::text,ch.movimiento_cobro_id::text,
                               ch.conciliacion_origen_id::text,ch.conciliacion_resolucion_id::text
                        FROM contared.cheques ch WHERE ch.id=%s
                        """,
                        [cheque[0]],
                    )
                    links = cursor.fetchone()
                    return Response({
                        "cheque_id": cheque[0],
                        "movimiento_emision_id": links[0],
                        "movimiento_cobro_id": links[1],
                        "conciliacion_origen_id": links[2],
                        "conciliacion_resolucion_id": links[3],
                        "events": [
                            {
                                "id": row[0],"estado_anterior":row[1],
                                "estado_nuevo":row[2],"fecha_efectiva":row[3].isoformat(),
                                "registrado_en":row[4].isoformat(),"usuario":row[5],
                                "motivo":row[6],"observacion":row[7],
                                "movimiento_banco_id":row[8],"conciliacion_id":row[9],
                                "cruce_id":row[10],"tiene_soporte":bool(row[11]),
                                "soporte_url":(
                                    f"/api/v1/conciliaciones/{reconciliation_id}/cheques/"
                                    f"{cheque[0]}/soportes/{row[0]}/"
                                    if row[11] else None
                                ),
                            }
                            for row in events
                        ],
                    })
        except DatabaseError as exc:
            _database_error(exc)


class ChequeSupportView(APIView):
    def get(self, request, reconciliation_id, cheque_id, event_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, READ_ROLES)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    current = _locked_scope(cursor, request, reconciliation_id, lock=False)
                    cheque = _locked_cheque(cursor, current, cheque_id, lock=False)
                    cursor.execute(
                        """
                        SELECT soporte_ruta FROM contared.cheque_eventos
                        WHERE id=%s AND cheque_id=%s AND soporte_ruta IS NOT NULL
                        """,
                        [event_id,cheque[0]],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise NotFound("El soporte no existe.")
                    object_path = row[0]
            key = getattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
            base_url = getattr(settings, "SUPABASE_URL", "")
            if not key or not base_url:
                raise ImportStorageUnavailable()
            url = f"{base_url}/storage/v1/object/contared-documentos/{quote(object_path, safe='/')}"
            http_request = Request(
                url,headers={"apikey":key,"Authorization":f"Bearer {key}"},
            )
            with urlopen(http_request, timeout=30) as response:
                body = response.read(SUPPORT_MAX_BYTES + 1)
                content_type = response.headers.get("Content-Type","application/octet-stream")
            if len(body) > SUPPORT_MAX_BYTES:
                raise ImportStorageUnavailable()
            return HttpResponse(body, content_type=content_type, headers={
                "Content-Disposition": "attachment; filename=\"soporte-cheque\"",
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            })
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ImportStorageUnavailable() from exc
        except DatabaseError as exc:
            _database_error(exc)


class ChequeImportTemplateView(APIView):
    def get(self, request, account_id):
        scope = _scope_for_account(request, account_id)
        _require_role(scope, READ_ROLES)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Cheques"
        sheet.append([
            "numero_cheque", "fecha_emision", "beneficiario", "monto",
            "estado_al_corte", "fecha_entrega", "descripcion", "referencia",
        ])
        instructions = workbook.create_sheet("Instrucciones")
        instructions.append(["Campo", "Uso"])
        instructions.append(["numero_cheque", "Obligatorio; conserva como texto los ceros iniciales."])
        instructions.append(["fecha_emision", "Obligatoria; fecha Excel o YYYY-MM-DD."])
        instructions.append(["beneficiario", "Beneficiario indicado en el documento."])
        instructions.append(["monto", "Importe positivo con hasta cuatro decimales."])
        instructions.append(["estado_al_corte", "Obligatorio: Retenido o En circulación."])
        instructions.append(["fecha_entrega", "Obligatoria si estado_al_corte es En circulación."])
        instructions.append(["descripcion / referencia", "Opcionales; detalle de origen del documento."])
        instructions.append(["Importación", "El archivo se conserva por versión; no crea movimientos en Libros."])
        instructions.append(["Cobrado o anulado", "No se importan como pendientes sin vincular su evidencia contable y bancaria."])
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        return HttpResponse(
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": 'attachment; filename="plantilla-cheques-independientes.xlsx"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


class ChequeImportsView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, account_id):
        scope = _scope_for_account(request, account_id)
        _require_role(scope, READ_ROLES)
        try:
            page = int(request.query_params.get("page", "1"))
            page_size = min(int(request.query_params.get("page_size", "25")), 100)
        except ValueError as exc:
            raise ValidationError({"page": "La página y su tamaño deben ser números enteros."}) from exc
        if page < 1 or page_size < 1:
            raise ValidationError({"page": "La página y su tamaño deben ser positivos."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        "SELECT count(*) FROM contared.cheque_importaciones WHERE cuenta_id=%s",
                        [account_id],
                    )
                    total = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        SELECT i.id::text,i.tipo_documento,i.fecha_corte,i.version,
                               i.nombre_archivo,i.sha256,i.perfil,i.registros_total,
                               i.creado_en,u.nombre
                        FROM contared.cheque_importaciones i
                        JOIN contared.usuarios u ON u.id=i.creado_por
                        WHERE i.cuenta_id=%s
                        ORDER BY i.creado_en DESC,i.version DESC
                        LIMIT %s OFFSET %s
                        """,
                        [account_id, page_size, (page - 1) * page_size],
                    )
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            _database_error(exc)
        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": [
                {
                    "id": row[0],
                    "tipo_documento": row[1],
                    "fecha_corte": row[2].isoformat(),
                    "version": row[3],
                    "nombre_archivo": row[4],
                    "sha256": row[5],
                    "perfil": row[6],
                    "registros_total": row[7],
                    "creado_en": row[8].isoformat(),
                    "creado_por": row[9],
                }
                for row in rows
            ],
        })

    def post(self, request, account_id):
        scope = _scope_for_account(request, account_id)
        _require_role(scope, CLASSIFY_ROLES)
        if set(request.data) - {"file", "tipo_documento", "fecha_corte", "perfil"}:
            raise ValidationError("La solicitud contiene campos no permitidos.")
        document_type = request.data.get("tipo_documento")
        if document_type not in ("registro_completo", "pendientes_al_corte"):
            raise ValidationError({"tipo_documento": "Elige registro completo o pendientes al corte."})
        cutoff = _parse_date(request.data.get("fecha_corte"), "fecha_corte")
        profile = _cheque_import_profile(request.data.get("perfil"))
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Adjunta el documento Excel de cheques."})
        original_name = Path(str(upload.name).replace("\\", "/")).name[:255]
        extension = Path(original_name).suffix.lower()
        if extension not in (".xlsx", ".xls"):
            raise ValidationError({"file": "Solo se admiten libros .xlsx o .xls."})
        if upload.size > MAX_UPLOAD_BYTES:
            raise ValidationError({"file": "El archivo supera el límite de 10 MiB."})

        with tempfile.TemporaryDirectory(prefix="contared-cheque-import-") as directory:
            original_path = Path(directory) / f"original{extension}"
            digest = hashlib.sha256()
            total_bytes = 0
            try:
                with original_path.open("xb") as destination:
                    for chunk in upload.chunks():
                        total_bytes += len(chunk)
                        if total_bytes > MAX_UPLOAD_BYTES:
                            raise ValidationError({"file": "El archivo supera el límite de 10 MiB."})
                        digest.update(chunk)
                        destination.write(chunk)
            except OSError as exc:
                raise ValidationError({"file": "No se pudo guardar temporalmente el archivo."}) from exc
            file_hash = digest.hexdigest()
            detected_format = _file_format(original_path)
            workbook_path = original_path
            if detected_format == "xls":
                workbook_path = Path(directory) / "normalized.xlsx"
                _normalize_xls(original_path, workbook_path)
            _check_archive(workbook_path)
            parsed, warnings = _parse_cheque_workbook(workbook_path, profile, cutoff)
            profile_json = json.dumps(profile, ensure_ascii=False)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT cb.moneda_codigo,cb.activa,b.activo
                        FROM contared.cuentas_bancarias cb
                        JOIN contared.bancos b ON b.id=cb.banco_id
                        WHERE cb.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s
                        FOR UPDATE OF cb
                        """,
                        [account_id, scope["company_id"], scope["country_code"]],
                    )
                    account = cursor.fetchone()
                    if account is None:
                        raise NotFound("La cuenta no pertenece al ámbito autorizado.")
                    if not account[1] or not account[2]:
                        raise ValidationError({"cuenta_id": "La cuenta o el banco están inactivos."})
                    cursor.execute(
                        """
                        SELECT id::text,version,registros_total
                        FROM contared.cheque_importaciones
                        WHERE cuenta_id=%s AND tipo_documento=%s AND sha256=%s
                        """,
                        [account_id, document_type, file_hash],
                    )
                    duplicate_import = cursor.fetchone()
                    if duplicate_import:
                        return Response({
                            "id": duplicate_import[0],
                            "version": duplicate_import[1],
                            "registros_total": duplicate_import[2],
                            "repetido": True,
                            "mensaje": "Este mismo archivo ya fue importado; no se duplicaron cheques.",
                            "advertencias": warnings,
                        })
            object_path = (
                f"cheques/{scope['company_id']}/{account_id}/"
                f"{uuid4()}-{file_hash}{extension}"
            )
            content_type = (
                "application/vnd.ms-excel" if extension == ".xls"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            try:
                body = original_path.read_bytes()
                _storage_request(
                    "POST",
                    object_path,
                    body=body,
                    content_type=content_type,
                )
            except OSError as exc:
                raise ValidationError({"file": "No se pudo leer el archivo temporal."}) from exc
            except ImportStorageUnavailable:
                _delete_storage_object(object_path)
                raise
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT cb.moneda_codigo,cb.activa,b.activo
                            FROM contared.cuentas_bancarias cb
                            JOIN contared.bancos b ON b.id=cb.banco_id
                            WHERE cb.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s
                            FOR UPDATE OF cb
                            """,
                            [account_id, scope["company_id"], scope["country_code"]],
                        )
                        account = cursor.fetchone()
                        if account is None:
                            raise NotFound("La cuenta no pertenece al ámbito autorizado.")
                        if not account[1] or not account[2]:
                            raise ValidationError({"cuenta_id": "La cuenta o el banco están inactivos."})
                        cursor.execute(
                            """
                            SELECT id::text,version,registros_total
                            FROM contared.cheque_importaciones
                            WHERE cuenta_id=%s AND tipo_documento=%s AND sha256=%s
                            """,
                            [account_id, document_type, file_hash],
                        )
                        duplicate_import = cursor.fetchone()
                        if duplicate_import:
                            _delete_storage_object(object_path)
                            return Response({
                                "id": duplicate_import[0],
                                "version": duplicate_import[1],
                                "registros_total": duplicate_import[2],
                                "repetido": True,
                                "mensaje": "Este mismo archivo ya fue importado; no se duplicaron cheques.",
                                "advertencias": warnings,
                            })
                        planned = []
                        conflicts = []
                        for item in parsed:
                            cursor.execute(
                                """
                                SELECT ch.id::text,ch.estado,ch.movimiento_emision_id::text,
                                       ch.monto::text,ch.conciliacion_origen_id::text,
                                       r.archivada_en,ch.fecha_entrega
                                FROM contared.cheques ch
                                LEFT JOIN contared.conciliaciones r
                                  ON r.id=ch.conciliacion_origen_id
                                WHERE ch.cuenta_id=%s AND ch.clave_fuente=%s
                                FOR UPDATE OF ch
                                """,
                                [account_id, item["clave_fuente"]],
                            )
                            matches = cursor.fetchall()
                            if not matches:
                                cursor.execute(
                                    """
                                    SELECT ch.id::text,ch.estado,ch.movimiento_emision_id::text,
                                           ch.monto::text,ch.conciliacion_origen_id::text,
                                           r.archivada_en
                                    FROM contared.cheques ch
                                    LEFT JOIN contared.conciliaciones r
                                      ON r.id=ch.conciliacion_origen_id
                                    WHERE ch.cuenta_id=%s AND ch.numero_cheque=%s
                                      AND ch.fecha_emision=%s
                                    FOR UPDATE OF ch
                                    """,
                                    [account_id, item["numero_cheque"], item["fecha_emision"]],
                                )
                                matches = cursor.fetchall()
                            exact_matches = [
                                match for match in matches
                                if Decimal(match[3]) == item["monto"]
                            ]
                            existing = exact_matches[0] if len(exact_matches) == 1 and len(matches) == 1 else None
                            if matches and existing is None:
                                conflicts.append({
                                    "fila": item["fila_origen"],
                                    "mensaje": "Ya existe un cheque con el mismo número y fecha, pero el importe no coincide o el registro es ambiguo.",
                                })
                                continue
                            if existing:
                                if existing[1] != item["estado"]:
                                    if existing[1] != "retenido" or item["estado"] != "en_circulacion":
                                        conflicts.append({
                                            "fila": item["fila_origen"],
                                            "mensaje": "El cambio de estado no es una transición permitida; no se sobrescribió el historial operativo.",
                                        })
                                        continue
                                if (
                                    existing[1] == "en_circulacion"
                                    and existing[6] != item["fecha_entrega"]
                                ):
                                    conflicts.append({
                                        "fila": item["fila_origen"],
                                        "mensaje": "La fecha de entrega difiere del historial operativo; no se sobrescribió.",
                                    })
                                    continue
                                if existing[2] and Decimal(existing[3]) != item["monto"]:
                                    conflicts.append({
                                        "fila": item["fila_origen"],
                                        "mensaje": "El importe no coincide con la emisión contable vinculada.",
                                    })
                                    continue
                                if existing[5] is not None:
                                    conflicts.append({
                                        "fila": item["fila_origen"],
                                        "mensaje": "El cheque está vinculado a una conciliación archivada; no se modificó.",
                                    })
                                    continue
                            planned.append((item, existing))
                        if conflicts:
                            raise ValidationError({
                                "detail": "Hay registros que requieren revisión; no se importó ningún cheque.",
                                "errores": conflicts,
                            })
                        cursor.execute(
                            """
                            SELECT COALESCE(max(version),0)+1
                            FROM contared.cheque_importaciones
                            WHERE cuenta_id=%s AND tipo_documento=%s
                            """,
                            [account_id, document_type],
                        )
                        version = cursor.fetchone()[0]
                        import_id = str(uuid4())
                        _set_actor(cursor, {
                            "user_id": scope["user_id"],
                        })
                        cursor.execute(
                            """
                            INSERT INTO contared.cheque_importaciones
                              (id,cuenta_id,tipo_documento,fecha_corte,version,
                               nombre_archivo,sha256,object_path,perfil,
                               registros_total,creado_por)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                            """,
                            [
                                import_id,account_id,document_type,cutoff,version,
                                original_name,file_hash,object_path,profile_json,
                                len(planned),scope["user_id"],
                            ],
                        )
                        period_origin = date(cutoff.year, cutoff.month, 1)
                        created = updated = 0
                        for item, existing in planned:
                            if existing:
                                cheque_id = existing[0]
                                cursor.execute(
                                    """
                                    UPDATE contared.cheques
                                    SET clave_fuente=COALESCE(clave_fuente,%s),
                                        descripcion=%s,referencia=%s,beneficiario=%s,
                                        monto=%s,
                                        estado=CASE WHEN estado='retenido'
                                          AND %s='en_circulacion' THEN 'en_circulacion'
                                          ELSE estado END,
                                        fecha_entrega=COALESCE(fecha_entrega,%s),
                                        entregado_por=COALESCE(entregado_por,%s),
                                        periodo_origen=CASE
                                          WHEN conciliacion_origen_id IS NULL THEN %s
                                          ELSE periodo_origen END,
                                        actualizado_en=now()
                                    WHERE id=%s
                                    """,
                                    [
                                        item["clave_fuente"],item["descripcion"],
                                        item["referencia"],item["beneficiario"],
                                        item["monto"],item["estado"],item["fecha_entrega"],
                                        scope["user_id"] if item["fecha_entrega"] else None,
                                        period_origin,cheque_id,
                                    ],
                                )
                                if existing[1] == "retenido" and item["estado"] == "en_circulacion":
                                    cursor.execute(
                                        """
                                        INSERT INTO contared.cheque_eventos
                                          (cheque_id,estado_anterior,estado_nuevo,
                                           fecha_efectiva,registrado_por,motivo,observacion)
                                        VALUES (%s,'retenido','en_circulacion',%s,%s,%s,%s)
                                        """,
                                        [
                                            cheque_id,item["fecha_entrega"],scope["user_id"],
                                            f"Entrega registrada desde documento {document_type}, versión {version}",
                                            original_name,
                                        ],
                                    )
                                updated += 1
                            else:
                                cheque_id = str(uuid4())
                                cursor.execute(
                                    """
                                    INSERT INTO contared.cheques
                                      (id,cuenta_id,conciliacion_origen_id,
                                       movimiento_emision_id,periodo_origen,clave_fuente,
                                       numero_cheque,moneda_codigo,fecha_emision,
                                       descripcion,referencia,beneficiario,monto,estado,
                                       fecha_entrega,entregado_por,creado_por)
                                    VALUES (%s,%s,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                    """,
                                    [
                                        cheque_id,account_id,period_origin,
                                        item["clave_fuente"],item["numero_cheque"],
                                        account[0],item["fecha_emision"],item["descripcion"],
                                        item["referencia"],item["beneficiario"],item["monto"],
                                        item["estado"],item["fecha_entrega"],
                                        scope["user_id"] if item["fecha_entrega"] else None,
                                        scope["user_id"],
                                    ],
                                )
                                cursor.execute(
                                    """
                                    INSERT INTO contared.cheque_eventos
                                      (cheque_id,estado_anterior,estado_nuevo,
                                       fecha_efectiva,registrado_por,motivo,observacion)
                                    VALUES (%s,NULL,%s,%s,%s,%s,%s)
                                    """,
                                    [
                                        cheque_id,item["estado"],
                                        item["fecha_entrega"] or item["fecha_emision"],
                                        scope["user_id"],
                                        f"Importado desde documento {document_type}, versión {version}",
                                        original_name,
                                    ],
                                )
                                created += 1
                            cursor.execute(
                                """
                                INSERT INTO contared.cheque_importacion_detalles
                                  (importacion_id,cheque_id,fila_origen,numero_cheque,
                                   fecha_emision,beneficiario,descripcion,referencia,
                                   monto,estado,datos_originales)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                                """,
                                [
                                    import_id,cheque_id,item["fila_origen"],
                                    item["numero_cheque"],item["fecha_emision"],
                                    item["beneficiario"],item["descripcion"],
                                    item["referencia"],item["monto"],item["estado"],
                                    json.dumps(item["datos_originales"],ensure_ascii=False),
                                ],
                            )
            except (DatabaseError, ValidationError):
                _delete_storage_object(object_path)
                raise
        return Response({
            "id": import_id,
            "version": version,
            "tipo_documento": document_type,
            "fecha_corte": cutoff.isoformat(),
            "registros_total": len(parsed),
            "registros_nuevos": created,
            "registros_actualizados": updated,
            "repetido": False,
            "advertencias": warnings,
            "mensaje": "Documento registrado con historial; no se crearon movimientos contables.",
        }, status=201)


class ChequeImportDetailView(APIView):
    def get(self, request, account_id, import_id):
        scope = _scope_for_account(request, account_id)
        _require_role(scope, READ_ROLES)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """
                        SELECT id::text,tipo_documento,fecha_corte,version,nombre_archivo,
                               sha256,perfil,registros_total,creado_en
                        FROM contared.cheque_importaciones
                        WHERE id=%s AND cuenta_id=%s
                        """,
                        [import_id,account_id],
                    )
                    document = cursor.fetchone()
                    if document is None:
                        raise NotFound("La versión del documento no existe para esta cuenta.")
                    cursor.execute(
                        """
                        SELECT fila_origen,cheque_id::text,numero_cheque,fecha_emision,
                               beneficiario,descripcion,referencia,monto::text,estado,
                               datos_originales
                        FROM contared.cheque_importacion_detalles
                        WHERE importacion_id=%s
                        ORDER BY fila_origen
                        """,
                        [import_id],
                    )
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            _database_error(exc)
        return Response({
            "id": document[0],
            "tipo_documento": document[1],
            "fecha_corte": document[2].isoformat(),
            "version": document[3],
            "nombre_archivo": document[4],
            "sha256": document[5],
            "perfil": document[6],
            "registros_total": document[7],
            "creado_en": document[8].isoformat(),
            "registros": [
                {
                    "fila_origen": row[0],
                    "cheque_id": row[1],
                    "numero_cheque": row[2],
                    "fecha_emision": row[3].isoformat(),
                    "beneficiario": row[4],
                    "descripcion": row[5],
                    "referencia": row[6],
                    "monto": row[7],
                    "estado": row[8],
                    "datos_originales": row[9],
                }
                for row in rows
            ],
        })


class ChequeCrossSync:
    @staticmethod
    def reflect(cursor, scope, bank_ids, book_ids, cross_id):
        if len(bank_ids) != 1 or len(book_ids) != 1:
            return
        cursor.execute("SELECT to_regclass('contared.cheques')")
        table_row = cursor.fetchone()
        if table_row is None or table_row[0] is None:
            return
        bank_id, book_id = bank_ids[0], book_ids[0]
        cursor.execute(
            """
            SELECT l.numero_cheque,l.fecha,l.monto::text,b.fecha,b.monto::text,
                   b.numero_cheque,c.conciliacion_id::text,cb.moneda_codigo,
                   COALESCE(NULLIF(l.datos_originales->>'beneficiario',''),
                            NULLIF(l.datos_originales->>'beneficiary',''))
            FROM contared.movimientos l
            JOIN contared.movimientos b ON b.cuenta_id=l.cuenta_id
            JOIN contared.cargas lc ON lc.id=l.carga_id
            JOIN contared.cargas bc ON bc.id=b.carga_id
            JOIN contared.conciliaciones c ON c.id=lc.conciliacion_id
            JOIN contared.cuentas_bancarias cb ON cb.id=l.cuenta_id
            WHERE l.id=%s AND b.id=%s AND l.lado='libros' AND b.lado='banco'
              AND lc.estado='validada' AND bc.estado='validada'
              AND lc.origen='libros' AND bc.origen='banco'
              AND l.numero_cheque IS NOT NULL AND b.numero_cheque=l.numero_cheque
              AND b.monto<0 AND l.monto<0 AND abs(b.monto)=abs(l.monto)
              AND b.fecha>=l.fecha AND c.id=%s
            FOR UPDATE OF l,b
            """,
            [book_id,bank_id,scope["reconciliation_id"]],
        )
        row = cursor.fetchone()
        if row is None:
            return
        number, issue_date, book_amount, bank_date, bank_amount, bank_number, origin_id, currency, beneficiary = row
        if not number or number != bank_number or abs(Decimal(book_amount)) != abs(Decimal(bank_amount)):
            return
        cursor.execute(
            """
            SELECT count(*) FROM contared.movimientos m
            JOIN contared.cargas c ON c.id=m.carga_id
            WHERE m.cuenta_id=%s AND m.lado='libros' AND c.estado='validada'
              AND m.numero_cheque=%s
            """,
            [scope["account_id"],number],
        )
        if cursor.fetchone()[0] != 1:
            return False
        cursor.execute(
            """
            SELECT count(*) FROM contared.movimientos m
            JOIN contared.cargas c ON c.id=m.carga_id
            WHERE m.cuenta_id=%s AND m.lado='banco' AND c.estado='validada'
              AND m.numero_cheque=%s
            """,
            [scope["account_id"],number],
        )
        if cursor.fetchone()[0] != 1:
            return False
        cursor.execute(
            "SELECT id::text,estado FROM contared.cheques WHERE movimiento_emision_id=%s FOR UPDATE",
            [book_id],
        )
        existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                """
                SELECT id::text,estado
                FROM contared.cheques
                WHERE cuenta_id=%s AND numero_cheque=%s AND fecha_emision=%s
                  AND monto=abs(%s::numeric) AND movimiento_emision_id IS NULL
                  AND conciliacion_origen_id IS NULL
                FOR UPDATE
                """,
                [scope["account_id"],number,issue_date,book_amount],
            )
            candidates = cursor.fetchall()
            if len(candidates) > 1:
                return False
            existing = candidates[0] if candidates else None
            if existing is not None:
                cursor.execute(
                    """
                    UPDATE contared.cheques
                    SET movimiento_emision_id=%s,conciliacion_origen_id=%s,
                        periodo_origen=%s,actualizado_en=now()
                    WHERE id=%s
                    """,
                    [
                        book_id,origin_id,date(issue_date.year,issue_date.month,1),
                        existing[0],
                    ],
                )
        cheque_id = existing[0] if existing else str(uuid4())
        previous = existing[1] if existing else None
        if previous == "anulado" or previous == "cobrado":
            return False
        cursor.execute(
            """
            INSERT INTO contared.cheques
              (id,cuenta_id,conciliacion_origen_id,movimiento_emision_id,periodo_origen,
               numero_cheque,moneda_codigo,fecha_emision,beneficiario,monto,
               estado,movimiento_cobro_id,conciliacion_resolucion_id,creado_por)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'cobrado',%s,%s,%s)
            ON CONFLICT (movimiento_emision_id) DO NOTHING
            """,
            [
                cheque_id,scope["account_id"],origin_id,book_id,
                date(issue_date.year,issue_date.month,1),number,currency,
                issue_date,beneficiary,abs(Decimal(book_amount)),bank_id,
                scope["reconciliation_id"],scope["user_id"],
            ],
        )
        if previous:
            cursor.execute(
                """
                UPDATE contared.cheques
                SET estado='cobrado',movimiento_cobro_id=%s,
                    conciliacion_resolucion_id=%s,actualizado_en=now()
                WHERE id=%s AND estado IN ('retenido','en_circulacion')
                """,
                [bank_id,scope["reconciliation_id"],cheque_id],
            )
        cursor.execute(
            """
            INSERT INTO contared.cheque_eventos
              (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,
               registrado_por,motivo,observacion,movimiento_banco_id,
               conciliacion_id,cruce_id)
            VALUES (%s,%s,'cobrado',%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                cheque_id,previous,bank_date,scope["user_id"],
                (
                    "Cobro reconocido mientras el cheque figuraba Retenido; "
                    "documentar la inconsistencia."
                    if previous == "retenido"
                    else "Cobro reconocido por cruce confirmado de movimientos"
                ),
                (
                    "El cruce confirma el cobro bancario. No se infirió una fecha "
                    "de entrega; documenta lo ocurrido."
                    if previous == "retenido" else None
                ),
                bank_id,scope["reconciliation_id"],cross_id,
            ],
        )
        return previous == "retenido"
