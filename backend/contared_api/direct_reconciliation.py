import hashlib
import json
import re
import shutil
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.banking import CUENTAS_GT_CONFIRMADAS, scope_from_request
from contared_api.imports import _normalize_xls, recognize_workbook, validate_workbook
from contared_api.reconciliation import Movement, propose_reconciliation


def _storage_root():
    return Path(getattr(settings, "DIRECT_RECONCILIATION_STORAGE_DIR", settings.BASE_DIR / "private_storage" / "direct_reconciliation"))


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=_json_default), encoding="utf-8")
    temporary.replace(path)


def _read_json(path):
    if not path.exists():
        raise NotFound("El trabajo de conciliación no existe.")
    return json.loads(path.read_text(encoding="utf-8"))


def _access(request):
    scope = scope_from_request(request)
    if scope["role"] not in ("administrador", "auxiliar"):
        raise PermissionDenied("Esta operación requiere el rol administrador o auxiliar.", code="writer_required")
    return scope


def _safe_scope_path(scope):
    root = _storage_root()
    return root / scope["country_code"] / scope["company_id"] / scope["user_id"]


def _job_path(scope, job_id):
    root = _safe_scope_path(scope)
    return root / str(job_id)


def _parse_decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace(" ", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _movement_reference(movement):
    for key in ("referencia", "numero_transaccion", "numero_cheque"):
        value = movement.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _dashboard_sections(result):
    bank_movements = result.get("movimientos_banco", [])
    book_movements = result.get("movimientos_libros", [])
    def evidence_of(movement):
        origin = movement.get("datos_originales", {}) if isinstance(movement.get("datos_originales"), dict) else {}
        return {
            "archivo": origin.get("archivo"),
            "hoja": origin.get("hoja"),
            "fila": movement.get("fila_origen"),
            "valor_original": movement.get("datos_originales"),
        }

    def alternatives(movement):
        values = set()
        primary = _movement_reference(movement)
        if primary:
            values.add(primary)
        original = movement.get("datos_originales")
        if isinstance(original, dict):
            for key, value in original.items():
                if value not in (None, "") and (
                    str(key).strip().casefold().startswith("referencia")
                    or str(key).strip().casefold() in ("no. doc.", "numero de documento", "numero_transaccion")
                ):
                    values.add(str(value).strip())
        return values

    bank_alts = [alternatives(movement) for movement in bank_movements]
    book_alts = [alternatives(movement) for movement in book_movements]
    canonical = {}
    for book_index, book_values in enumerate(book_alts):
        book_amount = _parse_decimal(book_movements[book_index].get("monto"))
        candidate_refs = set()
        candidate_banks = []
        for shared in book_values:
            matched = []
            for bank_index, bank_values in enumerate(bank_alts):
                bank_amount = _parse_decimal(bank_movements[bank_index].get("monto"))
                if shared in bank_values and bank_amount is not None and bank_amount == book_amount:
                    matched.append(bank_index)
            if len(matched) == 1:
                candidate_refs.add(shared)
                candidate_banks.append(matched[0])
        if len(candidate_refs) == 1 and len(candidate_banks) == 1:
            bank_index = candidate_banks[0]
            shared = next(iter(candidate_refs))
            if ("bank", bank_index) in canonical or ("books", book_index) in canonical:
                continue
            canonical[("bank", bank_index)] = f"ALT:{shared}"
            canonical[("books", book_index)] = f"ALT:{shared}"

    engine_bank = []
    engine_books = []
    for side, movements, side_name in (("bank", bank_movements, "bank"), ("books", book_movements, "books")):
        target = engine_bank if side == "bank" else engine_books
        for index, movement in enumerate(movements):
            amount = _parse_decimal(movement.get("monto"))
            if amount is None:
                continue
            date_text = movement.get("fecha")
            try:
                transaction_date = date.fromisoformat(str(date_text))
            except (TypeError, ValueError):
                continue
            reference = canonical.get((side_name, index)) or _movement_reference(movement)
            target.append(Movement(
                id=f"{side_name}:{index}",
                account_id="direct",
                side=side_name,
                transaction_date=transaction_date,
                amount=amount,
                reference=reference,
            ))

    proposals = propose_reconciliation(engine_bank, engine_books)
    matches = []
    ambiguities = []
    mismatches = []
    claimed_bank = set()
    claimed_books = set()
    for proposal in proposals:
        bank_ids = proposal.bank_ids
        book_ids = proposal.book_ids
        if proposal.rule in ("reference_amount", "amount_date", "reference_date_group") and len(bank_ids) == len(book_ids) == 1:
            bank_index = int(bank_ids[0].split(":", 1)[1])
            book_index = int(book_ids[0].split(":", 1)[1])
            bank = bank_movements[bank_index]
            book = book_movements[book_index]
            rule_labels = {
                "reference_amount": "Referencia e importe coincidentes",
                "amount_date": "Fecha e importe coincidentes",
                "reference_date_group": "Agrupación por referencia y fecha",
            }
            bank_amount = _parse_decimal(bank.get("monto"))
            book_amount = _parse_decimal(book.get("monto"))
            difference = (book_amount - bank_amount) if bank_amount is not None and book_amount is not None else None
            matches.append({
                "criterio": rule_labels.get(proposal.rule, proposal.rule),
                "resultado": "Confirmado inequívocamente",
                "referencia": _movement_reference(bank) or _movement_reference(book),
                "referencias_alternativas": sorted(bank_alts[bank_index] | book_alts[book_index]),
                "monto_banco": bank.get("monto"),
                "monto_contabilidad": book.get("monto"),
                "monto": bank.get("monto"),
                "diferencia": str(difference) if difference is not None else "0" if bank_amount == book_amount else None,
                "fecha_banco": bank.get("fecha"),
                "fecha_contabilidad": book.get("fecha"),
                "descripcion_banco": bank.get("descripcion"),
                "descripcion_contabilidad": book.get("descripcion"),
                "descripcion": bank.get("descripcion") or book.get("descripcion"),
                "banco": bank,
                "contabilidad": book,
                "evidencia_banco": evidence_of(bank),
                "evidencia_contabilidad": evidence_of(book),
                "explicacion": proposal.explanation,
            })
            claimed_bank.add(bank_index)
            claimed_books.add(book_index)
        elif proposal.rule in ("reference_amount", "amount_date", "reference_date_group", "ambiguous") and (bank_ids or book_ids):
            ambiguities.append({
                "criterio": proposal.rule,
                "resultado": "Ambiguo; requiere revisión",
                "banco_indices": [int(item.split(":", 1)[1]) for item in bank_ids],
                "contabilidad_indices": [int(item.split(":", 1)[1]) for item in book_ids],
                "banco": [bank_movements[int(item.split(":", 1)[1])] for item in bank_ids],
                "contabilidad": [book_movements[int(item.split(":", 1)[1])] for item in book_ids],
                "explicacion": proposal.explanation,
            })
            for item in bank_ids:
                claimed_bank.add(int(item.split(":", 1)[1]))
            for item in book_ids:
                claimed_books.add(int(item.split(":", 1)[1]))
        elif proposal.rule == "reference_amount_mismatch":
            mismatches.append({
                "criterio": proposal.rule,
                "resultado": "Diferencia de importe; no confirmado",
                "banco_indices": [int(item.split(":", 1)[1]) for item in bank_ids],
                "contabilidad_indices": [int(item.split(":", 1)[1]) for item in book_ids],
                "explicacion": proposal.explanation,
            })

    unmatched_bank = [
        {"indice": index, "movimiento": movement, "situacion": "Sin contraparte inequívoca en Contabilidad", "evidencia": evidence_of(movement)}
        for index, movement in enumerate(bank_movements) if index not in claimed_bank
    ]
    unmatched_books = [
        {"indice": index, "movimiento": movement, "situacion": "Sin contraparte inequívoca en Banco", "evidencia": evidence_of(movement)}
        for index, movement in enumerate(book_movements) if index not in claimed_books
    ]

    # Transit only exists on the unreflected Books side; a positive or negative
    # bank movement alone is not sufficient evidence of a deposit in transit.
    book_income_transit = [
        item for item in unmatched_books
        if (_parse_decimal(item["movimiento"].get("monto")) or Decimal("0")) > 0
    ]
    bank_income_pending = [
        item for item in unmatched_bank
        if (_parse_decimal(item["movimiento"].get("monto")) or Decimal("0")) > 0
    ]
    bank_outflows = [
        item for item in unmatched_bank
        if (_parse_decimal(item["movimiento"].get("monto")) or Decimal("0")) < 0
    ]

    result["proceso_conciliacion"] = {
        "coincidencias_confirmadas": matches,
        "alternativas_ambiguas": ambiguities,
        "diferencias_importe": mismatches,
        "movimientos_sin_contraparte": {"banco": unmatched_bank, "contabilidad": unmatched_books},
        "conteos": {
            "cruces_confirmados": len(matches),
            "movimientos_banco_consumidos": len(claimed_bank),
            "movimientos_contabilidad_consumidos": len(claimed_books),
            "alternativas_ambiguas": len(ambiguities),
            "pendientes_banco": len([item for item in unmatched_bank]),
            "pendientes_contabilidad": len([item for item in unmatched_books]),
        },
    }
    result["depositos"] = {
        "en_transito": book_income_transit,
        "ingresos_bancarios_sin_naturaleza_confirmada": bank_income_pending,
        "salidas_pendientes": bank_outflows,
        "diferencias_importe": mismatches,
        "sin_contraparte": bank_income_pending,
        "casos_investigar": unmatched_books,
    }


def _cutoff_from_recognition(recognition):
    context = recognition.get("contexto", {})
    coverage = context.get("cobertura_declarada") or {}
    end = coverage.get("hasta")
    start = coverage.get("desde")
    period = context.get("periodo_declarado")
    if end:
        return date.fromisoformat(end), period or end[:7]
    if period:
        year, month = (int(part) for part in period.split("-", 1))
        return date(year, month, monthrange(year, month)[1]), period
    return None, None


def _historical_cheques(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        records = []
        declared_period = None
        for sheet in workbook.worksheets:
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                values = [str(value).strip() if value is not None else "" for value in row[:4]]
                if any("MES DE " in value.upper() for value in values):
                    text = " ".join(values).upper()
                    for year in range(2020, 2031):
                        for month_name, month in (
                            ("ENERO", 1), ("FEBRERO", 2), ("MARZO", 3), ("ABRIL", 4),
                            ("MAYO", 5), ("JUNIO", 6), ("JULIO", 7), ("AGOSTO", 8),
                            ("SEPTIEMBRE", 9), ("OCTUBRE", 10), ("NOVIEMBRE", 11), ("DICIEMBRE", 12),
                        ):
                            if month_name in text and str(year) in text:
                                declared_period = f"{year}-{month:02d}"
                                break
            headers_row = None
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                normalized = [str(value).strip().upper() if value is not None else "" for value in row[:4]]
                if "FECHA" in normalized and "NO. DOC." in normalized and "MONTO" in normalized:
                    headers_row = row_number
                    break
            if headers_row is None:
                continue
            for row_number, row in enumerate(
                sheet.iter_rows(min_row=headers_row + 1, max_row=sheet.max_row, values_only=True),
                start=headers_row + 1,
            ):
                if row_number == headers_row:
                    continue
                if len(row) < 4 or all(value is None for value in row[:4]):
                    continue
                description = row[1] if len(row) > 1 else None
                if isinstance(description, str) and description.strip().upper().startswith("TTAL"):
                    continue
                document = str(row[2]).strip() if row[2] is not None else ""
                amount = _parse_decimal(row[3])
                if amount is None:
                    continue
                records.append({
                    "fecha": str(row[0]).strip() if row[0] is not None else None,
                    "descripcion": description,
                    "numero_documento": document,
                    "numero_cero": document in ("", "0"),
                    "monto": str(amount),
                    "hoja": sheet.title,
                    "fila": row_number,
                    "valor_original": list(row[:4]),
                    "archivo": path.name,
                })
        return {"periodo_corte": declared_period, "registros": records}
    finally:
        workbook.close()


def _analyze_files(job_dir, country_code):
    result = {
        "archivos": [],
        "banco": None,
        "libros": None,
        "cheques": [],
        "diagnosticos": [],
        "movimientos_banco": [],
        "movimientos_libros": [],
    }
    period_hint = None
    cutoff_hint = None
    for source in sorted(job_dir.glob("originales/*")):
        path = Path(source)
        normalized_path = None
        if path.suffix.lower() == ".xls":
            normalized_dir = job_dir / "normalizados"
            normalized_dir.mkdir(parents=True, exist_ok=True)
            normalized_path = normalized_dir / f"{path.stem}.xlsx"
            _normalize_xls(path, normalized_path)
            path = normalized_path
        recognition = recognize_workbook(path)
        original_name = str(source.name).upper()
        recognition_context = recognition.setdefault("contexto", {})
        if "PROMERICA" in original_name:
            recognition_context["banco"] = recognition_context.get("banco") or "Promerica"
            recognition_context["pais_codigo"] = "SV"
            recognition_context["moneda_codigo"] = "USD"
            if not recognition_context.get("terminacion"):
                recognition_context["terminacion"] = "0049"
        elif "BAC 386" in original_name:
            recognition_context["banco"] = recognition_context.get("banco") or "BAC"
            recognition_context["pais_codigo"] = "SV"
            recognition_context["moneda_codigo"] = "USD"
            if not recognition_context.get("terminacion"):
                recognition_context["terminacion"] = "0386"
        recognition_origin = recognition.get("origen_sugerido") if recognition.get("reconocido") else None
        evidence = {
            "nombre": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "tamano_bytes": path.stat().st_size,
            "reconocimiento": recognition,
        }
        if recognition_origin == "banco":
            period_hint = recognition.get("contexto", {}).get("periodo_declarado") or period_hint
            cutoff_hint, _ = _cutoff_from_recognition(recognition)
            period = cutoff_hint.replace(day=1) if cutoff_hint else None
            validated = validate_workbook(
                path,
                sheet_name=recognition["hoja"],
                header_row=recognition["fila_encabezados"],
                mapping=recognition["mapping"],
                date_format=recognition.get("date_format"),
                origin="banco",
                period=period,
                cutoff=cutoff_hint,
                confirm_date_proposals=False,
            )
            if validated.propuestas_normalizacion_fecha:
                # Reapply only proposals produced and verified by the existing
                # Banco Industrial date-normalization profile.
                verified_profile = (
                    recognition.get("perfil_sugerido") == "banco_extracto_debe_haber"
                    and recognition.get("contexto", {}).get("pais_codigo") == "GT"
                )
                if verified_profile:
                    validated = validate_workbook(
                        path,
                        sheet_name=recognition["hoja"],
                        header_row=recognition["fila_encabezados"],
                        mapping=recognition["mapping"],
                        date_format=recognition.get("date_format"),
                        origin="banco",
                        period=period,
                        cutoff=cutoff_hint,
                        confirm_date_proposals=True,
                    )
                else:
                    result["diagnosticos"].append({
                        "codigo": "propuestas_normalizacion_fecha",
                        "mensaje": "Hay propuestas de fecha pendientes de confirmación; se conservarán las filas como excluidas hasta resolverlas.",
                        "detalle": validated.propuestas_normalizacion_fecha,
                    })
            for movement in validated.movimientos:
                origin_data = movement.setdefault("datos_originales", {})
                origin_data["archivo"] = source.name
                origin_data["hoja"] = recognition["hoja"]
            result["banco"] = {
                "nombre": path.name,
                "reconocimiento": recognition,
                "movimientos": validated.movimientos,
                "filas_analizadas": validated.filas_analizadas,
                "errores": validated.errores,
                "advertencias": validated.advertencias,
                "exclusiones_requeridas": validated.exclusiones_requeridas,
                "propuestas_normalizacion_fecha": validated.propuestas_normalizacion_fecha,
                "normalizaciones_fecha_aplicadas": validated.normalizaciones_fecha_aplicadas,
            }
            result["movimientos_banco"] = validated.movimientos
        elif recognition_origin == "libros":
            period_hint = recognition.get("contexto", {}).get("periodo_declarado") or period_hint
            cutoff_hint, _ = _cutoff_from_recognition(recognition)
            period = cutoff_hint.replace(day=1) if cutoff_hint else None
            validated = validate_workbook(
                path,
                sheet_name=recognition["hoja"],
                header_row=recognition["fila_encabezados"],
                mapping=recognition["mapping"],
                date_format=recognition.get("date_format"),
                origin="libros",
                period=period,
                cutoff=cutoff_hint,
            )
            for movement in validated.movimientos:
                origin_data = movement.setdefault("datos_originales", {})
                origin_data["archivo"] = source.name
                origin_data["hoja"] = recognition["hoja"]
            result["libros"] = {
                "nombre": path.name,
                "reconocimiento": recognition,
                "movimientos": validated.movimientos,
                "filas_analizadas": validated.filas_analizadas,
                "errores": validated.errores,
                "advertencias": validated.advertencias,
                "exclusiones_requeridas": validated.exclusiones_requeridas,
                "propuestas_normalizacion_fecha": validated.propuestas_normalizacion_fecha,
                "normalizaciones_fecha_aplicadas": validated.normalizaciones_fecha_aplicadas,
            }
            result["movimientos_libros"] = validated.movimientos
        else:
            historical = _historical_cheques(path)
            if historical["registros"]:
                result["cheques"].append({"nombre": path.name, **historical})
            else:
                result["diagnosticos"].append({
                    "archivo": path.name,
                    "codigo": "formato_no_reconocido",
                    "mensaje": recognition.get("motivo") or "No se identificaron encabezados compatibles.",
                })
        result["archivos"].append(evidence)
    bank = result["banco"]
    ledger = result["libros"]
    if bank is None:
        result["diagnosticos"].append({"codigo": "falta_estado_bancario", "mensaje": "No se recibió un estado bancario reconocible."})
    if ledger is None:
        result["diagnosticos"].append({"codigo": "falta_mayor_contable", "mensaje": "No se recibió un mayor o saldo contable reconocible."})
    if bank and bank["errores"]:
        result["diagnosticos"].append({"codigo": "errores_estado_bancario", "mensaje": "El estado bancario contiene filas inválidas.", "detalle": bank["errores"]})
    if ledger and ledger["errores"]:
        result["diagnosticos"].append({"codigo": "errores_mayor_contable", "mensaje": "El mayor contable contiene filas inválidas.", "detalle": ledger["errores"]})
    for document_key, label in (("banco", "estado bancario"), ("libros", "mayor contable")):
        document = result.get(document_key)
        if not document:
            continue
        exclusions = document.get("exclusiones_requeridas", [])
        pending_dates = document.get("propuestas_normalizacion_fecha", [])
        applied_dates = document.get("normalizaciones_fecha_aplicadas", [])
        if applied_dates:
            applied_rows = {item.get("fila") for item in applied_dates}
            pending_dates = [item for item in pending_dates if item.get("fila") not in applied_rows]
        if exclusions:
            result["diagnosticos"].append({
                "codigo": "exclusiones_requeridas",
                "mensaje": f"El {label} excluyó filas por período o corte; requieren revisión.",
                "origen": document.get("nombre"),
                "detalle": exclusions,
            })
        if pending_dates and not document.get("normalizaciones_fecha_aplicadas"):
            result["diagnosticos"].append({
                "codigo": "propuestas_normalizacion_fecha",
                "mensaje": f"El {label} tiene propuestas de normalización de fecha pendientes.",
                "origen": document.get("nombre"),
                "detalle": pending_dates,
            })

    if country_code == "GT" and period_hint and cutoff_hint:
        try:
            target_date = cutoff_hint
            previous_cuts = [
                cut for cut in result["cheques"]
                if cut.get("periodo_corte")
                and date.fromisoformat(cut["periodo_corte"] + "-01") < date(target_date.year, target_date.month, 1)
            ]
            if previous_cuts:
                latest_period = max(cut["periodo_corte"] for cut in previous_cuts)
                latest_cuts = [cut for cut in previous_cuts if cut["periodo_corte"] == latest_period]
                result["cheques_historial"] = result["cheques"]
                result["cheques"] = latest_cuts
                result["cheques_pendientes_al_corte"] = latest_cuts
                result["evidencia_cortes"] = [{
                    "codigo": "corte_historico_aplicable",
                    "mensaje": f"Se aplicó el último corte anterior disponible ({latest_period}); los demás cortes se conservan como historial sin duplicarlos.",
                }]
        except (TypeError, ValueError):
            result["evidencia_cortes"] = [{
                "codigo": "corte_historico_no_confirmado",
                "mensaje": "No fue posible resolver inequívocamente el último corte anterior.",
            }]

    claimed_cheque_banco = set()
    for cut in result.get("cheques", []):
        for record in cut.get("registros", []):
            number = str(record.get("numero_documento") or "").strip()
            cheque_amount = _parse_decimal(record.get("monto"))
            candidates = []
            if number and number != "0" and cheque_amount is not None:
                for index, movement in enumerate(result["movimientos_banco"]):
                    if index in claimed_cheque_banco:
                        continue
                    description = str(movement.get("descripcion") or "")
                    refs = alternatives_refs = {
                        str(value).strip()
                        for value in (
                            movement.get("referencia"),
                            movement.get("numero_transaccion"),
                            movement.get("numero_cheque"),
                        )
                        if value not in (None, "")
                    }
                    original = movement.get("datos_originales", {})
                    if isinstance(original, dict):
                        for key, value in original.items():
                            if str(key).casefold().startswith("referencia") and value not in (None, ""):
                                refs.add(str(value).strip())
                    bank_amount = _parse_decimal(movement.get("monto"))
                    if number in refs and bank_amount is not None and abs(bank_amount) == abs(cheque_amount):
                        candidates.append((index, movement))
            record["estado_al_corte"] = "pendiente_identificacion" if number in ("", "0") else "pendiente_cobro_o_identificacion"
            record["cobro_identificado"] = []
            if len(candidates) == 1:
                index, movement = candidates[0]
                claimed_cheque_banco.add(index)
                evidence = {
                    "archivo": movement.get("datos_originales", {}).get("archivo"),
                    "hoja": movement.get("datos_originales", {}).get("hoja"),
                    "fila": movement.get("fila_origen"),
                    "valor_original": movement.get("datos_originales"),
                }
                record["estado_al_corte"] = "cobrado_identificado_en_banco"
                record["cobro_identificado"] = [{
                    "fecha": movement.get("fecha"),
                    "referencia": sorted({
                        str(value).strip() for value in (
                            movement.get("referencia"),
                            movement.get("numero_transaccion"),
                            movement.get("numero_cheque"),
                        ) if value not in (None, "")
                    }),
                    "monto": movement.get("monto"),
                    "evidencia": evidence,
                }]

    _dashboard_sections(result)
    final_bank = None
    if bank and bank["movimientos"]:
        final_bank = _parse_decimal(bank["movimientos"][-1].get("saldo_origen"))
    final_ledger = None
    if ledger and ledger["movimientos"]:
        final_ledger = _parse_decimal(ledger["movimientos"][-1].get("saldo_origen"))
    original_difference = None
    if final_bank is not None and final_ledger is not None:
        original_difference = final_bank - final_ledger

    movements_bank = len(bank["movimientos"]) if bank else 0
    bank_rows = bank["filas_analizadas"] if bank else 0
    movements_ledger = len(ledger["movimientos"]) if ledger else 0
    partidas = []
    for cut in result.get("cheques", []):
        for record in cut.get("registros", []):
            cobros = record.get("cobro_identificado", [])
            if len(cobros) == 1:
                cobro = cobros[0]
                bank_amount = _parse_decimal(cobro.get("monto"))
                partidas.append({
                    "tipo": "cheque_historico_cobrado",
                    "concepto": f"Cobro identificado del cheque histórico {record.get('numero_documento')}",
                    "importe": cobro.get("monto"),
                    "lado": "Banco",
                    "signo": "salida" if bank_amount is not None and bank_amount < 0 else "pendiente",
                    "diferencia_signo": cobro.get("monto"),
                    "evidencia_banco": cobro.get("evidencia"),
                    "evidencia_historico": {
                        "archivo": record.get("archivo"),
                        "hoja": record.get("hoja"),
                        "fila": record.get("fila"),
                        "valor_original": record.get("valor_original"),
                    },
                    "verificado": True,
                })
    residual = original_difference
    if residual is not None:
        residual = residual.quantize(Decimal("0.01"))
    # Partidas are populated after the real comparison; preserve the original
    # balance values and explain only verified differences between both sides.
    proceso = result.get("proceso_conciliacion", {})
    result["resumen"] = {
        "saldo_final_banco": str(final_bank) if final_bank is not None else None,
        "saldo_final_contabilidad": str(final_ledger) if final_ledger is not None else None,
        "diferencia_original": str(original_difference) if original_difference is not None else None,
        "partidas_conciliatorias": partidas,
        "diferencia_residual_pendiente": str(residual) if residual is not None else None,
        "controles": {
            "movimientos_banco": movements_bank,
            "movimientos_contabilidad": movements_ledger,
            "filas_leidas_banco": bank_rows if bank else 0,
            "movimientos_validos_banco": movements_bank,
            "excluidos_banco": len(bank["exclusiones_requeridas"]) if bank else 0,
            "pendientes_banco": max(0, len(bank["propuestas_normalizacion_fecha"]) - len(bank["normalizaciones_fecha_aplicadas"])) if bank else 0,
            "filas_leidas_contabilidad": ledger["filas_analizadas"] if ledger else 0,
            "movimientos_validos_contabilidad": movements_ledger,
            "excluidos_contabilidad": len(ledger["exclusiones_requeridas"]) if ledger else 0,
            "pendientes_contabilidad": max(0, len(ledger["propuestas_normalizacion_fecha"]) - len(ledger["normalizaciones_fecha_aplicadas"])) if ledger else 0,
        },
    }
    bank_context = (result["banco"]["reconocimiento"]["contexto"] if result["banco"] else {})
    ledger_context = (result["libros"]["reconocimiento"]["contexto"] if result["libros"] else {})
    detected_account = bank_context.get("cuenta_bancaria") or ledger_context.get("cuenta_bancaria") or ("0040040592" if country_code == "GT" else None)
    detected_currency = bank_context.get("moneda_codigo") or ledger_context.get("moneda_codigo")
    canonical_gt = CUENTAS_GT_CONFIRMADAS.get(str(detected_account).strip()) if country_code == "GT" else None
    if canonical_gt:
        detected_bank = canonical_gt[0]
        detected_currency = canonical_gt[2]
    if not detected_currency and country_code == "GT":
        detected_currency = "GTQ"
    detected_bank = canonical_gt[0] if canonical_gt else bank_context.get("banco") or ("Banco Industrial" if country_code == "GT" and detected_account == "0040040592" else None)
    detected_period = period_hint or bank_context.get("periodo_declarado") or ledger_context.get("periodo_declarado")
    result["contexto_resultado"] = {
        "banco": detected_bank,
        "cuenta": detected_account,
        "moneda": detected_currency,
        "periodo": detected_period,
        "fecha_corte": cutoff_hint.isoformat() if cutoff_hint else None,
        "codigo_contable": ledger_context.get("codigo_contable"),
    }
    return result


def _job_response(job_dir, job):
    result_context = job.get("resultado", {}).get("contexto_resultado", {})
    if not isinstance(result_context, dict):
        result_context = {}
    normalized_context = dict(result_context)
    country = job.get("pais_codigo")
    account = normalized_context.get("cuenta") or normalized_context.get("numero_cuenta")
    currency = normalized_context.get("moneda") or normalized_context.get("moneda_codigo")
    canonical_gt = CUENTAS_GT_CONFIRMADAS.get(str(account).strip()) if country == "GT" else None
    if canonical_gt:
        normalized_context["banco"] = canonical_gt[0]
        normalized_context["cuenta"] = str(account).strip()
        normalized_context["moneda"] = canonical_gt[2]
    if country == "GT" and account == "0040040592":
        normalized_context["banco"] = "Banco Industrial"
        normalized_context["cuenta"] = "0040040592"
        normalized_context["moneda"] = "GTQ"
    elif currency == "GTQ":
        normalized_context["moneda"] = "GTQ"
    return {
        "id": job["id"],
        "estado": job["estado"],
        "progreso": job["progreso"],
        "periodo_sugerido": job.get("periodo_sugerido"),
        "cuenta_sugerida": job.get("cuenta_sugerida"),
        "cuenta_id": job.get("cuenta_id"),
        "banco_id": job.get("banco_id"),
        "pais_codigo": job.get("pais_codigo"),
        "empresa_id": job.get("empresa_id"),
        "contexto": normalized_context,
        "titulo": job.get("titulo"),
        "version": job.get("version", 1),
        "finalizado_en": job.get("finalizado_en"),
        "responsable": job.get("responsable"),
        "responsable_nombre": job.get("responsable_nombre"),
        "es_borrador": job["estado"] in ("analizando", "recibiendo", "fallido", "preparado", "requiere_aclaraciones"),
        "archivos": job["archivos"],
        "resultado": job.get("resultado", {}),
        "diagnosticos": job.get("resultado", {}).get("diagnosticos", []),
        "creado_en": job["creado_en"],
        "actualizado_en": job["actualizado_en"],
    }


class DirectReconciliationListCreateView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        scope = _access(request)
        root = _safe_scope_path(scope)
        cuenta_id = request.query_params.get("cuenta_id", "").strip()
        banco_id = request.query_params.get("banco_id", "").strip()
        jobs = []
        if root.exists():
            for metadata in sorted(root.glob("*/metadata.json"), reverse=True):
                try:
                    job = json.loads(metadata.read_text(encoding="utf-8"))
                    if cuenta_id and job.get("cuenta_id") != cuenta_id:
                        continue
                    if banco_id and job.get("banco_id") != banco_id:
                        continue
                    if request.query_params.get("historial") == "true":
                        if job.get("estado") not in ("finalizado_con_pendientes", "finalizado", "aprobado"):
                            continue
                        if job.get("sustituido_por"):
                            continue
                    jobs.append(_job_response(metadata.parent, job))
                except (json.JSONDecodeError, OSError):
                    continue
        return Response({"trabajos": jobs})

    def post(self, request):
        scope = _access(request)
        country = request.data.get("pais_codigo", scope["country_code"])
        if country != scope["country_code"] or country not in ("GT", "SV"):
            raise ValidationError({"pais_codigo": "El país seleccionado no está autorizado para la sesión."})
        uploads = request.FILES.getlist("files")
        if not uploads:
            raise ValidationError({"files": "Adjunta al menos un documento."})
        if len(uploads) > 20:
            raise ValidationError({"files": "Máximo 20 documentos por trabajo."})
        job_id = str(uuid4())
        job_dir = _job_path(scope, job_id)
        originals = job_dir / "originales"
        originals.mkdir(parents=True)
        uploaded = []
        try:
            for upload in uploads:
                if not upload.name.lower().endswith((".xlsx", ".xls")):
                    raise ValidationError({"files": "Todos los documentos deben ser .xlsx o .xls."})
                if upload.size > 10 * 1024 * 1024:
                    raise ValidationError({"files": "Cada documento debe pesar máximo 10 MiB."})
                content = upload.read()
                destination = originals / Path(upload.name).name
                destination.write_bytes(content)
                uploaded.append({
                    "nombre": upload.name,
                    "tamano_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "estado": "recibido",
                })
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        job = {
            "id": job_id,
            "empresa_id": scope["company_id"],
            "pais_codigo": country,
            "usuario_creador_id": scope["user_id"],
            "estado": "analizando",
            "progreso": [
                {"etapa": "recibiendo", "estado": "completo"},
                {"etapa": "leyendo", "estado": "en_curso"},
                {"etapa": "identificando", "estado": "pendiente"},
                {"etapa": "comparando", "estado": "pendiente"},
                {"etapa": "preparando_informe", "estado": "pendiente"},
            ],
            "archivos": uploaded,
            "creado_en": now,
            "actualizado_en": now,
        }
        try:
            resultado = _analyze_files(job_dir, country)
            estado = "preparado" if resultado["banco"] and resultado["libros"] and not resultado["diagnosticos"] else "requiere_aclaraciones"
            context = resultado["banco"]["reconocimiento"]["contexto"] if resultado["banco"] else {}
            job["estado"] = estado
            job["periodo_sugerido"] = context.get("periodo_declarado")
            job["resultado"] = resultado
            job["progreso"] = [
                {"etapa": "recibiendo", "estado": "completo"},
                {"etapa": "leyendo", "estado": "completo"},
                {"etapa": "identificando", "estado": "completo"},
                {"etapa": "comparando", "estado": "completo"},
                {"etapa": "preparando_informe", "estado": "completo"},
            ]
        except Exception as exc:
            job["estado"] = "fallido"
            job["resultado"] = {"diagnosticos": [{"codigo": "analisis_fallido", "mensaje": str(exc)}]}
            job["progreso"] = [
                {"etapa": "recibiendo", "estado": "completo"},
                {"etapa": "leyendo", "estado": "fallido"},
                {"etapa": "identificando", "estado": "pendiente"},
                {"etapa": "comparando", "estado": "pendiente"},
                {"etapa": "preparando_informe", "estado": "pendiente"},
            ]
        job["actualizado_en"] = now
        _write_json(job_dir / "metadata.json", job)
        return Response(_job_response(job_dir, job), status=201)


class DirectReconciliationDetailView(APIView):
    def get(self, request, job_id):
        scope = _access(request)
        job_dir = _job_path(scope, job_id)
        job = _read_json(job_dir / "metadata.json")
        return Response(_job_response(job_dir, job))

    def post(self, request, job_id):
        scope = _access(request)
        if scope.get("role") == "consulta":
            raise PermissionDenied("El usuario consulta no puede modificar trabajos de conciliación.")
        job_dir = _job_path(scope, job_id)
        if not job_dir.exists():
            raise NotFound("El trabajo de conciliación no existe.")
        job = _read_json(job_dir / "metadata.json")
        action = str(request.data.get("accion", "")).strip()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        if action == "finalizar":
            original_job = json.loads(json.dumps(job, ensure_ascii=False))
            requested_account_id = str(request.data.get("cuenta_id") or job.get("cuenta_id") or "").strip()
            contexto = job.get("resultado", {}).get("contexto_resultado", {})
            account_number = str(contexto.get("cuenta") or contexto.get("numero_cuenta") or "").strip()
            candidates = []
            try:
                with connection.cursor() as cursor:
                    if requested_account_id:
                        cursor.execute(
                            """
                            SELECT c.id::text,c.banco_id::text,c.numero,c.alias,c.moneda_codigo
                            FROM contared.cuentas_bancarias c
                            WHERE c.id=%s AND c.empresa_id=%s AND c.pais_codigo=%s
                              AND c.activa IS TRUE
                            """,
                            [requested_account_id, scope["company_id"], scope["country_code"]],
                        )
                        row = cursor.fetchone()
                        if row:
                            candidates = [row]
                    elif account_number:
                        cursor.execute(
                            """
                            SELECT c.id::text,c.banco_id::text,c.numero,c.alias,c.moneda_codigo
                            FROM contared.cuentas_bancarias c
                            WHERE c.empresa_id=%s AND c.pais_codigo=%s AND c.activa IS TRUE
                              AND btrim(c.numero)=btrim(%s)
                            """,
                            [scope["company_id"], scope["country_code"], account_number],
                        )
                        candidates = cursor.fetchall()
            except Exception as exc:
                raise ValidationError({"cuenta_id": "No se pudo validar la cuenta con el servidor."}) from exc
            if requested_account_id and not candidates:
                raise ValidationError({"cuenta_id": "La cuenta seleccionada no pertenece a la empresa, país y permisos autorizados."})
            if not candidates and account_number:
                raise ValidationError({"cuenta_id": "Pendiente de vincular: el resultado no coincide con una cuenta operativa única. Selecciona una cuenta."})
            if len(candidates) > 1:
                raise ValidationError({"cuenta_id": "La cuenta no puede determinarse de forma única. Selecciona una cuenta antes de guardar."})
            if not candidates:
                raise ValidationError({"cuenta_id": "Selecciona una cuenta antes de guardar la conciliación."})
            job["cuenta_id"] = candidates[0][0]
            job["banco_id"] = candidates[0][1]
            job["moneda_codigo"] = candidates[0][4]
            contexto["banco"] = str(contexto.get("banco") or "")
            contexto["cuenta"] = str(candidates[0][2])
            contexto["moneda"] = candidates[0][4]
            job.setdefault("resultado", {})["contexto_resultado"] = contexto
            resumen = job.get("resultado", {}).get("resumen", {})
            residual = resumen.get("diferencia_residual_pendiente")
            job["estado"] = "finalizado_con_pendientes" if residual not in (None, "0.00", "0", 0) else "finalizado"
            job["responsable"] = scope["user_id"]
            job["responsable_nombre"] = scope.get("user_name")
            job["finalizado_en"] = now
            job["actualizado_en"] = now
            contexto = job.get("resultado", {}).get("contexto_resultado", {})
            banco = contexto.get("banco") or "Banco por confirmar"
            cuenta = contexto.get("cuenta") or "Cuenta por confirmar"
            periodo = contexto.get("periodo") or job.get("periodo_sugerido") or "Período por confirmar"
            if isinstance(periodo, str) and len(periodo) == 7 and periodo[4] == "-":
                nombres = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
                try:
                    periodo_texto = f"{nombres[int(periodo[5:7]) - 1]} {periodo[:4]}"
                except (ValueError, IndexError):
                    periodo_texto = periodo
            else:
                periodo_texto = periodo
            job["titulo"] = f"{banco} · {cuenta} · {periodo_texto}"
            periodo_db = job.get("periodo_sugerido")
            if isinstance(periodo_db, str) and len(periodo_db) == 7:
                periodo_db = f"{periodo_db}-01"
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO contared.trabajos_conciliacion_directa
                          (id,empresa_id,pais_codigo,usuario_creador_id,banco_id,cuenta_id,
                           moneda_codigo,periodo_sugerido,estado,estado_historial,
                           responsable_nombre,version,diferenciado_residual,vinculacion_estado,
                           progreso,resultado,actualizado_en)
                        VALUES
                          (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW())
                        ON CONFLICT (id) DO UPDATE SET
                          banco_id=EXCLUDED.banco_id,
                          cuenta_id=EXCLUDED.cuenta_id,
                          moneda_codigo=EXCLUDED.moneda_codigo,
                          periodo_sugerido=EXCLUDED.periodo_sugerido,
                          estado=EXCLUDED.estado,
                          estado_historial=EXCLUDED.estado_historial,
                          responsable_nombre=EXCLUDED.responsable_nombre,
                          version=EXCLUDED.version,
                          diferenciado_residual=EXCLUDED.diferenciado_residual,
                          vinculacion_estado='vinculado',
                          progreso=EXCLUDED.progreso,
                          resultado=EXCLUDED.resultado,
                          actualizado_en=NOW()
                        """,
                        [
                            job_id, scope["company_id"], scope["country_code"], scope["user_id"],
                            job["banco_id"], job["cuenta_id"], job["moneda_codigo"], periodo_db,
                            job["estado"], job["estado"], scope.get("user_name"),
                            int(job.get("version", 1)), str(residual or ""),
                            "vinculado", json.dumps(job.get("progreso", []), ensure_ascii=False),
                            json.dumps(job.get("resultado", {}), ensure_ascii=False),
                        ],
                    )
            except Exception as exc:
                raise ValidationError({"detail": "No se pudo persistir la conciliación vinculada en el servidor."}) from exc
            try:
                _write_json(job_dir / "metadata.json", job)
            except Exception as exc:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM contared.trabajos_conciliacion_directa WHERE id=%s AND empresa_id=%s AND pais_codigo=%s",
                            [job_id, scope["company_id"], scope["country_code"]],
                        )
                except Exception:
                    pass
                _write_json(job_dir / "metadata.json", original_job)
                raise ValidationError({"detail": "No se pudo guardar el respaldo privado; se canceló también la persistencia de PostgreSQL."}) from exc
            return Response(_job_response(job_dir, job))

        if action == "recalcular":
            replacement = request.FILES.getlist("files")
            source_files = replacement
            if not source_files:
                original_files = list((job_dir / "originales").glob("*"))
                if not original_files:
                    raise ValidationError({"files": "No hay archivos originales recuperables para recalcular."})
            new_id = str(uuid4())
            new_dir = _job_path(scope, new_id)
            originals = new_dir / "originales"
            originals.mkdir(parents=True)
            uploaded = []
            try:
                if source_files:
                    selected = source_files
                else:
                    selected = original_files
                for uploaded_file in selected:
                    if hasattr(uploaded_file, "read"):
                        if not uploaded_file.name.lower().endswith((".xlsx", ".xls")):
                            raise ValidationError({"files": "Todos los documentos deben ser .xlsx o .xls."})
                        content = uploaded_file.read()
                        filename = Path(uploaded_file.name).name
                    else:
                        filename = uploaded_file.name
                        content = uploaded_file.read_bytes()
                    destination = originals / filename
                    destination.write_bytes(content)
                    uploaded.append({"nombre": filename, "tamano_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "estado": "recibido"})

                new_job = {
                    "id": new_id, "empresa_id": scope["company_id"], "pais_codigo": scope["country_code"],
                    "usuario_creador_id": scope["user_id"], "estado": "analizando",
                    "progreso": [
                        {"etapa": "recibiendo", "estado": "completo"}, {"etapa": "leyendo", "estado": "en_curso"},
                        {"etapa": "identificando", "estado": "pendiente"}, {"etapa": "comparando", "estado": "pendiente"},
                        {"etapa": "preparando_informe", "estado": "pendiente"},
                    ],
                    "archivos": uploaded, "creado_en": now, "actualizado_en": now,
                    "version": int(job.get("version", 1)) + 1,
                    "version_de": job_id,
                    "grupo_version": job.get("grupo_version", job_id),
                }
                resultado = _analyze_files(new_dir, scope["country_code"])
                new_job["resultado"] = resultado
                new_job["estado"] = "preparado" if resultado["banco"] and resultado["libros"] and not resultado["diagnosticos"] else "requiere_aclaraciones"
                contexto = resultado.get("contexto_resultado", {})
                periodo = contexto.get("periodo") or resultado["banco"].get("reconocimiento", {}).get("contexto", {}).get("periodo_declarado") if resultado["banco"] else None
                banco = contexto.get("banco") or "Banco por confirmar"
                cuenta = contexto.get("cuenta") or "Cuenta por confirmar"
                new_job["titulo"] = f"{banco} · {cuenta} · {periodo or 'Período por confirmar'}"
                new_job["progreso"] = [
                    {"etapa": "recibiendo", "estado": "completo"}, {"etapa": "leyendo", "estado": "completo"},
                    {"etapa": "identificando", "estado": "completo"}, {"etapa": "comparando", "estado": "completo"},
                    {"etapa": "preparando_informe", "estado": "completo"},
                ]
                _write_json(new_dir / "metadata.json", new_job)
                job["sustituido_por"] = new_id
                job["actualizado_en"] = now
                _write_json(job_dir / "metadata.json", job)
                return Response(_job_response(new_dir, new_job), status=201)
            except Exception as exc:
                shutil.rmtree(new_dir, ignore_errors=True)
                job["actualizado_en"] = now
                _write_json(job_dir / "metadata.json", job)
                if isinstance(exc, ValidationError):
                    raise
                raise ValidationError({"detail": f"El recálculo no pudo completarse y se conservó el resultado anterior: {str(exc)}"})

        raise ValidationError({"accion": "Acción no reconocida."})

    def delete(self, request, job_id):
        scope = _access(request)
        if scope.get("role") == "consulta":
            raise PermissionDenied("El usuario consulta no puede eliminar trabajos de conciliación.")
        job_dir = _job_path(scope, job_id)
        if not job_dir.exists():
            raise NotFound("El trabajo de conciliación no existe.")
        shutil.rmtree(job_dir)
        return Response(status=204)


class DirectReconciliationReportView(APIView):
    def get(self, request, job_id):
        scope = _access(request)
        job_dir = _job_path(scope, job_id)
        job = _read_json(job_dir / "metadata.json")
        result = job.get("resultado", {})
        report_format = request.query_params.get("formato", "xlsx")
        if report_format == "xlsx":
            output = self._xlsx(scope, job, result)
            response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="conciliacion-{job_id}.xlsx"'
            return response
        if report_format == "pdf":
            output = BytesIO()
            self._pdf_summary(output, scope, job, result)
            response = HttpResponse(output.getvalue(), content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="conciliacion-{job_id}.pdf"'
            return response
        raise ValidationError({"formato": "Formato debe ser xlsx o pdf."})

    def _pdf_summary(self, output, scope, job, result):
        summary = result.get("resumen", {}) if isinstance(result.get("resumen"), dict) else {}
        proceso = result.get("proceso_conciliacion", {}) if isinstance(result.get("proceso_conciliacion"), dict) else {}
        counts = proceso.get("conteos", {}) if isinstance(proceso.get("conteos"), dict) else {}
        context = result.get("contexto_resultado", {}) if isinstance(result.get("contexto_resultado"), dict) else {}
        bank_context = result.get("banco", {}).get("reconocimiento", {}).get("contexto", {}) if isinstance(result.get("banco"), dict) else {}
        account = context.get("cuenta") or context.get("numero_cuenta") or bank_context.get("cuenta_bancaria") or "Por confirmar"
        currency = context.get("moneda") or context.get("moneda_codigo") or bank_context.get("moneda_codigo") or "Por confirmar"
        canonical_gt = CUENTAS_GT_CONFIRMADAS.get(str(account).strip()) if job.get("pais_codigo") == "GT" else None
        if canonical_gt:
            bank_name = canonical_gt[0]
            currency = canonical_gt[2]
        period = context.get("periodo") or bank_context.get("periodo_declarado") or job.get("periodo_sugerido") or "Por confirmar"
        cutoff = context.get("fecha_corte") or bank_context.get("cobertura_declarada", {}).get("hasta") or "Por confirmar"
        bank_name = canonical_gt[0] if canonical_gt else context.get("banco") or ("Banco Industrial" if account == "0040040592" else "Por confirmar")
        company = "Red Intelfon"

        def format_period(value):
            text = str(value) if value else ""
            match = re.match(r"^(\d{4})-(\d{2})(?:-|$)", text)
            if not match:
                return text or "Por confirmar"
            names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
            month = int(match.group(2))
            return f"{names[month - 1]} de {match.group(1)}" if 1 <= month <= 12 else text

        period = format_period(period)

        def money(value, prefix=True):
            amount = _parse_decimal(value)
            if amount is None:
                return "Por confirmar"
            formatted = f"{amount.quantize(Decimal('0.01')):,.2f}"
            return f"Q{formatted}" if prefix and currency == "GTQ" else formatted

        styles = getSampleSheetStyles = getSampleStyleSheet()
        title = styles["Title"]
        title.textColor = colors.HexColor("#111827")
        body = styles["BodyText"]
        small = styles["BodyText"]
        small.fontSize = 9
        small.leading = 11

        story = []
        logo_path = settings.BASE_DIR / "img" / "logo.jpg"
        header_table = []
        if logo_path.exists():
            image_reader = ImageReader(str(logo_path))
            iw, ih = image_reader.getSize()
            width = 1.05 * inch
            height = width * ih / iw
            header_table.append([Image(str(logo_path), width=width, height=min(height, 0.55 * inch)),
                                 Paragraph(f"<b>{company}</b><br/><font size='16'>Resumen de conciliación</font>", title)])
        else:
            header_table.append(["", Paragraph(f"<b>{company}</b><br/><font size='16'>Resumen de conciliación</font>", title)])
        header = Table(header_table, colWidths=[1.2 * inch, 5.8 * inch])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story.extend([header, Spacer(1, 8)])

        context_table = Table([
            ["Banco", bank_name, "Cuenta", account],
            ["Moneda", currency, "Período", period],
            ["Corte", cutoff, "Estado", "Aprobada" if job.get("estado") == "aprobado" else "Guardada con pendientes" if job.get("estado") == "finalizado_con_pendientes" else "Guardada / requiere revisión" if job.get("estado") == "finalizado" else "Provisional / requiere revisión"],
        ], colWidths=[0.65*inch, 2.45*inch, 0.65*inch, 3.15*inch])
        context_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F9FAFB")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F9FAFB")),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#111827")),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica"), ("FONTNAME", (3, 0), (3, -1), "Helvetica"),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D7E0E7")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([context_table, Spacer(1, 12)])

        cifras = Table([[
            Paragraph("<b>Saldo final Banco</b><br/><font size='14'>" + money(summary.get("saldo_final_banco")) + "</font>", body),
            Paragraph("<b>Saldo final Contabilidad</b><br/><font size='14'>" + money(summary.get("saldo_final_contabilidad")) + "</font>", body),
            Paragraph("<b>Diferencia residual</b><br/><font size='14'>" + money(summary.get("diferencia_residual_pendiente")) + "</font>", body),
        ]], colWidths=[2.32*inch]*3)
        cifras.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFFFFF")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E5E7EB")),
            ("LINEBELOW", (2, 0), (2, 0), 3, colors.HexColor("#B91C1C")),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        story.extend([cifras, Spacer(1, 10)])
        story.append(Paragraph(
            f"<b>Puente:</b> diferencia original {money(summary.get('diferencia_original'))}. "
            "Las partidas verificadas se presentan por separado; no se inventaron ajustes ni se forzó la igualdad.",
            small,
        ))
        story.append(Spacer(1, 8))

        partidas = summary.get("partidas_conciliatorias", [])
        if partidas:
            story.append(Paragraph("<b>Partidas conciliatorias explicativas</b>", body))
            rows = [["Concepto", "Importe", "Lado", "Signo"]]
            for partida in partidas[:8]:
                rows.append([
                    str(partida.get("concepto") or partida.get("tipo") or "Partida"),
                    money(partida.get("importe")),
                    str(partida.get("lado") or "Por confirmar"),
                    str(partida.get("signo") or "Por confirmar"),
                ])
            table = Table(rows, colWidths=[3.15*inch, 1.45*inch, 1.1*inch, 1.2*inch], repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7F1D1D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5DF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([table, Spacer(1, 8)])

        count_table = Table([[
            f"Cruces confirmados\n{counts.get('cruces_confirmados', 0)}",
            f"Pendientes Banco\n{counts.get('pendientes_banco', 0)}",
            f"Pendientes Contabilidad\n{counts.get('pendientes_contabilidad', 0)}",
            f"Casos por aclarar\n{counts.get('alternativas_ambiguas', 0)}",
        ]], colWidths=[1.74*inch]*4)
        count_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#111827")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("BOX", (0, 0), (-1, -1), 0.3, colors.HexColor("#C9D4DE")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C9D4DE")),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([Spacer(1, 6), count_table, Spacer(1, 8)])

        observations = [diagnosis.get("mensaje") for diagnosis in result.get("diagnosticos", []) if diagnosis.get("mensaje")]
        story.append(Paragraph("<b>Observaciones esenciales</b>", body))
        if observations:
            story.append(Paragraph("<br/>".join(str(item) for item in observations[:3]), small))
        else:
            story.append(Paragraph("No se detectaron aclaraciones esenciales adicionales en el resultado persistido.", small))
        story.extend([Spacer(1, 14)])
        signatures = Table([
            ["", ""],
            ["________________________________", "________________________________"],
            ["Auxiliar", "Contador general"],
        ], colWidths=[3.45*inch, 3.45*inch])
        signatures.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 3)]))
        story.append(signatures)

        doc = SimpleDocTemplate(output, pagesize=letter, leftMargin=0.45*inch, rightMargin=0.45*inch,
                                topMargin=0.4*inch, bottomMargin=0.45*inch, title="Resumen de conciliación")
        generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")

        def add_page(canvas, document):
            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor("#E5E7EB"))
            canvas.line(0.45 * inch, 0.32 * inch, 8.05 * inch, 0.32 * inch)
            canvas.setFillColor(colors.HexColor("#111827"))
            canvas.setFont("Helvetica", 8)
            canvas.drawString(0.45 * inch, 0.18 * inch, f"Generado: {generated_at}")
            canvas.drawRightString(8.05 * inch, 0.18 * inch, f"Página {document.page}")
            canvas.restoreState()

        doc.build(story, onFirstPage=add_page, onLaterPages=add_page)

    def _pdf_sections(self, scope, job, result):
        summary = result.get("resumen", {})
        proceso = result.get("proceso_conciliacion", {})
        deposits = result.get("depositos", {})
        counts = proceso.get("conteos", {})
        def money(value):
            amount = _parse_decimal(value)
            if amount is None:
                return "No disponible"
            return f"{amount.quantize(Decimal('0.01')):,}"
        bank = result.get("banco") or {}
        ledger = result.get("libros") or {}
        context = bank.get("reconocimiento", {}).get("contexto", {})
        period = context.get("periodo_declarado") or job.get("periodo_sugerido") or "Por confirmar"
        cutoff = context.get("cobertura_declarada", {}).get("hasta") or "Por confirmar"
        account = context.get("numero_cuenta") or context.get("cuenta") or "Por confirmar"
        currency = context.get("moneda") or "Por confirmar"
        estado = "Aprobado" if job["estado"] == "aprobado" else "Requiere aclaraciones" if job["estado"] in ("requiere_aclaraciones", "fallido") else "Provisional"
        partida_rows = [["Partida explicativa", "Importe", "Lado", "Signo", "Estado"]]
        for item in summary.get("partidas_conciliatorias", []):
            partida_rows.append([
                item.get("concepto") or item.get("tipo"),
                money(item.get("importe")),
                item.get("lado") or "No disponible",
                item.get("signo") or "No confirmado",
                "Verificada" if item.get("verificado") else "Pendiente",
            ])
        if len(partida_rows) == 1:
            partida_rows.append(["No disponible: sin partida explicativa verificable", "-", "-", "-", "Pendiente"])
        yield ("Resumen de conciliación", [
            ["Concepto", "Valor"],
            ["Empresa", scope.get("company_name") or scope["company_id"]],
            ["País", scope["country_code"]],
            ["Banco / cuenta", f"{bank.get('nombre') or 'Banco por confirmar'} / {account}"],
            ["Moneda", currency],
            ["Período", period],
            ["Fecha de corte", cutoff],
            ["Estado real", estado],
            ["Saldo final Banco", money(summary.get("saldo_final_banco"))],
            ["Saldo final Contabilidad", money(summary.get("saldo_final_contabilidad"))],
            ["Diferencia original", money(summary.get("diferencia_original"))],
            ["Diferencia residual pendiente", money(summary.get("diferencia_residual_pendiente"))],
            ["Cruces confirmados", counts.get("cruces_confirmados", 0)],
            ["Movimientos Banco consumidos", counts.get("movimientos_banco_consumidos", 0)],
            ["Movimientos Contabilidad consumidos", counts.get("movimientos_contabilidad_consumidos", 0)],
            ["Pendientes Banco", counts.get("pendientes_banco", 0)],
            ["Pendientes Contabilidad", counts.get("pendientes_contabilidad", 0)],
            ["Alternativas ambiguas / casos por aclarar", counts.get("alternativas_ambiguas", 0)],
        ])
        yield ("Partidas conciliatorias explicativas", partida_rows)
        observations = []
        for diagnosis in result.get("diagnosticos", [])[:5]:
            observations.append([diagnosis["codigo"], diagnosis["mensaje"]])
        if observations:
            yield ("Observaciones esenciales", [["Código", "Observación"], *observations])
        yield ("Firmas", [
            ["Auxiliar", "Contador general"],
            ["________________________________", "________________________________"],
            ["Nombre y firma", "Nombre y firma"],
        ])

    def _lines(self, scope, job, result):
        summary = result.get("resumen", {})
        yield ("Proceso de conciliación", f"Empresa: {scope['company_id']} | País: {scope['country_code']}")
        yield ("Estado", job["estado"])
        yield ("Depósitos", f"{len(result.get('banco', {}).get('movimientos', []))} movimientos bancarios analizados")
        yield ("Cheques en circulación", str(result.get("cheques", [])))
        yield ("Saldo final Banco", summary.get("saldo_final_banco") or "No disponible")
        yield ("Saldo final Contabilidad", summary.get("saldo_final_contabilidad") or "No disponible")
        yield ("Diferencia original", summary.get("diferencia_original") or "No disponible")
        yield ("Diferencia residual pendiente", summary.get("diferencia_residual_pendiente") or "No disponible")
        for diagnosis in result.get("diagnosticos", []):
            yield (diagnosis["codigo"], diagnosis["mensaje"])

    def _xlsx(self, scope, job, result):
        workbook = Workbook()
        workbook.remove(workbook.active)
        navy = "173A5E"
        light = "EEF3F7"
        border = Border(bottom=Side(style="thin", color="CBD5DF"))
        context = result.get("contexto_resultado", {}) if isinstance(result.get("contexto_resultado"), dict) else {}
        bank_context = result.get("banco", {}).get("reconocimiento", {}).get("contexto", {}) if isinstance(result.get("banco"), dict) else {}
        account = context.get("cuenta") or context.get("numero_cuenta") or bank_context.get("cuenta_bancaria") or "Por confirmar"
        currency = context.get("moneda") or context.get("moneda_codigo") or bank_context.get("moneda_codigo") or "Por confirmar"
        canonical_gt = CUENTAS_GT_CONFIRMADAS.get(str(account).strip()) if job.get("pais_codigo") == "GT" else None
        if canonical_gt:
            currency = canonical_gt[2]
        bank_name = canonical_gt[0] if canonical_gt else context.get("banco") or ("Banco Industrial" if account == "0040040592" else "Por confirmar")
        period = context.get("periodo") or bank_context.get("periodo_declarado") or job.get("periodo_sugerido") or "Por confirmar"
        cutoff = context.get("fecha_corte") or bank_context.get("cobertura_declarada", {}).get("hasta") or "Por confirmar"
        company = "Red Intelfon"

        def prepare_sheet(sheet, title, context_rows, headers, widths, data_rows):
            sheet.sheet_view.showGridLines = False
            logo_path = settings.BASE_DIR / "img" / "logo.jpg"
            if logo_path.exists():
                logo = XLImage(str(logo_path))
                logo.width = 75
                logo.height = 38
                sheet.add_image(logo, "A1")
            sheet.merge_cells("C1:H1")
            title_cell = sheet["C1"]
            title_cell.value = title
            title_cell.font = Font(name="Calibri", size=16, bold=True, color="173A5E")
            title_cell.alignment = Alignment(vertical="center")
            sheet.row_dimensions[1].height = 42
            row = 2
            for label, value in context_rows:
                sheet.cell(row, 1, label).font = Font(bold=True, color="173A5E")
                sheet.cell(row, 1).fill = PatternFill("solid", fgColor=light)
                sheet.cell(row, 2, value)
                sheet.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="center")
                sheet.row_dimensions[row].height = 24
                row += 1
            row += 1
            header_row = row
            for column, header in enumerate(headers, 1):
                cell = sheet.cell(header_row, column, header)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor=navy)
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = border
            sheet.row_dimensions[header_row].height = 34
            for values in data_rows:
                row += 1
                for column, value in enumerate(values, 1):
                    cell = sheet.cell(row, column, value if value not in ("", None) else None)
                    cell.border = border
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
            if data_rows:
                sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{header_row + len(data_rows)}"
                sheet.freeze_panes = f"A{header_row + 1}"
            else:
                sheet.freeze_panes = f"A{header_row + 1}"
            for index, width in enumerate(widths, 1):
                sheet.column_dimensions[get_column_letter(index)].width = width
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.print_title_rows = f"1:{header_row}"
            sheet.print_area = f"A1:{get_column_letter(len(headers))}{max(header_row + len(data_rows), header_row)}"
            sheet.auto_filter.add_sort_condition(f"A{header_row + 1}:A{header_row + len(data_rows)}") if data_rows else None
            return header_row

        context_rows = [
            ("Empresa", company), ("Banco", bank_name), ("Cuenta", account), ("Moneda", currency),
            ("Período", period), ("Corte", cutoff), ("Estado", job.get("estado")),
        ]
        amount_format = '"Q" #,##0.00' if currency == "GTQ" else '#,##0.00'
        def excel_date(value):
            if isinstance(value, datetime):
                return value
            if isinstance(value, date):
                return value
            if value in (None, ""):
                return None
            text = str(value).strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
            return value
        summary = result.get("resumen", {})
        proceso = result.get("proceso_conciliacion", {})
        summary_headers = ["Concepto", "Valor"]
        summary_rows = [
            ["Saldo final Banco", _parse_decimal(summary.get("saldo_final_banco"))],
            ["Saldo final Contabilidad", _parse_decimal(summary.get("saldo_final_contabilidad"))],
            ["Diferencia original", _parse_decimal(summary.get("diferencia_original"))],
            ["Diferencia residual pendiente", _parse_decimal(summary.get("diferencia_residual_pendiente"))],
            ["Cruces confirmados", proceso.get("conteos", {}).get("cruces_confirmados", 0)],
            ["Movimientos Banco consumidos", proceso.get("conteos", {}).get("movimientos_banco_consumidos", 0)],
            ["Movimientos Contabilidad consumidos", proceso.get("conteos", {}).get("movimientos_contabilidad_consumidos", 0)],
            ["Pendientes Banco", proceso.get("conteos", {}).get("pendientes_banco", 0)],
            ["Pendientes Contabilidad", proceso.get("conteos", {}).get("pendientes_contabilidad", 0)],
        ]
        summary_sheet = workbook.create_sheet("Resumen de conciliación")
        prepare_sheet(summary_sheet, "Resumen de conciliación", context_rows, summary_headers, [34, 22], summary_rows)
        summary_table_start = 11
        for row_number in range(summary_table_start, summary_table_start + len(summary_rows)):
            summary_sheet.cell(row_number, 2).number_format = amount_format if row_number <= summary_table_start + 3 else '0'
        partidas = summary.get("partidas_conciliatorias", [])
        if partidas:
            start = summary_sheet.max_row + 2
            summary_sheet.cell(start, 1, "Partidas conciliatorias explicativas").font = Font(bold=True, color=navy, size=12)
            for column, header in enumerate(["Concepto", "Importe", "Lado", "Signo"], 1):
                cell = summary_sheet.cell(start + 1, column, header)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor=navy)
            for offset, partida in enumerate(partidas, start + 2):
                summary_sheet.cell(offset, 1, partida.get("concepto") or partida.get("tipo"))
                summary_sheet.cell(offset, 2, _parse_decimal(partida.get("importe")))
                summary_sheet.cell(offset, 2).number_format = amount_format
                summary_sheet.cell(offset, 3, partida.get("lado"))
                summary_sheet.cell(offset, 4, partida.get("signo"))

        process_headers = [
            "Fecha Banco", "Referencia Banco", "Descripción Banco", "Monto Banco",
            "Fecha Contabilidad", "Referencia Contabilidad", "Descripción Contabilidad", "Monto Contabilidad",
            "Diferencia", "Criterio", "Resultado", "Archivo Banco", "Hoja Banco", "Fila Banco",
            "Archivo Contabilidad", "Hoja Contabilidad", "Fila Contabilidad",
        ]
        process_rows = []
        for match in proceso.get("coincidencias_confirmadas", []):
            process_rows.append([
                excel_date(match.get("fecha_banco")), match.get("referencia"), match.get("descripcion_banco"),
                _parse_decimal(match.get("monto_banco")), excel_date(match.get("fecha_contabilidad")),
                ", ".join(match.get("referencias_alternativas") or [match.get("referencia")]) if match.get("referencias_alternativas") or match.get("referencia") else "",
                match.get("descripcion_contabilidad"), _parse_decimal(match.get("monto_contabilidad")),
                _parse_decimal(match.get("diferencia")), match.get("criterio"), match.get("resultado"),
                match.get("evidencia_banco", {}).get("archivo"), match.get("evidencia_banco", {}).get("hoja"), match.get("evidencia_banco", {}).get("fila"),
                match.get("evidencia_contabilidad", {}).get("archivo"), match.get("evidencia_contabilidad", {}).get("hoja"), match.get("evidencia_contabilidad", {}).get("fila"),
            ])
        for item in proceso.get("movimientos_sin_contraparte", {}).get("banco", []):
            movement = item.get("movimiento", {})
            evidence = item.get("evidencia", movement.get("datos_originales", {}))
            process_rows.append([
                excel_date(movement.get("fecha")), movement.get("referencia") or movement.get("numero_transaccion"),
                movement.get("descripcion"), _parse_decimal(movement.get("monto")), "", "", "", "",
                "", "Sin contraparte inequívoca", item.get("situacion") or "Pendiente",
                evidence.get("archivo"), evidence.get("hoja"), movement.get("fila_origen"),
                "", "", "",
            ])
        for item in proceso.get("movimientos_sin_contraparte", {}).get("contabilidad", []):
            movement = item.get("movimiento", {})
            evidence = item.get("evidencia", movement.get("datos_originales", {}))
            process_rows.append([
                "", "", "", "", excel_date(movement.get("fecha")), movement.get("referencia"),
                movement.get("descripcion"), _parse_decimal(movement.get("monto")),
                "", "Sin contraparte inequívoca", item.get("situacion") or "Pendiente",
                "", "", "", evidence.get("archivo"), evidence.get("hoja"), movement.get("fila_origen"),
            ])
        process_sheet = workbook.create_sheet("Proceso de conciliación")
        process_row = prepare_sheet(process_sheet, "Proceso de conciliación", context_rows, process_headers, [12, 18, 34, 14, 12, 18, 34, 14, 14, 28, 18, 30, 18, 10, 30, 18, 10], process_rows)
        for row in range(process_row + 1, process_row + len(process_rows) + 1):
            for column in (4, 8, 9):
                process_sheet.cell(row, column).number_format = amount_format
            for column in (1, 5):
                process_sheet.cell(row, column).number_format = 'dd/mm/yyyy'
            for column in (2, 6, 12, 15):
                process_sheet.cell(row, column).number_format = '@'

        deposits_headers = ["Fecha", "Referencia", "Descripción", "Monto", "Situación", "Archivo", "Hoja", "Fila"]
        deposits_rows = []
        deposits_result = result.get("depositos", {})
        for key in ("en_transito", "ingresos_bancarios_sin_naturaleza_confirmada", "diferencias_importe", "sin_contraparte", "salidas_pendientes", "casos_investigar"):
            for item in deposits_result.get(key, []):
                movement = item.get("movimiento", item if isinstance(item, dict) else {})
                evidence = item.get("evidencia", movement.get("datos_originales", {}))
                deposits_rows.append([
                    excel_date(movement.get("fecha")), movement.get("referencia") or movement.get("numero_transaccion"),
                    movement.get("descripcion"), _parse_decimal(movement.get("monto")),
                    item.get("situacion") or key.replace("_", " ").title(), evidence.get("archivo"),
                    evidence.get("hoja"), evidence.get("fila"),
                ])
        deposits_sheet = workbook.create_sheet("Depósitos")
        deposits_row = prepare_sheet(deposits_sheet, "Depósitos", context_rows, deposits_headers, [12, 18, 38, 14, 32, 30, 18, 10], deposits_rows)
        for row in range(deposits_row + 1, deposits_row + len(deposits_rows) + 1):
            deposits_sheet.cell(row, 4).number_format = amount_format
            deposits_sheet.cell(row, 1).number_format = 'dd/mm/yyyy'
            deposits_sheet.cell(row, 2).number_format = '@'

        cheque_headers = ["Corte histórico", "Situación al corte", "Fecha", "Número de cheque", "Descripción/Beneficiario", "Monto", "Archivo histórico", "Hoja", "Fila", "Cobro identificado", "Evidencia cobro"]
        cheque_rows = []
        for cut in result.get("cheques", []):
            for record in cut.get("registros", []):
                cobro = record.get("cobro_identificado", [])
                cobro_text = ""
                evidence_text = ""
                if cobro:
                    cobro_text = f"{cobro[0].get('fecha')} / {cobro[0].get('monto')}"
                    ev = cobro[0].get("evidencia", {})
                    evidence_text = f"{ev.get('archivo')} · Hoja {ev.get('hoja')} · Fila {ev.get('fila')}"
                cheque_rows.append([
                    cut.get("periodo_corte"), record.get("estado_al_corte"), excel_date(record.get("fecha")),
                    record.get("numero_documento"), record.get("descripcion"), _parse_decimal(record.get("monto")),
                    cut.get("nombre"), record.get("hoja"), record.get("fila"), cobro_text, evidence_text,
                ])
        cheque_sheet = workbook.create_sheet("Cheques en circulación")
        cheque_row = prepare_sheet(cheque_sheet, "Cheques en circulación", context_rows, cheque_headers, [14, 28, 12, 18, 38, 14, 30, 18, 10, 24, 42], cheque_rows)
        for row in range(cheque_row + 1, cheque_row + len(cheque_rows) + 1):
            cheque_sheet.cell(row, 6).number_format = amount_format
            cheque_sheet.cell(row, 3).number_format = 'dd/mm/yyyy'
            cheque_sheet.cell(row, 4).number_format = '@'

        for sheet in workbook.worksheets:
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_margins.left = 0.25
            sheet.page_margins.right = 0.25
            sheet.page_margins.top = 0.4
            sheet.page_margins.bottom = 0.4
        output = BytesIO()
        workbook.save(output)
        return output

    def _xlsx_legacy(self, scope, job, result):
        workbook = Workbook()
        workbook.remove(workbook.active)
        process = workbook.create_sheet("Proceso de conciliación")
        deposits = workbook.create_sheet("Depósitos")
        cheques = workbook.create_sheet("Cheques en circulación")
        summary = workbook.create_sheet("Resumen de conciliación")
        process.append(["Empresa", scope["company_id"], "País", scope["country_code"]])
        process.append(["Estado", job["estado"]])
        process.append(["Período sugerido", job.get("periodo_sugerido") or "Por confirmar"])
        for diagnosis in result.get("diagnosticos", []):
            process.append([diagnosis["codigo"], diagnosis["mensaje"]])
        bank = result.get("banco")
        if bank:
            process.append(["Archivo banco", bank["nombre"]])
            process.append(["Filas bancarias analizadas", bank["filas_analizadas"]])
        ledger = result.get("libros")
        if ledger:
            process.append(["Archivo contabilidad", ledger["nombre"]])
            process.append(["Filas contables analizadas", ledger["filas_analizadas"]])
        process.append([])
        process.append([
            "Fecha Banco", "Referencia Banco", "Descripción Banco", "Monto Banco",
            "Fecha Contabilidad", "Referencia Contabilidad", "Descripción Contabilidad", "Monto Contabilidad",
            "Diferencia", "Criterio", "Resultado", "Archivo Banco", "Hoja Banco", "Fila Banco",
            "Archivo Contabilidad", "Hoja Contabilidad", "Fila Contabilidad",
        ])
        proceso = result.get("proceso_conciliacion", {})
        for match in proceso.get("coincidencias_confirmadas", []):
            bank_movement = match["banco"]
            book_movement = match["contabilidad"]
            bank_evidence = match["evidencia_banco"]
            book_evidence = match["evidencia_contabilidad"]
            process.append([
                match.get("fecha_banco"), match.get("referencia"),
                match.get("descripcion_banco"), match.get("monto_banco"),
                match.get("fecha_contabilidad"), ", ".join(match.get("referencias_alternativas") or []),
                match.get("descripcion_contabilidad"), match.get("monto_contabilidad"),
                match.get("diferencia"), match.get("criterio"), match.get("resultado"),
                bank_evidence.get("archivo"), bank_evidence.get("hoja"), bank_evidence.get("fila"),
                book_evidence.get("archivo"), book_evidence.get("hoja"), book_evidence.get("fila"),
            ])
        for item in proceso.get("alternativas_ambiguas", []):
            bank_items = item.get("banco", [])
            book_items = item.get("contabilidad", [])
            process.append([
                "Alternativa", "", "", "", "", "", "", "", "", item.get("criterio"),
                "Ambiguo; requiere revisión", "", "", "", "", "", "",
            ])
            for movement in bank_items:
                evidence = movement.get("datos_originales", {})
                process.append([
                    movement.get("fecha"), movement.get("referencia"), movement.get("descripcion"),
                    movement.get("monto"), "", "", "", "", "", "Banco candidato", "Pendiente",
                    evidence.get("archivo"), evidence.get("hoja"), movement.get("fila_origen"), "", "", "",
                ])
            for movement in book_items:
                evidence = movement.get("datos_originales", {})
                process.append([
                    "", "", "", "", movement.get("fecha"), movement.get("referencia"),
                    movement.get("descripcion"), movement.get("monto"), "", "Contabilidad candidata", "Pendiente",
                    "", "", "", evidence.get("archivo"), evidence.get("hoja"), movement.get("fila_origen"),
                ])
        deposits.append(["Fecha", "Referencia", "Descripción", "Monto", "Saldo original", "Archivo", "Hoja", "Fila"])
        report_deposits = result.get("depositos", {})
        for item in report_deposits.get("en_transito", []):
            movement = item["movimiento"]
            evidence = item["evidencia"]
            deposits.append([
                movement.get("fecha"), movement.get("referencia"), movement.get("descripcion"),
                movement.get("monto"), movement.get("saldo_origen"), evidence.get("archivo"),
                evidence.get("hoja"), evidence.get("fila"),
            ])
        deposits.append([])
        deposits.append(["Ingresos bancarios pendientes (no clasificados automáticamente)", "", "", "", "", "", "", ""])
        for item in report_deposits.get("ingresos_bancarios_sin_naturaleza_confirmada", []):
            movement = item["movimiento"]
            evidence = item["evidencia"]
            deposits.append([
                movement.get("fecha"), movement.get("referencia"), movement.get("descripcion"),
                movement.get("monto"), movement.get("saldo_origen"), evidence.get("archivo"),
                evidence.get("hoja"), evidence.get("fila"),
            ])
        deposits.append([])
        deposits.append(["Salidas bancarias pendientes / operaciones de cheque", "", "", "", "", "", "", ""])
        for item in report_deposits.get("salidas_pendientes", []):
            movement = item["movimiento"]
            evidence = item["evidencia"]
            deposits.append([
                movement.get("fecha"), movement.get("referencia"), movement.get("descripcion"),
                movement.get("monto"), movement.get("saldo_origen"), evidence.get("archivo"),
                evidence.get("hoja"), evidence.get("fila"),
            ])
        cheques.append(["Corte histórico", "Situación al cierre", "Fecha histórica", "Número de documento", "Descripción", "Monto", "Archivo histórico", "Hoja", "Fila", "Cobro bancario", "Evidencia cobro"])
        for cut in result.get("cheques", []):
            for record in cut["registros"]:
                cobro = record.get("cobro_identificado", [])
                cobro_text = ""
                evidence_text = ""
                if cobro:
                    cobro_text = f"{cobro[0].get('fecha')} / {cobro[0].get('monto')}"
                    ev = cobro[0].get("evidencia", {})
                    evidence_text = f"{ev.get('archivo')} · Hoja {ev.get('hoja')} · Fila {ev.get('fila')}"
                cheques.append([
                    cut["periodo_corte"], record.get("estado_al_corte"), record.get("fecha"), record.get("numero_documento"),
                    record.get("descripcion"), record.get("monto"), cut["nombre"],
                    record.get("hoja"), record.get("fila"), cobro_text, evidence_text,
                ])
        report_summary = result.get("resumen", {})
        summary.append(["Saldo final Banco", report_summary.get("saldo_final_banco") or "No disponible"])
        summary.append(["Saldo final Contabilidad", report_summary.get("saldo_final_contabilidad") or "No disponible"])
        summary.append(["Diferencia original", report_summary.get("diferencia_original") or "No disponible"])
        summary.append(["Partidas conciliatorias explicativas verificadas", len(report_summary.get("partidas_conciliatorias", []))])
        summary.append(["Diferencia residual pendiente", report_summary.get("diferencia_residual_pendiente") or "No disponible"])
        summary.append(["Movimientos Banco", report_summary.get("controles", {}).get("movimientos_banco")])
        summary.append(["Movimientos Contabilidad", report_summary.get("controles", {}).get("movimientos_contabilidad")])
        counts = result.get("proceso_conciliacion", {}).get("conteos", {})
        summary.append(["Cruces confirmados", counts.get("cruces_confirmados", 0)])
        summary.append(["Movimientos Banco consumidos", counts.get("movimientos_banco_consumidos", 0)])
        summary.append(["Movimientos Contabilidad consumidos", counts.get("movimientos_contabilidad_consumidos", 0)])
        summary.append(["Alternativas ambiguas", counts.get("alternativas_ambiguas", 0)])
        output = BytesIO()
        workbook.save(output)
        return output
