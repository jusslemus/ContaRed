import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.conciliaciones import (
    _delete_storage_object,
    _scope_for_reconciliation,
    _storage_request,
)
from contared_api.depositos import _deposit_schema_missing
from contared_api.session_access import DatabaseUnavailable, database_error_details


logger = logging.getLogger(__name__)
SUMMARY_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SUMMARY_PDF_MIME = "application/pdf"


def _generic_pending_query(include_deposits):
    deposit_exclusion = (
        "AND NOT EXISTS (SELECT 1 FROM contared.deposito_movimientos dm "
        "WHERE dm.movimiento_id=m.id)"
        if include_deposits else ""
    )
    return f"""
        SELECT m.lado,count(*),sum(abs(m.monto))
        FROM contared.movimientos m
        JOIN contared.cargas ca ON ca.id=m.carga_id AND ca.estado='validada'
        JOIN contared.conciliaciones c ON c.id=ca.conciliacion_id
        WHERE m.cuenta_id=%s AND c.fecha_corte<=%s AND m.fecha<=%s
          AND NOT EXISTS (
            SELECT 1 FROM contared.cruce_movimientos cm
            JOIN contared.cruces x ON x.id=cm.cruce_id
            WHERE cm.movimiento_id=m.id
              AND x.estado='confirmado' AND x.confirmado_en::date<=%s
          )
          AND NOT EXISTS (SELECT 1 FROM contared.cheques ch
            WHERE ch.movimiento_emision_id=m.id)
          AND NOT EXISTS (
            SELECT 1 FROM contared.propuestas_comparacion p
            WHERE p.conciliacion_id=ca.conciliacion_id
              AND p.creada_en::date<=%s
              AND (p.estado<>'confirmada' OR p.resuelta_en::date>%s)
              AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.banco_ids||p.libros_ids) x(id)
                WHERE x.id=m.id::text)
          )
          {deposit_exclusion}
        GROUP BY m.lado
    """


class SummaryReportUnavailable(APIException):
    status_code = 503
    default_detail = "No se pudo generar y registrar el resumen."
    default_code = "summary_report_unavailable"


def _decimal(value):
    return None if value is None else Decimal(str(value))


def _amount(value):
    return str(value) if value is not None else None


def _source_object(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise APIException(
                "La procedencia de un saldo detectado no tiene un formato válido.",
                code="invalid_balance_source",
            ) from exc
    if not isinstance(value, dict):
        raise APIException(
            "La procedencia de un saldo detectado no tiene un formato válido.",
            code="invalid_balance_source",
        )
    return value


def _load_summary(cursor, reconciliation_id, scope, *, include_deposits):
    cursor.execute(
        """SELECT r.id::text,r.cuenta_id::text,cb.alias,b.nombre,
                  cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,
                  r.saldo_inicial_banco,r.saldo_inicial_libros,
                  r.saldo_final_banco,r.saldo_final_libros,
                  e.nombre,pa.nombre,
                  r.saldo_final_banco_detectado,r.saldo_final_banco_fuente,
                  r.saldo_final_libros_detectado,r.saldo_final_libros_fuente
           FROM contared.conciliaciones r
           JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
           JOIN contared.bancos b ON b.id=cb.banco_id
           JOIN contared.empresas e ON e.id=cb.empresa_id
           JOIN contared.paises pa ON pa.codigo=cb.pais_codigo
           WHERE r.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s""",
        [reconciliation_id, scope["company_id"], scope["country_code"]],
    )
    recon = cursor.fetchone()
    if recon is None:
        raise NotFound("La conciliación no existe o no tienes acceso.")
    cutoff = recon[6]
    cursor.execute(
        """
        SELECT m.lado,
               count(*) AS cantidad,
               sum(m.monto) FILTER (WHERE m.monto>0),
               sum(abs(m.monto)) FILTER (WHERE m.monto<0),
               sum(m.monto)
        FROM contared.movimientos m
        JOIN contared.cargas c ON c.id=m.carga_id AND c.estado='validada'
        WHERE m.cuenta_id=%s AND c.conciliacion_id=%s AND m.fecha<=%s
        GROUP BY m.lado
        """,
        [scope["account_id"], reconciliation_id, cutoff],
    )
    movement_totals = {
        row[0]: {"cantidad": row[1], "entradas": _decimal(row[2]), "salidas": _decimal(row[3]), "neto": _decimal(row[4])}
        for row in cursor.fetchall()
    }
    for side in ("libros", "banco"):
        movement_totals.setdefault(side, {"cantidad": 0, "entradas": None, "salidas": None, "neto": None})

    cursor.execute(
        """
        SELECT COALESCE(ev.estado_nuevo,ch.estado),count(*),sum(ch.monto)
        FROM contared.cheques ch
        JOIN contared.movimientos em ON em.id=ch.movimiento_emision_id
        LEFT JOIN LATERAL (
          SELECT e.estado_nuevo FROM contared.cheque_eventos e
          WHERE e.cheque_id=ch.id AND e.fecha_efectiva<=%s
          ORDER BY e.fecha_efectiva DESC,e.registrado_en DESC,e.id DESC LIMIT 1
        ) ev ON true
        WHERE ch.cuenta_id=%s AND ch.fecha_emision<=%s
          AND COALESCE(ev.estado_nuevo,ch.estado) IN ('retenido','en_circulacion')
        GROUP BY COALESCE(ev.estado_nuevo,ch.estado)
        """,
        [cutoff, scope["account_id"], cutoff],
    )
    cheque_pending = {
        row[0]: {"cantidad": row[1], "importe": _amount(row[2])}
        for row in cursor.fetchall()
    }
    for state in ("retenido", "en_circulacion"):
        cheque_pending.setdefault(state, {"cantidad": 0, "importe": "0"})

    deposit_pending = []
    if include_deposits:
        cursor.execute(
            """
            SELECT d.clasificacion,dm.rol,count(DISTINCT d.id),sum(abs(m.monto))
            FROM contared.casos_deposito d
            JOIN contared.conciliaciones ro ON ro.id=d.conciliacion_origen_id
            JOIN contared.deposito_movimientos dm ON dm.caso_id=d.id AND dm.rol<>'resolucion'
            JOIN contared.movimientos m ON m.id=dm.movimiento_id
            WHERE d.cuenta_id=%s AND ro.fecha_corte<=%s
              AND (d.estado='abierto' OR d.fecha_efectiva>%s)
            GROUP BY d.clasificacion,dm.rol
            ORDER BY d.clasificacion,dm.rol
            """,
            [scope["account_id"], cutoff, cutoff],
        )
        deposit_pending = [{
            "clasificacion": row[0], "lado": row[1], "cantidad": row[2],
            "importe": _amount(row[3]),
        } for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT p.regla_codigo,p.estado,b.total,l.total,
               CASE WHEN p.estado='confirmada' AND p.resuelta_en::date>%s THEN true
                    WHEN p.estado='confirmada' THEN false ELSE true END AS pendiente_al_corte
        FROM contared.propuestas_comparacion p
        JOIN contared.conciliaciones r ON r.id=p.conciliacion_id
        LEFT JOIN LATERAL (
          SELECT sum(m.monto) AS total FROM jsonb_array_elements_text(p.banco_ids) x(id)
          JOIN contared.movimientos m ON m.id=x.id::uuid
        ) b ON true
        LEFT JOIN LATERAL (
          SELECT sum(m.monto) AS total FROM jsonb_array_elements_text(p.libros_ids) x(id)
          JOIN contared.movimientos m ON m.id=x.id::uuid
        ) l ON true
        WHERE p.cuenta_id=%s AND r.fecha_corte<=%s AND p.creada_en::date<=%s
        """,
        [cutoff, scope["account_id"], cutoff, cutoff],
    )
    proposal_pending = []
    for rule, state, bank_total, books_total, pending in cursor.fetchall():
        if not pending:
            continue
        proposal_pending.append({
            "regla": rule,
            "estado": "descartada" if state == "descartada" else "por revisar",
            "banco": _amount(bank_total),
            "libros": _amount(books_total),
            "diferencia": _amount(abs((bank_total or Decimal(0)) - (books_total or Decimal(0)))),
        })

    cursor.execute(
        _generic_pending_query(include_deposits),
        [scope["account_id"], cutoff, cutoff, cutoff, cutoff, cutoff],
    )
    generic_pending = {
        row[0]: {"cantidad": row[1], "importe": _amount(row[2])}
        for row in cursor.fetchall()
    }
    for side in ("libros", "banco"):
        generic_pending.setdefault(side, {"cantidad": 0, "importe": "0"})

    cursor.execute(
        """SELECT lado,count(*),sum(monto)
           FROM contared.ajustes
           WHERE conciliacion_id=%s AND cuenta_id=%s
             AND aprobado_por IS NOT NULL AND aprobado_en::date<=%s
           GROUP BY lado""",
        [reconciliation_id, scope["account_id"], cutoff],
    )
    approved_adjustments = {
        row[0]: {"cantidad": row[1], "importe": _amount(row[2])}
        for row in cursor.fetchall()
    }
    for side in ("libros", "banco"):
        approved_adjustments.setdefault(side, {"cantidad": 0, "importe": "0"})

    final_manual = {
        "libros": _decimal(recon[11]),
        "banco": _decimal(recon[10]),
    }
    final_detected = {
        "banco": _decimal(recon[14]),
        "libros": _decimal(recon[16]),
    }
    final_source = {
        "banco": _source_object(recon[15]),
        "libros": _source_object(recon[17]),
    }
    final_conflicts = {
        side: (
            {
                "capturado_manualmente": _amount(final_manual[side]),
                "detectado_en_carga": _amount(final_detected[side]),
                "fuente_detectada": final_source[side],
            }
            if final_manual[side] is not None
            and final_detected[side] is not None
            and final_manual[side] != final_detected[side]
            else None
        )
        for side in ("banco", "libros")
    }
    balances = {
        "saldo_inicial_libros": _decimal(recon[9]),
        "saldo_inicial_banco": _decimal(recon[8]),
        "saldo_final_libros": (
            final_manual["libros"]
            if final_manual["libros"] is not None
            else final_detected["libros"]
        ),
        "saldo_final_banco": (
            final_manual["banco"]
            if final_manual["banco"] is not None
            else final_detected["banco"]
        ),
    }
    for side in ("libros", "banco"):
        if final_manual[side] is not None:
            balances[f"saldo_final_{side}"] = final_manual[side]
    calculations = {}
    for side in ("libros", "banco"):
        initial = balances[f"saldo_inicial_{side}"]
        final = balances[f"saldo_final_{side}"]
        net = movement_totals[side]["neto"]
        calculated = initial + (net or Decimal(0)) if initial is not None else None
        calculations[side] = {
            "inicial_declarado": _amount(initial),
            "final_declarado": _amount(final_manual[side]),
            "final_usado": _amount(final),
            "final_capturado_manualmente": _amount(final_manual[side]),
            "final_detectado_en_carga": _amount(final_detected[side]),
            "fuente_final_detectado": final_source[side],
            "origen_final": (
                "manual" if final_manual[side] is not None
                else "carga" if final_detected[side] is not None
                else None
            ),
            "conflicto_final": final_conflicts[side],
            "entradas": _amount(movement_totals[side]["entradas"]),
            "salidas": _amount(movement_totals[side]["salidas"]),
            "movimientos_netos": _amount(net),
            "saldo_calculado": _amount(calculated),
            "diferencia_calculado_vs_declarado": _amount(calculated - final) if calculated is not None and final is not None else None,
            "cantidad_movimientos": movement_totals[side]["cantidad"],
            "ajustes_aprobados": approved_adjustments[side],
        }

    has_pending_policy = bool(
        any(item["cantidad"] for item in cheque_pending.values())
        or deposit_pending
        or proposal_pending
        or any(item["cantidad"] for item in generic_pending.values())
        or not include_deposits
    )
    book_calculated = _decimal(calculations["libros"]["saldo_calculado"])
    bank_calculated = _decimal(calculations["banco"]["saldo_calculado"])
    pending_difference = None
    bank_final = balances["saldo_final_banco"]
    books_final = balances["saldo_final_libros"]
    final_balance_difference = (
        bank_final - books_final
        if bank_final is not None and books_final is not None
        else None
    )
    if bank_final is not None and books_final is not None:
        pending_difference = _amount(
            bank_final + Decimal(approved_adjustments["banco"]["importe"])
            - books_final - Decimal(approved_adjustments["libros"]["importe"])
        )
        difference_basis = "saldos_finales"
    elif bank_calculated is not None and book_calculated is not None:
        pending_difference = _amount(
            bank_calculated + Decimal(approved_adjustments["banco"]["importe"])
            - book_calculated - Decimal(approved_adjustments["libros"]["importe"])
        )
        difference_basis = "saldos_calculados"
    else:
        difference_basis = None
    generated_at = timezone.localtime(timezone.now())
    return {
        "report_type": "reconciliation_summary",
        "version": 1,
        "title": "Resumen del proceso de conciliación — Borrador",
        "generated_at": generated_at.isoformat(),
        "timezone": timezone.get_current_timezone_name(),
        "generated_by": scope["user_name"],
        "context": {
            "empresa": recon[12], "pais": recon[13], "banco": recon[3],
            "cuenta": recon[2], "moneda": recon[4],
            "periodo": recon[5].isoformat(), "fecha_corte": cutoff.isoformat(),
            "conciliacion_id": recon[0],
        },
        "estado": recon[7],
        "saldos": calculations,
        "pendientes": {
            "cheques": cheque_pending,
            "depositos": deposit_pending,
            "comparaciones": proposal_pending,
            "otros_movimientos": generic_pending,
        },
        "diferencia_pendiente": pending_difference,
        "diferencia_saldos_finales": _amount(final_balance_difference),
        "base_diferencia_pendiente": difference_basis,
        "diferencia_final_pendiente_explicacion": (
            final_balance_difference != 0
            if final_balance_difference is not None else None
        ),
        "saldo_ajustado_definitivo": None,
        "politica_pendiente": has_pending_policy or (
            pending_difference is not None and Decimal(pending_difference) != 0
        ),
        "depositos_disponibles": include_deposits,
        "aviso": None if include_deposits else (
            "Los depósitos no están disponibles hasta aplicar "
            "supabase/migrations/202610300002_seguimiento_depositos.sql."
        ),
        "alcance": "Movimientos validados, ajustes aprobados y registros persistidos al corte; no se aplican ajustes automáticos.",
        "firmas": ["Preparado por", "Revisado por", "Fecha"],
    }


def _summary_highlight_rows(snapshot):
    balances = snapshot["saldos"]
    highlights = []
    for label, side in (
        ("Saldo final Banco", "banco"),
        ("Saldo final Contabilidad", "libros"),
    ):
        item = balances[side]
        conflict = item.get("conflicto_final")
        selected_balance = item.get("final_usado", item.get("final_declarado"))
        highlights.append([
            label,
            selected_balance if selected_balance is not None else "No disponible",
            (
                "Capturado manualmente" if item.get("origen_final") == "manual"
                else "Detectado del archivo" if item.get("origen_final") == "carga"
                else "No disponible"
            ),
            (
                f"Conflicto: se conserva el valor manual {conflict['capturado_manualmente']}; "
                f"el archivo detectó {conflict['detectado_en_carga']}."
                if conflict else ""
            ),
        ])
    difference = snapshot.get("diferencia_saldos_finales")
    highlights.append([
        "Diferencia entre saldos finales (Banco − Contabilidad)",
        difference,
        (
            "Pendiente de explicación"
            if difference is not None and Decimal(str(difference)) != 0
            else "Sin diferencia entre saldos finales" if difference is not None
            else "No disponible"
        ),
        "",
    ])
    return highlights


def _summary_table_rows(snapshot):
    rows = [["Concepto", "Libros", "Banco", "Explicación"]]
    def value(item, key):
        if key == "final_usado":
            return item.get(key, item.get("final_declarado"))
        return item.get(key)

    for concept, key in (
        ("Saldo inicial declarado", "inicial_declarado"),
        ("Entradas", "entradas"),
        ("Salidas", "salidas"),
        ("Saldo calculado", "saldo_calculado"),
        ("Saldo final capturado manualmente", "final_declarado"),
        ("Saldo final detectado en carga", "final_detectado_en_carga"),
        ("Saldo final utilizado", "final_usado"),
        ("Diferencia: calculado menos saldo final utilizado", "diferencia_calculado_vs_declarado"),
    ):
        books = snapshot["saldos"]["libros"]
        bank = snapshot["saldos"]["banco"]
        explanation = ""
        if key == "inicial_declarado":
            explanation = (
                "Dato capturado manualmente y conservado en la conciliación; "
                "no se recupera de cargas sin evidencia verificable."
            )
        elif key == "final_detectado_en_carga":
            sources = []
            for label, item in (("Libros", books), ("Banco", bank)):
                source = item.get("fuente_final_detectado")
                if source:
                    sources.append(
                        f"{label}: {source.get('archivo') or 'archivo sin nombre'}, "
                        f"hoja {source.get('hoja') or 'no informada'}, "
                        f"fila {source.get('fila') or 'no informada'}; "
                        f"{source.get('metodo') or 'método no informado'}"
                    )
            explanation = " · ".join(sources) or "No se detectó saldo en una carga."
        elif key == "final_usado":
            conflicts = []
            for label, item in (("Libros", books), ("Banco", bank)):
                conflict = item.get("conflicto_final")
                if conflict:
                    conflicts.append(
                        f"{label}: se conserva el valor manual "
                        f"{conflict['capturado_manualmente']} y difiere del detectado "
                        f"{conflict['detectado_en_carga']}; revisar el origen"
                    )
            explanation = " · ".join(conflicts) or (
                "Se usa el valor manual si existe; de lo contrario, el saldo detectado."
            )
        elif key == "diferencia_calculado_vs_declarado":
            explanation = "Saldo inicial más movimientos netos frente al saldo final utilizado."
        rows.append([
            concept,
            (
                "No disponible"
                if key == "inicial_declarado" and value(books, key) is None
                else value(books, key)
            ),
            (
                "No disponible"
                if key == "inicial_declarado" and value(bank, key) is None
                else value(bank, key)
            ),
            explanation or (
                "No disponible"
                if key == "inicial_declarado"
                and (value(books, key) is None or value(bank, key) is None)
                else "No informado"
                if value(books, key) is None or value(bank, key) is None
                else ""
            ),
        ])
    rows.append([
        "Diferencia entre saldos finales (Banco − Contabilidad)",
        None,
        snapshot.get("diferencia_saldos_finales"),
        (
            "Pendiente de explicación; diferencia entre los saldos finales utilizados, sin aplicar ajustes."
            if snapshot.get("diferencia_final_pendiente_explicacion")
            else "Diferencia directa entre los saldos finales utilizados, sin aplicar ajustes."
        ),
    ])
    rows.append([
        "Ajustes aprobados explícitos",
        snapshot["saldos"]["libros"]["ajustes_aprobados"]["importe"],
        snapshot["saldos"]["banco"]["ajustes_aprobados"]["importe"],
        "Solo aprobados hasta el corte; no modifica movimientos fuente.",
    ])
    rows.append([
        "Diferencia pendiente de conciliación",
        None,
        snapshot["diferencia_pendiente"],
        (
            "Diferencia entre saldos finales utilizados, menos ajustes aprobados; "
            "queda pendiente de explicación y no representa cierre."
            if snapshot["base_diferencia_pendiente"] == "saldos_finales"
            else "Diferencia entre saldos calculados más ajustes aprobados; no representa cierre."
        ),
    ])
    rows.append([
        "Saldo ajustado definitivo",
        None,
        None,
        "No disponible: el resumen no cierra la conciliación ni inventa políticas.",
    ])
    return rows


def _build_summary_xlsx(snapshot):
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Resumen"
    sheet.append([snapshot["title"]])
    sheet["A1"].font = Font(bold=True, size=16, color="8F1B22")
    for label, key in (
        ("Empresa", "empresa"), ("País", "pais"), ("Banco", "banco"),
        ("Cuenta", "cuenta"), ("Moneda", "moneda"), ("Período", "periodo"),
        ("Fecha de corte", "fecha_corte"),
    ):
        value = date.fromisoformat(snapshot["context"][key]) if key in ("periodo", "fecha_corte") else snapshot["context"][key]
        sheet.append([label, value])
        if key in ("periodo", "fecha_corte"):
            sheet.cell(sheet.max_row, 2).number_format = "yyyy-mm-dd"
    sheet.append([
        "Generado",
        datetime.fromisoformat(snapshot["generated_at"]).replace(tzinfo=None),
        snapshot["timezone"],
    ])
    sheet.cell(sheet.max_row, 2).number_format = "yyyy-mm-dd hh:mm:ss"
    sheet.append(["Usuario", snapshot["generated_by"]])
    sheet.append(["Alcance", snapshot["alcance"]])
    sheet.append(["Versión", snapshot["version"]])
    sheet.append([])
    highlight_start = sheet.max_row + 1
    sheet.append(["Cifras principales"])
    sheet.cell(highlight_start, 1).font = Font(bold=True, size=13, color="8F1B22")
    sheet.append(["Indicador", "Importe", "Selección", "Conflicto"])
    for row in _summary_highlight_rows(snapshot):
        if isinstance(row[1], str):
            try:
                row[1] = Decimal(row[1])
            except InvalidOperation:
                pass
        sheet.append(row)
    for cell in sheet[highlight_start + 1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="8F1B22")
    for row_number in range(highlight_start + 2, highlight_start + 5):
        for cell in sheet[row_number]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.column == 2 and isinstance(cell.value, Decimal):
                cell.number_format = '"Q" #,##0.00;[Red]-"Q" #,##0.00' if snapshot["context"]["moneda"] == "GTQ" else '#,##0.00;[Red]-#,##0.00'
                cell.font = Font(bold=True, size=13, color="8F1B22")
    sheet.append([])
    table_start = sheet.max_row + 1
    for row in _summary_table_rows(snapshot):
        sheet.append(row)
    sheet.freeze_panes = f"A{table_start + 1}"
    sheet.auto_filter.ref = f"A{table_start}:D{sheet.max_row}"
    for cell in sheet[table_start]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="8F1B22")
    for row in sheet.iter_rows(min_row=table_start + 1):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.column in (2, 3) and isinstance(cell.value, str):
                try:
                    cell.value = Decimal(cell.value)
                    cell.number_format = '#,##0.0000;[Red]-#,##0.0000'
                except InvalidOperation:
                    pass
    for index, width in enumerate((42, 24, 24, 62), 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.append([])
    for signature in snapshot["firmas"]:
        sheet.append([signature, "", ""])
        sheet.append(["____________________________"])
    pending = workbook.create_sheet("Pendientes al corte")
    pending.append(["Categoría", "Estado", "Lado", "Cantidad", "Importe", "Naturaleza / explicación"])
    for state, data in snapshot["pendientes"]["cheques"].items():
        pending.append(["Cheque", state, "Libros", data["cantidad"], Decimal(data["importe"]), "Importe de emisión; no es ajuste."])
    for entry in snapshot["pendientes"]["depositos"]:
        pending.append(["Depósito", entry["clasificacion"], entry["lado"], entry["cantidad"],
                        Decimal(entry["importe"]), "Seguimiento operativo; no es ajuste."])
    for entry in snapshot["pendientes"]["comparaciones"]:
        pending.append(["Comparación", entry["estado"], "Banco / Libros", 1,
                        Decimal(entry["diferencia"]), f"Regla: {entry['regla']}; movimientos sin resolver."])
    for side, data in snapshot["pendientes"]["otros_movimientos"].items():
        pending.append(["Movimiento sin contraparte", "Pendiente", side, data["cantidad"],
                        Decimal(data["importe"]), "Importe absoluto informativo."])
    pending.freeze_panes = "A2"
    pending.auto_filter.ref = pending.dimensions
    for cell in pending[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="8F1B22")
    for row in pending.iter_rows(min_row=2):
        row[4].number_format = '#,##0.0000;[Red]-#,##0.0000'
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for index, width in enumerate((28, 27, 18, 12, 20, 54), 1):
        pending.column_dimensions[get_column_letter(index)].width = width
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _build_summary_pdf(snapshot):
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=landscape(letter), leftMargin=0.4*inch, rightMargin=0.4*inch,
                                 topMargin=0.45*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("SummarySmall", parent=styles["BodyText"], fontSize=8, leading=10, alignment=TA_LEFT)
    story = [Paragraph(snapshot["title"], styles["Title"])]
    context_text = " · ".join([
        snapshot["context"]["empresa"], snapshot["context"]["pais"], snapshot["context"]["banco"],
        snapshot["context"]["cuenta"], snapshot["context"]["moneda"],
        snapshot["context"]["periodo"], f"Corte {snapshot['context']['fecha_corte']}",
    ])
    story.extend([
        Paragraph(context_text, small),
        Paragraph(f"Generado: {snapshot['generated_at']} ({snapshot['timezone']}) · Usuario: {snapshot['generated_by']}", small),
        Paragraph(f"Versión: {snapshot['version']} · Alcance: {snapshot['alcance']}", small),
        Paragraph(snapshot["alcance"], small), Spacer(1, 0.18*inch),
    ])
    highlight_rows = [[
        Paragraph(str(cell if cell is not None and cell != "" else "No disponible"), small)
        for cell in ["Indicador", "Importe", "Selección", "Conflicto"]
    ]]
    highlight_rows.extend([
        [
            Paragraph(str(cell if cell is not None and cell != "" else "No disponible"), small)
            for cell in row
        ]
        for row in _summary_highlight_rows(snapshot)
    ])
    highlight_table = Table(
        highlight_rows,
        colWidths=[2.5*inch, 1.6*inch, 1.8*inch, 4.1*inch],
        repeatRows=1,
        hAlign="LEFT",
    )
    highlight_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#8f1b22")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("BACKGROUND", (0,1), (2,-1), colors.HexColor("#fff5f3")),
        ("GRID", (0,0), (-1,-1), 0.35, colors.HexColor("#d5dce1")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.extend([
        Paragraph("Cifras principales", styles["Heading2"]),
        highlight_table,
        Spacer(1, 0.18*inch),
    ])
    rows = [[
        Paragraph(str(cell if cell is not None and cell != "" else "No disponible"), small)
        for cell in row
    ] for row in _summary_table_rows(snapshot)]
    table = Table(rows, colWidths=[2.5*inch, 1.7*inch, 1.7*inch, 4.1*inch], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#8f1b22")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.35, colors.HexColor("#d5dce1")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.25*inch))
    story.append(Paragraph("Pendientes al corte (conteos e importes conservan su naturaleza; no se suman entre categorías):", styles["Heading2"]))
    pending_rows = [["Categoría", "Estado", "Lado", "Cantidad", "Importe", "Explicación"]]
    for status, data in snapshot["pendientes"]["cheques"].items():
        pending_rows.append(["Cheque", status.replace("_", " "), "Libros", str(data["cantidad"]), data["importe"], "Seguimiento operativo; no es ajuste."])
    for entry in snapshot["pendientes"]["depositos"]:
        pending_rows.append(["Depósito", entry["clasificacion"].replace("_", " "), entry["lado"], str(entry["cantidad"]), entry["importe"], "Seguimiento operativo sin ajuste automático."])
    for entry in snapshot["pendientes"]["comparaciones"]:
        pending_rows.append(["Comparación", entry["estado"], "Banco / Libros", "1", entry["diferencia"], "La propuesta no resuelve movimientos hasta ser confirmada."])
    for side, data in snapshot["pendientes"]["otros_movimientos"].items():
        pending_rows.append(["Movimientos sin contraparte", "Pendiente", side, str(data["cantidad"]), data["importe"], "Monto absoluto informativo."])
    ptable = Table([[Paragraph(str(cell or "—"), small) for cell in row] for row in pending_rows],
                   colWidths=[1.6*inch, 1.5*inch, 0.8*inch, 0.7*inch, 1.3*inch, 4.1*inch], repeatRows=1)
    ptable.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#42525d")),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#d5dce1")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
    ]))
    story.extend([ptable, Spacer(1, 0.35*inch)])
    signature = Table([[f"{name}\n\n____________________________" for name in snapshot["firmas"]]],
                      colWidths=[3.4*inch]*3)
    signature.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"BOTTOM"),("ALIGN",(0,0),(-1,-1),"CENTER")]))
    story.append(signature)
    document.build(story)
    return stream.getvalue()


class ReconciliationSummaryView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        include_deposits = not _deposit_schema_missing()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    snapshot = _load_summary(cursor, reconciliation_id, scope, include_deposits=include_deposits)
        except DatabaseError as exc:
            logger.error(
                "Reconciliation summary query failed (%s).",
                database_error_details(exc),
            )
            raise DatabaseUnavailable() from exc
        return Response(snapshot)

    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        report_format = request.data.get("formato")
        if report_format not in ("xlsx", "pdf"):
            raise ValidationError({"formato": "Selecciona Excel o PDF."})
        include_deposits = not _deposit_schema_missing()
        report_id = uuid4()
        object_path = None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    cursor.execute(
                        """SELECT id FROM contared.conciliaciones
                           WHERE id=%s AND cuenta_id=%s FOR UPDATE""",
                        [reconciliation_id, scope["account_id"]],
                    )
                    if cursor.fetchone() is None:
                        raise NotFound("La conciliación no existe.")
                    cursor.execute("SELECT set_config('contared.actor_id',%s,true)", [scope["user_id"]])
                    snapshot = _load_summary(cursor, reconciliation_id, scope, include_deposits=include_deposits)
                    cursor.execute(
                        "SELECT COALESCE(max(version),0)+1 FROM contared.reportes WHERE conciliacion_id=%s",
                        [reconciliation_id],
                    )
                    version = cursor.fetchone()[0]
                    snapshot["version"] = version
                    snapshot["report_id"] = str(report_id)
                    body = _build_summary_xlsx(snapshot) if report_format == "xlsx" else _build_summary_pdf(snapshot)
                    object_path = f"reports/summary/{scope['company_id']}/{scope['account_id']}/{report_id}.{report_format}"
                    _storage_request(
                        "POST", object_path, body=body,
                        content_type=SUMMARY_XLSX_MIME if report_format == "xlsx" else SUMMARY_PDF_MIME,
                    )
                    cursor.execute(
                        """INSERT INTO contared.reportes
                           (id,cuenta_id,conciliacion_id,version,formato,storage_ruta,snapshot,generado_por)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                        [report_id, scope["account_id"], reconciliation_id, version, report_format,
                         object_path, json.dumps(snapshot), scope["user_id"]],
                    )
        except (DatabaseError, IntegrityError) as exc:
            if object_path:
                _delete_storage_object(object_path)
            logger.error("Reconciliation summary report failed (error type: %s).", type(exc).__name__)
            raise SummaryReportUnavailable() from exc
        except APIException:
            if object_path:
                _delete_storage_object(object_path)
            raise
        except (ImportError, OSError, TypeError, ValueError) as exc:
            if object_path:
                _delete_storage_object(object_path)
            logger.error("Reconciliation summary report generation failed (error type: %s).", type(exc).__name__)
            raise SummaryReportUnavailable() from exc
        response = HttpResponse(body, content_type=SUMMARY_XLSX_MIME if report_format == "xlsx" else SUMMARY_PDF_MIME)
        response["Content-Disposition"] = f'attachment; filename="resumen-conciliacion-v{version}.{report_format}"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
