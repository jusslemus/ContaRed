import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
from django.conf import settings
from openpyxl import load_workbook
from rest_framework.exceptions import NotFound, ValidationError


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


@dataclass(frozen=True)
class ValidatedWorkbook:
    movimientos: list[dict]
    errores: list[dict]
    advertencias: list[dict]
    exclusiones_requeridas: list[dict]
    filas_analizadas: int


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
        raise ValueError(f"{field_name}: use una fecha de Excel o texto con formato explícito.")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}: la fecha es obligatoria.")
    text = value.strip()
    formats = [date_format] if date_format else ["%Y-%m-%d", "%Y/%m/%d"]
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


def workbook_preview(path):
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
        return {"hojas": sheets}
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


def validate_workbook(path, *, sheet_name, header_row, mapping, date_format, origin, period, cutoff):
    if origin not in ("banco", "libros"):
        raise ValidationError({"origen": "El origen debe ser banco o libros."})
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
            is_total_label = any(
                isinstance(value, str)
                and re.match(r"^\s*(total(?:es)?|total general|subtotal|suma|sum)\b", value, re.IGNORECASE)
                for value in values
            )
            if is_total_label and (date_value is None or not str(date_value).strip()):
                advertencias.append({
                    "fila": row_number,
                    "campo": "fila",
                    "mensaje": "Fila de total o subtotal; no se importará como movimiento.",
                })
                continue
            try:
                movement_date = parse_date(date_value, date_format, "fecha")
            except ValueError as exc:
                errores.append({"fila": row_number, "errores": [{"campo": "fecha", "mensaje": str(exc)}]})
                continue
            if movement_date < period or movement_date > cutoff:
                exclusiones.append({
                    "fila": row_number,
                    "fecha": movement_date.isoformat(),
                    "motivo": "fuera_periodo" if movement_date < period or movement_date.month != period.month else "posterior_fecha_corte",
                })
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
            movimientos.append(values_out)
        formulas.close()
        cached.close()
        return ValidatedWorkbook(movimientos, errores, advertencias, exclusiones, rows_seen)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError({"archivo": "No se pudo validar la hoja seleccionada."}) from exc


def create_preview(upload, *, user_id, scope, reconciliation_id, origin):
    if not upload.name.lower().endswith(".xlsx"):
        raise ValidationError({"archivo": "Solo se admiten libros .xlsx."})
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError({"archivo": "El archivo supera el límite de 10 MiB."})
    root = Path(settings.IMPORT_PREVIEW_STORAGE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    cleanup_expired_previews(root)
    preview_id = uuid4()
    folder = root / str(preview_id)
    folder.mkdir(mode=0o700)
    file_path = folder / "original.xlsx"
    try:
        digest = hashlib.sha256()
        total = 0
        with file_path.open("xb") as destination:
            for chunk in upload.chunks():
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise ValidationError({"archivo": "El archivo supera el límite de 10 MiB."})
                digest.update(chunk)
                destination.write(chunk)
        metadata = {
            "id": str(preview_id),
            "user_id": str(user_id),
            "empresa_id": scope["company_id"],
            "pais_codigo": scope["country_code"],
            "cuenta_id": scope["account_id"],
            "conciliacion_id": str(reconciliation_id),
            "origen": origin,
            "nombre_archivo": Path(upload.name).name[:255],
            "sha256": digest.hexdigest(),
            "expira_en": (datetime.now().astimezone().timestamp() + settings.IMPORT_PREVIEW_TTL_SECONDS),
        }
        (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return preview_id, metadata, file_path
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


def get_preview(preview_id, *, user_id, scope, reconciliation_id):
    try:
        preview_uuid = UUID(str(preview_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise NotFound("La vista previa no existe o expiró.") from exc
    root = Path(settings.IMPORT_PREVIEW_STORAGE_DIR)
    cleanup_expired_previews(root)
    folder = root / str(preview_uuid)
    try:
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        path = folder / "original.xlsx"
        if (
            metadata["user_id"] != str(user_id)
            or metadata["empresa_id"] != scope["company_id"]
            or metadata["pais_codigo"] != scope["country_code"]
            or metadata["cuenta_id"] != scope["account_id"]
            or metadata["conciliacion_id"] != str(reconciliation_id)
            or float(metadata["expira_en"]) <= datetime.now().astimezone().timestamp()
            or not path.is_file()
        ):
            raise NotFound("La vista previa no existe o expiró.")
        return metadata, path
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise NotFound("La vista previa no existe o expiró.") from exc
