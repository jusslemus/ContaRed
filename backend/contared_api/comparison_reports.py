from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import json
from uuid import uuid4
from xml.sax.saxutils import escape

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import APIView

from contared_api.comparison import (
    _current_reconciliation,
    _database_error,
    _latest_comparison,
    _results_for_execution,
)
from contared_api.conciliaciones import (
    _delete_storage_object,
    ImportStorageUnavailable,
    _scope_for_reconciliation,
    _storage_request,
)


GROUPS = ("review", "confirmed", "differences", "dismissed")
GROUP_LABELS = {
    "review": "Por revisar",
    "confirmed": "Confirmados",
    "differences": "Diferencias y sin contraparte",
    "dismissed": "Descartadas",
}
RULE_LABELS = {
    "reference_amount": "Coincidencia por referencia y monto",
    "amount_date": "Posible coincidencia por fecha y monto",
    "reference_date_group": "Coincidencia agrupada por referencia y fecha",
    "reference_amount_mismatch": "Diferencia de monto",
    "ambiguous": "Caso ambiguo",
    "unmatched": "Sin contraparte",
}
STATUS_LABELS = {
    "propuesta": "Por revisar",
    "confirmada": "Confirmada",
    "descartada": "Descartada",
}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"


class ReportGenerationUnavailable(APIException):
    status_code = 503
    default_detail = "No se pudo generar y registrar el reporte."
    default_code = "report_generation_unavailable"


def _proposal_groups(proposal):
    groups = []
    if proposal["estado"] == "confirmada":
        groups.append("confirmed")
    elif proposal["estado"] == "descartada":
        groups.append("dismissed")
    elif proposal["regla"] not in ("reference_amount_mismatch", "unmatched"):
        groups.append("review")
    if proposal["regla"] in ("reference_amount_mismatch", "unmatched"):
        groups.append("differences")
    return groups


def _selected_proposals(proposals, groups):
    return [
        proposal for proposal in proposals
        if any(group in _proposal_groups(proposal) for group in groups)
    ]


def _report_snapshot(report_id, scope, metadata, proposals, groups, report_scope):
    generated_at = timezone.localtime(timezone.now())
    counts = {
        group: sum(group in _proposal_groups(proposal) for proposal in proposals)
        for group in groups
    }
    return {
        "version": 1,
        "report_id": report_id,
        "title": "Reporte del proceso de conciliación — Borrador",
        "generated_at": generated_at.isoformat(),
        "timezone": timezone.get_current_timezone_name(),
        "generated_by": scope["user_name"],
        "scope": {
            "type": report_scope,
            "label": "Todo el proceso" if report_scope == "all" else "Pestaña actual",
            "groups": [{"key": group, "label": GROUP_LABELS[group]} for group in groups],
        },
        "filters": {
            "groups": list(groups),
            "description": (
                "Se aplicaron únicamente los grupos seleccionados. "
                "No se aplicaron filtros de búsqueda ni paginación."
            ),
        },
        "context": metadata,
        "counts": counts,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "closed": False,
    }


def _text(cell, value):
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"
    cell.number_format = "@"
    cell.alignment = Alignment(vertical="top", wrap_text=True)
    return cell


def build_xlsx(snapshot):
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Resumen"
    summary.sheet_view.showGridLines = False
    summary["A1"] = snapshot["title"]
    summary["A1"].font = Font(size=16, bold=True, color="FFFFFF")
    summary["A1"].fill = PatternFill("solid", fgColor="8F1B22")
    summary.merge_cells("A1:D1")
    context_rows = [
        ("Empresa", snapshot["context"]["company"]),
        ("País", snapshot["context"]["country"]),
        ("Banco", snapshot["context"]["bank"]),
        ("Cuenta", snapshot["context"]["account"]),
        ("Moneda", snapshot["context"]["currency"]),
        ("Período", date.fromisoformat(snapshot["context"]["period"])),
        ("Fecha de corte", date.fromisoformat(snapshot["context"]["cutoff"])),
        ("Generado", snapshot["generated_at"]),
        ("Zona horaria", snapshot["timezone"]),
        ("Usuario", snapshot["generated_by"]),
        ("Alcance", snapshot["scope"]["label"]),
        ("Filtros aplicados", snapshot["filters"]["description"]),
        ("Estado del proceso", "Borrador; no representa una conciliación cerrada."),
        ("Referencia de seguimiento", snapshot["report_id"]),
    ]
    for row_number, (label, value) in enumerate(context_rows, start=3):
        _text(summary.cell(row_number, 1), label)
        cell = summary.cell(row_number, 2)
        if isinstance(value, date):
            cell.value = value
            cell.number_format = "dd/mm/yyyy"
        else:
            _text(cell, value)
    count_header = 19
    for column, label in enumerate(("Grupo", "Cantidad de propuestas"), start=1):
        cell = _text(summary.cell(count_header, column), label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="53616C")
    for row_number, group in enumerate(snapshot["scope"]["groups"], start=count_header + 1):
        _text(summary.cell(row_number, 1), group["label"])
        summary.cell(row_number, 2, snapshot["counts"][group["key"]])
    summary.freeze_panes = f"A{count_header + 1}"
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 80
    summary.column_dimensions["C"].width = 20
    summary.column_dimensions["D"].width = 20

    columns = [
        "Referencia de propuesta", "Tipo de fila", "Estado de revisión",
        "Clasificación", "Regla / explicación", "Lado", "Fecha",
        "Referencia", "Número de cheque", "Número de transacción",
        "Descripción", "Monto del movimiento", "Suma Banco", "Suma Libros",
        "Diferencia (Banco − Libros)", "Resuelta por", "Fecha de resolución",
        "Motivo del descarte",
    ]
    for group in snapshot["scope"]["groups"]:
        sheet = workbook.create_sheet(GROUP_LABELS[group["key"]][:31])
        sheet.sheet_view.showGridLines = False
        for column, label in enumerate(columns, start=1):
            cell = _text(sheet.cell(1, column), label)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="53616C")
        row_number = 2
        group_proposals = [
            proposal for proposal in snapshot["proposals"]
            if group["key"] in _proposal_groups(proposal)
        ]
        for proposal_index, proposal in enumerate(group_proposals, start=1):
            proposal_ref = f"{group['key'].upper()}-{proposal_index:05d}"
            summary_values = [
                proposal_ref, "Resumen de propuesta",
                STATUS_LABELS[proposal["estado"]],
                RULE_LABELS.get(proposal["regla"], "Propuesta sin clasificar"),
                f"{RULE_LABELS.get(proposal['regla'], 'Propuesta sin clasificar')}: {proposal['explicacion']}",
                None, None, None, None, None, None, None,
                Decimal(proposal["suma_banco"]),
                Decimal(proposal["suma_libros"]),
                Decimal(proposal["diferencia"]),
                proposal.get("resuelta_por_nombre"),
                _parse_datetime(proposal.get("resuelta_en")),
                proposal.get("motivo_descarte"),
            ]
            for column, value in enumerate(summary_values, start=1):
                cell = sheet.cell(row_number, column)
                if isinstance(value, Decimal):
                    cell.value = value
                    cell.number_format = "#,##0.0000;[Red]-#,##0.0000"
                elif isinstance(value, datetime):
                    cell.value = timezone.localtime(value).replace(tzinfo=None)
                    cell.number_format = "dd/mm/yyyy hh:mm"
                elif isinstance(value, date):
                    cell.value = value
                    cell.number_format = "dd/mm/yyyy"
                else:
                    _text(cell, value)
            for cell in sheet[row_number]:
                cell.fill = PatternFill("solid", fgColor="E8EDF1")
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            row_number += 1
            for side_label, movements in (("Banco", proposal["banco"]), ("Libros", proposal["libros"])):
                for movement in movements:
                    values = [
                        proposal_ref, "Movimiento",
                        STATUS_LABELS[proposal["estado"]],
                        RULE_LABELS.get(proposal["regla"], "Propuesta sin clasificar"),
                        proposal["explicacion"],
                        side_label,
                        date.fromisoformat(movement["fecha"]),
                        movement.get("referencia"),
                        movement.get("numero_cheque"),
                        movement.get("numero_transaccion"),
                        movement.get("descripcion"),
                        Decimal(movement["monto"]),
                        None, None, None,
                        proposal.get("resuelta_por_nombre"),
                        _parse_datetime(proposal.get("resuelta_en")),
                        proposal.get("motivo_descarte"),
                    ]
                    for column, value in enumerate(values, start=1):
                        cell = sheet.cell(row_number, column)
                        if isinstance(value, Decimal):
                            cell.value = value
                            cell.number_format = "#,##0.0000;[Red]-#,##0.0000"
                        elif isinstance(value, datetime):
                            cell.value = timezone.localtime(value).replace(tzinfo=None)
                            cell.number_format = "dd/mm/yyyy hh:mm"
                        elif isinstance(value, date):
                            cell.value = value
                            cell.number_format = "dd/mm/yyyy"
                        else:
                            _text(cell, value)
                    row_number += 1
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(row_number - 1, 1)}"
        widths = (22, 21, 19, 39, 62, 12, 14, 22, 20, 23, 48, 19, 18, 18, 26, 22, 24, 42)
        for column, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(column)].width = width
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _parse_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def _paragraph(value, style):
    return Paragraph(escape("" if value is None else str(value)).replace("\n", "<br/>"), style)


def _page_number(canvas, document):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#5E6A73"))
    canvas.drawRightString(document.pagesize[0] - 0.45 * inch, 0.3 * inch, f"Página {document.page}")
    canvas.restoreState()


def build_pdf(snapshot):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(letter),
        rightMargin=0.4 * inch,
        leftMargin=0.4 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.55 * inch,
        title=snapshot["title"],
        author=snapshot["generated_by"],
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=15, leading=19, alignment=TA_LEFT, textColor=colors.HexColor("#8F1B22"),
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="ReportSmall", parent=styles["BodyText"], fontSize=7.5, leading=10,
        wordWrap="CJK",
    ))
    styles.add(ParagraphStyle(
        name="ReportSection", parent=styles["Heading2"], fontSize=11, leading=14,
        textColor=colors.HexColor("#8F1B22"), spaceBefore=9, spaceAfter=5,
    ))
    styles.add(ParagraphStyle(
        name="ReportCell", parent=styles["BodyText"], fontSize=7, leading=9,
        wordWrap="CJK",
    ))
    story = [
        Paragraph(escape(snapshot["title"]), styles["ReportTitle"]),
        Paragraph(
            "Borrador de trabajo. Este documento no declara la conciliación cerrada "
            "ni incorpora ajustes contables.",
            styles["BodyText"],
        ),
        Spacer(1, 8),
    ]
    context = snapshot["context"]
    metadata_rows = [
        ["Empresa", context["company"], "País", context["country"]],
        ["Banco", context["bank"], "Cuenta", context["account"]],
        ["Moneda", context["currency"], "Período", context["period"]],
        ["Fecha de corte", context["cutoff"], "Generado", snapshot["generated_at"]],
        ["Zona horaria", snapshot["timezone"], "Usuario", snapshot["generated_by"]],
        ["Alcance", snapshot["scope"]["label"], "Seguimiento", snapshot["report_id"]],
        ["Filtros aplicados", snapshot["filters"]["description"], "", ""],
    ]
    metadata_table = Table(
        [[_paragraph(cell, styles["ReportSmall"]) for cell in row] for row in metadata_rows],
        colWidths=[0.85 * inch, 2.7 * inch, 0.9 * inch, 5.75 * inch],
        repeatRows=0,
    )
    metadata_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF1F3")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#EEF1F3")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DEE3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([metadata_table, Spacer(1, 10), Paragraph("Conteo por grupo", styles["Heading2"])])
    count_table = Table(
        [["Grupo", "Cantidad de propuestas"]]
        + [[item["label"], str(snapshot["counts"][item["key"]])]
           for item in snapshot["scope"]["groups"]],
        colWidths=[5.5 * inch, 2.2 * inch],
        repeatRows=1,
    )
    count_table.setStyle(_table_style())
    story.extend([count_table, Spacer(1, 12)])

    for group in snapshot["scope"]["groups"]:
        group_proposals = [
            proposal for proposal in snapshot["proposals"]
            if group["key"] in _proposal_groups(proposal)
        ]
        story.append(Paragraph(
            f"{escape(group['label'])} ({len(group_proposals)})",
            styles["ReportSection"],
        ))
        for proposal_index, proposal in enumerate(group_proposals, start=1):
            proposal_ref = f"{group['key'].upper()}-{proposal_index:05d}"
            status = STATUS_LABELS[proposal["estado"]]
            classification = RULE_LABELS.get(proposal["regla"], "Propuesta sin clasificar")
            header_values = [
                ["Referencia", proposal_ref, "Estado de revisión", status],
                ["Clasificación", classification, "Regla y explicación",
                 f"{classification}: {proposal['explicacion']}"],
                ["Suma Banco", proposal["suma_banco"], "Suma Libros", proposal["suma_libros"]],
                ["Diferencia (Banco − Libros)", proposal["diferencia"], "Resuelta por",
                 proposal.get("resuelta_por_nombre") or "—"],
                ["Fecha de resolución", proposal.get("resuelta_en") or "—", "Motivo del descarte",
                 proposal.get("motivo_descarte") or "—"],
            ]
            proposal_table = Table(
                [[_paragraph(cell, styles["ReportCell"]) for cell in row] for row in header_values],
                colWidths=[1.15 * inch, 1.8 * inch, 1.35 * inch, 5.9 * inch],
            )
            proposal_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF1F3")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#EEF1F3")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D7DEE3")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.extend([
                Paragraph(f"Propuesta {proposal_ref}", styles["Heading3"]),
                proposal_table,
                Spacer(1, 5),
            ])
            for side_name, movements in (("Banco", proposal["banco"]), ("Libros", proposal["libros"])):
                story.append(Paragraph(f"{side_name} · {len(movements)} movimiento(s)", styles["BodyText"]))
                table_rows = [[
                    "Fecha", "Referencia", "Número de cheque",
                    "Número de transacción", "Descripción", "Monto",
                ]]
                table_rows.extend([
                    [
                        _paragraph(_format_date(item.get("fecha")), styles["ReportCell"]),
                        _paragraph(item.get("referencia") or "—", styles["ReportCell"]),
                        _paragraph(item.get("numero_cheque") or "—", styles["ReportCell"]),
                        _paragraph(item.get("numero_transaccion") or "—", styles["ReportCell"]),
                        _paragraph(item.get("descripcion") or "—", styles["ReportCell"]),
                        _paragraph(item.get("monto"), styles["ReportCell"]),
                    ]
                    for item in movements
                ])
                side_table = Table(
                    table_rows,
                    colWidths=[0.75 * inch, 1.1 * inch, 1.1 * inch,
                               1.2 * inch, 5.35 * inch, 0.7 * inch],
                    repeatRows=1,
                    splitByRow=1,
                )
                side_table.setStyle(_table_style())
                story.extend([side_table, Spacer(1, 7)])
        story.append(Spacer(1, 7))
    document.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return output.getvalue()


def _format_date(value):
    if not value:
        return "—"
    return date.fromisoformat(value).strftime("%d/%m/%Y")


def _table_style():
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#53616C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DEE3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])


class ReconciliationReportView(APIView):
    def post(self, request, reconciliation_id):
        if set(request.data) - {"formato", "alcance", "grupos", "grupo_actual"}:
            raise ValidationError({"detail": "La solicitud contiene campos no permitidos."})
        report_format = request.data.get("formato")
        report_scope = request.data.get("alcance")
        if report_format not in ("xlsx", "pdf"):
            raise ValidationError({"formato": "El formato debe ser xlsx o pdf."})
        if report_scope not in ("all", "current"):
            raise ValidationError({"alcance": "El alcance debe ser all o current."})
        if report_scope == "all":
            groups = request.data.get("grupos")
            if (
                not isinstance(groups, list)
                or not groups
                or any(not isinstance(group, str) or group not in GROUPS for group in groups)
                or len(set(groups)) != len(groups)
            ):
                raise ValidationError({
                    "grupos": "Selecciona al menos un grupo válido sin repetir."
                })
        else:
            current_group = request.data.get("grupo_actual")
            if current_group not in GROUPS:
                raise ValidationError({"grupo_actual": "La pestaña actual no es válida."})
            groups = [current_group]

        _scope_for_reconciliation(request, reconciliation_id)
        uploaded_path = None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
                    current = _current_reconciliation(
                        cursor, request, reconciliation_id, lock=True
                    )
                    cursor.execute(
                        "SELECT set_config('contared.actor_id', %s, true)",
                        [current["user_id"]],
                    )
                    cursor.execute(
                        """
                        SELECT e.nombre,pa.nombre,b.nombre,cb.alias,cb.numero,
                               mon.nombre,mon.codigo
                        FROM contared.conciliaciones r
                        JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
                        JOIN contared.bancos b ON b.id=cb.banco_id
                        JOIN contared.empresas e ON e.id=cb.empresa_id
                        JOIN contared.paises pa ON pa.codigo=cb.pais_codigo
                        JOIN contared.monedas mon ON mon.codigo=cb.moneda_codigo
                        WHERE r.id=%s AND cb.id=%s AND cb.empresa_id=%s
                          AND cb.pais_codigo=%s
                        """,
                        [
                            current["reconciliation_id"],
                            current["account_id"],
                            current["company_id"],
                            current["country_code"],
                        ],
                    )
                    metadata = cursor.fetchone()
                    if metadata is None:
                        raise ValidationError(
                            "La conciliación ya no está disponible para este usuario."
                        )
                    execution = _latest_comparison(cursor, current)
                    if execution is None:
                        raise ValidationError(
                            "Genera primero una comparación para poder descargar su reporte."
                        )
                    all_proposals = _results_for_execution(
                        cursor, current, execution[0]
                    )
                    proposals = _selected_proposals(all_proposals, groups)
                    report_id = str(uuid4())
                    snapshot = _report_snapshot(
                        report_id,
                        current,
                        {
                            "company": metadata[0],
                            "country": f"{metadata[1]} ({current['country_code']})",
                            "bank": metadata[2],
                            "account": f"{metadata[3]} · {metadata[4]}",
                            "currency": f"{metadata[5]} ({metadata[6]})",
                            "period": current["period"].isoformat(),
                            "cutoff": current["cutoff"].isoformat(),
                        },
                        proposals,
                        groups,
                        report_scope,
                    )
                    content = build_xlsx(snapshot) if report_format == "xlsx" else build_pdf(snapshot)
                    extension = report_format
                    object_path = (
                        f"reports/{current['company_id']}/{current['account_id']}/"
                        f"{current['reconciliation_id']}/{report_id}.{extension}"
                    )
                    try:
                        _storage_request(
                            "POST",
                            object_path,
                            body=content,
                            content_type=XLSX_MIME if report_format == "xlsx" else PDF_MIME,
                        )
                    except ImportStorageUnavailable:
                        _delete_storage_object(object_path)
                        raise
                    uploaded_path = object_path
                    cursor.execute(
                        """
                        SELECT COALESCE(MAX(version),0)+1
                        FROM contared.reportes
                        WHERE conciliacion_id=%s AND formato=%s
                        """,
                        [current["reconciliation_id"], report_format],
                    )
                    version = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        INSERT INTO contared.reportes
                          (cuenta_id,conciliacion_id,version,formato,storage_ruta,
                           snapshot,generado_por)
                        VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                        """,
                        [
                            current["account_id"],
                            current["reconciliation_id"],
                            version,
                            report_format,
                            object_path,
                            json.dumps(snapshot, ensure_ascii=False),
                            current["user_id"],
                        ],
                    )
            filename = f"reporte-conciliacion-{report_id}.{extension}"
            response = HttpResponse(
                content,
                content_type=XLSX_MIME if report_format == "xlsx" else PDF_MIME,
            )
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response["Cache-Control"] = "private, no-store"
            return response
        except DatabaseError as exc:
            if uploaded_path:
                _delete_storage_object(uploaded_path)
            if isinstance(exc, IntegrityError):
                raise ReportGenerationUnavailable() from exc
            _database_error(exc)
