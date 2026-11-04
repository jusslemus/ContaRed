import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from calendar import monthrange
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
from django.conf import settings
from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel
from rest_framework.exceptions import NotFound, ValidationError
import xlrd
from openpyxl import Workbook


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_SHEETS = 30
MAX_COLUMNS = 200
MAX_DATA_ROWS = 20_000
BALANCE_FIELDS = {
    "saldo_inicial_banco",
    "saldo_inicial_libros",
    "saldo_final_banco",
    "saldo_final_libros",
}
IDENTIFIER_FIELDS = {"referencia", "numero_transaccion", "numero_cheque", "codigo_tt"}
XLS_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_preview_locks = {}
_preview_locks_guard = threading.Lock()


@contextmanager
def _preview_metadata_lock(path):
    with _preview_locks_guard:
        entry = _preview_locks.get(path)
        if entry is None:
            entry = [threading.Lock(), 0]
            _preview_locks[path] = entry
        entry[1] += 1
    lock, _ = entry
    try:
        with lock:
            with path.open("a+b") as lock_file:
                if os.name == "nt":
                    import msvcrt

                    lock_file.seek(0, os.SEEK_END)
                    if lock_file.tell() == 0:
                        lock_file.write(b"\0")
                        lock_file.flush()
                    lock_file.seek(0)
                    while True:
                        try:
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            time.sleep(0.05)
                    try:
                        yield
                    finally:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        with _preview_locks_guard:
            entry[1] -= 1
            if entry[1] == 0:
                _preview_locks.pop(path, None)


@dataclass(frozen=True)
class ValidatedWorkbook:
    movimientos: list[dict]
    errores: list[dict]
    advertencias: list[dict]
    exclusiones_requeridas: list[dict]
    filas_analizadas: int
    propuestas_normalizacion_fecha: list[dict]
    normalizaciones_fecha_aplicadas: list[dict]


def parse_decimal(value, field_name):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name}: monto no válido.")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        text = str(value).strip().replace("\u00a0", "").replace(" ", "")
        if "," in text and "." in text:
            decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
            grouping_separator = "." if decimal_separator == "," else ","
            text = text.replace(grouping_separator, "").replace(decimal_separator, ".")
        elif "," in text:
            tail = text.rsplit(",", 1)[1]
            text = text.replace(",", ".") if len(tail) <= 4 else text.replace(",", "")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name}: monto no válido.") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name}: monto no válido.")
    parts = number.as_tuple()
    effective_exponent = parts.exponent
    digits = list(parts.digits)
    while digits and digits[-1] == 0 and effective_exponent < 0:
        digits.pop()
        effective_exponent += 1
    if effective_exponent < -4:
        raise ValueError(f"{field_name}: use un monto finito con hasta cuatro decimales.")
    while digits and digits[0] == 0:
        digits.pop(0)
    if len(digits) > 20 or (number != 0 and number.adjusted() > 15):
        raise ValueError(f"{field_name}: el monto supera el máximo permitido.")
    return number


def parse_date(value, date_format=None, field_name="fecha"):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            excel_date = from_excel(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{field_name}: el serial de Excel no representa una fecha válida.") from exc
        if isinstance(excel_date, datetime):
            return excel_date.date()
        if isinstance(excel_date, date):
            return excel_date
        raise ValueError(f"{field_name}: el serial de Excel no representa una fecha completa.")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}: la fecha es obligatoria.")
    text = value.strip()
    formats = [date_format] if date_format else ["%Y-%m-%d", "%Y/%m/%d"]
    if date_format:
        separator_alternative = date_format.replace("/", "-") if "/" in date_format else date_format.replace("-", "/")
        if separator_alternative != date_format:
            formats.append(separator_alternative)
    for candidate in formats:
        if candidate:
            try:
                return datetime.strptime(text, candidate).date()
            except ValueError:
                pass
    raise ValueError(f"{field_name}: use date_format para indicar el formato de la fecha.")


def _json_value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not value.is_integer():
        return str(value)
    if isinstance(value, float):
        return str(int(value))
    return value if value is None or isinstance(value, (str, int, bool)) else str(value)


def _file_format(path):
    try:
        with Path(path).open("rb") as source:
            signature = source.read(8)
    except OSError as exc:
        raise ValidationError({"archivo": "No se pudo leer el archivo."}) from exc
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsx" and signature.startswith(b"PK"):
        return "xlsx"
    if suffix == ".xls" and signature == XLS_SIGNATURE:
        return "xls"
    raise ValidationError({
        "archivo": "El contenido no coincide con un libro .xlsx o .xls válido. Cambiar la extensión no convierte el archivo."
    })


def _normalize_xls(source_path, target_path):
    if _file_format(source_path) != "xls":
        raise ValidationError({"archivo": "El archivo no es un libro .xls binario válido."})
    source = None
    converted = None
    try:
        source = xlrd.open_workbook(str(source_path), on_demand=True)
        if source.nsheets > MAX_SHEETS:
            raise ValidationError({"archivo": "El libro supera el límite de hojas permitido."})
        converted = Workbook(write_only=False)
        converted.remove(converted.active)
        for source_sheet in source.sheets():
            if source_sheet.ncols > MAX_COLUMNS or source_sheet.nrows > MAX_DATA_ROWS + 100:
                raise ValidationError({"archivo": "Una hoja supera los límites de filas o columnas permitidos."})
            target_sheet = converted.create_sheet(source_sheet.name[:31] or "Hoja")
            for row_index in range(source_sheet.nrows):
                for column_index in range(source_sheet.ncols):
                    cell = source_sheet.cell(row_index, column_index)
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate.xldate_as_datetime(value, source.datemode)
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        value = f"#ERROR:{xlrd.error_text_from_code.get(value, 'unknown')}"
                    elif cell.ctype == xlrd.XL_CELL_EMPTY:
                        value = None
                    target = target_sheet.cell(row=row_index + 1, column=column_index + 1)
                    target.value = value
                    if cell.xf_index is not None and cell.xf_index < len(source.xf_list):
                        format_key = source.xf_list[cell.xf_index].format_key
                        cell_format = source.format_map.get(format_key)
                        if cell_format is not None:
                            target.number_format = cell_format.format_str
            target_sheet.sheet_view.showGridLines = True
        converted.save(target_path)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError({"archivo": "No se pudo leer el libro .xls."}) from exc
    finally:
        if converted is not None:
            converted.close()
        if source is not None:
            source.release_resources()


def _normalized_header(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


HEADER_ALIASES = {
    "fecha": ("fecha de contabilizacion", "fecha valor", "fecha", "date", "posting date"),
    "referencia": ("referencia 1", "referencia", "no doc", "numero de documento", "reference", "ref"),
    "descripcion": ("info detallada", "descripci髇", "descripcion", "concepto", "detalle", "description", "narrative"),
    "numero_transaccion": ("n trans", "no trans", "numero transaccion", "transaccion", "transaction id"),
    "numero_cheque": ("numero cheque", "no cheque", "cheque", "check number"),
    "codigo_tt": ("codigo tt", "tt", "tipo transaccion", "codigo", "origen", "transaction code"),
    "monto": ("monto firmado", "amount", "importe"),
    "saldo_origen": ("saldo acumulado ml", "saldo acumulado", "saldo gtq", "saldo qtz", "balance", "saldo", "balance*"),
    "salida": ("debe", "debe gtq", "debe qtz", "cargo ml", "cargo", "debito", "debit"),
    "entrada": ("haber", "haber gtq", "haber qtz", "abono ml", "abono", "credito", "credit"),
}


def _suggest_mapping(headers, origin):
    normalized = [_normalized_header(header) for header in headers]
    mapping = {}
    used = set()
    for field in ("fecha", "referencia", "descripcion", "numero_transaccion",
                  "numero_cheque", "codigo_tt", "saldo_origen"):
        for alias in HEADER_ALIASES[field]:
            index = next(
                (index for index, header in enumerate(normalized)
                 if index not in used and (
                     header == alias
                     or field == "descripcion" and header.startswith("descripci")
                 )),
                None,
            )
            if index is not None:
                mapping[field] = index + 1
                used.add(index)
                break
    signed_amount = next((
        index for alias in HEADER_ALIASES["monto"]
        for index, header in enumerate(normalized)
        if header == alias
    ), None)
    debit = next((
        index for alias in HEADER_ALIASES["salida"]
        for index, header in enumerate(normalized)
        if header == alias
    ), None)
    credit = next((
        index for alias in HEADER_ALIASES["entrada"]
        for index, header in enumerate(normalized)
        if header == alias
    ), None)
    if origin == "banco" and debit is not None and credit is not None:
        mapping["salida"] = debit + 1
        mapping["entrada"] = credit + 1
    elif signed_amount is not None:
        mapping["monto"] = signed_amount + 1
    return mapping


def _read_workbook_context(sheet, headers, mapping, origin):
    rows = list(sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 25, 25), values_only=True))
    text = " ".join(
        str(value).strip()
        for row in rows
        for value in row
        if isinstance(value, str) and value.strip()
    )
    account_match = re.search(
        r"(?:cuenta|n[uú]mero\s+de\s+cuenta)\s*:?\s*([0-9][0-9\s-]{4,})",
        text,
        re.IGNORECASE,
    )
    if not account_match:
        account_match = re.search(
            r"(?:producto|account|n[uú]mero)\s*:?\s*([0-9][0-9\s-]{4,})",
            text,
            re.IGNORECASE,
        )
    account_number = (
        re.sub(r"\D", "", account_match.group(1))
        if account_match else None
    )
    termination = account_number[-4:] if account_number else None
    range_match = re.search(
        r"(?:del|desde)\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})\s+"
        r"(?:al|hasta)\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        text,
        re.IGNORECASE,
    )
    declared_period = None
    coverage = None
    if range_match:
        start = end = None
        for date_format in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                start = datetime.strptime(range_match.group(1), date_format).date()
                end = datetime.strptime(range_match.group(2), date_format).date()
                break
            except ValueError:
                continue
        if start and end and start <= end:
            declared_period = start.strftime("%Y-%m")
            coverage = {"desde": start.isoformat(), "hasta": end.isoformat()}
    if not declared_period:
        found_dates = []
        for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or MAX_DATA_ROWS, MAX_DATA_ROWS), values_only=True):
            for value in row:
                if isinstance(value, datetime):
                    found_dates.append(value.date())
                elif isinstance(value, date):
                    found_dates.append(value)
                elif isinstance(value, str):
                    for date_format in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                        try:
                            found_dates.append(datetime.strptime(value.strip(), date_format).date())
                            break
                        except ValueError:
                            pass
        if found_dates:
            month_counts = {}
            for found_date in found_dates:
                month_key = found_date.strftime("%Y-%m")
                month_counts[month_key] = month_counts.get(month_key, 0) + 1
            dominant_month = max(sorted(month_counts), key=lambda key: month_counts[key])
            month_dates = [
                found_date for found_date in found_dates
                if found_date.strftime("%Y-%m") == dominant_month
            ]
            declared_period = dominant_month
            coverage = {
                "desde": min(month_dates).isoformat(),
                "hasta": max(month_dates).isoformat(),
            }

    currency = None
    currency_match = re.search(
        r"\b(GTQ|QTZ|USD|US\$|D[ÓO]LARES|QUETZALES)\b",
        text,
        re.IGNORECASE,
    )
    if currency_match:
        token = currency_match.group(1).upper()
        currency = "USD" if token in ("USD", "US$", "DOLARES", "DÓLARES") else "GTQ"
    country = (
        "GT" if re.search(r"\bGuatemala\b", text, re.IGNORECASE)
        else "SV" if re.search(r"\bEl Salvador\b", text, re.IGNORECASE)
        else None
    )
    bank_match = re.search(
        r"\b(Banco\s+Industrial|Banco\s+Agr[ií]cola|Promerica|BAC)\b",
        text,
        re.IGNORECASE,
    )
    bank_name = bank_match.group(1) if bank_match else None
    opening_balance = None
    opening_match = re.search(
        r"(?:saldo|balance)\s+inicial(?:\s*\([^)]*\))?\s*:\s*"
        r"([-+]?\d[\d.,]*)",
        text,
        re.IGNORECASE,
    )
    if opening_match:
        try:
            opening_balance = format(parse_decimal(opening_match.group(1), "saldo inicial"), "f")
        except ValueError:
            pass

    normalized_headers = [_normalized_header(value) for value in headers]
    account_code_index = next((
        index for index, value in enumerate(normalized_headers)
        if value in ("sn codigo de cuenta", "codigo de cuenta", "cuenta contable")
    ), None)
    account_code = None
    if account_code_index is not None:
        codes = set()
        for row in sheet.iter_rows(
            min_row=2,
            max_row=min(sheet.max_row or MAX_DATA_ROWS + 1, MAX_DATA_ROWS + 1),
            values_only=True,
        ):
            if account_code_index < len(row) and row[account_code_index] not in (None, ""):
                value = str(row[account_code_index]).strip()
                if value.casefold() not in ("si", "saldo inicial"):
                    codes.add(value)
        if len(codes) == 1:
            account_code = next(iter(codes))

    if opening_balance is None and "saldo_origen" in mapping and "codigo_tt" in mapping:
        balance_index = int(mapping["saldo_origen"]) - 1
        code_index = int(mapping["codigo_tt"]) - 1
        for row in sheet.iter_rows(min_row=2, max_row=min(sheet.max_row or 20, 20), values_only=True):
            if code_index < len(row) and str(row[code_index] or "").strip().casefold() in ("si", "saldo inicial"):
                if balance_index < len(row):
                    try:
                        value = parse_decimal(row[balance_index], "saldo inicial")
                        opening_balance = format(value, "f") if value is not None else None
                    except ValueError:
                        pass
                break

    return {
        "cuenta_bancaria": account_number,
        "terminacion": termination,
        "codigo_contable": account_code,
        "banco": bank_name,
        "pais_codigo": country,
        "moneda_codigo": currency,
        "periodo_declarado": declared_period,
        "cobertura_declarada": coverage,
        "saldo_inicial": opening_balance,
        "tipo_identificador_cuenta": "codigo_contable" if origin == "libros" else "cuenta_bancaria",
    }


def recognize_workbook(path, *, requested_origin=None):
    _check_archive(path)
    workbook = load_workbook(path, read_only=True, data_only=True)
    candidates = []
    try:
        if len(workbook.sheetnames) > MAX_SHEETS:
            raise ValidationError({"archivo": "El libro supera el límite de hojas permitido."})
        for sheet in workbook.worksheets:
            if sheet.max_column and sheet.max_column > MAX_COLUMNS:
                continue
            for row_number, row in enumerate(
                sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 100, 100), values_only=True),
                start=1,
            ):
                headers = [str(value).strip() if value is not None else "" for value in row]
                normalized = [_normalized_header(value) for value in headers]
                strong_ledger = (
                    "c d ml" in normalized and "saldo acumulado ml" in normalized
                )
                numeric_ledger_amount = (
                    "c d ml" in normalized
                    and "c d ml moneda" in normalized
                    and "saldo acumulado ml" in normalized
                    and "saldo acumulado ml moneda" in normalized
                )
                bank_mapping = _suggest_mapping(headers, "banco")
                strong_bank = {"salida", "entrada"} <= bank_mapping.keys()
                origin = "libros" if strong_ledger else (
                    "banco" if strong_bank
                    else requested_origin if requested_origin in ("banco", "libros")
                    else "banco"
                )
                mapping = _suggest_mapping(headers, origin)
                if (
                    origin == "libros"
                    and "c d ml" in normalized
                    and "saldo acumulado ml" in normalized
                    and (
                        "sn codigo de cuenta" in normalized
                        or numeric_ledger_amount
                    )
                ):
                    mapping["monto"] = normalized.index("c d ml") + 1
                required = (
                    strong_ledger
                    or
                    "fecha" in mapping and (
                        "monto" in mapping
                        or (origin == "banco" and {"salida", "entrada"} <= mapping.keys())
                    )
                )
                score = sum(field in mapping for field in (
                    "fecha", "monto", "salida", "entrada", "referencia",
                    "descripcion", "saldo_origen",
                ))
                if required:
                    candidates.append((score, sheet.title, row_number, headers, origin, mapping))
    finally:
        workbook.close()
    if not candidates:
        return {
            "reconocido": False,
            "origen_sugerido": requested_origin,
            "motivo": "No se encontró una fila de encabezados con fecha e importes compatibles; usa Revisar configuración.",
        }
    candidates.sort(key=lambda item: (-item[0], item[2], item[1]))
    score, sheet_name, header_row, headers, origin, mapping = candidates[0]
    context_workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        context = _read_workbook_context(
            context_workbook[sheet_name],
            headers,
            mapping,
            origin,
        )
    finally:
        context_workbook.close()
    profile = (
        "banco_industrial_libros"
        if origin == "libros" and "saldo acumulado ml" in [_normalized_header(value) for value in headers]
        else "banco_extracto_debe_haber"
        if origin == "banco" and {"salida", "entrada"} <= mapping.keys()
        else "generico_monto_firmado"
    )
    recognized_fields = sorted(mapping)
    return {
        "reconocido": score >= 3 and (
            "monto" in mapping
            or (origin == "banco" and {"salida", "entrada"} <= mapping.keys())
        ),
        "origen_sugerido": origin,
        "perfil_sugerido": profile,
        "confianza": "alta" if score >= 5 else "media",
        "hoja": sheet_name,
        "fila_encabezados": header_row,
        "mapping": mapping,
        "contexto": context,
        "campos_reconocidos": recognized_fields,
        "date_format": "%d/%m/%Y",
        "motivo": (
            None
            if score >= 3 and (
                "monto" in mapping
                or (origin == "banco" and {"salida", "entrada"} <= mapping.keys())
            )
            else "Formato detectado parcialmente; asigna manualmente las columnas de importes antes de validar."
        ),
    }


def _check_archive(path):
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2_000 or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValidationError({"archivo": "El libro supera el límite de contenido permitido."})
            if any(item.file_size > MAX_UNCOMPRESSED_BYTES for item in members):
                raise ValidationError({"archivo": "El libro contiene una hoja demasiado grande."})
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError({"archivo": "El archivo no es un libro .xlsx válido."}) from exc


def workbook_preview(path, *, origin=None):
    _check_archive(path)
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        if len(workbook.sheetnames) > MAX_SHEETS:
            raise ValidationError({"archivo": "El libro supera el límite de hojas permitido."})
        sheets = []
        for sheet in workbook.worksheets:
            if sheet.max_column and sheet.max_column > MAX_COLUMNS:
                raise ValidationError({"archivo": "Una hoja supera el límite de columnas permitido."})
            sample = list(sheet.iter_rows(min_row=1, max_row=6, values_only=True))
            frame = pd.DataFrame(sample).astype(object)
            frame = frame.where(pd.notna(frame), None)
            sheets.append({
                "nombre": sheet.title,
                "columnas": min(sheet.max_column or 0, MAX_COLUMNS),
                "filas_estimadas": sheet.max_row or 0,
                "muestra": [
                    [_json_value(value) for value in row]
                    for row in frame.values.tolist()
                ],
            })
        workbook.close()
        return {
            "hojas": sheets,
            "reconocimiento": recognize_workbook(path, requested_origin=origin),
        }
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError({"archivo": "No se pudo leer el libro .xlsx."}) from exc


def inspect_workbook_sheet(path, *, sheet_name, header_row):
    if isinstance(header_row, bool) or not isinstance(header_row, int) or not 1 <= header_row <= 100:
        raise ValidationError({"header_row": "header_row debe ser un número entre 1 y 100."})
    _check_archive(path)
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        if sheet_name not in workbook.sheetnames:
            raise ValidationError({"sheet": "La hoja seleccionada no existe."})
        sheet = workbook[sheet_name]
        if sheet.max_column and sheet.max_column > MAX_COLUMNS:
            raise ValidationError({"archivo": "La hoja supera el límite de columnas permitido."})
        rows = list(sheet.iter_rows(
            min_row=header_row,
            max_row=min(header_row + 5, sheet.max_row or header_row),
            values_only=True,
        ))
        if not rows or not any(value is not None for value in rows[0]):
            raise ValidationError({"header_row": "La fila seleccionada está vacía."})
        width = max((len(row) for row in rows), default=0)

        def column_name(index):
            name = ""
            number = index + 1
            while number:
                number, remainder = divmod(number - 1, 26)
                name = chr(65 + remainder) + name
            return name

        headers = [
            {
                "columna": column_name(index),
                "encabezado": str(rows[0][index]).strip() if index < len(rows[0]) and rows[0][index] is not None else "",
            }
            for index in range(width)
        ]
        sample = [
            [_json_value(value) for value in list(row)[:width]]
            for row in rows[1:]
        ]
        workbook.close()
        return {"columnas": headers, "muestra_datos": sample}
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError({"archivo": "No se pudo inspeccionar la hoja seleccionada."}) from exc


def _resolve_mapping(headers, mapping, origin):
    fields = {
        "fecha": "fecha",
        "referencia": "referencia",
        "descripcion": "descripcion",
        "fecha_valor": "fecha_valor",
        "numero_transaccion": "numero_transaccion",
        "numero_cheque": "numero_cheque",
        "codigo_tt": "codigo_tt",
        "saldo_origen": "saldo_origen",
        "saldo": "saldo_origen",
        "monto": "monto",
        "salida": "salida",
        "salidas": "salida",
        "entrada": "entrada",
        "entradas": "entrada",
    }
    if not isinstance(mapping, dict):
        raise ValidationError({"mapping": "mapping debe ser un objeto de nombres de campo a columna."})
    unknown = set(mapping) - set(fields)
    if unknown:
        raise ValidationError({"mapping": "El mapeo contiene campos no permitidos."})
    normalized_mapping = {}
    for field, column in mapping.items():
        normalized = fields[field]
        if normalized in normalized_mapping:
            raise ValidationError({"mapping": f"El campo {normalized} está mapeado más de una vez."})
        normalized_mapping[normalized] = column
    directional = {"salida", "entrada"} & set(normalized_mapping)
    if origin == "banco":
        if "monto" in normalized_mapping and directional:
            raise ValidationError({"mapping": "Use monto firmado o el par salida/entrada, no ambos."})
        if "monto" not in normalized_mapping and directional != {"salida", "entrada"}:
            raise ValidationError({"mapping": "Banco requiere monto firmado o ambas columnas salida y entrada."})
    elif directional:
        raise ValidationError({"mapping": "salida y entrada solo aplican al origen banco."})
    required = {"fecha", "monto"} if "monto" in normalized_mapping else {"fecha", "salida", "entrada"}
    missing = required - set(normalized_mapping)
    if missing:
        raise ValidationError({"mapping": f"Faltan campos obligatorios: {', '.join(sorted(missing))}."})
    indexes = {}
    for field, column in normalized_mapping.items():
        if not isinstance(column, (str, int)):
            raise ValidationError({"mapping": f"La columna de {field} no es válida."})
        if isinstance(column, bool):
            raise ValidationError({"mapping": f"La columna de {field} no es válida."})
        if isinstance(column, int) or (isinstance(column, str) and column.strip().isdigit()):
            index = int(column) - 1
        elif isinstance(column, str) and re.fullmatch(r"[A-Za-z]{1,3}", column.strip()):
            index = 0
            for char in column.strip().upper():
                index = index * 26 + ord(char) - ord("A") + 1
            index -= 1
        else:
            matches = [i for i, header in enumerate(headers) if header == str(column).strip()]
            if len(matches) != 1:
                raise ValidationError({"mapping": f"La columna seleccionada para {field} no es única."})
            index = matches[0]
        if index < 0 or index >= len(headers):
            raise ValidationError({"mapping": f"La columna seleccionada para {field} no existe."})
        indexes[field] = index
    if len(set(indexes[field] for field in required)) != len(required):
        raise ValidationError({"mapping": "Cada campo obligatorio debe usar una columna distinta."})
    return indexes


def _bi_statement_date_proposals(path, *, sheet_name, header_row, indexes, origin, period, cutoff):
    if origin != "banco" or period.day != 1:
        return []
    last_day = date(period.year, period.month, monthrange(period.year, period.month)[1])
    if cutoff != last_day:
        return []

    recognition = recognize_workbook(path, requested_origin=origin)
    context = recognition.get("contexto", {})
    bank = re.sub(r"[^a-z0-9]", "", str(context.get("banco") or "").casefold())
    specific_account = context.get("cuenta_bancaria") == "0040040592"
    if (
        recognition.get("hoja") != sheet_name
        or recognition.get("fila_encabezados") != header_row
        or recognition.get("perfil_sugerido") != "banco_extracto_debe_haber"
        or (bank != "bancoindustrial" and not specific_account)
        or context.get("pais_codigo") != "GT"
        or context.get("moneda_codigo") != "GTQ"
        or context.get("periodo_declarado") != period.strftime("%Y-%m")
    ):
        return []

    recognized_mapping = recognition.get("mapping", {})
    expected_fields = {
        "fecha", "codigo_tt", "descripcion", "referencia",
        "salida", "entrada", "saldo_origen",
    }
    if any(
        field not in indexes
        or recognized_mapping.get(field) != indexes[field] + 1
        for field in expected_fields
    ):
        return []
    format_identity = "Banco Industrial" if bank == "bancoindustrial" else "la cuenta bancaria 0040040592"

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name]
        candidates = []
        normalized_dates = []
        text_dates = []
        last_numeric_row = None
        for row_number, row in enumerate(
            sheet.iter_rows(min_row=header_row + 1, max_col=max(indexes.values()) + 1),
            header_row + 1,
        ):
            date_cell = row[indexes["fecha"]]
            value = date_cell.value
            if value is None or not str(value).strip():
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                last_numeric_row = row_number
            try:
                interpreted = parse_date(value, "%d/%m/%Y", "fecha")
            except ValueError:
                return []
            if isinstance(value, str):
                text_dates.append((row_number, interpreted))
            proposal = None
            if interpreted < period or interpreted > cutoff:
                if (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and float(value).is_integer()
                    and date_cell.number_format == "General"
                    and interpreted.year == period.year
                    and interpreted.day == period.month
                    and interpreted.month != period.month
                ):
                    proposed_date = date(period.year, period.month, interpreted.month)
                    if period <= proposed_date <= cutoff:
                        proposal = {
                            "fila": row_number,
                            "valor_original": _json_value(value),
                            "formato_original": date_cell.number_format,
                            "fecha_original": interpreted.isoformat(),
                            "fecha_propuesta": proposed_date.isoformat(),
                            "motivo": (
                                f"El documento asociado a {format_identity} declara {period:%Y-%m}. "
                                "El serial numérico está en formato General; al intercambiar "
                                "día y mes, la fecha mantiene la secuencia del extracto y "
                                "continúa con las fechas textuales posteriores. Confirma antes "
                                "de aplicar esta propuesta."
                            ),
                        }
                        candidates.append(proposal)
                        normalized_dates.append(proposed_date)
                        continue
                return []
            normalized_dates.append(interpreted)

        if (
            not candidates
            or not text_dates
            or last_numeric_row is None
            or text_dates[0][0] <= last_numeric_row
            or text_dates[0][1] <= max(
                date.fromisoformat(item["fecha_propuesta"]) for item in candidates
            )
            or any(
                current < previous
                for previous, current in zip(normalized_dates, normalized_dates[1:])
            )
        ):
            return []
        return candidates
    finally:
        workbook.close()


def validate_workbook(
    path, *, sheet_name, header_row, mapping, date_format, origin, period, cutoff,
    confirm_date_proposals=False,
):
    if origin not in ("banco", "libros"):
        raise ValidationError({"origen": "El origen debe ser banco o libros."})
    if not isinstance(confirm_date_proposals, bool):
        raise ValidationError({"confirmar_normalizacion_fecha": "El valor debe ser verdadero o falso."})
    if isinstance(header_row, bool) or not isinstance(header_row, int) or not 1 <= header_row <= 100:
        raise ValidationError({"header_row": "header_row debe ser un número entre 1 y 100."})
    _check_archive(path)
    try:
        formulas = load_workbook(path, read_only=True, data_only=False)
        cached = load_workbook(path, read_only=True, data_only=True)
        if sheet_name not in formulas.sheetnames:
            raise ValidationError({"sheet": "La hoja seleccionada no existe."})
        sheet = formulas[sheet_name]
        value_sheet = cached[sheet_name]
        if sheet.max_column and sheet.max_column > MAX_COLUMNS:
            raise ValidationError({"archivo": "La hoja supera el límite de columnas permitido."})
        formula_header = next(sheet.iter_rows(min_row=header_row, max_row=header_row), None)
        value_header = next(value_sheet.iter_rows(min_row=header_row, max_row=header_row), None)
        if formula_header is None or value_header is None:
            raise ValidationError({"header_row": "La fila de encabezados no contiene datos."})
        width = max(len(formula_header), len(value_header))
        headers = [str(cell.value).strip() if cell.value is not None else "" for cell in value_header[:width]]
        if not any(headers):
            raise ValidationError({"header_row": "La fila seleccionada está vacía."})
        indexes = _resolve_mapping(headers, mapping, origin)
        if date_format is not None and (not isinstance(date_format, str) or len(date_format) > 50):
            raise ValidationError({"date_format": "date_format no es válido."})
        date_proposals = _bi_statement_date_proposals(
            path,
            sheet_name=sheet_name,
            header_row=header_row,
            indexes=indexes,
            origin=origin,
            period=period,
            cutoff=cutoff,
        )
        proposals_by_row = {proposal["fila"]: proposal for proposal in date_proposals}

        movimientos = []
        errores = []
        advertencias = []
        exclusiones = []
        rows_seen = 0
        formula_rows = sheet.iter_rows(min_row=header_row + 1, max_col=width)
        value_rows = value_sheet.iter_rows(min_row=header_row + 1, max_col=width)
        for row_number, (formula_row, value_row) in enumerate(zip(formula_rows, value_rows), header_row + 1):
            if row_number - header_row > MAX_DATA_ROWS:
                raise ValidationError({"archivo": f"La hoja supera el máximo de {MAX_DATA_ROWS} filas de datos."})
            values = [cell.value for cell in value_row]
            has_formula = any(
                formula_row[index].data_type == "f"
                for index in indexes.values()
            )
            if not has_formula and not any(value is not None and str(value).strip() for value in values):
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Fila vacía; no se importará como movimiento.",
                })
                continue
            rows_seen += 1
            row_errors = [
                {"campo": field, "mensaje": "La fórmula no tiene un valor calculado guardado."}
                for field, index in indexes.items()
                if formula_row[index].data_type == "f" and values[index] is None
            ]
            if row_errors:
                errores.append({"fila": row_number, "errores": row_errors})
                continue
            required_fields = {"fecha", "monto"} if "monto" in indexes else {"fecha", "salida", "entrada"}
            normalized_header_cells = [
                (field, index)
                for field, index in indexes.items()
                if field in required_fields
            ]
            if normalized_header_cells and all(
                values[index] is not None
                and str(values[index]).strip().casefold() == headers[index].strip().casefold()
                for _, index in normalized_header_cells
            ):
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Encabezado repetido; no se importará como movimiento.",
                })
                continue
            date_value = values[indexes["fecha"]]
            labels = [
                str(value).strip().casefold()
                for value in values
                if isinstance(value, str) and value.strip()
            ]
            is_total_label = any(
                isinstance(value, str)
                and re.match(
                    r"^\s*(total(?:es)?|total general|subtotal|suma|sum|"
                    r"saldo inicial|saldo final|balance inicial|balance actual|"
                    r"saldo disponible)\b",
                    value,
                    re.IGNORECASE,
                )
                for value in values
            )
            amount_fields = ("monto",) if "monto" in indexes else ("salida", "entrada")
            has_amount = any(
                values[indexes[field]] is not None
                and str(values[indexes[field]]).strip()
                for field in amount_fields
            )
            opening_balance_row = (
                not has_amount
                and "saldo_origen" in indexes
                and values[indexes["saldo_origen"]] is not None
                and (
                    any(label in ("si", "saldo inicial", "balance inicial") for label in labels)
                    or date_value is None
                )
            )
            if opening_balance_row:
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Fila de saldo inicial; se conserva como contexto y no se importará como movimiento.",
                })
                continue
            if date_value is None and not has_amount:
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Fila sin fecha ni importe; no se importará como movimiento.",
                })
                continue
            if is_total_label and (date_value is None or not str(date_value).strip()):
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Fila de total o subtotal; no se importará como movimiento.",
                })
                continue
            if isinstance(date_value, str) and re.search(r"[A-Za-zÁÉÍÓÚÜÑ]", date_value):
                advertencias.append({
                    "fila": row_number,
                    "campo": "fecha",
                    "mensaje": "Fila de leyenda, resumen o pie de informe; no se importará como movimiento.",
                })
                continue
            summary_label = any(
                isinstance(value, str)
                and re.match(
                    r"^\s*(cuadro de resumen|d[eé]bitos|cr[eé]ditos|c[oó]digo movimiento|"
                    r"totales|subtotal)\b",
                    value,
                    re.IGNORECASE,
                )
                for value in values
            )
            if summary_label:
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Fila de resumen; no se importará como movimiento.",
                })
                continue
            try:
                movement_date = parse_date(date_value, date_format, "fecha")
            except ValueError as exc:
                errores.append({"fila": row_number, "errores": [{"campo": "fecha", "mensaje": str(exc)}]})
                continue
            date_proposal = proposals_by_row.get(row_number)
            if date_proposal and confirm_date_proposals:
                movement_date = date.fromisoformat(date_proposal["fecha_propuesta"])
            if movement_date < period or movement_date > cutoff:
                exclusion = {
                    "fila": row_number,
                    "fecha": movement_date.isoformat(),
                    "motivo": "fuera_periodo" if movement_date < period or movement_date.month != period.month else "posterior_fecha_corte",
                }
                if date_proposal:
                    exclusion["propuesta_normalizacion"] = date_proposal
                exclusiones.append(exclusion)
                continue
            values_out = {
                "fecha": movement_date.isoformat(),
                "fecha_valor": None,
                "referencia": None,
                "numero_transaccion": None,
                "numero_cheque": None,
                "codigo_tt": None,
                "descripcion": "",
                "monto": None,
                "saldo_origen": None,
            }
            if "fecha_valor" in indexes and values[indexes["fecha_valor"]] not in (None, ""):
                try:
                    values_out["fecha_valor"] = parse_date(values[indexes["fecha_valor"]], date_format, "fecha_valor").isoformat()
                except ValueError as exc:
                    row_errors.append({"campo": "fecha_valor", "mensaje": str(exc)})
            for field in IDENTIFIER_FIELDS:
                if field in indexes:
                    cell = value_row[indexes[field]]
                    value = cell.value
                    if value is not None:
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            numeric = str(int(value)) if float(value).is_integer() else str(value)
                            number_format = cell.number_format or ""
                            zeros = re.fullmatch(r"0+", number_format)
                            value = numeric.zfill(len(number_format)) if zeros else numeric
                            advertencias.append({
                                "fila": row_number,
                                "campo": field,
                                "mensaje": "El identificador proviene de una celda numérica; usa texto para conservar ceros iniciales.",
                            })
                        values_out[field] = str(value).strip() or None
            if "descripcion" in indexes and values[indexes["descripcion"]] is not None:
                values_out["descripcion"] = str(values[indexes["descripcion"]]).strip()
            if "saldo_origen" in indexes:
                try:
                    balance = parse_decimal(values[indexes["saldo_origen"]], "saldo_origen")
                    values_out["saldo_origen"] = format(balance, "f") if balance is not None else None
                except ValueError as exc:
                    row_errors.append({"campo": "saldo_origen", "mensaje": str(exc)})
            try:
                if "monto" in indexes:
                    amount = parse_decimal(values[indexes["monto"]], "monto")
                elif origin == "banco":
                    outbound = parse_decimal(values[indexes["salida"]], "salida") or Decimal(0)
                    inbound = parse_decimal(values[indexes["entrada"]], "entrada") or Decimal(0)
                    if outbound < 0 or inbound < 0:
                        raise ValueError("salida y entrada deben contener magnitudes positivas.")
                    if outbound and inbound:
                        raise ValueError("La fila tiene salida y entrada; indique solo una dirección.")
                    amount = inbound - outbound
                else:
                    amount = None
                if amount is None or amount == 0:
                    raise ValueError("El movimiento debe tener un monto distinto de cero.")
                values_out["monto"] = format(amount, "f")
            except ValueError as exc:
                row_errors.append({"campo": "monto", "mensaje": str(exc)})
            if row_errors:
                errores.append({"fila": row_number, "errores": row_errors})
                continue
            values_out["fila_origen"] = row_number
            values_out["datos_originales"] = {
                headers[i] or f"columna_{i + 1}": _json_value(value)
                for i, value in enumerate(values)
                if i < len(headers)
            }
            if date_proposal and confirm_date_proposals:
                values_out["datos_originales"]["_normalizacion_fecha"] = date_proposal
            movimientos.append(values_out)
        formulas.close()
        cached.close()
        return ValidatedWorkbook(
            movimientos,
            errores,
            advertencias,
            exclusiones,
            rows_seen,
            date_proposals,
            date_proposals if confirm_date_proposals else [],
        )
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError({"archivo": "No se pudo validar la hoja seleccionada."}) from exc


def create_preview(upload, *, user_id, scope, reconciliation_id=None, origin):
    extension = Path(upload.name).suffix.lower()
    if extension not in (".xlsx", ".xls"):
        raise ValidationError({"archivo": "Solo se admiten libros .xlsx y .xls."})
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError({"archivo": "El archivo supera el límite de 10 MiB."})
    root = Path(settings.IMPORT_PREVIEW_STORAGE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    cleanup_expired_previews(root)
    preview_id = uuid4()
    folder = root / str(preview_id)
    folder.mkdir(mode=0o700)
    source_path = folder / f"original{extension}"
    workbook_path = folder / "normalized.xlsx" if extension == ".xls" else source_path
    try:
        digest = hashlib.sha256()
        total = 0
        with source_path.open("xb") as destination:
            for chunk in upload.chunks():
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise ValidationError({"archivo": "El archivo supera el límite de 10 MiB."})
                digest.update(chunk)
                destination.write(chunk)
        detected_format = _file_format(source_path)
        if detected_format == "xls":
            _normalize_xls(source_path, workbook_path)
        parsed_digest = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
        metadata = {
            "id": str(preview_id),
            "user_id": str(user_id),
            "empresa_id": scope["company_id"],
            "pais_codigo": scope["country_code"],
            "cuenta_id": str(scope["account_id"]) if scope.get("account_id") else None,
            "conciliacion_id": str(reconciliation_id) if reconciliation_id else None,
            "origen": origin,
            "nombre_archivo": Path(upload.name).name[:255],
            "formato": detected_format,
            "sha256": digest.hexdigest(),
            "workbook_sha256": parsed_digest,
            "expira_en": (datetime.now().astimezone().timestamp() + settings.IMPORT_PREVIEW_TTL_SECONDS),
        }
        (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        metadata["_source_path"] = str(source_path)
        return preview_id, metadata, workbook_path
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def cleanup_expired_previews(root=None):
    root = Path(root or settings.IMPORT_PREVIEW_STORAGE_DIR)
    if not root.exists():
        return
    now = datetime.now().astimezone().timestamp()
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        try:
            metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            expired = float(metadata.get("expira_en", 0)) <= now
        except (OSError, ValueError, TypeError):
            expired = True
        if expired:
            shutil.rmtree(folder, ignore_errors=True)


def get_preview(preview_id, *, user_id, scope, reconciliation_id, allow_unbound=False):
    try:
        preview_uuid = UUID(str(preview_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise NotFound("La vista previa no existe o expiró.") from exc
    root = Path(settings.IMPORT_PREVIEW_STORAGE_DIR)
    cleanup_expired_previews(root)
    folder = root / str(preview_uuid)
    try:
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        source_path = folder / f"original{Path(metadata['nombre_archivo']).suffix.lower()}"
        path = folder / ("normalized.xlsx" if metadata.get("formato") == "xls" else source_path.name)
        if (
            metadata["user_id"] != str(user_id)
            or metadata["empresa_id"] != scope["company_id"]
            or metadata["pais_codigo"] != scope["country_code"]
            or (
                metadata["cuenta_id"] != scope.get("account_id")
                and not (allow_unbound and metadata["cuenta_id"] is None)
            )
            or (
                metadata["conciliacion_id"] != (str(reconciliation_id) if reconciliation_id else None)
                and not (allow_unbound and metadata["conciliacion_id"] is None)
            )
            or float(metadata["expira_en"]) <= datetime.now().astimezone().timestamp()
            or not source_path.is_file()
            or not path.is_file()
            or hashlib.sha256(source_path.read_bytes()).hexdigest() != metadata["sha256"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != metadata["workbook_sha256"]
        ):
            raise NotFound("La vista previa no existe o expiró.")
        metadata["_source_path"] = str(source_path)
        return metadata, path
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise NotFound("La vista previa no existe o expiró.") from exc


def bind_preview(preview_id, *, user_id, scope, reconciliation_id):
    try:
        preview_uuid = UUID(str(preview_id))
        reconciliation_uuid = UUID(str(reconciliation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise NotFound("La vista previa no existe o expiró.") from exc
    account_id = scope.get("account_id")
    if not account_id:
        raise NotFound("La vista previa no existe o expiró.")

    folder = Path(settings.IMPORT_PREVIEW_STORAGE_DIR) / str(preview_uuid)
    metadata_path = folder / "metadata.json"
    get_preview(
        preview_uuid,
        user_id=user_id,
        scope=scope,
        reconciliation_id=reconciliation_uuid,
        allow_unbound=True,
    )
    with _preview_metadata_lock(folder / ".metadata.lock"):
        metadata, _ = get_preview(
            preview_uuid,
            user_id=user_id,
            scope=scope,
            reconciliation_id=reconciliation_uuid,
            allow_unbound=True,
        )
        expected_account = str(account_id)
        expected_reconciliation = str(reconciliation_uuid)
        stored_account = metadata["cuenta_id"]
        stored_reconciliation = metadata["conciliacion_id"]
        if (stored_account, stored_reconciliation) == (None, None):
            metadata["cuenta_id"] = expected_account
            metadata["conciliacion_id"] = expected_reconciliation
            metadata.pop("_source_path", None)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=folder,
                    prefix=".metadata-",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    json.dump(metadata, temporary, ensure_ascii=False)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, metadata_path)
            except OSError:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                raise
        elif (stored_account, stored_reconciliation) != (
            expected_account,
            expected_reconciliation,
        ):
            raise NotFound("La vista previa no existe o expiró.")

    return get_preview(
        preview_uuid,
        user_id=user_id,
        scope={**scope, "account_id": expected_account},
        reconciliation_id=reconciliation_uuid,
    )
