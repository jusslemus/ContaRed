import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook
from openpyxl import load_workbook
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.cheques import (
    ChequeImportTemplateView,
    DEFAULT_CHEQUE_IMPORT_PROFILE,
    _cheque_import_profile,
    _parse_cheque_workbook,
)


class ChequeImportParserTests(unittest.TestCase):
    def create_workbook(self, rows, headers=None):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Cheques"
        sheet.append(headers or [
            "numero_cheque", "fecha_emision", "beneficiario", "monto",
            "estado_al_corte", "fecha_entrega", "descripcion", "referencia",
        ])
        for row in rows:
            sheet.append(row)
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        path = Path(handle.name)
        workbook.save(path)
        workbook.close()
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_extracts_independent_cheque_fields_and_preserves_leading_zeroes(self):
        path = self.create_workbook([[
            "000121", date(2026, 9, 9), "Proveedor", 125.5,
            "En circulación", date(2026, 9, 10), "Pago de servicios", "REF-121",
        ]])

        profile = _cheque_import_profile(DEFAULT_CHEQUE_IMPORT_PROFILE)
        rows, warnings = _parse_cheque_workbook(path, profile, date(2026, 9, 30))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["numero_cheque"], "000121")
        self.assertEqual(rows[0]["fecha_emision"], date(2026, 9, 9))
        self.assertEqual(rows[0]["beneficiario"], "Proveedor")
        self.assertEqual(str(rows[0]["monto"]), "125.5")
        self.assertEqual(rows[0]["estado"], "en_circulacion")
        self.assertEqual(rows[0]["fecha_entrega"], date(2026, 9, 10))
        self.assertEqual(rows[0]["referencia"], "REF-121")
        self.assertEqual(warnings, [])

    def test_configurable_headers_and_date_format(self):
        path = self.create_workbook([[
            "000122", "09/09/2026", "Proveedor", 80,
            "Retenido", None, "Servicios", "",
        ]], [
            "Número", "Fecha emisión", "Beneficiario", "Monto",
            "Estado al corte", "Fecha entrega", "Descripción", "Referencia",
        ])
        profile = _cheque_import_profile({
            "sheet": "Cheques",
            "header_row": 1,
            "date_format": "%d/%m/%Y",
            "mapping": {
                "numero_cheque": "Número",
                "fecha_emision": "Fecha emisión",
                "beneficiario": "Beneficiario",
                "monto": "Monto",
                "estado": "Estado al corte",
                "fecha_entrega": "Fecha entrega",
                "descripcion": "Descripción",
            },
        })

        rows, _ = _parse_cheque_workbook(path, profile, date(2026, 9, 30))

        self.assertEqual(rows[0]["fecha_emision"], date(2026, 9, 9))
        self.assertEqual(rows[0]["estado"], "retenido")
        self.assertEqual(rows[0]["descripcion"], "Servicios")

    def test_duplicate_rows_reject_the_whole_document(self):
        path = self.create_workbook([
            ["000123", date(2026, 9, 9), "Proveedor", 25, "Retenido", None, "", ""],
            ["000123", date(2026, 9, 9), "Proveedor", 25, "Retenido", None, "", ""],
        ])
        profile = _cheque_import_profile(DEFAULT_CHEQUE_IMPORT_PROFILE)

        with self.assertRaises(ValidationError) as raised:
            _parse_cheque_workbook(path, profile, date(2026, 9, 30))

        self.assertIn("repetidos", str(raised.exception.detail))

    def test_invalid_cutoff_state_rejects_the_document(self):
        path = self.create_workbook([[
            "000124", date(2026, 9, 9), "Proveedor", 25,
            "Cobrado", None, "", "",
        ]])
        profile = _cheque_import_profile(DEFAULT_CHEQUE_IMPORT_PROFILE)

        with self.assertRaises(ValidationError) as raised:
            _parse_cheque_workbook(path, profile, date(2026, 9, 30))

        self.assertIn("Retenido o En circulación", str(raised.exception.detail))

    def test_template_download_contains_the_configurable_columns(self):
        request = APIRequestFactory().get("/api/v1/cuentas/account-id/cheques/plantilla/")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        with patch(
            "contared_api.cheques._scope_for_account",
            return_value={"role": "administrador"},
        ):
            response = ChequeImportTemplateView.as_view()(request, account_id="account-id")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ))
        if hasattr(response, "render"):
            response.render()
        workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.sheetnames, ["Cheques", "Instrucciones"])
        self.assertEqual(
            next(workbook["Cheques"].iter_rows(values_only=True)),
            (
                "numero_cheque", "fecha_emision", "beneficiario", "monto",
                "estado_al_corte", "fecha_entrega", "descripcion", "referencia",
            ),
        )
