import hashlib
import json
import re
import shutil
import unicodedata
from zipfile import BadZipFile
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree.ElementTree import ParseError
from xml.sax.saxutils import escape

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.http import FileResponse, HttpResponse
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.banking import CUENTAS_GT_CONFIRMADAS, CUENTAS_SV_CONFIRMADAS, scope_from_request
from contared_api.imports import _normalize_xls, recognize_workbook, validate_workbook
from contared_api.reconciliation import Movement, propose_reconciliation
from contared_api.session_access import DatabaseUnavailable


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
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=_json_default), encoding="utf-8")
    temporary.replace(path)


def _read_json(path):
    if not path.exists():
        raise NotFound("El trabajo de conciliación no existe.")
    return json.loads(path.read_text(encoding="utf-8"))


def _access(request):
    return scope_from_request(request)


def _writer_access(request):
    scope = _access(request)
    if scope["role"] not in ("administrador", "auxiliar"):
        raise PermissionDenied("Esta operación requiere el rol administrador o auxiliar.", code="writer_required")
    return scope


def _safe_scope_path(scope):
    root = _storage_root()
    return root / scope["country_code"] / scope["company_id"] / scope["user_id"]


def _scope_jobs_path(scope):
    return _storage_root() / scope["country_code"] / scope["company_id"]


def _job_path(scope, job_id):
    root = _scope_jobs_path(scope)
    for metadata in root.glob(f"*/{job_id}/metadata.json"):
        try:
            job = json.loads(metadata.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (
            job.get("empresa_id") == scope["company_id"]
            and job.get("pais_codigo") == scope["country_code"]
        ):
            return metadata.parent
    return _safe_scope_path(scope) / str(job_id)


def _parse_decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace(" ", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _account_digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _bank_key(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    normalized = re.sub(r"[^a-z0-9]", "", text)
    if "bac" in normalized or "americacentral" in normalized:
        return "bac"
    return normalized


def _is_cuscatlan_account(account):
    identity = f"{account.get('banco', '')} {account.get('alias', '')}".casefold()
    return "cuscat" in identity or "cusca 2509" in identity


def _period_dates(period):
    if isinstance(period, date):
        start = period.replace(day=1)
    else:
        match = re.fullmatch(r"(\d{4})-(\d{2})", str(period or "").strip())
        if not match:
            raise ValidationError({"periodo": "Selecciona un mes y año válidos para conciliar."})
        try:
            start = date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError as exc:
            raise ValidationError({"periodo": "Selecciona un mes y año válidos para conciliar."}) from exc
    end = date(start.year, start.month, monthrange(start.year, start.month)[1])
    previous_end = start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    return start, end, previous_start, previous_end


def _resolve_cuscatlan_opening_balance(movements, recognition, period):
    start, end, previous_start, previous_end = _period_dates(period)
    previous_month = [
        movement for movement in movements
        if previous_start.isoformat() <= str(movement.get("fecha") or "") <= previous_end.isoformat()
    ]
    if previous_month:
        latest_date = max(movement["fecha"] for movement in previous_month)
        last_day_movements = sorted(
            (movement for movement in previous_month if movement["fecha"] == latest_date),
            key=lambda item: int(item.get("fila_origen") or 0),
        )
        sequence_verified = True
        for earlier, later in zip(last_day_movements, last_day_movements[1:]):
            earlier_balance = _parse_decimal(earlier.get("saldo_origen"))
            later_balance = _parse_decimal(later.get("saldo_origen"))
            later_amount = _parse_decimal(later.get("monto"))
            if (
                earlier_balance is None or later_balance is None or later_amount is None
                or (earlier_balance + later_amount).quantize(Decimal("0.01"))
                != later_balance.quantize(Decimal("0.01"))
            ):
                sequence_verified = False
                break
        latest = last_day_movements[-1]
        balance = _parse_decimal(latest.get("saldo_origen")) if sequence_verified else None
        if balance is not None:
            source = latest.get("datos_originales") or {}
            return {
                "saldo": str(balance),
                "fecha": latest.get("fecha"),
                "archivo": source.get("archivo"),
                "hoja": source.get("hoja"),
                "fila": latest.get("fila_origen"),
                "metodo": "ultimo_movimiento_mes_anterior",
                "secuencia_verificada": True,
            }, None
        if len(last_day_movements) > 1:
            return None, "No se pudo comprobar la secuencia de movimientos con la misma fecha; revise el orden y el Saldo Real."

    context = recognition.get("contexto") or {}
    if context.get("periodo_declarado") == start.strftime("%Y-%m") and context.get("saldo_inicial") is not None:
        return {
            **(context.get("saldo_inicial_evidencia") or {}),
            "saldo": str(context["saldo_inicial"]),
            "archivo": (context.get("saldo_inicial_evidencia") or {}).get("archivo"),
            "hoja": (context.get("saldo_inicial_evidencia") or {}).get("hoja") or recognition.get("hoja"),
            "metodo": "saldo_inicial_explicito",
            "secuencia_verificada": True,
        }, None
    return None, "Falta el saldo inicial bancario. Descargue el estado con un rango que incluya el último movimiento del mes anterior"


def _account_row(row):
    return {
        "id": row[0],
        "banco_id": row[1],
        "banco": row[2],
        "numero": row[3],
        "alias": row[4],
        "moneda_codigo": row[5],
        "numero_terminacion": row[6],
        "numero_completo_verificado": row[7],
        "incorporacion_provisional": row[8],
    }


def _authorized_account(cursor, scope, account_id):
    cursor.execute(
        """
        SELECT c.id::text,c.banco_id::text,b.nombre,c.numero,c.alias,c.moneda_codigo,
               c.numero_terminacion,c.numero_completo_verificado,c.incorporacion_provisional
        FROM contared.cuentas_bancarias c
        JOIN contared.bancos b ON b.id=c.banco_id
        WHERE c.id=%s AND c.empresa_id=%s AND c.pais_codigo=%s
          AND c.activa IS TRUE AND b.activo IS TRUE
        """,
        [account_id, scope["company_id"], scope["country_code"]],
    )
    row = cursor.fetchone()
    if row is None:
        raise ValidationError({
            "cuenta_id": "La cuenta seleccionada no está activa o no pertenece a la empresa y país autorizados."
        })
    account = _account_row(row)
    if not account["moneda_codigo"]:
        raise ValidationError({"cuenta_id": "La cuenta seleccionada no tiene una moneda configurada."})
    return account


def _authorized_accounts(cursor, scope):
    cursor.execute(
        """
        SELECT c.id::text,c.banco_id::text,b.nombre,c.numero,c.alias,c.moneda_codigo,
               c.numero_terminacion,c.numero_completo_verificado,c.incorporacion_provisional
        FROM contared.cuentas_bancarias c
        JOIN contared.bancos b ON b.id=c.banco_id
        WHERE c.empresa_id=%s AND c.pais_codigo=%s
          AND c.activa IS TRUE AND b.activo IS TRUE
        """,
        [scope["company_id"], scope["country_code"]],
    )
    return [_account_row(row) for row in cursor.fetchall()]


def _authorized_accounts_for_scope(scope):
    try:
        with connection.cursor() as cursor:
            return _authorized_accounts(cursor, scope)
    except DatabaseError as exc:
        raise DatabaseUnavailable() from exc


def _document_contexts(result, origin):
    contexts = []
    for document in result.get("archivos", []):
        recognition = document.get("reconocimiento") or {}
        if recognition.get("origen_sugerido") == origin:
            context = recognition.get("contexto") or {}
            if isinstance(context, dict):
                contexts.append(context)
    section = result.get("banco" if origin == "banco" else "libros") or {}
    recognition = section.get("reconocimiento") or {}
    section_context = recognition.get("contexto") or {}
    if isinstance(section_context, dict) and section_context not in contexts:
        contexts.append(section_context)
    return contexts


def _validate_account_result(account, available_accounts, result, country_code):
    diagnostics = []
    suggestion = None
    expected_bank = _bank_key(account.get("banco"))
    expected_currency = str(account.get("moneda_codigo") or "").strip().upper()
    selected_number = _account_digits(account.get("numero"))
    selected_ending = _account_digits(account.get("numero_terminacion")) or (
        selected_number if len(selected_number) == 4 else selected_number[-4:]
    )
    bank_contexts = _document_contexts(result, "banco")
    ledger_contexts = _document_contexts(result, "libros")
    bank_identities = []

    def report(code, message):
        item = {"codigo": code, "mensaje": message}
        if item not in diagnostics:
            diagnostics.append(item)

    for context in bank_contexts:
        detected_bank = _bank_key(context.get("banco"))
        detected_currency = str(context.get("moneda_codigo") or context.get("moneda") or "").strip().upper()
        if detected_bank and expected_bank and detected_bank != expected_bank:
            report("cuenta_banco_incompatible", "El banco identificado en el estado no coincide con la cuenta seleccionada.")
        if detected_currency and expected_currency and detected_currency != expected_currency:
            report("cuenta_moneda_incompatible", "La moneda identificada en el estado no coincide con la cuenta seleccionada.")

        full_number = _account_digits(
            context.get("cuenta_bancaria") or context.get("numero_cuenta") or context.get("cuenta")
        )
        ending = _account_digits(context.get("terminacion"))
        if full_number:
            if len(full_number) > 4:
                bank_identities.append((detected_bank or expected_bank, detected_currency or expected_currency, full_number))
            exact = selected_number == full_number
            suffix_match = (
                country_code == "SV"
                and len(selected_ending) == 4
                and full_number.endswith(selected_ending)
            )
            matching_accounts = [
                candidate for candidate in available_accounts
                if _bank_key(candidate.get("banco")) == (detected_bank or expected_bank)
                and str(candidate.get("moneda_codigo") or "").strip().upper() == (detected_currency or expected_currency)
                and (
                    _account_digits(candidate.get("numero")) == full_number
                    or (
                        country_code == "SV"
                        and len(_account_digits(candidate.get("numero_terminacion")) or _account_digits(candidate.get("numero"))) == 4
                        and full_number.endswith(_account_digits(candidate.get("numero_terminacion")) or _account_digits(candidate.get("numero")))
                    )
                )
            ]
            if not (exact or suffix_match) or not matching_accounts or account["id"] not in {item["id"] for item in matching_accounts}:
                report("cuenta_numero_incompatible", "El número completo del documento no coincide con la cuenta seleccionada.")
            elif suffix_match and selected_number != full_number and len(matching_accounts) == 1:
                suggestion = {
                    "numero_completo": full_number,
                    "terminacion": selected_ending,
                    "cuenta_id": account["id"],
                    "requiere_confirmacion": True,
                }
            elif len(matching_accounts) != 1:
                report("cuenta_coincidencia_ambigua", "El número del documento coincide con más de una cuenta del mismo banco y moneda. Selecciona o vincula una cuenta única.")
        elif ending:
            if country_code != "SV":
                report("cuenta_numero_incompatible", "El documento solo proporciona una terminación y no se puede validar con seguridad para este país.")
                continue
            if not detected_bank or not detected_currency:
                report("cuenta_datos_insuficientes", "Para validar una terminación de cuenta se requieren banco y moneda identificados en el documento.")
                continue
            bank_identities.append((detected_bank, detected_currency, ending[-4:]))
            matching_accounts = [
                candidate for candidate in available_accounts
                if _bank_key(candidate.get("banco")) == detected_bank
                and str(candidate.get("moneda_codigo") or "").strip().upper() == detected_currency
                and (_account_digits(candidate.get("numero_terminacion")) or _account_digits(candidate.get("numero"))).endswith(ending[-4:])
            ]
            if len(matching_accounts) != 1 or matching_accounts[0]["id"] != account["id"]:
                report("cuenta_coincidencia_ambigua", "La terminación no identifica de forma única la cuenta seleccionada para ese banco y moneda.")

    for context in ledger_contexts:
        ledger_bank = _bank_key(context.get("banco"))
        detected_currency = str(context.get("moneda_codigo") or context.get("moneda") or "").strip().upper()
        if detected_currency and expected_currency and detected_currency != expected_currency:
            report("cuenta_moneda_incompatible", "La moneda identificada en el mayor no coincide con la cuenta seleccionada.")
        full_number = _account_digits(
            context.get("cuenta_bancaria") or context.get("numero_cuenta") or context.get("cuenta")
        )
        ending = _account_digits(context.get("terminacion"))
        if full_number:
            exact = selected_number == full_number
            suffix_match = country_code == "SV" and len(selected_ending) == 4 and full_number.endswith(selected_ending)
            matching_accounts = [
                candidate for candidate in available_accounts
                if _bank_key(candidate.get("banco")) == (ledger_bank or expected_bank)
                and str(candidate.get("moneda_codigo") or "").strip().upper() == (detected_currency or expected_currency)
                and (
                    _account_digits(candidate.get("numero")) == full_number
                    or (
                        country_code == "SV"
                        and len(_account_digits(candidate.get("numero_terminacion")) or _account_digits(candidate.get("numero"))) == 4
                        and full_number.endswith(_account_digits(candidate.get("numero_terminacion")) or _account_digits(candidate.get("numero")))
                    )
                )
            ]
            if not (exact or suffix_match) or not matching_accounts or account["id"] not in {item["id"] for item in matching_accounts}:
                report("cuenta_numero_incompatible", "El número de cuenta indicado en el mayor contradice la cuenta seleccionada.")
            elif len(matching_accounts) != 1:
                report("cuenta_coincidencia_ambigua", "El número indicado en el mayor coincide con más de una cuenta del mismo banco y moneda.")
        elif ending:
            document_bank = ledger_bank or next((_bank_key(item.get("banco")) for item in bank_contexts if item.get("banco")), "")
            if country_code != "SV" or not document_bank or not detected_currency:
                report("cuenta_datos_insuficientes", "La terminación del mayor requiere banco y moneda verificables antes de vincularse.")
            else:
                matching_accounts = [
                    candidate for candidate in available_accounts
                    if _bank_key(candidate.get("banco")) == document_bank
                    and str(candidate.get("moneda_codigo") or "").strip().upper() == detected_currency
                    and (_account_digits(candidate.get("numero_terminacion")) or _account_digits(candidate.get("numero"))).endswith(ending[-4:])
                ]
                if len(matching_accounts) != 1 or matching_accounts[0]["id"] != account["id"]:
                    report("cuenta_coincidencia_ambigua", "La terminación del mayor no identifica de forma única la cuenta seleccionada.")

    has_multiple_accounts = any(
        left[0] != right[0]
        or left[1] != right[1]
        or (
            left[2] != right[2]
            and not (
                len(left[2]) == 4 and right[2].endswith(left[2])
                or len(right[2]) == 4 and left[2].endswith(right[2])
            )
        )
        for index, left in enumerate(bank_identities)
        for right in bank_identities[index + 1:]
    )
    if has_multiple_accounts:
        report("documentos_cuentas_multiples", "Los estados bancarios recibidos identifican más de una cuenta; no se mezclarán en este análisis.")
    return diagnostics, suggestion


def _movement_reference(movement):
    for key in ("referencia", "numero_transaccion", "numero_cheque"):
        value = movement.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _movement_references(movement):
    references = set()
    primary = _movement_reference(movement)
    if primary:
        references.add(primary)
    original = movement.get("datos_originales")
    if isinstance(original, dict):
        for key, value in original.items():
            normalized = _normalized_text(key)
            if value not in (None, "") and (
                "referencia" in normalized
                or "numero de origen" in normalized
                or "numero de pago" in normalized
            ):
                references.add(str(value).strip())
    return references


def _normalized_text(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in text if not unicodedata.combining(character))


def _has_explicit_cancellation(movement):
    descriptions = [movement.get("descripcion"), movement.get("codigo_tt")]
    original = movement.get("datos_originales")
    if isinstance(original, dict):
        descriptions.extend(
            value for key, value in original.items()
            if any(word in _normalized_text(key) for word in ("descripcion", "transaccion", "tipo"))
        )
    return any(
        re.search(r"\b(cancel\w*|anul\w*|revers\w*|extorn\w*|void|reversal)\b", _normalized_text(value))
        for value in descriptions
        if value not in (None, "")
    )


def _pending_reversal_groups(items):
    candidates = []
    for left_index, left in enumerate(items):
        left_movement = left.get("movimiento", {})
        left_amount = _parse_decimal(left_movement.get("monto"))
        left_references = _movement_references(left_movement)
        if left_amount is None or not left_references:
            continue
        for right_index in range(left_index + 1, len(items)):
            right = items[right_index]
            right_movement = right.get("movimiento", {})
            right_amount = _parse_decimal(right_movement.get("monto"))
            if right_amount != -left_amount:
                continue
            shared_references = left_references & _movement_references(right_movement)
            cancellation_pair = (
                _has_explicit_cancellation(left_movement)
                != _has_explicit_cancellation(right_movement)
            )
            if shared_references and cancellation_pair:
                candidates.append((left_index, right_index, sorted(shared_references)[0]))

    partners = {}
    for left_index, right_index, reference in candidates:
        partners.setdefault(left_index, []).append((right_index, reference))
        partners.setdefault(right_index, []).append((left_index, reference))

    groups = []
    grouped_indexes = set()
    for left_index, right_index, reference in candidates:
        if (
            left_index in grouped_indexes
            or right_index in grouped_indexes
            or len(partners.get(left_index, [])) != 1
            or len(partners.get(right_index, [])) != 1
        ):
            continue
        left, right = items[left_index], items[right_index]
        left_movement, right_movement = left["movimiento"], right["movimiento"]
        pair = [left, right]
        pair.sort(key=lambda item: (
            str(item.get("fecha") or ""),
            int(item.get("evidencia", {}).get("fila") or item.get("movimiento", {}).get("fila_origen") or 0),
        ))
        group_id = f"reversion:{reference}:{left.get('id')}:{right.get('id')}"
        groups.append({
            "id": group_id,
            "referencia": reference,
            "neto": "0.00",
            "fecha": min(str(left_movement.get("fecha") or ""), str(right_movement.get("fecha") or "")),
            "movimientos": pair,
            "motivo": "Cancelación identificada por referencia de pago, importe opuesto y descripción explícita.",
        })
        left["grupo_reversion_id"] = group_id
        right["grupo_reversion_id"] = group_id
        grouped_indexes.update((left_index, right_index))

    ambiguous_adjacency = {}
    ambiguous_references = {}
    for left_index, right_index, reference in candidates:
        if left_index in grouped_indexes and right_index in grouped_indexes:
            continue
        ambiguous_adjacency.setdefault(left_index, set()).add(right_index)
        ambiguous_adjacency.setdefault(right_index, set()).add(left_index)
        ambiguous_references.setdefault(left_index, set()).add(reference)
        ambiguous_references.setdefault(right_index, set()).add(reference)
    ambiguous = []
    visited = set()
    for start_index in sorted(ambiguous_adjacency):
        if start_index in visited:
            continue
        component = set()
        stack = [start_index]
        while stack:
            index = stack.pop()
            if index in visited:
                continue
            visited.add(index)
            component.add(index)
            stack.extend(ambiguous_adjacency.get(index, set()) - visited)
        references = sorted({
            reference
            for index in component
            for reference in ambiguous_references.get(index, set())
        })
        ambiguous.append({
            "id": f"reversion_pendiente:{':'.join(references)}:{':'.join(str(items[index].get('id')) for index in sorted(component))}",
            "referencia": ", ".join(references),
            "movimientos": [items[index] for index in sorted(component)],
            "motivo": "Hay varias contrapartes posibles para esta referencia; requiere revisión.",
        })
    return groups, ambiguous, grouped_indexes


def _pending_date_groups(items, grouped_indexes):
    dates = {}
    for index, item in enumerate(items):
        movement = item.get("movimiento", {})
        day = str(movement.get("fecha") or "Sin fecha")
        group = dates.setdefault(day, {
            "fecha": day,
            "neto": Decimal("0.00"),
            "cantidad": 0,
            "cantidad_reversiones": 0,
            "movimientos": [],
        })
        amount = _parse_decimal(item.get("importe_con_signo"))
        if amount is not None:
            group["neto"] += amount
        if index not in grouped_indexes:
            group["cantidad"] += 1
            group["movimientos"].append(item)
        else:
            group["cantidad_reversiones"] += 1
    return [
        {
            **group,
            "neto": str(group["neto"].quantize(Decimal("0.01"))),
        }
        for _, group in sorted(dates.items())
    ]


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
                "banco_ids": list(bank_ids),
                "contabilidad_ids": list(book_ids),
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
                "banco_ids": list(bank_ids),
                "contabilidad_ids": list(book_ids),
                "banco": [bank_movements[int(item.split(":", 1)[1])] for item in bank_ids],
                "contabilidad": [book_movements[int(item.split(":", 1)[1])] for item in book_ids],
                "explicacion": proposal.explanation,
            })

    unmatched_bank = [
        {"indice": index, "movimiento_id": f"bank:{index}", "movimiento": movement, "situacion": "Sin contraparte inequívoca en Contabilidad", "evidencia": evidence_of(movement)}
        for index, movement in enumerate(bank_movements) if index not in claimed_bank
    ]
    unmatched_books = [
        {"indice": index, "movimiento_id": f"books:{index}", "movimiento": movement, "situacion": "Sin contraparte inequívoca en Banco", "evidencia": evidence_of(movement)}
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
                text = " ".join(values).upper()
                cutoff_match = re.search(r"\bAL\s+(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
                if cutoff_match:
                    cutoff = date(*(int(value) for value in (cutoff_match[3], cutoff_match[2], cutoff_match[1])))
                    declared_period = cutoff.strftime("%Y-%m")
                elif any("MES DE " in value for value in values):
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
            column_indexes = None
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                normalized = [str(value).strip().upper() if value is not None else "" for value in row]
                date_index = next((i for i, value in enumerate(normalized) if value == "FECHA"), None)
                document_index = next((
                    i for i, value in enumerate(normalized)
                    if value in {"NO. DOC.", "NO. DOC", "NO. REFERENCIA"}
                ), None)
                amount_index = next((i for i, value in enumerate(normalized) if value == "MONTO"), None)
                if date_index is not None and document_index is not None and amount_index is not None:
                    headers_row = row_number
                    column_indexes = (date_index, document_index, amount_index)
                    break
            if headers_row is None:
                continue
            date_index, document_index, amount_index = column_indexes
            for row_number, row in enumerate(
                sheet.iter_rows(min_row=headers_row + 1, max_row=sheet.max_row, values_only=True),
                start=headers_row + 1,
            ):
                if all(value is None for value in row):
                    continue
                if any(
                    str(value).strip().upper().startswith(("TOTAL", "TTAL"))
                    for value in row if value is not None
                ):
                    continue
                description = row[1] if len(row) > 1 else None
                document = str(row[document_index]).strip() if row[document_index] is not None else ""
                amount = _parse_decimal(row[amount_index])
                if amount is None:
                    raise ValueError(f"Fila {row_number}: el monto del cheque no es numérico.")
                records.append({
                    "fecha": str(row[date_index]).strip() if row[date_index] is not None else None,
                    "descripcion": description,
                    "numero_documento": document,
                    "numero_cero": document in ("", "0"),
                    "monto": str(amount),
                    "hoja": sheet.title,
                    "fila": row_number,
                    "valor_original": list(row[:max(date_index, document_index, amount_index) + 1]),
                    "archivo": path.name,
                })
        return {"periodo_corte": declared_period, "registros": records}
    finally:
        workbook.close()


def _analyze_files(job_dir, country_code, period=None):
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
    requested_period = None
    if period not in (None, ""):
        requested_period, _, _, _ = _period_dates(period)
    for source in sorted(job_dir.glob("originales/*")):
        path = Path(source)
        normalized_path = None
        synthetic_header = False
        is_cheque_file = "cheque" in source.name.casefold()
        try:
            if path.suffix.lower() == ".xls":
                normalized_dir = job_dir / "normalizados"
                normalized_dir.mkdir(parents=True, exist_ok=True)
                normalized_path = normalized_dir / f"{path.stem}.xlsx"
                synthetic_header = _normalize_xls(path, normalized_path)
                path = normalized_path
            recognition = recognize_workbook(path)
        except (BadZipFile, InvalidFileException, OSError, ParseError, ValueError, KeyError, IndexError, ValidationError) as exc:
            if not is_cheque_file:
                raise
            reason = (
                "; ".join(str(value) for value in exc.detail.values())
                if isinstance(exc, ValidationError) and isinstance(exc.detail, dict)
                else str(exc).strip()
            ) or exc.__class__.__name__
            result["diagnosticos"].append({
                "archivo": source.name,
                "codigo": "error_importacion_cheques",
                "mensaje": f"No se pudo leer el archivo de cheques: {reason}",
            })
            result["archivos"].append({
                "nombre": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tamano_bytes": source.stat().st_size,
                "reconocimiento": {"reconocido": False, "motivo": reason},
            })
            continue
        if synthetic_header:
            recognition["encabezado_sintetico"] = True
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
        elif recognition.get("perfil_sugerido") == "banco_cuscatlan_movimientos":
            account_digits = re.sub(r"\D", "", re.sub(r"\(\d+\)", "", source.stem))
            recognition_context.update({
                "banco": "Banco Cuscatlán",
                "pais_codigo": "SV",
                "moneda_codigo": recognition_context.get("moneda_codigo") or "USD",
            })
            if account_digits:
                recognition_context["cuenta_bancaria"] = account_digits
                recognition_context["terminacion"] = account_digits[-4:]
        recognition_origin = recognition.get("origen_sugerido") if recognition.get("reconocido") else None
        evidence = {
            "nombre": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "tamano_bytes": path.stat().st_size,
            "reconocimiento": recognition,
        }
        if recognition_origin == "banco":
            recognition_period = recognition.get("contexto", {}).get("periodo_declarado")
            period_hint = (
                requested_period.strftime("%Y-%m")
                if requested_period else recognition_period or period_hint
            )
            cutoff_hint, _ = _cutoff_from_recognition(recognition)
            validation_period = cutoff_hint.replace(day=1) if cutoff_hint else None
            validation_cutoff = cutoff_hint
            if (
                requested_period
                and recognition.get("perfil_sugerido") == "banco_cuscatlan_movimientos"
            ):
                selected_start, selected_end, previous_start, _ = _period_dates(requested_period)
                validation_period = previous_start
                validation_cutoff = selected_end
            validated = validate_workbook(
                path,
                sheet_name=recognition["hoja"],
                header_row=recognition["fila_encabezados"],
                mapping=recognition["mapping"],
                date_format=recognition.get("date_format"),
                origin="banco",
                period=validation_period,
                cutoff=validation_cutoff,
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
                        period=validation_period,
                        cutoff=validation_cutoff,
                        confirm_date_proposals=True,
                    )
                else:
                    result["diagnosticos"].append({
                        "codigo": "propuestas_normalizacion_fecha",
                        "mensaje": "Hay propuestas de fecha pendientes de confirmación; se conservarán las filas como excluidas hasta resolverlas.",
                        "detalle": validated.propuestas_normalizacion_fecha,
                    })
            bank_movements = validated.movimientos
            is_cuscatlan = recognition.get("perfil_sugerido") == "banco_cuscatlan_movimientos"
            bank_exclusions = validated.exclusiones_requeridas
            if is_cuscatlan:
                for movement in validated.movimientos:
                    origin_data = movement.setdefault("datos_originales", {})
                    origin_data["archivo"] = source.name
                    origin_data["hoja"] = recognition["hoja"]
                    if recognition.get("encabezado_sintetico"):
                        movement["fila_origen"] = max(1, movement["fila_origen"] - 1)
                target_period = requested_period or (
                    date.fromisoformat(f"{recognition_period}-01") if recognition_period else None
                )
                if target_period is None:
                    result["diagnosticos"].append({
                        "archivo": source.name,
                        "codigo": "periodo_cuscatlan_faltante",
                        "mensaje": "Selecciona el mes y año que deseas conciliar.",
                    })
                else:
                    bank_exclusions = []
                    opening, opening_error = _resolve_cuscatlan_opening_balance(
                        validated.movimientos, recognition, target_period
                    )
                    if opening and not opening.get("archivo"):
                        opening["archivo"] = source.name
                    selected_start, selected_end, _, _ = _period_dates(target_period)
                    current_movements = [
                        movement for movement in validated.movimientos
                        if selected_start.isoformat() <= movement["fecha"] <= selected_end.isoformat()
                    ]
                    current_movements.sort(
                        key=lambda movement: (
                            movement.get("fecha") or "",
                            int(movement.get("fila_origen") or 0),
                        )
                    )
                    bank_movements = current_movements
                    context = recognition["contexto"]
                    context["periodo_declarado"] = selected_start.strftime("%Y-%m")
                    context["periodo_seleccionado"] = selected_start.strftime("%Y-%m")
                    current_dates = [movement["fecha"] for movement in current_movements]
                    context["cobertura_periodo_observada"] = (
                        {"desde": min(current_dates), "hasta": max(current_dates)}
                        if current_dates else None
                    )
                    declared_coverage = context.get("cobertura_declarada") or {}
                    context["cobertura_periodo_estado"] = (
                        "completa"
                        if context.get("tipo_cobertura") == "declarada"
                        and declared_coverage
                        and declared_coverage.get("desde", "") <= selected_start.isoformat()
                        and declared_coverage.get("hasta", "") >= selected_end.isoformat()
                        else "incierta"
                    )
                    context["saldo_inicial"] = opening.get("saldo") if opening else None
                    context["saldo_inicial_evidencia"] = opening
                    if opening_error:
                        result["diagnosticos"].append({
                            "archivo": source.name,
                            "codigo": "saldo_inicial_bancario_faltante",
                            "mensaje": opening_error,
                        })
                    if not context["cobertura_periodo_estado"] == "completa":
                        result["diagnosticos"].append({
                            "archivo": source.name,
                            "codigo": "cobertura_bancaria_incierta",
                            "mensaje": (
                                "La cobertura del estado no confirma el cierre del mes seleccionado; "
                                "el saldo final mostrado es el último Saldo Real observado."
                            ),
                        })
                    if current_movements:
                        context["saldo_final_observado"] = current_movements[-1].get("saldo_origen")
                        context["saldo_final_observado_evidencia"] = {
                            "archivo": source.name,
                            "hoja": recognition["hoja"],
                            "fila": current_movements[-1].get("fila_origen"),
                            "fecha": current_movements[-1].get("fecha"),
                        }
                        cutoff_hint = date.fromisoformat(current_movements[-1]["fecha"])
            for movement in validated.movimientos:
                origin_data = movement.setdefault("datos_originales", {})
                origin_data["archivo"] = source.name
                origin_data["hoja"] = recognition["hoja"]
            result["banco"] = {
                "nombre": path.name,
                "reconocimiento": recognition,
                "movimientos": bank_movements,
                "saldo_inicial": recognition.get("contexto", {}).get("saldo_inicial"),
                "saldo_inicial_evidencia": recognition.get("contexto", {}).get("saldo_inicial_evidencia"),
                "filas_analizadas": validated.filas_analizadas,
                "errores": validated.errores,
                "advertencias": validated.advertencias,
                "exclusiones_requeridas": bank_exclusions,
                "propuestas_normalizacion_fecha": validated.propuestas_normalizacion_fecha,
                "normalizaciones_fecha_aplicadas": validated.normalizaciones_fecha_aplicadas,
            }
            result["movimientos_banco"] = bank_movements
        elif recognition_origin == "libros":
            period_hint = recognition.get("contexto", {}).get("periodo_declarado") or period_hint
            cutoff_hint, _ = _cutoff_from_recognition(recognition)
            period = requested_period or (cutoff_hint.replace(day=1) if cutoff_hint else None)
            ledger_cutoff = (
                date(period.year, period.month, monthrange(period.year, period.month)[1])
                if period else cutoff_hint
            )
            validated = validate_workbook(
                path,
                sheet_name=recognition["hoja"],
                header_row=recognition["fila_encabezados"],
                mapping=recognition["mapping"],
                date_format=recognition.get("date_format"),
                origin="libros",
                period=period,
                cutoff=ledger_cutoff,
            )
            for movement in validated.movimientos:
                origin_data = movement.setdefault("datos_originales", {})
                origin_data["archivo"] = source.name
                origin_data["hoja"] = recognition["hoja"]
            ledger_context = recognition.get("contexto") or {}
            opening_evidence = ledger_context.get("saldo_inicial_evidencia")
            if isinstance(opening_evidence, dict):
                opening_evidence["archivo"] = source.name
                opening_evidence["hoja"] = opening_evidence.get("hoja") or recognition["hoja"]
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
            try:
                historical = _historical_cheques(path)
            except (BadZipFile, InvalidFileException, OSError, ParseError, ValueError, KeyError, IndexError, ValidationError) as exc:
                reason = (
                    "; ".join(str(value) for value in exc.detail.values())
                    if isinstance(exc, ValidationError) and isinstance(exc.detail, dict)
                    else str(exc).strip()
                ) or exc.__class__.__name__
                result["diagnosticos"].append({
                    "archivo": source.name,
                    "codigo": "error_importacion_cheques",
                    "mensaje": f"No se pudo importar el archivo de cheques: {reason}",
                })
                historical = None
            if historical is None:
                result["archivos"].append(evidence)
                continue
            if historical["registros"]:
                result["cheques"].append({"nombre": path.name, **historical})
            else:
                result["diagnosticos"].append({
                    "archivo": source.name,
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
                    if str(movement.get("codigo_tt") or "").strip().upper() != "CQ":
                        continue
                    refs = {
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
                    if number in refs and bank_amount is not None and bank_amount == cheque_amount:
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
                    "tipo_movimiento": movement.get("codigo_tt"),
                    "referencia": number,
                    "referencias_banco": sorted({
                        str(value).strip() for value in (
                            movement.get("referencia"),
                            movement.get("numero_transaccion"),
                            movement.get("numero_cheque"),
                        ) if value not in (None, "")
                    }),
                    "monto": movement.get("monto"),
                    "movimiento_id": f"bank:{index}",
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
    pending_bank_ids = {
        item.get("movimiento_id")
        for item in result.get("proceso_conciliacion", {}).get("movimientos_sin_contraparte", {}).get("banco", [])
    }
    partidas = []
    for cut in result.get("cheques", []):
        for record in cut.get("registros", []):
            cobros = record.get("cobro_identificado", [])
            if len(cobros) == 1:
                cobro = cobros[0]
                bank_amount = _parse_decimal(cobro.get("monto"))
                affects_residual = (
                    cobro.get("movimiento_id") in pending_bank_ids
                    and bank_amount is not None
                    and bank_amount < 0
                )
                partidas.append({
                    "id": cobro.get("movimiento_id"),
                    "movimiento_id": cobro.get("movimiento_id"),
                    "tipo": "cheque_historico_cobrado",
                    "concepto": f"Cobro identificado del cheque histórico {record.get('numero_documento')}",
                    "importe": cobro.get("monto"),
                    "importe_con_signo": str(bank_amount) if bank_amount is not None else None,
                    "lado": "Banco",
                    "signo": "salida" if bank_amount is not None and bank_amount < 0 else "pendiente",
                    "diferencia_signo": str(bank_amount) if affects_residual else "0.00",
                    "afecta_residual": affects_residual,
                    "movimiento_id": cobro.get("movimiento_id"),
                    "motivo": (
                        "Cargo bancario CQ identificado por referencia e importe exactos; sigue sin contraparte contable y se aplica una sola vez al residual."
                        if affects_residual else
                        "Cargo bancario CQ identificado, pero ya está consumido por otro cruce o no es un cargo pendiente; no se vuelve a aplicar al residual."
                    ),
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
        "saldo_inicial_banco": (
            bank.get("saldo_inicial") if bank and bank.get("saldo_inicial") is not None else None
        ),
        "saldo_inicial_banco_evidencia": (
            bank.get("saldo_inicial_evidencia") if bank else None
        ),
        "cobertura_periodo_bancario": (
            bank.get("reconocimiento", {}).get("contexto", {}).get("cobertura_periodo_estado")
            if bank else None
        ),
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
    _difference_explanation(result)
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


def _difference_explanation(result):
    summary = result.get("resumen", {})
    proceso = result.get("proceso_conciliacion", {})
    original = _parse_decimal(summary.get("diferencia_original"))
    explanatory = []
    applied = []
    applied_effect = Decimal("0.00")
    for partida in summary.get("partidas_conciliatorias", []):
        amount = _parse_decimal(partida.get("importe_con_signo", partida.get("importe")))
        affects = bool(partida.get("afecta_residual"))
        effect = _parse_decimal(partida.get("diferencia_signo")) if affects else Decimal("0.00")
        if effect is None:
            effect = Decimal("0.00")
        item = {
            "id": partida.get("movimiento_id", partida.get("id")),
            "concepto": partida.get("concepto") or partida.get("tipo") or "Partida",
            "importe_con_signo": str(amount) if amount is not None else None,
            "lado": partida.get("lado") or "No identificado",
            "motivo": partida.get("motivo") or "No hay evidencia registrada.",
            "evidencia": {
                "banco": partida.get("evidencia_banco"),
                "historico": partida.get("evidencia_historico"),
            },
            "verificada": bool(partida.get("verificado")),
            "afecta_residual": affects,
            "impacto_residual": str(effect),
        }
        explanatory.append(item)
        if affects and item["verificada"]:
            applied.append(item)
            applied_effect += effect

    residual = (original + applied_effect).quantize(Decimal("0.01")) if original is not None else None
    summary["diferencia_residual_pendiente"] = str(residual) if residual is not None else None
    unmatched = proceso.get("movimientos_sin_contraparte", {})
    identified_historical_bank_ids = {
        partida.get("movimiento_id")
        for partida in summary.get("partidas_conciliatorias", [])
        if partida.get("tipo") == "cheque_historico_cobrado" and partida.get("verificado")
    }

    def pending(side):
        total = Decimal("0")
        items = []
        for item in unmatched.get(side, []):
            if side == "banco" and item.get("movimiento_id") in identified_historical_bank_ids:
                continue
            movement = item.get("movimiento", {})
            amount = _parse_decimal(movement.get("monto"))
            if amount is not None:
                total += amount
            items.append({
                "id": item.get("movimiento_id"),
                "fecha": movement.get("fecha"),
                "referencia": _movement_reference(movement),
                "descripcion": movement.get("descripcion"),
                "importe_con_signo": str(amount) if amount is not None else None,
                "movimiento": movement,
                "evidencia": item.get("evidencia"),
                "estado_al_cierre": item.get("situacion") or "Sin contraparte inequívoca al cierre",
            })
        return {"neto": str(total.quantize(Decimal("0.01"))), "movimientos": items}

    bank_pending = pending("banco")
    books_pending = pending("contabilidad")
    bank_pending["grupos_fecha"] = _pending_date_groups(bank_pending["movimientos"], set())
    reversal_groups, reversal_review, grouped_book_indexes = _pending_reversal_groups(
        books_pending["movimientos"]
    )
    books_pending["grupos_fecha"] = _pending_date_groups(
        books_pending["movimientos"], grouped_book_indexes
    )
    books_pending["reversiones_identificadas"] = reversal_groups
    books_pending["reversiones_por_revisar"] = reversal_review
    candidates = []
    candidates.extend({
        "tipo": "reversion_ambigua",
        "criterio": f"Referencia de pago {group['referencia']}",
        "explicacion": group["motivo"],
        "contabilidad": [
            {
                "id": item.get("id"),
                "movimiento": item.get("movimiento", {}),
                "evidencia": item.get("evidencia"),
            }
            for item in group["movimientos"]
        ],
        "banco": [],
    } for group in reversal_review)
    for category, groups in (
        ("agrupacion_ambigua", proceso.get("alternativas_ambiguas", [])),
        ("diferencia_de_importe", proceso.get("diferencias_importe", [])),
    ):
        for group in groups:
            candidates.append({
                "tipo": category,
                "criterio": group.get("criterio"),
                "explicacion": group.get("explicacion"),
                "banco": [
                    {
                        "id": f"bank:{index}",
                        "movimiento": movement,
                        "evidencia": {
                            "archivo": (movement.get("datos_originales") or {}).get("archivo"),
                            "hoja": (movement.get("datos_originales") or {}).get("hoja"),
                            "fila": movement.get("fila_origen"),
                        },
                    }
                    for index, movement in zip(group.get("banco_indices", []), group.get("banco", []))
                ],
                "contabilidad": [
                    {
                        "id": f"books:{index}",
                        "movimiento": movement,
                        "evidencia": {
                            "archivo": (movement.get("datos_originales") or {}).get("archivo"),
                            "hoja": (movement.get("datos_originales") or {}).get("hoja"),
                            "fila": movement.get("fila_origen"),
                        },
                    }
                    for index, movement in zip(group.get("contabilidad_indices", []), group.get("contabilidad", []))
                ],
            })

    bank_context = ((result.get("banco") or {}).get("reconocimiento") or {}).get("contexto") or {}
    ledger_context = ((result.get("libros") or {}).get("reconocimiento") or {}).get("contexto") or {}
    direction = "banco_mayor" if original is not None and original > 0 else (
        "banco_menor" if original is not None and original < 0 else "iguales" if original == 0 else "indeterminada"
    )
    currency = str(bank_context.get("moneda_codigo") or ledger_context.get("moneda_codigo") or "").upper()
    currency_prefix = "$" if currency == "USD" else "Q " if currency == "GTQ" else ""
    if residual is None:
        formatted_residual = "No disponible"
    else:
        residual_amount = Decimal(str(residual)).quantize(Decimal("0.01"))
        signed_amount = f"{abs(residual_amount):,.2f}"
        formatted_residual = f"-{currency_prefix}{signed_amount}" if residual_amount < 0 else f"{currency_prefix}{signed_amount}"
    bank_opening = _parse_decimal(bank_context.get("saldo_inicial"))
    books_opening = _parse_decimal(ledger_context.get("saldo_inicial"))
    bank_opening_evidence = bank_context.get("saldo_inicial_evidencia")
    books_opening_evidence = ledger_context.get("saldo_inicial_evidencia")

    def has_opening_evidence(evidence):
        return isinstance(evidence, dict) and any(
            evidence.get(key) not in (None, "")
            for key in ("archivo", "hoja", "fila", "fecha", "metodo")
        )

    opening_difference = (
        (bank_opening - books_opening).quantize(Decimal("0.01"))
        if bank_opening is not None
        and books_opening is not None
        and has_opening_evidence(bank_opening_evidence)
        and has_opening_evidence(books_opening_evidence)
        else None
    )
    movement_difference = (
        Decimal(bank_pending["neto"]) - Decimal(books_pending["neto"])
    ).quantize(Decimal("0.01"))
    breakdown_total = (
        (opening_difference + movement_difference).quantize(Decimal("0.01"))
        if opening_difference is not None else None
    )
    summary["explicacion_diferencia"] = {
        "version": 1,
        "diferencia_original": {
            "saldo_banco": summary.get("saldo_final_banco"),
            "saldo_contabilidad": summary.get("saldo_final_contabilidad"),
            "importe": summary.get("diferencia_original"),
            "importe_absoluto": str(abs(original)) if original is not None else None,
            "formula": "saldo_banco - saldo_contabilidad",
            "direccion": direction,
        },
        "residual": {
            "partidas_aplicadas": applied,
            "impacto_total_aplicado": str(applied_effect.quantize(Decimal("0.01"))),
            "importe": summary.get("diferencia_residual_pendiente"),
            "mensaje_sin_partidas": (
                f"No hay partidas explicativas aplicadas; la diferencia residual sigue siendo {formatted_residual}."
                if not applied and summary.get("diferencia_residual_pendiente") is not None else None
            ),
        },
        "partidas_explicativas": explanatory,
        "pendientes": {
            "banco": bank_pending,
            "contabilidad": books_pending,
            "diferencia_neta_movimientos": str(movement_difference),
        },
        "candidatos": candidates,
        "cobertura_y_saldos_iniciales": {
            "banco": {
                "cobertura": bank_context.get("cobertura_declarada"),
                "saldo_inicial": bank_context.get("saldo_inicial"),
                "evidencia": bank_context.get("saldo_inicial_evidencia"),
            },
            "contabilidad": {
                "cobertura": ledger_context.get("cobertura_declarada"),
                "saldo_inicial": ledger_context.get("saldo_inicial"),
                "evidencia": ledger_context.get("saldo_inicial_evidencia"),
            },
        },
        "diferencia_inicial": {
            "saldo_banco": str(bank_opening) if bank_opening is not None else None,
            "saldo_contabilidad": str(books_opening) if books_opening is not None else None,
            "importe": str(opening_difference) if opening_difference is not None else None,
            "evidencia_banco": bank_opening_evidence,
            "evidencia_contabilidad": books_opening_evidence,
            "origen_pendiente": (
                "Origen pendiente de revisar en el cierre anterior"
                if opening_difference is not None else None
            ),
        },
        "desglose_diferencia": {
            "diferencia_inicial": str(opening_difference) if opening_difference is not None else None,
            "diferencia_neta_movimientos": str(movement_difference),
            "total": str(breakdown_total) if breakdown_total is not None else None,
            "coincide_con_diferencia_original": (
                breakdown_total == original.quantize(Decimal("0.01"))
                if breakdown_total is not None and original is not None else False
            ),
        },
        "causa_determinada": bool(applied) and residual == 0,
    }


def _job_period(job):
    value = job.get("periodo_sugerido")
    if not value:
        value = (job.get("resultado") or {}).get("contexto_resultado", {}).get("periodo")
    match = re.match(r"^(\d{4}-\d{2})", str(value or ""))
    return match.group(1) if match else None


def _historical_difference_evidence(result, scope, account, current_job_id=None):
    summary = result.get("resumen") or {}
    explanation = summary.get("explicacion_diferencia") or {}
    context = result.get("contexto_resultado") or {}
    current_period = _job_period({"periodo_sugerido": context.get("periodo")})
    if not current_period:
        return
    try:
        current_start, _, _, _ = _period_dates(current_period)
    except ValidationError:
        return
    previous_period = (current_start - timedelta(days=1)).strftime("%Y-%m")
    selected_account_id = str(account.get("id") or "")
    selected_currency = str(account.get("moneda_codigo") or "").upper()
    history_root = _scope_jobs_path(scope)
    related_jobs = []
    if history_root.exists():
        for metadata_path in history_root.glob("*/*/metadata.json"):
            try:
                saved_job = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if (
                saved_job.get("id") == current_job_id
                or saved_job.get("empresa_id") != scope.get("company_id")
                or saved_job.get("pais_codigo") != scope.get("country_code")
                or str(saved_job.get("cuenta_id") or "") != selected_account_id
                or str(saved_job.get("moneda_codigo") or "").upper() != selected_currency
                or saved_job.get("sustituido_por")
            ):
                continue
            saved_result = saved_job.get("resultado") or {}
            saved_bank_context = (
                ((saved_result.get("banco") or {}).get("reconocimiento") or {}).get("contexto") or {}
            )
            if str(saved_bank_context.get("moneda_codigo") or "").upper() != selected_currency:
                continue
            related_jobs.append((saved_job, saved_result, _job_period(saved_job)))

    previous_jobs = [
        saved_job for saved_job, _, period in related_jobs
        if period == previous_period
        and saved_job.get("estado") in ("finalizado_con_pendientes", "finalizado", "aprobado")
    ]
    previous_jobs.sort(
        key=lambda job: (str(job.get("finalizado_en") or job.get("actualizado_en") or ""), str(job.get("id") or "")),
        reverse=True,
    )
    previous = previous_jobs[0] if previous_jobs else None
    explanation["conciliacion_anterior"] = ({
        "id": previous.get("id"),
        "periodo": previous_period,
        "titulo": previous.get("titulo") or f"Conciliación de {previous_period}",
    } if previous else None)
    opening_difference = explanation.get("diferencia_inicial") or {}
    if opening_difference.get("importe") is not None:
        opening_difference["origen_pendiente"] = (
            None if previous else "Origen pendiente de revisar en el cierre anterior"
        )

    later_bank_movements = []
    seen_sources = set()
    for saved_job, saved_result, period in related_jobs:
        if not period or period <= current_period:
            continue
        bank = saved_result.get("banco") or {}
        for movement in bank.get("movimientos", []):
            movement_date = str(movement.get("fecha") or "")
            if movement_date[:7] <= current_period:
                continue
            references = _movement_references(movement)
            amount = _parse_decimal(movement.get("monto"))
            if not references or amount is None:
                continue
            original = movement.get("datos_originales") or {}
            source = (
                movement_date,
                tuple(sorted(references)),
                str(amount),
                original.get("archivo"),
                original.get("hoja"),
                movement.get("fila_origen"),
            )
            if source in seen_sources:
                continue
            seen_sources.add(source)
            later_bank_movements.append({
                "fecha": movement_date,
                "referencias": references,
                "monto": amount,
                "periodo": period,
                "evidencia": {
                    "archivo": original.get("archivo"),
                    "hoja": original.get("hoja"),
                    "fila": movement.get("fila_origen"),
                },
                "movimiento": movement,
            })

    pending_books = explanation.get("pendientes", {}).get("contabilidad", {}).get("movimientos", [])
    for item in pending_books:
        movement = item.get("movimiento") or {}
        item["reflejado_en_periodo_posterior"] = None
        references = _movement_references(movement)
        amount = _parse_decimal(movement.get("monto"))
        if not references or amount is None:
            continue
        matches = [
            bank_item for bank_item in later_bank_movements
            if references & bank_item["referencias"] and amount == bank_item["monto"]
        ]
        matching_books = [
            candidate for candidate in pending_books
            if _parse_decimal((candidate.get("movimiento") or {}).get("monto")) == amount
            and references & _movement_references(candidate.get("movimiento") or {})
        ]
        if len(matches) == 1 and len(matching_books) == 1:
            bank_item = matches[0]
            item["reflejado_en_periodo_posterior"] = {
                "estado": "Reflejado en el banco en un período posterior",
                "fecha": bank_item["fecha"],
                "periodo": bank_item["periodo"],
                "referencia": sorted(references & bank_item["referencias"])[0],
                "importe_con_signo": str(bank_item["monto"]),
                "evidencia": bank_item["evidencia"],
            }


def _validate_job_account(scope, job, requested_account_id=None, validate_result=True):
    account_id = str(job.get("cuenta_id") or "").strip()
    if not account_id:
        raise ValidationError({"cuenta_id": "El trabajo no tiene una cuenta elegida. Conserva los documentos y vuelve a analizarlos después de seleccionarla."})
    if requested_account_id and str(requested_account_id).strip() != account_id:
        raise ValidationError({"cuenta_id": "No se puede reasignar un resultado calculado a otra cuenta. Selecciona la cuenta original o inicia un análisis nuevo."})
    try:
        UUID(account_id)
    except ValueError as exc:
        raise ValidationError({"cuenta_id": "El identificador de cuenta del trabajo no es válido."}) from exc

    try:
        with connection.cursor() as cursor:
            account = _authorized_account(cursor, scope, account_id)
            available_accounts = _authorized_accounts(cursor, scope)
    except DatabaseError as exc:
        raise DatabaseUnavailable() from exc
    diagnostics = []
    suggestion = job.get("vinculacion_cuenta")
    if validate_result:
        diagnostics, suggestion = _validate_account_result(
            account,
            available_accounts,
            job.get("resultado") or {},
            scope["country_code"],
        )
        if diagnostics:
            raise ValidationError({
                "cuenta_id": diagnostics[0]["mensaje"],
                "diagnosticos": diagnostics,
            })
    return account, suggestion


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
        "cuenta_seleccionada": job.get("cuenta_seleccionada"),
        "vinculacion_cuenta": job.get("vinculacion_cuenta"),
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
        root = _scope_jobs_path(scope)
        cuenta_id = request.query_params.get("cuenta_id", "").strip()
        banco_id = request.query_params.get("banco_id", "").strip()
        jobs = []
        if root.exists():
            for metadata in sorted(root.glob("*/*/metadata.json"), reverse=True):
                try:
                    job = json.loads(metadata.read_text(encoding="utf-8"))
                    if (
                        job.get("empresa_id") != scope["company_id"]
                        or job.get("pais_codigo") != scope["country_code"]
                    ):
                        continue
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
        scope = _writer_access(request)
        country = request.data.get("pais_codigo", scope["country_code"])
        if country != scope["country_code"] or country not in ("GT", "SV"):
            raise ValidationError({"pais_codigo": "El país seleccionado no está autorizado para la sesión."})
        account_id = str(request.data.get("cuenta_id") or "").strip()
        if not account_id:
            raise ValidationError({"cuenta_id": "Elige una cuenta autorizada antes de iniciar el análisis."})
        try:
            UUID(account_id)
        except ValueError as exc:
            raise ValidationError({"cuenta_id": "El identificador de cuenta no es válido."}) from exc
        try:
            with connection.cursor() as cursor:
                selected_account = _authorized_account(cursor, scope, account_id)
                available_accounts = _authorized_accounts(cursor, scope)
        except DatabaseError as exc:
            raise DatabaseUnavailable() from exc
        requested_period = request.data.get("periodo")
        if _is_cuscatlan_account(selected_account):
            if not requested_period:
                raise ValidationError({"periodo": "Selecciona el mes y año a conciliar para Banco Cuscatlán."})
            _period_dates(requested_period)
        else:
            requested_period = None
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
            "cuenta_id": selected_account["id"],
            "banco_id": selected_account["banco_id"],
            "moneda_codigo": selected_account["moneda_codigo"],
            "cuenta_seleccionada": selected_account,
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
            resultado = _analyze_files(job_dir, country, period=requested_period)
            account_diagnostics, account_link = _validate_account_result(
                selected_account,
                available_accounts,
                resultado,
                country,
            )
            resultado["diagnosticos"].extend(account_diagnostics)
            _historical_difference_evidence(
                resultado, scope, selected_account, current_job_id=job_id
            )
            estado = "preparado" if resultado["banco"] and resultado["libros"] and not resultado["diagnosticos"] else "requiere_aclaraciones"
            context = resultado["banco"]["reconocimiento"]["contexto"] if resultado["banco"] else {}
            job["estado"] = estado
            job["periodo_sugerido"] = (
                str(requested_period) if requested_period else context.get("periodo_declarado")
            )
            job["resultado"] = resultado
            job["vinculacion_cuenta"] = account_link
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
        requested_account_id = request.query_params.get("cuenta_id")
        if requested_account_id and requested_account_id != job.get("cuenta_id"):
            raise ValidationError({"cuenta_id": "El trabajo no pertenece a la cuenta solicitada."})
        result = job.get("resultado") or {}
        if result.get("resumen") and result.get("proceso_conciliacion"):
            result_context = result.setdefault("contexto_resultado", {})
            if isinstance(result_context, dict):
                result_context.setdefault("periodo", job.get("periodo_sugerido"))
            _difference_explanation(result)
            account = job.get("cuenta_seleccionada") or {
                "id": job.get("cuenta_id"),
                "moneda_codigo": job.get("moneda_codigo"),
            }
            _historical_difference_evidence(result, scope, account, current_job_id=job_id)
        return Response(_job_response(job_dir, job))

    def post(self, request, job_id):
        scope = _writer_access(request)
        job_dir = _job_path(scope, job_id)
        if not job_dir.exists():
            raise NotFound("El trabajo de conciliación no existe.")
        job = _read_json(job_dir / "metadata.json")
        action = str(request.data.get("accion", "")).strip()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        if action == "vincular_numero":
            if scope.get("role") != "administrador":
                raise PermissionDenied("Solo un administrador puede confirmar el vínculo del número completo.")
            account, suggestion = _validate_job_account(
                scope,
                job,
                request.data.get("cuenta_id"),
            )
            if not suggestion:
                if account.get("numero_completo_verificado"):
                    return Response(_job_response(job_dir, job))
                raise ValidationError({"cuenta_id": "No hay una coincidencia única de número completo para confirmar."})
            full_number = suggestion["numero_completo"]
            ending = suggestion["terminacion"]
            if scope["country_code"] != "SV" or len(full_number) <= 4 or not full_number.endswith(ending):
                raise ValidationError({"cuenta_id": "El número detectado no puede vincularse con seguridad a esta terminación."})
            original_job = json.loads(json.dumps(job, ensure_ascii=False))
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                        cursor.execute(
                            """
                            SELECT id::text,banco_id::text,numero,alias,moneda_codigo,numero_terminacion
                            FROM contared.cuentas_bancarias
                            WHERE id=%s AND empresa_id=%s AND pais_codigo='SV' AND activa IS TRUE
                            FOR UPDATE
                            """,
                            [account["id"], scope["company_id"]],
                        )
                        current = cursor.fetchone()
                        if current is None:
                            raise ValidationError({"cuenta_id": "La cuenta dejó de estar activa o ya no pertenece al ámbito autorizado."})
                        current_ending = _account_digits(current[5]) or _account_digits(current[2])
                        if len(current_ending) != 4 or not full_number.endswith(current_ending):
                            raise ValidationError({"cuenta_id": "La terminación operativa ya no coincide con el número detectado."})
                        cursor.execute(
                            """
                            SELECT id::text FROM contared.cuentas_bancarias
                            WHERE empresa_id=%s AND pais_codigo='SV' AND banco_id=%s
                              AND moneda_codigo=%s AND btrim(numero)=btrim(%s) AND id<>%s
                            LIMIT 1
                            """,
                            [scope["company_id"], current[1], current[4], full_number, account["id"]],
                        )
                        duplicate = cursor.fetchone()
                        if duplicate:
                            raise ValidationError({
                                "cuenta_id": "Ya existe otra cuenta operativa con ese número completo. No se creó un duplicado; selecciona la cuenta existente e inicia un análisis nuevo."
                            })
                        cursor.execute(
                            """
                            UPDATE contared.cuentas_bancarias
                            SET numero=%s,numero_terminacion=%s,numero_completo_verificado=true,
                                incorporacion_provisional=false
                            WHERE id=%s AND empresa_id=%s AND pais_codigo='SV'
                            """,
                            [full_number, current_ending, account["id"], scope["company_id"]],
                        )
                    account["numero"] = full_number
                    account["numero_terminacion"] = current_ending
                    account["numero_completo_verificado"] = True
                    account["incorporacion_provisional"] = False
                    job["cuenta_seleccionada"] = account
                    job["vinculacion_cuenta"] = None
                    job["resultado"]["contexto_resultado"]["cuenta_operativa"] = full_number
                    job["actualizado_en"] = now
                    _write_json(job_dir / "metadata.json", job)
            except DatabaseError as exc:
                _write_json(job_dir / "metadata.json", original_job)
                raise DatabaseUnavailable() from exc
            except OSError as exc:
                _write_json(job_dir / "metadata.json", original_job)
                raise ValidationError({"detail": "No se pudo guardar el vínculo; el cambio en la cuenta se revirtió."}) from exc
            return Response(_job_response(job_dir, job))

        if action == "finalizar":
            original_job = json.loads(json.dumps(job, ensure_ascii=False))
            account, suggestion = _validate_job_account(
                scope,
                job,
                request.data.get("cuenta_id"),
            )
            if job.get("estado") in ("finalizado_con_pendientes", "finalizado", "aprobado"):
                return Response(_job_response(job_dir, job))
            contexto = job.get("resultado", {}).get("contexto_resultado", {})
            job["cuenta_id"] = account["id"]
            job["banco_id"] = account["banco_id"]
            job["moneda_codigo"] = account["moneda_codigo"]
            job["cuenta_seleccionada"] = account
            job["vinculacion_cuenta"] = suggestion
            contexto["cuenta_operativa"] = str(account["numero"])
            contexto["moneda"] = account["moneda_codigo"]
            job.setdefault("resultado", {})["contexto_resultado"] = contexto
            resumen = job.get("resultado", {}).get("resumen", {})
            residual = resumen.get("diferencia_residual_pendiente")
            residual_amount = _parse_decimal(residual)
            job["estado"] = "finalizado_con_pendientes" if residual_amount is None or residual_amount != 0 else "finalizado"
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
                with transaction.atomic():
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
                    _write_json(job_dir / "metadata.json", job)
            except DatabaseError as exc:
                _write_json(job_dir / "metadata.json", original_job)
                raise DatabaseUnavailable() from exc
            except OSError as exc:
                _write_json(job_dir / "metadata.json", original_job)
                raise ValidationError({"detail": "No se pudo guardar el respaldo privado; el cambio en PostgreSQL se revirtió."}) from exc
            return Response(_job_response(job_dir, job))

        if action == "recalcular":
            selected_account, _ = _validate_job_account(
                scope,
                job,
                request.data.get("cuenta_id"),
                validate_result=False,
            )
            requested_period = request.data.get("periodo")
            if _is_cuscatlan_account(selected_account):
                requested_period = (
                    requested_period
                    or (job.get("resultado") or {}).get("contexto_resultado", {}).get("periodo")
                    or job.get("periodo_sugerido")
                )
                if not requested_period:
                    raise ValidationError({"periodo": "Selecciona el mes y año a conciliar para Banco Cuscatlán."})
                _period_dates(requested_period)
            else:
                requested_period = None
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
                    "cuenta_id": selected_account["id"], "banco_id": selected_account["banco_id"],
                    "moneda_codigo": selected_account["moneda_codigo"],
                    "cuenta_seleccionada": selected_account,
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
                resultado = _analyze_files(
                    new_dir, scope["country_code"], period=requested_period
                )
                account_diagnostics, account_link = _validate_account_result(
                    selected_account,
                    _authorized_accounts_for_scope(scope),
                    resultado,
                    scope["country_code"],
                )
                resultado["diagnosticos"].extend(account_diagnostics)
                _historical_difference_evidence(
                    resultado, scope, selected_account, current_job_id=new_id
                )
                new_job["resultado"] = resultado
                new_job["estado"] = "preparado" if resultado["banco"] and resultado["libros"] and not resultado["diagnosticos"] else "requiere_aclaraciones"
                new_job["vinculacion_cuenta"] = account_link
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
        if scope.get("role") != "administrador":
            raise PermissionDenied("Solo un administrador puede eliminar definitivamente un trabajo.")
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
        requested_account_id = request.query_params.get("cuenta_id")
        if requested_account_id and requested_account_id != job.get("cuenta_id"):
            raise ValidationError({"cuenta_id": "El informe no pertenece a la cuenta solicitada."})
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
        context = result.get("contexto_resultado", {}) if isinstance(result.get("contexto_resultado"), dict) else {}
        bank_context = result.get("banco", {}).get("reconocimiento", {}).get("contexto", {}) if isinstance(result.get("banco"), dict) else {}
        selected_account = job.get("cuenta_seleccionada") or {}
        account = selected_account.get("numero") or context.get("cuenta") or context.get("numero_cuenta") or bank_context.get("cuenta_bancaria") or "Por confirmar"
        currency = selected_account.get("moneda_codigo") or context.get("moneda") or context.get("moneda_codigo") or bank_context.get("moneda_codigo") or "Por confirmar"
        canonical_gt = CUENTAS_GT_CONFIRMADAS.get(str(account).strip()) if job.get("pais_codigo") == "GT" else None
        if canonical_gt:
            bank_name = canonical_gt[0]
            currency = canonical_gt[2]
        period = context.get("periodo") or bank_context.get("periodo_declarado") or job.get("periodo_sugerido") or "Por confirmar"
        cutoff = context.get("fecha_corte") or bank_context.get("cobertura_declarada", {}).get("hasta") or "Por confirmar"
        bank_name = canonical_gt[0] if canonical_gt else selected_account.get("banco") or context.get("banco") or ("Banco Industrial" if account == "0040040592" else "Por confirmar")
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
            symbol = {"GTQ": "Q", "USD": "$"}.get(currency, "") if prefix else ""
            sign = "-" if amount < 0 else ""
            return f"{sign}{symbol}{abs(amount).quantize(Decimal('0.01')):,.2f}"

        styles = getSampleStyleSheet()
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

        explanation = summary.get("explicacion_diferencia", {})
        applied_items = explanation.get("residual", {}).get("partidas_aplicadas", [])
        bank_balance = _parse_decimal(summary.get("saldo_final_banco"))
        book_balance = _parse_decimal(summary.get("saldo_final_contabilidad"))
        residual = _parse_decimal(summary.get("diferencia_residual_pendiente"))
        red = colors.HexColor("#7F1D1D")
        ink = colors.HexColor("#111827")
        border = colors.HexColor("#E5E7EB")
        muted = colors.HexColor("#4B5563")
        heading = ParagraphStyle("ConciliacionHeading", parent=body, fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=ink)
        card_label = ParagraphStyle("ConciliacionCardLabel", parent=body, fontName="Helvetica-Bold", textColor=muted)
        card_amount = ParagraphStyle("ConciliacionCardAmount", parent=body, fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=ink)
        card_width = 2.34 * inch
        gap = 0.14 * inch
        cards = []
        for label, amount in (
            ("Saldo final Banco", bank_balance),
            ("Saldo final Contabilidad", book_balance),
            ("Diferencia residual", residual),
        ):
            highlight = label == "Diferencia residual" and amount not in (None, 0)
            amount_style = ParagraphStyle("ConciliacionResidualAmount", parent=card_amount, textColor=red) if highlight else card_amount
            card = Table([[[Paragraph(label, card_label), Spacer(1, 4), Paragraph(money(amount), amount_style)]]], colWidths=[card_width])
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF2F2" if highlight else "#F9FAFB")),
                ("BOX", (0, 0), (-1, -1), 0.6, border),
                ("LINEABOVE", (0, 0), (-1, 0), 2, red if highlight else border),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 11),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
            ]))
            cards.append(card)
        cifras = Table([[cards[0], "", cards[1], "", cards[2]]], colWidths=[card_width, gap, card_width, gap, card_width])
        cifras.setStyle(TableStyle([
            (command, (0, 0), (-1, -1), 0)
            for command in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")
        ]))
        story.extend([cifras, Spacer(1, 15)])

        item_types = {}
        for item in summary.get("partidas_conciliatorias", []):
            item_id = item.get("movimiento_id") or item.get("id")
            if item_id is not None:
                item_types[item_id] = item.get("tipo")

        deposits = {"banco": Decimal("0.00"), "contabilidad": Decimal("0.00")}
        checks = deposits.copy()
        others = deposits.copy()
        groups = {"deposito_transito": deposits, "cheque_historico_cobrado": checks}
        effects_available = True
        has_other_items = False
        for item in applied_items:
            effect = _parse_decimal(item.get("impacto_residual"))
            side = str(item.get("lado") or "").strip().casefold()
            if effect is None or side not in deposits:
                effects_available = False
                continue
            group = groups.get(item_types.get(item.get("id")), others)
            has_other_items |= group is others
            # The residual is Banco minus Contabilidad.
            group[side] += effect if side == "banco" else -effect

        balanced_values = effects_available and all(value is not None for value in (bank_balance, book_balance, residual))
        if balanced_values:
            bank_subtotal = bank_balance + deposits["banco"]
            book_subtotal = book_balance + deposits["contabilidad"]
            adjusted_bank = bank_subtotal + checks["banco"] + others["banco"]
            adjusted_books = book_subtotal + checks["contabilidad"] + others["contabilidad"]
            balanced_values = (adjusted_bank - adjusted_books).quantize(Decimal("0.01")) == residual

        rows = [
            ["Concepto", "Contabilidad", "Banco", "Observación"],
            ["Saldos finales", money(book_balance), money(bank_balance), Paragraph(f"Diferencia original de {money(summary.get('diferencia_original'))}.", small)],
        ]
        if balanced_values:
            rows.extend([
                ["Depósitos en circulación", money(deposits["contabilidad"]), money(deposits["banco"]), Paragraph(
                    "Importes aplicados por lado." if any(deposits.values()) else "Sin depósitos aplicados.", small,
                )],
                ["Subtotal", money(book_subtotal), money(bank_subtotal), Paragraph("Saldos después de los depósitos aplicados.", small)],
                ["Cheques en circulación", money(checks["contabilidad"]), money(checks["banco"]), Paragraph(
                    "Cobros de cheques de períodos anteriores." if any(checks.values()) else "Sin partidas de cheques aplicadas.", small,
                )],
            ])
            if has_other_items:
                rows.append(["Otras partidas aplicadas", money(others["contabilidad"]), money(others["banco"]), Paragraph("Otras partidas verificadas aplicadas al saldo.", small)])
            balance_label = "Saldo conciliado" if residual == 0 else "Saldo ajustado"
            rows.append([balance_label, money(adjusted_books), money(adjusted_bank), Paragraph(
                "Los saldos ajustados coinciden." if residual == 0 else "Queda una diferencia por conciliar.", small,
            )])
        else:
            rows.append(["Saldos ajustados", "—", "—", Paragraph("El resultado guardado no permite desglosar el ajuste por lado.", small)])
        rows.append(["Diferencia residual", "", money(residual), Paragraph("Banco menos Contabilidad.", small)])

        balance_table = Table(rows, colWidths=[1.82*inch, 1.15*inch, 1.15*inch, 3.18*inch])
        balance_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), red),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -2), (-1, -2), colors.HexColor("#EAF4E8" if balanced_values and residual == 0 else "#F3F4F6")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FEF2F2" if residual not in (None, 0) else "#F9FAFB")),
            ("ALIGN", (1, 0), (2, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.45, border),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        signatures = Table([
            ["", "", ""],
            ["Auxiliar de Contabilidad", "", "Contador General"],
            ["Nombre y firma", "", "Nombre y firma"],
        ], colWidths=[3.18*inch, 0.94*inch, 3.18*inch], rowHeights=[31, 16, 14])
        signatures.setStyle(TableStyle([
            ("LINEABOVE", (0, 1), (0, 1), 0.6, muted),
            ("LINEABOVE", (2, 1), (2, 1), 0.6, muted),
            ("ALIGN", (0, 1), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 2), (-1, 2), 8),
            ("TEXTCOLOR", (0, 2), (-1, 2), muted),
        ]))
        story.append(KeepTogether([Paragraph("Cuadro de conciliación", heading), Spacer(1, 6), balance_table, signatures]))

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
        selected_account = job.get("cuenta_seleccionada") or {}
        account = selected_account.get("numero") or context.get("cuenta") or context.get("numero_cuenta") or bank_context.get("cuenta_bancaria") or "Por confirmar"
        currency = selected_account.get("moneda_codigo") or context.get("moneda") or context.get("moneda_codigo") or bank_context.get("moneda_codigo") or "Por confirmar"
        canonical_gt = CUENTAS_GT_CONFIRMADAS.get(str(account).strip()) if job.get("pais_codigo") == "GT" else None
        if canonical_gt:
            currency = canonical_gt[2]
        bank_name = canonical_gt[0] if canonical_gt else selected_account.get("banco") or context.get("banco") or ("Banco Industrial" if account == "0040040592" else "Por confirmar")
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

        explanation = summary.get("explicacion_diferencia", {})
        original_detail = explanation.get("diferencia_original", {})
        detail_rows = [
            ["Cálculo aritmético", "", "", "Saldo final Banco", _parse_decimal(original_detail.get("saldo_banco")), "", "No aplicado", "", "Saldo Banco − Saldo Contabilidad"],
            ["Cálculo aritmético", "", "", "Saldo final Contabilidad", _parse_decimal(original_detail.get("saldo_contabilidad")), "", "No aplicado", "", "Saldo reportado por el mayor"],
            ["Cálculo aritmético", "", "", "Diferencia original", _parse_decimal(original_detail.get("importe")), "", "No aplicado", "", original_detail.get("formula")],
            ["Residual", "", "", "Impacto total aplicado", _parse_decimal(explanation.get("residual", {}).get("impacto_total_aplicado")), "", "Aplicado", "", ""],
            ["Residual", "", "", "Diferencia residual", _parse_decimal(explanation.get("residual", {}).get("importe")), "", "Resultado", "", explanation.get("residual", {}).get("mensaje_sin_partidas") or ""],
        ]
        for item in explanation.get("partidas_explicativas", []):
            evidence = item.get("evidencia") or {}
            source = evidence.get("banco") or evidence.get("historico") or {}
            source_text = " · ".join(str(source.get(key)) for key in ("archivo", "hoja", "fila") if source.get(key))
            detail_rows.append([
                "Partida explicativa", "", "", item.get("concepto"),
                _parse_decimal(item.get("importe_con_signo")), item.get("lado"),
                "Afecta residual" if item.get("afecta_residual") else "No afecta residual",
                "Verificada" if item.get("verificada") else "Pendiente",
                " · ".join(filter(None, (item.get("motivo"), source_text))),
            ])
        for side, label in (("banco", "Banco"), ("contabilidad", "Contabilidad")):
            side_data = explanation.get("pendientes", {}).get(side, {})
            detail_rows.append(["Neto pendiente", "", "", f"Neto {label}", _parse_decimal(side_data.get("neto")), label, "No aplicado", "Pendiente", "Suma firmada de movimientos sin contraparte inequívoca"])
            for item in side_data.get("movimientos", []):
                evidence = item.get("evidencia") or {}
                source = " · ".join(str(evidence.get(key)) for key in ("archivo", "hoja", "fila") if evidence.get(key))
                detail_rows.append([
                    "Movimiento pendiente", item.get("id"), item.get("fecha"), item.get("referencia"),
                    _parse_decimal(item.get("importe_con_signo")), label, "No aplicado", "Pendiente",
                    " · ".join(filter(None, (item.get("descripcion"), source))),
                ])
        for candidate in explanation.get("candidatos", []):
            for side in ("banco", "contabilidad"):
                for movement in candidate.get(side, []):
                    source = movement.get("evidencia") or {}
                    source_text = " · ".join(str(source.get(key)) for key in ("archivo", "hoja", "fila") if source.get(key))
                    detail_rows.append([
                        "Candidato por revisar", movement.get("id"), movement.get("movimiento", {}).get("fecha"),
                        movement.get("movimiento", {}).get("referencia"),
                        _parse_decimal(movement.get("movimiento", {}).get("monto")),
                        "Banco" if side == "banco" else "Contabilidad", "No aplicado", "Requiere revisión",
                        " · ".join(filter(None, (candidate.get("criterio"), candidate.get("explicacion"), source_text))),
                    ])
        difference_sheet = workbook.create_sheet("Detalle de diferencia")
        detail_header_row = prepare_sheet(
            difference_sheet,
            "Detalle de diferencia",
            context_rows,
            ["Sección", "ID movimiento", "Fecha", "Concepto / referencia", "Importe con signo", "Lado", "Efecto residual", "Estado", "Motivo / evidencia"],
            [24, 20, 14, 34, 20, 18, 20, 20, 64],
            detail_rows,
        )
        for row_number in range(detail_header_row + 1, detail_header_row + len(detail_rows) + 1):
            difference_sheet.cell(row_number, 5).number_format = amount_format

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


class DirectReconciliationSourceView(APIView):
    def get(self, request, job_id, filename):
        scope = _access(request)
        job_dir = _job_path(scope, job_id)
        job = _read_json(job_dir / "metadata.json")
        requested_account_id = request.query_params.get("cuenta_id")
        if requested_account_id and requested_account_id != job.get("cuenta_id"):
            raise ValidationError({"cuenta_id": "El archivo no pertenece a la cuenta solicitada."})
        safe_name = Path(filename).name
        if safe_name != filename or safe_name not in {item.get("nombre") for item in job.get("archivos", [])}:
            raise NotFound("El archivo de origen no pertenece a este trabajo.")
        source = job_dir / "originales" / safe_name
        if not source.is_file():
            raise NotFound("El archivo original ya no está disponible.")
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if source.suffix.lower() == ".xlsx" else "application/vnd.ms-excel"
        return FileResponse(source.open("rb"), as_attachment=True, filename=safe_name, content_type=content_type)
