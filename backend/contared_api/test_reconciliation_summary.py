import json
from types import SimpleNamespace
from datetime import date
from decimal import Decimal

from django.db import DatabaseError
from django.test import SimpleTestCase
from rest_framework.response import Response

from contared_api import reconciliation_summary
from contared_api.reconciliation_summary import (
    _generic_pending_query,
    _summary_highlight_rows,
    _summary_table_rows,
    _load_summary,
)
from contared_api.session_access import database_error_details


class ReconciliationSummaryQueryTests(SimpleTestCase):
    def test_generic_pending_query_interpolates_optional_deposit_filter(self):
        with_deposits = _generic_pending_query(True)
        without_deposits = _generic_pending_query(False)

        self.assertIn(
            "AND NOT EXISTS (SELECT 1 FROM contared.deposito_movimientos",
            with_deposits,
        )
        self.assertNotIn("deposito_movimientos", without_deposits)
        self.assertNotIn("{", with_deposits)
        self.assertNotIn("{", without_deposits)

    def test_database_error_details_include_sql_diagnostic_without_query_text(self):
        error = DatabaseError("sensitive query text must not be logged")
        cause = Exception()
        cause.sqlstate = "42601"
        cause.diag = SimpleNamespace(
            schema_name="contared",
            table_name=None,
            column_name=None,
            constraint_name=None,
            message_primary='syntax error at or near "{"',
        )
        error.__cause__ = cause

        details = database_error_details(error)

        self.assertIn("SQLSTATE: 42601", details)
        self.assertIn('message=syntax error at or near "{"', details)
        self.assertNotIn("sensitive query text", details)

    def test_summary_view_uses_an_imported_response_class(self):
        self.assertIs(reconciliation_summary.Response, Response)

    def test_detected_final_balances_are_used_without_initial_balances(self):
        source_bank = {
            "archivo": "Estado bancario.xlsx",
            "hoja": "Movimientos",
            "fila": 193,
            "metodo": "Último saldo acumulado no vacío.",
        }
        source_books = {
            "archivo": "Mayor.xlsx",
            "hoja": "Sheet1",
            "fila": 202,
            "metodo": "Último saldo acumulado no vacío.",
        }
        reconciliation = (
            "reconciliation-id", "account-id", "Cuenta 0040040592",
            "Banco Industrial de Guatemala", "GTQ", date(2026, 9, 1),
            date(2026, 9, 30), "borrador", None, None, None, None,
            "Empresa", "Guatemala", Decimal("1462269.13"),
            json.dumps(source_bank), Decimal("1447082.30"),
            json.dumps(source_books),
        )

        class Cursor:
            query = ""

            def execute(self, query, params=None):
                self.query = query

            def fetchone(self):
                return reconciliation if "SELECT r.id::text" in self.query else None

            def fetchall(self):
                return []

        snapshot = _load_summary(
            Cursor(),
            "reconciliation-id",
            {
                "account_id": "account-id",
                "company_id": "company-id",
                "country_code": "GT",
                "user_name": "Usuario autorizado",
            },
            include_deposits=False,
        )

        self.assertIsNone(snapshot["saldos"]["banco"]["inicial_declarado"])
        self.assertEqual(snapshot["saldos"]["banco"]["final_usado"], "1462269.13")
        self.assertEqual(snapshot["saldos"]["libros"]["final_usado"], "1447082.30")
        self.assertEqual(snapshot["diferencia_saldos_finales"], "15186.83")
        self.assertEqual(snapshot["diferencia_pendiente"], "15186.83")
        highlights = _summary_highlight_rows(snapshot)
        summary_rows = _summary_table_rows(snapshot)
        used_balances = next(row for row in summary_rows if row[0] == "Saldo final utilizado")
        final_difference = next(
            row for row in summary_rows
            if row[0] == "Diferencia entre saldos finales (Banco − Contabilidad)"
        )
        self.assertEqual(highlights[0][1], "1462269.13")
        self.assertEqual(highlights[1][1], "1447082.30")
        self.assertEqual(highlights[2][1], "15186.83")
        self.assertEqual(used_balances[2], highlights[0][1])
        self.assertEqual(used_balances[1], highlights[1][1])
        self.assertEqual(final_difference[2], highlights[2][1])
        self.assertEqual(highlights[0][2], "Detectado del archivo")
        self.assertEqual(
            snapshot["saldos"]["banco"]["fuente_final_detectado"]["fila"],
            193,
        )

    def test_summary_highlights_keep_zero_and_flag_final_balance_conflicts(self):
        snapshot = {
            "saldos": {
                "banco": {
                    "final_usado": "0",
                    "origen_final": "manual",
                    "conflicto_final": {
                        "capturado_manualmente": "0",
                        "detectado_en_carga": "1",
                    },
                },
                "libros": {
                    "final_usado": "0",
                    "origen_final": "carga",
                    "conflicto_final": None,
                },
            },
            "diferencia_saldos_finales": "0",
        }

        highlights = _summary_highlight_rows(snapshot)

        self.assertEqual(highlights[0][1], "0")
        self.assertEqual(highlights[0][2], "Capturado manualmente")
        self.assertIn("0", highlights[0][3])
        self.assertIn("1", highlights[0][3])
        self.assertEqual(highlights[1][2], "Detectado del archivo")
        self.assertEqual(highlights[2][1], "0")
        self.assertEqual(highlights[2][2], "Sin diferencia entre saldos finales")
