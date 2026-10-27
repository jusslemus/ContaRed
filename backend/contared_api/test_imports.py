from io import BytesIO
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
from openpyxl import Workbook
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseUser
from contared_api.conciliaciones import (
    ReconciliationPreviewCreateView,
    ReconciliationPreviewValidateView,
    _validate_exclusions,
    _validate_options,
)
from contared_api.imports import get_preview, inspect_workbook_sheet, parse_decimal, validate_workbook
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
                        "origen": "libros",
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
