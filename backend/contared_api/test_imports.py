from io import BytesIO
from contextlib import nullcontext
import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import resolve
from rest_framework.test import APIClient
from openpyxl import Workbook, load_workbook
from openpyxl.utils.datetime import to_excel
import xlrd
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseUser
from contared_api.conciliaciones import (
    ImportConflict,
    ReconciliationPreviewConfirmView,
    ReconciliationPreviewCreateView,
    ReconciliationPreviewValidateView,
    UnboundReconciliationPreviewCreateView,
    UnboundReconciliationPreviewDeleteView,
    UnboundReconciliationPreviewValidateView,
    _preview_coverage,
    _scope_for_account,
    _validate_workbook_account,
    _validate_exclusions,
    _validate_options,
    _existing_import_result,
)
from contared_api.banking import AccountDetailView, AccountListCreateView
from contared_api.imports import (
    XLS_SIGNATURE,
    bind_preview,
    _normalize_xls,
    create_preview,
    get_preview,
    inspect_workbook_sheet,
    parse_decimal,
    recognize_workbook,
    validate_workbook,
)
from rest_framework.exceptions import NotFound


class WorkbookImportTests(SimpleTestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "test_artifacts" / str(uuid4())
        self.folder.mkdir(parents=True)
        self.path = self.folder / "sample.xlsx"

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def save_sheet(self, headers, rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(self.path)
        workbook.close()

    def validate(self, origin, mapping, date_format=None, period=None, cutoff=None):
        return validate_workbook(
            self.path,
            sheet_name="Estado",
            header_row=1,
            mapping=mapping,
            date_format=date_format,
            origin=origin,
            period=period or date(2026, 1, 1),
            cutoff=cutoff or date(2026, 1, 31),
        )

    def test_bank_debit_credit_are_explicit_and_signs_are_normalized(self):
        self.save_sheet(
            ["Fecha", "Salidas", "Entradas", "Referencia", "Detalle"],
            [
                [date(2026, 1, 2), 10.5, None, "0000123", "Pago proveedor"],
                [date(2026, 1, 3), None, 25, "0000124", "Depósito"],
            ],
        )
        result = self.validate(
            "banco",
            {
                "fecha": "Fecha",
                "salidas": "Salidas",
                "entradas": "Entradas",
                "referencia": "Referencia",
                "descripcion": "Detalle",
            },
        )
        self.assertEqual(result.errores, [])
        self.assertEqual([row["monto"] for row in result.movimientos], ["-10.5", "25"])
        self.assertEqual(result.movimientos[0]["referencia"], "0000123")
        self.assertEqual(result.movimientos[0]["fila_origen"], 2)

    def test_bank_rows_with_both_sides_filled_are_rejected(self):
        self.save_sheet(
            ["Fecha", "Salida", "Entrada"],
            [[date(2026, 1, 2), 10, 10]],
        )
        result = self.validate(
            "banco",
            {"fecha": "Fecha", "salida": "Salida", "entrada": "Entrada"},
        )
        self.assertEqual(result.movimientos, [])
        self.assertIn("indique solo una dirección", result.errores[0]["errores"][0]["mensaje"])

    def test_signed_bank_amount_column_is_supported_without_direction_columns(self):
        self.save_sheet(
            ["Fecha", "Importe firmado"],
            [[date(2026, 1, 2), -8.25], [date(2026, 1, 3), 9]],
        )
        result = self.validate(
            "banco",
            {"fecha": "Fecha", "monto": "Importe firmado"},
        )
        self.assertEqual([row["monto"] for row in result.movimientos], ["-8.25", "9"])

    def test_signed_amount_cannot_be_combined_with_direction_mapping(self):
        self.save_sheet(
            ["Fecha", "Monto", "Salidas", "Entradas"],
            [[date(2026, 1, 2), -8, 8, None]],
        )
        with self.assertRaises(ValidationError):
            self.validate(
                "banco",
                {"fecha": "Fecha", "monto": "Monto", "salidas": "Salidas", "entradas": "Entradas"},
            )

    def test_out_of_period_rows_are_reported_for_explicit_exclusion(self):
        self.save_sheet(
            ["Fecha", "Monto"],
            [[date(2025, 12, 31), 10], [date(2026, 1, 15), 20], [date(2026, 2, 1), 30]],
        )
        result = self.validate(
            "libros",
            {"fecha": "Fecha", "monto": "Monto"},
        )
        self.assertEqual([item["fila"] for item in result.exclusiones_requeridas], [2, 4])
        self.assertEqual([item["monto"] for item in result.movimientos], ["20"])
        self.assertEqual(result.errores, [])

    def test_banco_industrial_date_inversion_is_proposed_and_requires_confirmation(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado de cuenta Banco Septiemb"
        for value in (
            "Banco Industrial",
            "Cuenta: 0040040592",
            "Guatemala",
            "Saldo inicial (GTQ): 1000",
            "Del 01/09/2026 al 30/09/2026",
            None,
            None,
            None,
        ):
            sheet.append([value] if value is not None else [])
        sheet.append(["Fecha", "TT", "Descripción", "No. Doc", "Debe (GTQ)", "Haber (GTQ)", "Saldo (GTQ)"])
        for raw_date, description, debit, credit, balance in (
            (date(2026, 1, 9), "Primer día propuesto", None, 10, 1010),
            (date(2026, 2, 9), "Segundo día propuesto", 5, None, 1005),
            (date(2026, 9, 9), "Fecha dentro de septiembre", None, 20, 1025),
        ):
            sheet.append([
                to_excel(raw_date, workbook.epoch),
                "NC",
                description,
                "1001",
                debit,
                credit,
                balance,
            ])
            sheet.cell(sheet.max_row, 1).number_format = "General"
        sheet.append(["13-09-2026", "NC", "Continuación textual", "1004", None, 5, 1030])
        workbook.save(self.path)
        workbook.close()
        mapping = {
            "fecha": "A",
            "codigo_tt": "B",
            "descripcion": "C",
            "referencia": "D",
            "salida": "E",
            "entrada": "F",
            "saldo_origen": "G",
        }

        proposed = validate_workbook(
            self.path,
            sheet_name="Estado de cuenta Banco Septiemb",
            header_row=9,
            mapping=mapping,
            date_format="%d/%m/%Y",
            origin="banco",
            period=date(2026, 9, 1),
            cutoff=date(2026, 9, 30),
        )
        self.assertEqual([item["fila"] for item in proposed.propuestas_normalizacion_fecha], [10, 11])
        self.assertEqual(
            [item["fecha_propuesta"] for item in proposed.propuestas_normalizacion_fecha],
            ["2026-09-01", "2026-09-02"],
        )
        self.assertEqual(len(proposed.movimientos), 2)
        self.assertEqual([item["fila"] for item in proposed.exclusiones_requeridas], [10, 11])
        self.assertEqual(proposed.normalizaciones_fecha_aplicadas, [])

        confirmed = validate_workbook(
            self.path,
            sheet_name="Estado de cuenta Banco Septiemb",
            header_row=9,
            mapping=mapping,
            date_format="%d/%m/%Y",
            origin="banco",
            period=date(2026, 9, 1),
            cutoff=date(2026, 9, 30),
            confirm_date_proposals=True,
        )
        self.assertEqual(confirmed.exclusiones_requeridas, [])
        self.assertEqual(
            [item["fecha"] for item in confirmed.movimientos],
            ["2026-09-01", "2026-09-02", "2026-09-09", "2026-09-13"],
        )
        self.assertEqual(len(confirmed.normalizaciones_fecha_aplicadas), 2)
        self.assertEqual(confirmed.movimientos[0]["datos_originales"]["Fecha"], 46031)
        self.assertEqual(
            confirmed.movimientos[0]["datos_originales"]["_normalizacion_fecha"]["fecha_original"],
            "2026-01-09",
        )

        user_id = uuid4()
        company_id = str(uuid4())
        account_id = str(uuid4())
        scope = {
            "user_id": str(user_id),
            "company_id": company_id,
            "country_code": "GT",
            "role": "auxiliar",
        }
        account_scope = {
            **scope,
            "account_id": account_id,
            "account_number": "0040040592",
            "currency_code": "GTQ",
            "bank_name": "Banco Industrial",
            "accounting_code": "1101030103",
        }
        preview_storage = self.folder / "previews"
        factory = APIRequestFactory()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=preview_storage):
            preview_id, metadata, _ = create_preview(
                SimpleUploadedFile(
                    "Estado de cuenta Banco Septiembre.xlsx",
                    self.path.read_bytes(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                user_id=user_id,
                scope=scope,
                origin="banco",
            )
            metadata["contexto"] = recognize_workbook(self.path, requested_origin="banco")["contexto"]
            (preview_storage / str(preview_id) / "metadata.json").write_text(
                json.dumps({key: value for key, value in metadata.items() if not key.startswith("_")}),
                encoding="utf-8",
            )
            responses = []
            with (
                patch("contared_api.conciliaciones._unbound_preview_scope", return_value=scope),
                patch("contared_api.conciliaciones._scope_for_account", return_value=account_scope),
            ):
                for confirm_normalization in (False, True):
                    request = factory.post(
                        f"/api/v1/conciliaciones/previsualizaciones/{preview_id}/validar/",
                        {
                            "empresa_id": company_id,
                            "pais_codigo": "GT",
                            "cuenta_id": account_id,
                            "periodo": "2026-09",
                            "origen": "banco",
                            "sheet": "Estado de cuenta Banco Septiemb",
                            "header_row": 9,
                            "mapping": mapping,
                            "date_format": "%d/%m/%Y",
                            "confirmar_normalizacion_fecha": confirm_normalization,
                        },
                        format="json",
                    )
                    force_authenticate(request, user=SupabaseUser(user_id))
                    responses.append(
                        UnboundReconciliationPreviewValidateView.as_view()(
                            request,
                            preview_id=preview_id,
                        )
                    )
        self.assertEqual(responses[0].status_code, 200, responses[0].data)
        self.assertEqual(len(responses[0].data["exclusiones_requeridas"]), 2)
        self.assertEqual(len(responses[0].data["propuestas_normalizacion_fecha"]), 2)
        self.assertEqual(responses[0].data["normalizaciones_fecha_aplicadas"], [])
        self.assertEqual(responses[1].status_code, 200, responses[1].data)
        self.assertEqual(responses[1].data["movimientos_validos"], 4)
        self.assertEqual(responses[1].data["exclusiones_requeridas"], [])
        self.assertEqual(len(responses[1].data["normalizaciones_fecha_aplicadas"]), 2)

        reconciliation_id = str(uuid4())
        confirmation_scope = {
            **scope,
            "state": "borrador",
            "account_id": account_id,
            "account_number": "0040040592",
            "currency_code": "GTQ",
            "bank_name": "Banco Industrial",
            "accounting_code": "1101030103",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
        }
        advanced_validation_request = factory.post(
            f"/api/v1/conciliaciones/{reconciliation_id}/previsualizaciones/{preview_id}/validar/",
            {
                "origen": "banco",
                "mapping": mapping,
                "sheet": "Estado de cuenta Banco Septiemb",
                "header_row": 9,
                "date_format": "%d/%m/%Y",
                "confirmar_normalizacion_fecha": True,
            },
            format="json",
        )
        force_authenticate(advanced_validation_request, user=SupabaseUser(user_id))
        with (
            patch("contared_api.conciliaciones._scope_for_reconciliation", return_value=confirmation_scope),
            patch("contared_api.conciliaciones._writer"),
            patch(
                "contared_api.conciliaciones.get_preview",
                return_value=(
                    {
                        "origen": "banco",
                        "contexto": recognize_workbook(self.path, requested_origin="banco")["contexto"],
                    },
                    self.path,
                ),
            ),
        ):
            advanced_validation_response = ReconciliationPreviewValidateView.as_view()(
                advanced_validation_request,
                reconciliation_id=reconciliation_id,
                preview_id=preview_id,
            )
        self.assertEqual(advanced_validation_response.status_code, 200, advanced_validation_response.data)
        self.assertEqual(advanced_validation_response.data["exclusiones_requeridas"], [])
        self.assertEqual(len(advanced_validation_response.data["normalizaciones_fecha_aplicadas"]), 2)

        confirmation_request = factory.post(
            f"/api/v1/conciliaciones/{reconciliation_id}/previsualizaciones/{preview_id}/confirmar/",
            {
                "origen": "banco",
                "mapping": mapping,
                "sheet": "Estado de cuenta Banco Septiemb",
                "header_row": 9,
                "date_format": "%d/%m/%Y",
                "excluded_rows": [10, 11],
                "save_profile": False,
                "catalogo_tipo_transaccion": "{}",
                "confirmar_normalizacion_fecha": False,
            },
            format="json",
        )
        force_authenticate(confirmation_request, user=SupabaseUser(user_id))
        with (
            patch("contared_api.conciliaciones._scope_for_reconciliation", return_value=confirmation_scope),
            patch("contared_api.conciliaciones._writer"),
            patch(
                "contared_api.conciliaciones.get_preview",
                return_value=(
                    {
                        "origen": "banco",
                        "contexto": recognize_workbook(self.path, requested_origin="banco")["contexto"],
                    },
                    self.path,
                ),
            ),
        ):
            excluded_response = ReconciliationPreviewConfirmView.as_view()(
                confirmation_request,
                reconciliation_id=reconciliation_id,
                preview_id=preview_id,
            )
        self.assertEqual(excluded_response.status_code, 400)
        self.assertIn("no se pueden omitir", str(excluded_response.data).casefold())

        confirmed_import_request = factory.post(
            f"/api/v1/conciliaciones/{reconciliation_id}/previsualizaciones/{preview_id}/confirmar/",
            {
                "origen": "banco",
                "mapping": mapping,
                "sheet": "Estado de cuenta Banco Septiemb",
                "header_row": 9,
                "date_format": "%d/%m/%Y",
                "excluded_rows": [],
                "save_profile": False,
                "catalogo_tipo_transaccion": "{}",
                "confirmar_normalizacion_fecha": True,
            },
            format="json",
        )
        force_authenticate(confirmed_import_request, user=SupabaseUser(user_id))

        def stop_before_import(excluded_rows, required_rows):
            self.assertEqual(excluded_rows, [])
            self.assertEqual(required_rows, [])
            raise ValidationError({"test": "Confirmed dates validated; import intentionally not run."})

        with (
            patch("contared_api.conciliaciones._scope_for_reconciliation", return_value=confirmation_scope),
            patch("contared_api.conciliaciones._writer"),
            patch(
                "contared_api.conciliaciones.get_preview",
                return_value=(
                    {
                        "origen": "banco",
                        "contexto": recognize_workbook(self.path, requested_origin="banco")["contexto"],
                    },
                    self.path,
                ),
            ),
            patch("contared_api.conciliaciones._validate_exclusions", side_effect=stop_before_import),
        ):
            confirmed_import_response = ReconciliationPreviewConfirmView.as_view()(
                confirmed_import_request,
                reconciliation_id=reconciliation_id,
                preview_id=preview_id,
            )
        self.assertEqual(confirmed_import_response.status_code, 400)
        self.assertIn("Confirmed dates validated", str(confirmed_import_response.data))

        workbook = load_workbook(self.path)
        workbook["Estado de cuenta Banco Septiemb"]["A13"] = "01-09-2026"
        workbook.save(self.path)
        workbook.close()
        conflicting_sequence = validate_workbook(
            self.path,
            sheet_name="Estado de cuenta Banco Septiemb",
            header_row=9,
            mapping=mapping,
            date_format="%d/%m/%Y",
            origin="banco",
            period=date(2026, 9, 1),
            cutoff=date(2026, 9, 30),
        )
        self.assertEqual(conflicting_sequence.propuestas_normalizacion_fecha, [])
        self.assertEqual(len(conflicting_sequence.exclusiones_requeridas), 2)

    def test_formula_without_cached_value_is_an_error(self):
        self.save_sheet(
            ["Fecha", "Monto"],
            [[date(2026, 1, 2), "=1+2"]],
        )
        result = self.validate(
            "libros",
            {"fecha": "Fecha", "monto": "Monto"},
        )
        self.assertEqual(result.movimientos, [])
        self.assertEqual(result.errores[0]["fila"], 2)
        self.assertIn("fórmula", result.errores[0]["errores"][0]["mensaje"])

    def test_text_dates_require_an_explicit_format_when_ambiguous(self):
        self.save_sheet(["Fecha", "Monto"], [["01/02/2026", "5.25"]])
        ambiguous = self.validate("libros", {"fecha": "Fecha", "monto": "Monto"})
        self.assertTrue(ambiguous.errores)
        explicit = self.validate(
            "libros",
            {"fecha": "Fecha", "monto": "Monto"},
            date_format="%d/%m/%Y",
            period=date(2026, 2, 1),
            cutoff=date(2026, 2, 28),
        )
        self.assertEqual(explicit.movimientos[0]["fecha"], "2026-02-01")

    def test_decimal_parser_preserves_exact_decimal_values(self):
        self.assertEqual(parse_decimal("1.234,5600", "monto"), Decimal("1234.5600"))
        self.assertEqual(parse_decimal("0012.30", "monto"), Decimal("12.30"))
        self.assertEqual(parse_decimal("1.23000", "monto"), Decimal("1.23000"))
        with self.assertRaises(ValueError):
            parse_decimal("1.00001", "monto")

    def test_inspection_returns_selected_header_and_sample_rows(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        for index in range(1, 8):
            sheet.append([f"Nota {index}", None])
        sheet.append(["Fecha", "Importe"])
        sheet.append([date(2026, 1, 5), Decimal("12.34")])
        workbook.save(self.path)
        workbook.close()

        result = inspect_workbook_sheet(self.path, sheet_name="Estado", header_row=8)

        self.assertEqual(result["columnas"], [
            {"columna": "A", "encabezado": "Fecha"},
            {"columna": "B", "encabezado": "Importe"},
        ])
        self.assertEqual(result["muestra_datos"][0], ["2026-01-05T00:00:00", "12.34"])

    def test_unbound_preview_can_be_deleted_by_its_authorized_owner(self):
        workbook = Workbook()
        workbook.active.append(["Fecha", "Importe"])
        workbook.active.append([date(2026, 1, 5), -12.5])
        workbook.save(self.path)
        workbook.close()
        user_id = uuid4()
        scope = {
            "user_id": str(user_id),
            "company_id": str(uuid4()),
            "country_code": "GT",
            "account_id": None,
            "reconciliation_id": None,
        }
        preview_storage = self.folder / "private"
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=preview_storage):
            preview_id, metadata, _ = create_preview(
                SimpleUploadedFile("pending.xlsx", self.path.read_bytes()),
                user_id=user_id,
                scope=scope,
                origin="banco",
            )
            preview_folder = Path(metadata["_source_path"]).parent
            request = APIRequestFactory().delete(
                f"/api/v1/conciliaciones/previsualizaciones/{preview_id}/"
            )
            force_authenticate(request, user=SupabaseUser(user_id))
            with (
                patch("contared_api.conciliaciones._unbound_preview_scope", return_value=scope),
                patch("contared_api.conciliaciones._writer"),
            ):
                response = UnboundReconciliationPreviewDeleteView.as_view()(
                    request,
                    preview_id=preview_id,
                )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(preview_folder.exists())


    def test_confirm_retry_returns_existing_load_without_duplicating_movements(self):
        reconciliation_id = str(uuid4())
        load_id = str(uuid4())

        class Cursor:
            def __init__(self):
                self.results = [
                    (load_id, reconciliation_id, "validada", "estado.xlsx", None),
                    (184,),
                ]

            def execute(self, query, params):
                self.query = query
                self.params = params

            def fetchone(self):
                return self.results.pop(0)

        cursor = Cursor()
        result = _existing_import_result(
            cursor,
            {"account_id": str(uuid4())},
            reconciliation_id,
            "banco",
            "a" * 64,
            [],
        )

        self.assertEqual(result["carga_id"], load_id)
        self.assertEqual(result["movimientos_insertados"], 184)
        self.assertTrue(result["repetido"])
        self.assertEqual(len(cursor.results), 0)

    def test_confirm_retry_does_not_reuse_import_from_another_reconciliation(self):
        class Cursor:
            def execute(self, query, params):
                pass

            def fetchone(self):
                return (str(uuid4()), str(uuid4()), "validada", "estado.xlsx", None)

        with self.assertRaises(ImportConflict):
            _existing_import_result(
                Cursor(),
                {"account_id": str(uuid4())},
                str(uuid4()),
                "banco",
                "b" * 64,
                [],
            )

    def test_bank_statement_context_and_numeric_date_conflicts_are_preserved(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append(["Tipo de Transacciones"])
        sheet.append([None, "DE = Depósito", None, None, "CQ = Pago de Cheque"])
        sheet.append([None, "NC = Nota de Crédito", None, None, "ND = Nota de Débito"])
        sheet.append([])
        sheet.append(["Cuenta: 0040040592 - INTELFON GUATEMALA, S.A."])
        sheet.append(["Saldo inicial (GTQ): 1497475.67"])
        sheet.append(["Del 01/09/2026 al 30/09/2026"])
        sheet.append([])
        sheet.append(["Fecha", "TT", "Descripción", "No. Doc", "Debe (GTQ)", "Haber (GTQ)", "Saldo (GTQ)"])
        sheet.append([46031, "NC", "Fecha serial fuera del mes", 1001, None, 10, 1497485.67])
        sheet.append([46274, "NC", "Fecha serial correcta", 1002, None, 20, 1497505.67])
        sheet.append(["13-09-2026", "CQ", "Fecha con separador alternativo", 1003, 5, None, 1497500.67])
        sheet.append([None, None, "Total general", None, None, 30, None])
        workbook.save(self.path)
        workbook.close()

        recognition = recognize_workbook(self.path, requested_origin="banco")
        self.assertTrue(recognition["reconocido"])
        self.assertEqual(recognition["fila_encabezados"], 9)
        self.assertEqual(recognition["contexto"]["cuenta_bancaria"], "0040040592")
        self.assertEqual(recognition["contexto"]["pais_codigo"], "GT")
        self.assertEqual(recognition["contexto"]["moneda_codigo"], "GTQ")
        self.assertEqual(recognition["contexto"]["periodo_declarado"], "2026-09")
        self.assertEqual(recognition["contexto"]["saldo_inicial"], "1497475.67")

        result = validate_workbook(
            self.path,
            sheet_name="Estado",
            header_row=9,
            mapping=recognition["mapping"],
            date_format=recognition["date_format"],
            origin="banco",
            period=date(2026, 9, 1),
            cutoff=date(2026, 9, 30),
        )
        self.assertEqual(result.errores, [])
        self.assertEqual([item["fecha"] for item in result.exclusiones_requeridas], ["2026-01-09"])
        self.assertEqual([item["fecha"] for item in result.movimientos], ["2026-09-09", "2026-09-13"])
        self.assertTrue(any("total o subtotal" in item["mensaje"] for item in result.advertencias))

    def test_bank_statement_content_overrides_accounting_upload_zone(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append(["Fecha", "No. Doc", "Descripción", "Debe (GTQ)", "Haber (GTQ)", "Saldo (GTQ)"])
        sheet.append([date(2026, 9, 1), "001", "Cargo", 10, None, 90])
        workbook.save(self.path)
        workbook.close()

        recognition = recognize_workbook(self.path, requested_origin="libros")

        self.assertTrue(recognition["reconocido"])
        self.assertEqual(recognition["origen_sugerido"], "banco")
        self.assertIn("salida", recognition["mapping"])
        self.assertIn("entrada", recognition["mapping"])

    def test_banco_industrial_ledger_recognition_excludes_opening_balance_row(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append([
            "#", "Fecha de contabilizacion", "N trans", "Origen",
            "Referencia 1", "Info.detallada", "C/D (ML)", "Saldo acumulado (ML)",
            "SN/Codigo de cuenta",
        ])
        sheet.append(["1", None, None, "SI", None, None, None, 1000, "1101030103"])
        sheet.append(["2", "01/09/2026", 123, "PR", "8494", "Pago recibido BI", 10, 1010, "1101030103"])
        workbook.save(self.path)
        workbook.close()

        recognition = recognize_workbook(self.path, requested_origin="banco")
        self.assertEqual(recognition["origen_sugerido"], "libros")
        self.assertEqual(recognition["contexto"]["codigo_contable"], "1101030103")
        self.assertEqual(recognition["contexto"]["saldo_inicial"], "1000")
        result = self.validate(
            "libros",
            recognition["mapping"],
            date_format=recognition["date_format"],
            period=date(2026, 9, 1),
            cutoff=date(2026, 9, 30),
        )
        self.assertEqual(len(result.movimientos), 1)
        self.assertEqual(result.movimientos[0]["monto"], "10")
        self.assertTrue(any("saldo inicial" in item["mensaje"] for item in result.advertencias))

    def test_numeric_ledger_columns_with_adjacent_currency_columns_are_recognized(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append([
            "#", "Fecha de contabilización", "Fecha de vencimiento", "N° trans.",
            "Origen", "Número de origen", "Referencia 1", "Referencia 2",
            "Referencia 3", "Cuenta de contrapartida", "Info.detallada",
            "C/D (ML) (moneda)", "C/D (ML)",
            "Saldo acumulado (ML) (moneda)", "Saldo acumulado (ML)",
            "Cargo (ML) (moneda)", "Cargo (ML)", "Abono (ML) (moneda)",
            "Abono (ML)", "Creador", "Norma de reparto", "Acuerdo global",
        ])
        sheet.append([
            "1", None, None, None, "SI", None, None, None, None, None, None,
            None, None, "USD", 59324.67, None, None, None, None, "TESORERIA1",
        ])
        for index in range(59):
            day = min(index + 1, 23)
            amount = 100 if index % 2 == 0 else -100
            sheet.append([
                str(index + 2), f"{day:02d}/10/2026", f"{day:02d}/10/2026",
                200000 + index, "DP", str(index + 1), f"ref-{index + 1}",
                f"alt-{index + 1}", f"third-{index + 1}", "1101010101",
                "Movimiento contable", "USD", amount, "USD",
                28663.26 if index == 58 else 59324.67 + amount * (index + 1),
                "USD", abs(amount) if amount < 0 else None, "USD",
                amount if amount > 0 else None, "TESORERIA1", None, None,
            ])
        workbook.save(self.path)
        workbook.close()

        recognition = recognize_workbook(self.path, requested_origin="banco")

        self.assertTrue(recognition["reconocido"])
        self.assertEqual(recognition["hoja"], "Sheet1")
        self.assertEqual(recognition["origen_sugerido"], "libros")
        self.assertEqual(recognition["mapping"]["monto"], 13)
        self.assertEqual(recognition["mapping"]["saldo_origen"], 15)
        self.assertEqual(recognition["contexto"]["saldo_inicial"], "59324.67")

        result = validate_workbook(
            self.path,
            sheet_name="Sheet1",
            header_row=1,
            mapping=recognition["mapping"],
            date_format=recognition["date_format"],
            origin="libros",
            period=date(2026, 10, 1),
            cutoff=date(2026, 10, 31),
        )

        self.assertEqual(result.errores, [])
        self.assertEqual(len(result.movimientos), 59)
        self.assertEqual(result.movimientos[-1]["fecha"], "2026-10-23")
        self.assertEqual(result.movimientos[-1]["saldo_origen"], "28663.26")
        self.assertEqual(result.movimientos[0]["datos_originales"]["Referencia 1"], "ref-1")
        self.assertEqual(result.movimientos[0]["datos_originales"]["Referencia 2"], "alt-1")
        self.assertEqual(result.movimientos[0]["datos_originales"]["Referencia 3"], "third-1")
        self.assertTrue(any("saldo inicial" in item["mensaje"] for item in result.advertencias))

    def test_recognition_prefers_posting_date_and_does_not_treat_cd_code_as_amount(self):
        self.save_sheet(
            [
                "Fecha valor",
                "Fecha de contabilización",
                "C/D (ML)",
                "Saldo acumulado (ML)",
                "Cargo (ML)",
                "Abono (ML)",
                "Referencia 1",
                "Info detallada",
            ],
            [],
        )

        result = recognize_workbook(self.path, requested_origin="banco")

        self.assertEqual(result["origen_sugerido"], "libros")
        self.assertEqual(result["perfil_sugerido"], "banco_industrial_libros")
        self.assertEqual(result["fila_encabezados"], 1)
        self.assertEqual(result["mapping"]["fecha"], 2)
        self.assertNotIn("monto", result["mapping"])
        self.assertFalse(result["reconocido"])

    def test_xls_preview_preserves_original_and_validates_normalized_hash(self):
        workbook = Workbook()
        workbook.active.append(["Fecha", "Importe"])
        workbook.save(self.path)
        workbook.close()
        xls_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic"
        upload = SimpleUploadedFile("synthetic.xls", xls_bytes)
        scope = {
            "company_id": str(uuid4()),
            "country_code": "SV",
            "account_id": str(uuid4()),
        }
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.folder / "private"):
            with patch("contared_api.imports._normalize_xls", side_effect=lambda _source, target: shutil.copyfile(self.path, target)):
                preview_id, metadata, normalized_path = create_preview(
                    upload,
                    user_id=uuid4(),
                    scope=scope,
                    reconciliation_id=uuid4(),
                    origin="banco",
                )
            stored_metadata, verified_path = get_preview(
                preview_id,
                user_id=metadata["user_id"],
                scope=scope,
                reconciliation_id=metadata["conciliacion_id"],
            )

        self.assertEqual(Path(stored_metadata["_source_path"]).read_bytes(), xls_bytes)
        self.assertEqual(normalized_path, verified_path)
        self.assertEqual(normalized_path.name, "normalized.xlsx")

    def test_xls_normalizer_handles_legacy_empty_cells_without_format_indexes(self):
        source_path = self.folder / "empty-cells.xls"
        target_path = self.folder / "normalized.xlsx"
        source_path.write_bytes(XLS_SIGNATURE + b"test")
        source_sheet = SimpleNamespace(
            name="Legacy",
            nrows=1,
            ncols=1,
            cell=lambda _row, _column: SimpleNamespace(
                value="", ctype=0, xf_index=None,
            ),
        )
        source = SimpleNamespace(
            nsheets=1,
            sheets=lambda: [source_sheet],
            datemode=0,
            xf_list=[],
            format_map={},
            release_resources=lambda: None,
        )
        with patch("contared_api.imports.xlrd.open_workbook", return_value=source):
            _normalize_xls(source_path, target_path)

        self.assertTrue(target_path.is_file())
        converted = load_workbook(target_path, read_only=True, data_only=True)
        try:
            self.assertEqual(converted.sheetnames, ["Legacy"])
            self.assertIsNone(converted["Legacy"]["A1"].value)
        finally:
            converted.close()

    def test_cuscatlan_headerless_xls_uses_positions_only_for_matching_structure(self):
        source_path = self.folder / "Movimientos_008303000002509_fixture.xls"
        target_path = self.folder / "normalized.xlsx"
        source_path.write_bytes(XLS_SIGNATURE + b"test")
        rows = [
            ["30/09/2026", "BANCA ELECTRONICA", "0000123", "ABONO", "+", "5.00", "1005.00", "1005.00"],
            ["01/10/2026", "BANCA ELECTRONICA", "0000124", "CARGO", "-", "2.00", "1003.00", "1003.00"],
        ]
        source_sheet = SimpleNamespace(
            name="Movimientos",
            nrows=len(rows),
            ncols=8,
            cell=lambda row, column: SimpleNamespace(
                value=rows[row][column],
                ctype=xlrd.XL_CELL_NUMBER if column in (5, 6, 7) else xlrd.XL_CELL_TEXT,
                xf_index=None,
            ),
        )
        source = SimpleNamespace(
            nsheets=1,
            sheets=lambda: [source_sheet],
            datemode=0,
            xf_list=[],
            format_map={},
            release_resources=lambda: None,
        )

        with patch("contared_api.imports.xlrd.open_workbook", return_value=source):
            synthetic_header = _normalize_xls(source_path, target_path)

        self.assertTrue(synthetic_header)
        converted = load_workbook(target_path, read_only=True, data_only=True)
        try:
            sheet = converted["Movimientos"]
            self.assertEqual(sheet["A1"].value, "Fecha Transacción")
            self.assertEqual(sheet["C2"].value, "0000123")
            self.assertEqual(sheet["F3"].value, "2.00")
        finally:
            converted.close()

    def test_cuscatlan_headerless_xls_with_mismatched_structure_is_not_positionally_mapped(self):
        source_path = self.folder / "Movimientos_008303000002509_fixture.xls"
        target_path = self.folder / "normalized.xlsx"
        source_path.write_bytes(XLS_SIGNATURE + b"test")
        source_sheet = SimpleNamespace(
            name="Movimientos",
            nrows=2,
            ncols=7,
            cell=lambda _row, _column: SimpleNamespace(
                value="texto",
                ctype=xlrd.XL_CELL_TEXT,
                xf_index=None,
            ),
        )
        source = SimpleNamespace(
            nsheets=1,
            sheets=lambda: [source_sheet],
            datemode=0,
            xf_list=[],
            format_map={},
            release_resources=lambda: None,
        )

        with patch("contared_api.imports.xlrd.open_workbook", return_value=source):
            synthetic_header = _normalize_xls(source_path, target_path)

        self.assertFalse(synthetic_header)
        converted = load_workbook(target_path, read_only=True, data_only=True)
        try:
            self.assertEqual(converted["Movimientos"].max_row, 2)
            self.assertEqual(converted["Movimientos"]["A1"].value, "texto")
        finally:
            converted.close()

    def test_xlsx_content_cannot_be_uploaded_with_xls_extension(self):
        workbook = Workbook()
        workbook.active.append(["Fecha", "Importe"])
        workbook.save(self.path)
        workbook.close()
        upload = SimpleUploadedFile("misnamed.xls", self.path.read_bytes())

        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.folder / "private"):
            with self.assertRaises(ValidationError):
                create_preview(
                    upload,
                    user_id=uuid4(),
                    scope={
                        "company_id": str(uuid4()),
                        "country_code": "SV",
                        "account_id": str(uuid4()),
                    },
                    reconciliation_id=uuid4(),
                    origin="banco",
                )

    def test_blank_rows_are_reported_as_warnings(self):
        self.save_sheet(
            ["Fecha", "Monto"],
            [[date(2026, 1, 2), 1], [None, None], [date(2026, 1, 3), 2]],
        )

        result = self.validate("libros", {"fecha": "Fecha", "monto": "Monto"})

        self.assertEqual([row["fila_origen"] for row in result.movimientos], [2, 4])
        self.assertEqual([warning["fila"] for warning in result.advertencias], [3])

    def test_repeated_headers_and_totals_are_reported_not_imported(self):
        self.save_sheet(
            ["Fecha", "Monto", "Descripción"],
            [
                ["Fecha", "Monto", "Descripción"],
                [None, 100, "Total general"],
                [date(2026, 1, 3), 5, "Pago"],
            ],
        )

        result = self.validate(
            "libros",
            {"fecha": "Fecha", "monto": "Monto", "descripcion": "Descripción"},
        )

        self.assertEqual([row["fila_origen"] for row in result.movimientos], [4])
        self.assertIn("Encabezado repetido", result.advertencias[0]["mensaje"])
        self.assertIn("total o subtotal", result.advertencias[1]["mensaje"])

    def test_confirm_options_accept_explicit_exclusions_and_require_exact_rows(self):
        request = SimpleNamespace(
            data={
                "sheet": "Estado",
                "header_row": 1,
                "mapping": {"fecha": "Fecha", "monto": "Monto"},
                "excluded_rows": [4],
                "save_profile": True,
                "profile_name": "Banco principal",
            }
        )
        self.assertEqual(_validate_options(
            request,
            {"excluded_rows", "save_profile", "profile_name"},
        ), ("Estado", 1, {"fecha": "Fecha", "monto": "Monto"}, None))
        _validate_exclusions([4], [{"fila": 4}])
        with self.assertRaises(ValidationError):
            _validate_exclusions([], [{"fila": 4}])

    def test_preview_is_user_bound_and_validation_does_not_write_movements(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append(["Fecha", "Importe"])
        sheet.append([date(2026, 1, 5), -12.5])
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        user_id = uuid4()
        reconciliation_id = uuid4()
        scope = {
            "user_id": str(user_id),
            "company_id": str(uuid4()),
            "country_code": "SV",
            "account_id": str(uuid4()),
            "role": "auxiliar",
            "state": "borrador",
            "period": date(2026, 1, 1),
            "cutoff": date(2026, 1, 31),
        }
        preview_storage = self.folder / "private"
        factory = APIRequestFactory()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=preview_storage):
            with patch("contared_api.conciliaciones._scope_for_reconciliation", return_value=scope):
                upload_request = factory.post(
                    "/api/v1/conciliaciones/",
                    {
                        "origen": "banco",
                        "file": SimpleUploadedFile(
                            "libros.xlsx",
                            content.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ),
                    },
                    format="multipart",
                )
                force_authenticate(upload_request, user=SupabaseUser(user_id))
                upload_response = ReconciliationPreviewCreateView.as_view()(
                    upload_request, reconciliation_id=reconciliation_id
                )
                self.assertEqual(upload_response.status_code, 201)
                preview_id = upload_response.data["preview_id"]

                validation_request = factory.post(
                    "/api/v1/conciliaciones/",
                    {
                        "origen": "libros",
                        "sheet": "Estado",
                        "header_row": 1,
                        "mapping": {"fecha": "Fecha", "monto": "Importe"},
                    },
                    format="json",
                )
                force_authenticate(validation_request, user=SupabaseUser(user_id))
                validation_response = ReconciliationPreviewValidateView.as_view()(
                    validation_request,
                    reconciliation_id=reconciliation_id,
                    preview_id=preview_id,
                )
                self.assertEqual(validation_response.status_code, 200)
                self.assertEqual(validation_response.data["movimientos"][0]["monto"], "-12.5")
                self.assertEqual(validation_response.data["movimientos"][0]["direccion"], "salida")

            with self.assertRaises(NotFound):
                get_preview(
                    preview_id,
                    user_id=uuid4(),
                    scope=scope,
                    reconciliation_id=reconciliation_id,
                )

    def test_confirm_rejects_a_bank_document_with_an_incompatible_currency(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append(["Fecha", "Importe"])
        sheet.append([date(2026, 9, 5), 12.5])
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        user_id = uuid4()
        reconciliation_id = uuid4()
        scope = {
            "user_id": str(user_id),
            "company_id": str(uuid4()),
            "country_code": "GT",
            "account_id": str(uuid4()),
            "account_number": "0040040592",
            "currency_code": "GTQ",
            "bank_name": "Banco Industrial",
            "accounting_code": "1101030103",
            "role": "auxiliar",
            "state": "borrador",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
        }
        preview_storage = self.folder / "private"
        factory = APIRequestFactory()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=preview_storage):
            with patch("contared_api.conciliaciones._scope_for_reconciliation", return_value=scope):
                upload_request = factory.post(
                    "/api/v1/conciliaciones/",
                    {
                        "origen": "banco",
                        "file": SimpleUploadedFile(
                            "estado.xlsx",
                            content.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ),
                    },
                    format="multipart",
                )
                force_authenticate(upload_request, user=SupabaseUser(user_id))
                upload_response = ReconciliationPreviewCreateView.as_view()(
                    upload_request, reconciliation_id=reconciliation_id
                )
                preview_id = upload_response.data["preview_id"]
                metadata_path = preview_storage / preview_id / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["contexto"] = {
                    "pais_codigo": "GT",
                    "moneda_codigo": "USD",
                    "cuenta_bancaria": "0040040592",
                    "banco": "Banco Industrial",
                }
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                confirm_request = factory.post(
                    "/api/v1/conciliaciones/",
                    {
                        "origen": "banco",
                        "sheet": "Estado",
                        "header_row": 1,
                        "mapping": {"fecha": "Fecha", "monto": "Importe"},
                    },
                    format="json",
                )
                force_authenticate(confirm_request, user=SupabaseUser(user_id))
                response = ReconciliationPreviewConfirmView.as_view()(
                    confirm_request,
                    reconciliation_id=reconciliation_id,
                    preview_id=preview_id,
                )

        self.assertEqual(response.status_code, 400)
        self.assertIn("USD", str(response.data))
        self.assertIn("GTQ", str(response.data))

    def test_month_coverage_defaults_to_the_full_month_and_bounds_partial_dates(self):
        self.assertEqual(
            _preview_coverage({"periodo": "2026-09"}),
            (date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.assertEqual(
            _preview_coverage({
                "periodo": "2026-09",
                "cobertura_parcial": True,
                "fecha_desde": "2026-09-10",
                "fecha_hasta": "2026-09-20",
            }),
            (date(2026, 9, 10), date(2026, 9, 20)),
        )
        with self.assertRaises(ValidationError):
            _preview_coverage({
                "periodo": "2026-09",
                "cobertura_parcial": True,
                "fecha_desde": "2026-08-31",
                "fecha_hasta": "2026-09-20",
            })

    def test_account_binding_rejects_country_number_and_bank_mismatches(self):
        scope = {
            "country_code": "GT",
            "currency_code": "GTQ",
            "account_number": "0040040592",
            "bank_name": "Banco Industrial",
            "accounting_code": "1101030103",
        }
        cases = [
            (
                {"pais_codigo": "SV", "moneda_codigo": "GTQ"},
                "país",
            ),
            (
                {"pais_codigo": "GT", "moneda_codigo": "USD"},
                "USD",
            ),
            (
                {"pais_codigo": "GT", "moneda_codigo": "GTQ", "cuenta_bancaria": "123"},
                "número de cuenta",
            ),
            (
                {"pais_codigo": "GT", "moneda_codigo": "GTQ", "banco": "Banco Agrícola"},
                "banco declarado",
            ),
            (
                {"pais_codigo": "GT", "moneda_codigo": "GTQ", "codigo_contable": "999"},
                "código contable",
            ),
        ]
        for context, expected_message in cases:
            with self.subTest(context=context), self.assertRaises(ValidationError) as raised:
                _validate_workbook_account(
                    context,
                    "libros" if "codigo_contable" in context else "banco",
                    scope,
                )
            self.assertIn(expected_message, str(raised.exception))

    def test_unbound_bank_validation_requires_and_checks_selected_account(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        for _ in range(8):
            sheet.append(["Información de cuenta"])
        sheet.append(["Fecha", "TT", "Descripción", "No. Doc", "Debe (GTQ)", "Haber (GTQ)", "Saldo (GTQ)"])
        sheet.append([date(2026, 1, 9), "NC", "Fecha serial fuera del período", "1001", None, 10, 1497485.67])
        sheet.append([date(2026, 9, 13), "CQ", "Movimiento de septiembre", "1002", 5, None, 1497480.67])
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        user_id = uuid4()
        account_id = str(uuid4())
        scope = {
            "user_id": str(user_id),
            "company_id": str(uuid4()),
            "country_code": "GT",
            "role": "auxiliar",
        }
        account_scope = {
            **scope,
            "account_id": account_id,
            "account_number": "0040040592",
            "currency_code": "GTQ",
            "bank_name": "Banco Industrial",
            "accounting_code": "1101030103",
        }
        preview_storage = self.folder / "private"
        factory = APIRequestFactory()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=preview_storage):
            with (
                patch("contared_api.conciliaciones._unbound_preview_scope", return_value=scope),
                patch("contared_api.conciliaciones._scope_for_account", return_value=account_scope),
            ):
                upload_request = factory.post(
                    "/api/v1/conciliaciones/previsualizaciones/",
                    {
                        "empresa_id": scope["company_id"],
                        "pais_codigo": "GT",
                        "origen": "banco",
                        "file": SimpleUploadedFile(
                            "Estado de cuenta Banco Septiembre.xlsx",
                            content.getvalue(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ),
                    },
                    format="multipart",
                )
                force_authenticate(upload_request, user=SupabaseUser(user_id))
                upload_response = UnboundReconciliationPreviewCreateView.as_view()(upload_request)
                self.assertEqual(upload_response.status_code, 201)
                preview_id = upload_response.data["preview_id"]
                metadata_path = preview_storage / preview_id / "metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["contexto"] = {
                    "pais_codigo": "GT",
                    "moneda_codigo": "GTQ",
                    "cuenta_bancaria": "0040040592",
                    "periodo_declarado": "2026-09",
                }
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                missing_account_request = factory.post(
                    f"/api/v1/conciliaciones/previsualizaciones/{preview_id}/validar/",
                    {
                        "empresa_id": scope["company_id"],
                        "pais_codigo": "GT",
                        "periodo": "2026-09",
                        "origen": "banco",
                        "sheet": "Estado",
                        "header_row": 9,
                        "mapping": {
                            "fecha": "A", "codigo_tt": "B", "descripcion": "C",
                            "referencia": "D", "salida": "E",
                            "entrada": "F", "saldo_origen": "G",
                        },
                    },
                    format="json",
                )
                force_authenticate(missing_account_request, user=SupabaseUser(user_id))
                missing_account_response = UnboundReconciliationPreviewValidateView.as_view()(
                    missing_account_request,
                    preview_id=preview_id,
                )
                self.assertEqual(missing_account_response.status_code, 400)
                self.assertIn("cuenta_id", missing_account_response.data)

                validation_request = factory.post(
                    f"/api/v1/conciliaciones/previsualizaciones/{preview_id}/validar/",
                    {
                        "empresa_id": scope["company_id"],
                        "pais_codigo": "GT",
                        "cuenta_id": account_id,
                        "periodo": "2026-09",
                        "origen": "banco",
                        "sheet": "Estado",
                        "header_row": 9,
                        "mapping": {
                            "fecha": "A", "codigo_tt": "B", "descripcion": "C",
                            "referencia": "D", "salida": "E",
                            "entrada": "F", "saldo_origen": "G",
                        },
                        "date_format": "%d/%m/%Y",
                    },
                    format="json",
                )
                force_authenticate(validation_request, user=SupabaseUser(user_id))
                response = UnboundReconciliationPreviewValidateView.as_view()(
                    validation_request,
                    preview_id=preview_id,
                )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["movimientos_validos"], 1)
        self.assertEqual(len(response.data["exclusiones_requeridas"]), 1)
        self.assertEqual(response.data["exclusiones_requeridas"][0]["fecha"], "2026-01-09")

    def test_account_api_saves_and_returns_accounting_code(self):
        account_id = str(uuid4())
        code = "1101030103"
        scope = {
            "company_id": str(uuid4()),
            "country_code": "GT",
            "user_id": str(uuid4()),
            "role": "administrador",
        }

        class Cursor:
            def __init__(self):
                self.last = None
                self.rows = []
                self.update_params = None
                self.update_query = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params=None):
                if query.startswith("SELECT COUNT(*)"):
                    self.last = (1,)
                elif query.startswith("SELECT c.id::text,c.banco_id"):
                    self.rows = [(account_id, "bank-id", "Banco Industrial", "Operativa", "0040040592", "GTQ", True, True, code)]
                elif query.startswith("SELECT c.id::text,b.activo"):
                    self.last = (account_id, True)
                elif query.startswith("UPDATE contared.cuentas_bancarias"):
                    self.update_query = query
                    self.update_params = params
                    self.last = (account_id, "0040040592", "Operativa", "GTQ", True, params[0])

            def fetchone(self):
                return self.last

            def fetchall(self):
                return self.rows

        read_cursor = Cursor()
        request = APIRequestFactory().get(
            "/api/v1/cuentas/",
            {"empresa_id": scope["company_id"], "pais_codigo": "GT"},
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        with (
            patch("contared_api.banking.scope_from_request", return_value=scope),
            patch("contared_api.banking.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.banking.connection", SimpleNamespace(cursor=lambda: read_cursor)),
        ):
            account_response = AccountListCreateView.as_view()(request)

        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(account_response.data["results"][0]["codigo_contable"], code)

        write_cursor = Cursor()
        update_request = APIRequestFactory().patch(
            f"/api/v1/cuentas/{account_id}/",
            {
                "empresa_id": scope["company_id"],
                "pais_codigo": "GT",
                "codigo_contable": code,
            },
            format="json",
        )
        force_authenticate(update_request, user=SupabaseUser(uuid4()))
        with (
            patch("contared_api.banking.scope_from_request", return_value=scope),
            patch("contared_api.banking.require_administrator"),
            patch("contared_api.banking.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.banking.connection", SimpleNamespace(cursor=lambda: write_cursor)),
        ):
            update_response = AccountDetailView.as_view()(update_request, account_id=account_id)

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.data["codigo_contable"], code)
        self.assertIn("codigo_contable=%s", write_cursor.update_query)
        self.assertEqual(write_cursor.update_params[0], code)

    def test_selected_account_scope_supplies_ledger_code_for_document_validation(self):
        company_id = str(uuid4())
        account_id = str(uuid4())
        user_id = str(uuid4())
        code = "1101030103"

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                return None

            def fetchone(self):
                return (company_id, "GT", "0040040592", "GTQ", "Banco Industrial", code)

        request = SimpleNamespace(user=SimpleNamespace(auth_user_id=user_id))
        session = {
            "usuario": {"id": str(uuid4())},
            "accesos": [{"empresa_id": company_id, "pais_codigo": "GT", "rol": "auxiliar"}],
        }
        with (
            patch("contared_api.conciliaciones.load_session_access", return_value=session),
            patch("contared_api.conciliaciones.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.conciliaciones.connection", SimpleNamespace(cursor=lambda: Cursor())),
        ):
            account_scope = _scope_for_account(request, account_id)

        self.assertEqual(account_scope["accounting_code"], code)
        _validate_workbook_account({"codigo_contable": code, "moneda_codigo": "GTQ"}, "libros", account_scope)
        with self.assertRaises(ValidationError):
            _validate_workbook_account(
                {"codigo_contable": "9999999999", "moneda_codigo": "GTQ"},
                "libros",
                account_scope,
            )


class PreviewBindingTests(SimpleTestCase):
    def setUp(self):
        self.storage = Path(__file__).resolve().parents[1] / "test_artifacts" / str(uuid4())
        self.storage.mkdir(parents=True)
        self.user_id = str(uuid4())
        self.company_id = str(uuid4())
        self.account_id = str(uuid4())
        self.reconciliation_id = str(uuid4())
        self.scope = {
            "user_id": self.user_id,
            "company_id": self.company_id,
            "country_code": "GT",
            "account_id": self.account_id,
            "account_number": "0040040592",
            "currency_code": "GTQ",
            "bank_name": "Banco Industrial",
            "accounting_code": "1101030103",
            "bank_id": str(uuid4()),
            "role": "administrador",
            "state": "borrador",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
        }

    def tearDown(self):
        shutil.rmtree(self.storage, ignore_errors=True)

    def create_unbound_preview(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Estado"
        sheet.append(["Fecha", "Monto"])
        sheet.append([date(2026, 9, 1), 125])
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/previsualizaciones/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "origen": "banco",
                "file": SimpleUploadedFile(
                    "estado.xlsx",
                    content.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
            format="multipart",
        )
        force_authenticate(request, user=SupabaseUser(self.user_id))
        with (
            override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.storage),
            patch(
                "contared_api.conciliaciones._unbound_preview_scope",
                return_value={
                    "user_id": self.user_id,
                    "company_id": self.company_id,
                    "country_code": "GT",
                    "account_id": None,
                    "bank_id": None,
                    "reconciliation_id": None,
                    "role": "administrador",
                },
            ),
            patch("contared_api.conciliaciones._writer"),
        ):
            response = UnboundReconciliationPreviewCreateView.as_view()(request)
        self.assertEqual(response.status_code, 201, response.data)
        preview_id = response.data["preview_id"]
        metadata_path = self.storage / str(preview_id) / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["contexto"] = {
            "pais_codigo": "GT",
            "moneda_codigo": "GTQ",
            "cuenta_bancaria": "0040040592",
        }
        metadata_path.write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )
        return preview_id

    def test_unbound_preview_binds_once_and_same_destination_retry_is_allowed(self):
        preview_id = self.create_unbound_preview()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.storage):
            metadata, path = bind_preview(
                preview_id,
                user_id=self.user_id,
                scope=self.scope,
                reconciliation_id=self.reconciliation_id,
            )
            retry_metadata, retry_path = bind_preview(
                preview_id,
                user_id=self.user_id,
                scope=self.scope,
                reconciliation_id=self.reconciliation_id,
            )
            bound_metadata, _ = get_preview(
                preview_id,
                user_id=self.user_id,
                scope=self.scope,
                reconciliation_id=self.reconciliation_id,
            )

        self.assertEqual(metadata["cuenta_id"], self.account_id)
        self.assertEqual(metadata["conciliacion_id"], self.reconciliation_id)
        self.assertEqual(retry_metadata["conciliacion_id"], self.reconciliation_id)
        self.assertEqual(bound_metadata["conciliacion_id"], self.reconciliation_id)
        self.assertEqual(path, retry_path)
        self.assertEqual(path.read_bytes(), retry_path.read_bytes())

    def test_binding_rejects_another_user_company_or_destination(self):
        preview_id = self.create_unbound_preview()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.storage):
            with self.assertRaises(NotFound):
                bind_preview(
                    preview_id,
                    user_id=str(uuid4()),
                    scope=self.scope,
                    reconciliation_id=self.reconciliation_id,
                )
            with self.assertRaises(NotFound):
                bind_preview(
                    preview_id,
                    user_id=self.user_id,
                    scope={**self.scope, "company_id": str(uuid4())},
                    reconciliation_id=self.reconciliation_id,
                )
            bind_preview(
                preview_id,
                user_id=self.user_id,
                scope=self.scope,
                reconciliation_id=self.reconciliation_id,
            )
            with self.assertRaises(NotFound):
                bind_preview(
                    preview_id,
                    user_id=self.user_id,
                    scope=self.scope,
                    reconciliation_id=str(uuid4()),
                )

    def test_confirmation_binds_unbound_preview_before_idempotent_retry(self):
        preview_id = self.create_unbound_preview()
        factory = APIRequestFactory()
        request_data = {
            "origen": "banco",
            "mapping": {"fecha": "Fecha", "monto": "Monto"},
            "sheet": "Estado",
            "header_row": 1,
            "date_format": "%d/%m/%Y",
            "excluded_rows": [],
            "save_profile": False,
            "catalogo_tipo_transaccion": "{}",
        }
        repeated_result = {
            "carga_id": str(uuid4()),
            "sha256": "a" * 64,
            "nombre_archivo": "estado.xlsx",
            "origen": "banco",
            "movimientos_insertados": 1,
            "filas_excluidas": [],
            "perfil_id": None,
            "repetido": True,
        }

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        responses = []
        with (
            override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.storage),
            patch(
                "contared_api.conciliaciones._scope_for_reconciliation",
                return_value=self.scope,
            ),
            patch("contared_api.conciliaciones._writer"),
            patch("contared_api.conciliaciones.connection", SimpleNamespace(cursor=Cursor)),
            patch("contared_api.conciliaciones.transaction.atomic", return_value=nullcontext()),
            patch(
                "contared_api.conciliaciones._existing_import_result",
                return_value=repeated_result,
            ),
        ):
            for _ in range(2):
                request = factory.post(
                    f"/api/v1/conciliaciones/{self.reconciliation_id}/previsualizaciones/{preview_id}/confirmar/",
                    request_data,
                    format="json",
                )
                force_authenticate(request, user=SupabaseUser(self.user_id))
                responses.append(ReconciliationPreviewConfirmView.as_view()(
                    request,
                    reconciliation_id=self.reconciliation_id,
                    preview_id=preview_id,
                ))
            metadata = json.loads(
                (self.storage / str(preview_id) / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertTrue(all(response.data["repetido"] for response in responses))
        self.assertEqual(metadata["cuenta_id"], self.account_id)
        self.assertEqual(metadata["conciliacion_id"], self.reconciliation_id)

    def test_binding_preserves_preview_integrity_and_expiration_checks(self):
        preview_id = self.create_unbound_preview()
        with override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.storage):
            original = self.storage / str(preview_id) / "original.xlsx"
            original.write_bytes(original.read_bytes() + b"tampered")
            with self.assertRaises(NotFound):
                bind_preview(
                    preview_id,
                    user_id=self.user_id,
                    scope=self.scope,
                    reconciliation_id=self.reconciliation_id,
                )
            expired_id = self.create_unbound_preview()
            expired_path = self.storage / str(expired_id) / "metadata.json"
            expired_metadata = json.loads(expired_path.read_text(encoding="utf-8"))
            expired_metadata["expira_en"] = 0
            expired_path.write_text(json.dumps(expired_metadata), encoding="utf-8")
            with self.assertRaises(NotFound):
                bind_preview(
                    expired_id,
                    user_id=self.user_id,
                    scope=self.scope,
                    reconciliation_id=self.reconciliation_id,
                )
            self.assertFalse((self.storage / str(expired_id)).exists())

    def test_confirmation_does_not_bind_preview_with_incompatible_currency(self):
        preview_id = self.create_unbound_preview()
        metadata_path = self.storage / str(preview_id) / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["contexto"]["moneda_codigo"] = "USD"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        request = APIRequestFactory().post(
            f"/api/v1/conciliaciones/{self.reconciliation_id}/previsualizaciones/{preview_id}/confirmar/",
            {
                "origen": "banco",
                "mapping": {"fecha": "Fecha", "monto": "Monto"},
                "sheet": "Estado",
                "header_row": 1,
                "date_format": "%d/%m/%Y",
                "excluded_rows": [],
                "save_profile": False,
                "catalogo_tipo_transaccion": "{}",
            },
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(self.user_id))
        with (
            override_settings(IMPORT_PREVIEW_STORAGE_DIR=self.storage),
            patch(
                "contared_api.conciliaciones._scope_for_reconciliation",
                return_value=self.scope,
            ),
            patch("contared_api.conciliaciones._writer"),
        ):
            response = ReconciliationPreviewConfirmView.as_view()(
                request,
                reconciliation_id=self.reconciliation_id,
                preview_id=preview_id,
            )
            unchanged = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(unchanged["cuenta_id"])
        self.assertIsNone(unchanged["conciliacion_id"])

    def test_catalog_and_operational_account_delete_routes_resolve_separately(self):
        catalog_id = uuid4()
        account_id = uuid4()

        catalog_match = resolve(
            f"/api/v1/catalogo-cuentas/{catalog_id}/retiro/"
        )
        account_match = resolve(
            f"/api/v1/cuentas/{account_id}/"
        )

        self.assertEqual(catalog_match.url_name, "api-catalog-account-retirement")
        self.assertIn("catalog_id", catalog_match.kwargs)
        self.assertEqual(account_match.url_name, "api-account-detail")
        self.assertIn("account_id", account_match.kwargs)

    def test_catalog_retirement_url_executes_through_django_resolver(self):
        catalog_id = uuid4()
        scope = {
            "company_id": str(uuid4()),
            "country_code": "GT",
            "user_id": str(uuid4()),
            "role": "administrador",
        }

        class CatalogCursor:
            def __init__(self):
                self.last = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, query, params=None):
                if "to_regclass('contared.catalogo_cuentas_retiradas')" in query:
                    self.last = ("contared.catalogo_cuentas_retiradas",)
                elif "FROM contared.catalogo_cuentas_bancarias" in query:
                    self.last = (str(catalog_id),)
                else:
                    self.last = None

            def fetchone(self):
                return self.last

        client = APIClient()
        client.force_authenticate(SupabaseUser(uuid4()))
        cursor = CatalogCursor()
        url = (
            f"/api/v1/catalogo-cuentas/{catalog_id}/retiro/"
            f"?empresa_id={scope['company_id']}&pais_codigo=GT"
        )
        match = resolve(url.split("?")[0])
        self.assertEqual(match.url_name, "api-catalog-account-retirement")

        with (
            patch("contared_api.banking.scope_from_request", return_value=scope),
            patch("contared_api.banking.require_administrator"),
            patch("contared_api.banking.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.banking.connection", SimpleNamespace(cursor=lambda: cursor)),
        ):
            response = client.delete(url)

        self.assertEqual(response.status_code, 204)

    def test_operational_account_delete_url_resolves_to_account_detail(self):
        account_id = uuid4()
        match = resolve(f"/api/v1/cuentas/{account_id}/")
        self.assertEqual(match.url_name, "api-account-detail")
        self.assertEqual(match.kwargs["account_id"], account_id)
