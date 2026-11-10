import json
import shutil
import tempfile
from pathlib import Path
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from openpyxl import Workbook
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseUser
from contared_api.direct_reconciliation import (
    DirectReconciliationDetailView,
    DirectReconciliationListCreateView,
    _analyze_files,
    _dashboard_sections,
    _difference_explanation,
    _historical_cheques,
    _historical_difference_evidence,
    _job_response,
    _validate_account_result,
)
from contared_api.banking import CUENTAS_GT_CONFIRMADAS, CUENTAS_SV_CONFIRMADAS


class DirectReconciliationCreateEndpointTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.factory = APIRequestFactory()
        self.user_id = uuid4()
        self.company_id = str(uuid4())
        self.account_id = str(uuid4())
        self.bank_id = str(uuid4())
        self.scope = {
            "user_id": str(self.user_id),
            "user_name": "Test user",
            "company_id": self.company_id,
            "company_name": "Test company",
            "country_code": "GT",
            "role": "administrador",
        }
        self.account_row = (
            self.account_id,
            self.bank_id,
            "Banco Industrial de Guatemala",
            "0040040592",
            "BIQ",
            "GTQ",
            None,
            True,
            False,
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def post(self, account_id):
        request = self.factory.post(
            "/api/v1/conciliaciones-directas/trabajos/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "cuenta_id": account_id,
                "files": SimpleUploadedFile("sample.xlsx", b"workbook fixture"),
            },
            format="multipart",
        )
        force_authenticate(request, user=SupabaseUser(self.user_id))
        return DirectReconciliationListCreateView.as_view()(request)

    @patch("contared_api.direct_reconciliation._analyze_files")
    @patch("contared_api.direct_reconciliation._safe_scope_path")
    @patch("contared_api.direct_reconciliation.scope_from_request")
    @patch("contared_api.direct_reconciliation.connection")
    def test_post_with_valid_account_id_reaches_authorized_account_lookup(
        self,
        mock_connection,
        mock_scope_from_request,
        mock_safe_scope_path,
        mock_analyze_files,
    ):
        mock_scope_from_request.return_value = self.scope
        mock_safe_scope_path.return_value = self.root
        mock_analyze_files.return_value = {
            "archivos": [],
            "banco": None,
            "libros": None,
            "cheques": [],
            "diagnosticos": [],
            "movimientos_banco": [],
            "movimientos_libros": [],
        }
        cursor = MagicMock()
        cursor.fetchone.return_value = self.account_row
        cursor.fetchall.return_value = [self.account_row]
        mock_connection.cursor.return_value.__enter__.return_value = cursor

        response = self.post(self.account_id)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["cuenta_id"], self.account_id)
        self.assertEqual(response.data["cuenta_seleccionada"]["numero"], "0040040592")
        self.assertEqual(cursor.execute.call_args_list[0].args[1][0], self.account_id)
        mock_analyze_files.assert_called_once()

    @patch("contared_api.direct_reconciliation._validate_account_result", return_value=([], None))
    @patch("contared_api.direct_reconciliation._analyze_files")
    @patch("contared_api.direct_reconciliation._safe_scope_path")
    @patch("contared_api.direct_reconciliation.scope_from_request")
    @patch("contared_api.direct_reconciliation.connection")
    def test_cuscatlan_analysis_requires_and_passes_selected_period(
        self,
        mock_connection,
        mock_scope_from_request,
        mock_safe_scope_path,
        mock_analyze_files,
        _mock_validate_account_result,
    ):
        scope = {**self.scope, "country_code": "SV"}
        account_row = (
            self.account_id, self.bank_id, "Banco Cuscatlán", "2509", "CUSCA 2509",
            "USD", "2509", True, False,
        )
        mock_scope_from_request.return_value = scope
        mock_safe_scope_path.return_value = self.root
        mock_analyze_files.return_value = {
            "archivos": [],
            "banco": None,
            "libros": None,
            "cheques": [],
            "diagnosticos": [],
            "movimientos_banco": [],
            "movimientos_libros": [],
        }
        cursor = MagicMock()
        cursor.fetchone.return_value = account_row
        cursor.fetchall.return_value = [account_row]
        mock_connection.cursor.return_value.__enter__.return_value = cursor
        request = self.factory.post(
            "/api/v1/conciliaciones-directas/trabajos/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "SV",
                "cuenta_id": self.account_id,
                "periodo": "2026-10",
                "files": SimpleUploadedFile("statement.xlsx", b"workbook fixture"),
            },
            format="multipart",
        )
        force_authenticate(request, user=SupabaseUser(self.user_id))

        response = DirectReconciliationListCreateView.as_view()(request)

        self.assertEqual(response.status_code, 201)
        mock_analyze_files.assert_called_once()
        self.assertEqual(mock_analyze_files.call_args.kwargs["period"], "2026-10")

    @patch("contared_api.direct_reconciliation.scope_from_request")
    @patch("contared_api.direct_reconciliation.connection")
    def test_cuscatlan_analysis_requires_month_and_year(
        self,
        mock_connection,
        mock_scope_from_request,
    ):
        mock_scope_from_request.return_value = {**self.scope, "country_code": "SV"}
        account_row = (
            self.account_id, self.bank_id, "Banco Cuscatlán", "2509", "CUSCA 2509",
            "USD", "2509", True, False,
        )
        cursor = MagicMock()
        cursor.fetchone.return_value = account_row
        cursor.fetchall.return_value = [account_row]
        mock_connection.cursor.return_value.__enter__.return_value = cursor
        request = self.factory.post(
            "/api/v1/conciliaciones-directas/trabajos/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "SV",
                "cuenta_id": self.account_id,
                "files": SimpleUploadedFile("statement.xlsx", b"workbook fixture"),
            },
            format="multipart",
        )
        force_authenticate(request, user=SupabaseUser(self.user_id))

        response = DirectReconciliationListCreateView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("periodo", response.data)

    @patch("contared_api.direct_reconciliation._validate_account_result", return_value=([], None))
    @patch("contared_api.direct_reconciliation._safe_scope_path")
    @patch("contared_api.direct_reconciliation.scope_from_request")
    @patch("contared_api.direct_reconciliation.connection")
    def test_analyze_endpoint_returns_october_cheque_results(
        self,
        mock_connection,
        mock_scope_from_request,
        mock_safe_scope_path,
        _mock_validate_account_result,
    ):
        mock_scope_from_request.return_value = self.scope
        mock_safe_scope_path.return_value = self.root
        cursor = MagicMock()
        cursor.fetchone.return_value = self.account_row
        cursor.fetchall.return_value = [self.account_row]
        mock_connection.cursor.return_value.__enter__.return_value = cursor
        source = Path(__file__).resolve().parents[2] / "docs contared" / "GT" / "octubre"
        def analyze(paths):
            uploads = [
                SimpleUploadedFile(path.name, path.read_bytes(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                for path in paths
            ]
            request = self.factory.post(
                "/api/v1/conciliaciones-directas/trabajos/",
                {
                    "empresa_id": self.company_id,
                    "pais_codigo": "GT",
                    "cuenta_id": self.account_id,
                    "files": uploads,
                },
                format="multipart",
            )
            force_authenticate(request, user=SupabaseUser(self.user_id))
            return DirectReconciliationListCreateView.as_view()(request)

        response = analyze(list(source.glob("*.xlsx")))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["resultado"]["cheques"][0]["registros"]), 6)
        self.assertEqual(
            response.data["resultado"]["resumen"]["diferencia_residual_pendiente"],
            "8166.39",
        )
        old_cheques = Path(__file__).resolve().parents[2] / "docs contared" / "GT" / "septiembre" / "Cheques en circulación.xlsx"
        previous_format_response = analyze([
            *[path for path in source.glob("*.xlsx") if "cheque" not in path.name.casefold()],
            old_cheques,
        ])
        self.assertEqual(previous_format_response.status_code, 201)
        self.assertEqual(
            previous_format_response.data["resultado"]["cheques"][0]["periodo_corte"],
            "2026-08",
        )
        self.assertTrue(any(
            record["numero_documento"] == "13721"
            for record in previous_format_response.data["resultado"]["cheques"][0]["registros"]
        ))

    @patch("contared_api.direct_reconciliation.connection")
    @patch("contared_api.direct_reconciliation.scope_from_request")
    def test_post_with_invalid_account_id_returns_json_validation_error(
        self,
        mock_scope_from_request,
        mock_connection,
    ):
        mock_scope_from_request.return_value = self.scope

        response = self.post("not-a-uuid")
        response.render()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("identificador de cuenta no es válido", str(response.data["cuenta_id"]))
        mock_connection.cursor.assert_not_called()


class DirectElSalvadorReconciliationTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.job = self.root / "job"
        self.originals = self.job / "originales"
        self.originals.mkdir(parents=True)
        self.source = Path(__file__).resolve().parents[2] / "docs contared" / "SV"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _analyze_pair(self, bank_name, ledger_name):
        shutil.copy2(self.source / bank_name, self.originals / bank_name)
        shutil.copy2(self.source / ledger_name, self.originals / ledger_name)
        result = _analyze_files(self.job, "SV")
        return result

    def _write_cuscatlan_fixture(self, rows, name="Movimientos_008303000002509_fixture.xlsx"):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Movimientos"
        sheet.append([
            "Fecha Transacción", "Oficina", "Referencia", "Transacción",
            "Deb./Cred.", "Valor", "Saldo Real", "Saldo Disponible",
        ])
        for row in rows:
            sheet.append(row)
        path = self.originals / name
        workbook.save(path)
        workbook.close()
        return path

    def test_promerica_real_xls_and_ledger_pair(self):
        result = self._analyze_pair(
            "PROMERICA (8) (1).xls",
            "CUENTA CONTA PROMERICA 49 (1).xlsx",
        )
        self.assertEqual(result["resumen"]["saldo_final_banco"], "29894.74")
        self.assertEqual(result["resumen"]["saldo_final_contabilidad"], "28663.26")
        self.assertEqual(result["resumen"]["diferencia_original"], "1231.48")
        self.assertEqual(result["resumen"]["controles"]["movimientos_banco"], 50)
        self.assertEqual(result["resumen"]["controles"]["movimientos_contabilidad"], 59)
        self.assertFalse(result["diagnosticos"])
        self.assertEqual(result["banco"]["reconocimiento"]["contexto"]["banco"], "Promerica")
        self.assertEqual(result["banco"]["reconocimiento"]["contexto"]["terminacion"], "0049")
        self.assertEqual(result["banco"]["reconocimiento"]["contexto"]["moneda_codigo"], "USD")

    def test_bac_real_xls_and_ledger_pair(self):
        result = self._analyze_pair(
            "BAC 386 (8) (1).xls",
            "CUENTA CONTA BAC 386.xlsx",
        )
        self.assertEqual(result["resumen"]["saldo_final_banco"], "25686.01")
        self.assertEqual(result["resumen"]["saldo_final_contabilidad"], "25625.6")
        self.assertEqual(result["resumen"]["diferencia_original"], "60.41")
        self.assertEqual(result["resumen"]["controles"]["movimientos_banco"], 34)
        self.assertEqual(result["resumen"]["controles"]["movimientos_contabilidad"], 60)
        self.assertFalse([item for item in result["diagnosticos"] if item["codigo"] == "errores_estado_bancario"])
        self.assertEqual(result["banco"]["reconocimiento"]["contexto"]["banco"], "BAC")
        self.assertEqual(result["banco"]["reconocimiento"]["contexto"]["terminacion"], "0386")
        self.assertEqual(result["banco"]["reconocimiento"]["contexto"]["moneda_codigo"], "USD")

    def test_cuscatlan_filters_selected_month_and_uses_verified_last_prior_movement(self):
        self._write_cuscatlan_fixture([
            [date(2026, 9, 27), "008", "0000123", "Abono", "C", 5, 1005, 1005],
            [date(2026, 9, 27), "008", "0000124", "Cargo", "D", 2, 1003, 1003],
            [date(2026, 10, 1), "008", "0000125", "Abono", "C", 10, 1013, 1013],
            [date(2026, 10, 30), "008", "0000126", "Cargo", "D", 4, 1009, 1009],
        ])

        result = _analyze_files(self.job, "SV", period="2026-10")

        bank = result["banco"]
        self.assertEqual(bank["reconocimiento"]["perfil_sugerido"], "banco_cuscatlan_movimientos")
        self.assertEqual(bank["reconocimiento"]["campos_reconocidos"], [
            "descripcion", "fecha", "monto", "referencia", "saldo_origen", "signo_monto",
        ])
        self.assertEqual(result["resumen"]["controles"]["movimientos_banco"], 2)
        self.assertEqual([item["referencia"] for item in bank["movimientos"]], ["0000125", "0000126"])
        self.assertEqual(result["resumen"]["saldo_inicial_banco"], "1003")
        self.assertEqual(result["resumen"]["saldo_final_banco"], "1009")
        self.assertEqual(result["resumen"]["cobertura_periodo_bancario"], "incierta")
        self.assertEqual(
            result["resumen"]["saldo_inicial_banco_evidencia"],
            {
                "saldo": "1003",
                "fecha": "2026-09-27",
                "archivo": "Movimientos_008303000002509_fixture.xlsx",
                "hoja": "Movimientos",
                "fila": 3,
                "metodo": "ultimo_movimiento_mes_anterior",
                "secuencia_verificada": True,
            },
        )

    def test_cuscatlan_ambiguous_same_day_order_requires_review(self):
        self._write_cuscatlan_fixture([
            [date(2026, 9, 27), "008", "0000123", "Abono", "C", 5, 1005, 1005],
            [date(2026, 9, 27), "008", "0000124", "Cargo", "D", 2, 999, 999],
            [date(2026, 10, 1), "008", "0000125", "Abono", "C", 10, 1009, 1009],
        ])

        result = _analyze_files(self.job, "SV", period="2026-10")

        self.assertIsNone(result["resumen"]["saldo_inicial_banco"])
        diagnostic = next(item for item in result["diagnosticos"] if item["codigo"] == "saldo_inicial_bancario_faltante")
        self.assertIn("revise el orden y el Saldo Real", diagnostic["mensaje"])

    def test_cuscatlan_explicit_opening_balance_is_accepted_and_cited(self):
        self._write_cuscatlan_fixture([
            [None, None, None, "Saldo inicial", None, None, 500, 500],
            [date(2026, 10, 1), "008", "0000125", "Abono", "C", 10, 510, 510],
        ])

        result = _analyze_files(self.job, "SV", period="2026-10")

        self.assertEqual(result["resumen"]["saldo_inicial_banco"], "500")
        evidence = result["resumen"]["saldo_inicial_banco_evidencia"]
        self.assertEqual(evidence["metodo"], "saldo_inicial_explicito")
        self.assertEqual(evidence["archivo"], "Movimientos_008303000002509_fixture.xlsx")
        self.assertEqual(evidence["hoja"], "Movimientos")
        self.assertEqual(evidence["fila"], 2)

    def test_cuscatlan_missing_opening_balance_returns_actionable_message(self):
        self._write_cuscatlan_fixture([
            [date(2026, 10, 1), "008", "0000125", "Abono", "C", 10, 510, 510],
        ])

        result = _analyze_files(self.job, "SV", period="2026-10")

        diagnostic = next(item for item in result["diagnosticos"] if item["codigo"] == "saldo_inicial_bancario_faltante")
        self.assertEqual(
            diagnostic["mensaje"],
            "Falta el saldo inicial bancario. Descargue el estado con un rango que incluya el último movimiento del mes anterior",
        )


class DirectGuatemalaReconciliationTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.job = self.root / "job"
        originals = self.job / "originales"
        originals.mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / "docs contared" / "GT" / "septiembre"
        names = [
            next(source.glob("Estado de cuenta Banco Septiembre.xlsx")).name,
            next(source.glob("Saldo de cuenta - 1101030103*.xlsx")).name,
        ]
        names.extend(file.name for file in source.glob("*.xlsx") if "circul" in file.name.lower())
        for name in names:
            shutil.copy2(source / name, originals / name)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _clear_originals(self):
        for path in (self.job / "originales").iterdir():
            path.unlink()

    def test_confirmed_catalog_currencies_do_not_infer_from_alias(self):
        for account in ("0040040592", "0040040584", "090003566", "901020461", "3445256003"):
            bank, alias, currency = CUENTAS_GT_CONFIRMADAS[account]
            self.assertEqual(currency, "GTQ")
            self.assertTrue(bank and alias)
        self.assertEqual(CUENTAS_GT_CONFIRMADAS["0040040584"][1], "BI$3")
        self.assertTrue(all(currency == "USD" for _, _, currency in CUENTAS_SV_CONFIRMADAS.values()))
        self.assertEqual(CUENTAS_SV_CONFIRMADAS["0386"][1], "BAC 386")

    def test_real_guatemala_controls_and_august_historical_cut(self):
        result = _analyze_files(self.job, "GT")
        summary = result["resumen"]
        self.assertEqual(summary["saldo_final_banco"], "1462269.13")
        self.assertEqual(summary["saldo_final_contabilidad"], "1447082.3")
        self.assertEqual(summary["diferencia_original"], "15186.83")
        self.assertEqual(summary["controles"]["movimientos_banco"], 184)
        self.assertEqual(summary["controles"]["movimientos_contabilidad"], 200)
        self.assertEqual([cut["periodo_corte"] for cut in result["cheques"]], ["2026-08"])
        self.assertEqual(result["cheques"][0]["registros"][0]["numero_documento"], "13721")
        cheque_13721 = result["cheques"][0]["registros"][0]
        self.assertEqual(cheque_13721["estado_al_corte"], "cobrado_identificado_en_banco")
        self.assertNotIn("errores_estado_bancario", [item["codigo"] for item in result["diagnosticos"]])
        references_8494 = {
            match["referencia"]
            for match in result["proceso_conciliacion"]["coincidencias_confirmadas"]
            if match["referencia"]
        }
        self.assertTrue("8494" in references_8494)
        proceso = result["proceso_conciliacion"]
        counts = proceso["conteos"]
        self.assertEqual(counts["cruces_confirmados"], 172)
        self.assertEqual(counts["movimientos_banco_consumidos"], 172)
        self.assertEqual(counts["movimientos_contabilidad_consumidos"], 172)
        bank_ids = []
        book_ids = []
        for match in proceso["coincidencias_confirmadas"]:
            bank_ids.append(match["banco"].get("fila_origen"))
            book_ids.append(match["contabilidad"].get("fila_origen"))
        self.assertEqual(len(bank_ids), len(set(bank_ids)))
        self.assertEqual(len(book_ids), len(set(book_ids)))
        self.assertEqual(result["depositos"]["sin_contraparte"], result["depositos"]["ingresos_bancarios_sin_naturaleza_confirmada"])
        self.assertTrue(all(
            Decimal(str(item["movimiento"]["monto"])) < 0
            for item in result["depositos"]["salidas_pendientes"]
        ))

    def test_october_biq_imports_september_cheques_and_applies_unique_cq_charges_once(self):
        self._clear_originals()
        source = Path(__file__).resolve().parents[2] / "docs contared" / "GT" / "octubre"
        for path in source.glob("*.xlsx"):
            shutil.copy2(path, self.job / "originales" / path.name)
        self.assertEqual(len(list((self.job / "originales").iterdir())), 3)

        result = _analyze_files(self.job, "GT")

        self.assertEqual(len(result["cheques"]), 1)
        cut = result["cheques"][0]
        self.assertEqual(cut["periodo_corte"], "2026-09")
        self.assertEqual(len(cut["registros"]), 6)
        self.assertNotIn("TOTAL", [record["numero_documento"] for record in cut["registros"]])
        by_number = {record["numero_documento"]: record for record in cut["registros"]}
        for number, amount, date_value in (
            ("13725", "-3700", "2026-10-07"),
            ("13733", "-2240", "2026-10-01"),
            ("13734", "-2358.48", "2026-10-01"),
            ("13735", "-2734.1", "2026-10-03"),
        ):
            record = by_number[number]
            self.assertEqual(record["estado_al_corte"], "cobrado_identificado_en_banco")
            cobro = record["cobro_identificado"][0]
            self.assertEqual(cobro["tipo_movimiento"], "CQ")
            self.assertEqual(cobro["referencia"], number)
            self.assertEqual(cobro["monto"], amount)
            self.assertEqual(cobro["fecha"], date_value)
            self.assertEqual(cobro["evidencia"]["archivo"], "Estado de cuenta bi.xlsx")
        self.assertEqual(by_number["0"]["estado_al_corte"], "pendiente_identificacion")
        self.assertEqual(sum(record["numero_cero"] for record in cut["registros"]), 2)

        explanation = result["resumen"]["explicacion_diferencia"]
        applied = explanation["residual"]["partidas_aplicadas"]
        self.assertEqual(len(applied), 4)
        self.assertTrue(all(item["afecta_residual"] for item in applied))
        self.assertEqual(result["resumen"]["diferencia_residual_pendiente"], "8166.39")
        identified_ids = [item["id"] for item in applied]
        self.assertEqual(len(identified_ids), len(set(identified_ids)))
        self.assertFalse({
            item["id"] for item in explanation["pendientes"]["banco"]["movimientos"]
        } & set(identified_ids))

    def test_october_analysis_keeps_the_previous_cheque_header_format(self):
        self._clear_originals()
        source = Path(__file__).resolve().parents[2] / "docs contared" / "GT"
        october = source / "octubre"
        for path in october.glob("*.xlsx"):
            if "cheque" not in path.name.casefold():
                shutil.copy2(path, self.job / "originales" / path.name)
        old_format = source / "septiembre" / "Cheques en circulación.xlsx"
        parsed_old_format = _historical_cheques(old_format)
        self.assertEqual(parsed_old_format["periodo_corte"], "2026-08")
        self.assertTrue(any(
            record["numero_documento"] == "13721"
            for record in parsed_old_format["registros"]
        ))
        shutil.copy2(old_format, self.job / "originales" / old_format.name)
        self.assertEqual(len(list((self.job / "originales").iterdir())), 3)

        result = _analyze_files(self.job, "GT")

        self.assertEqual([cut["periodo_corte"] for cut in result["cheques"]], ["2026-08"])
        self.assertTrue(any(
            record["numero_documento"] == "13721"
            for record in result["cheques"][0]["registros"]
        ))
        self.assertNotIn("formato_no_reconocido", [item["codigo"] for item in result["diagnosticos"]])

    def test_failed_cheque_file_reports_filename_and_reason(self):
        self._clear_originals()
        source = Path(__file__).resolve().parents[2] / "docs contared" / "GT" / "octubre"
        for path in source.glob("*.xlsx"):
            if "cheque" not in path.name.casefold():
                shutil.copy2(path, self.job / "originales" / path.name)
        (self.job / "originales" / "Cheques dañados.xlsx").write_bytes(b"not an Excel workbook")
        self.assertEqual(len(list((self.job / "originales").iterdir())), 3)

        result = _analyze_files(self.job, "GT")

        diagnostic = next(item for item in result["diagnosticos"] if item.get("archivo") == "Cheques dañados.xlsx")
        self.assertEqual(diagnostic["codigo"], "error_importacion_cheques")
        self.assertIn("El archivo no es un libro .xlsx válido.", diagnostic["mensaje"])

    def test_groupings_are_confirmed_only_when_the_amount_is_unique(self):
        result = {
            "movimientos_banco": [
                {"fecha": "2026-09-09", "referencia": "G1", "monto": "820", "fila_origen": 1, "datos_originales": {}},
                {"fecha": "2026-09-09", "referencia": "G1", "monto": "400", "fila_origen": 2, "datos_originales": {}},
            ],
            "movimientos_libros": [
                {"fecha": "2026-09-09", "referencia": "G1", "monto": "1220", "fila_origen": 3, "datos_originales": {}},
            ],
        }
        _dashboard_sections(result)
        proceso = result["proceso_conciliacion"]
        self.assertTrue(proceso["alternativas_ambiguas"] or proceso["coincidencias_confirmadas"])
        claimed = {
            match["banco"].get("fila_origen")
            for match in proceso["coincidencias_confirmadas"]
            if "banco" in match
        }
        self.assertLessEqual(len(claimed), 2)

    def test_reference_alternativa_8494_decimal_8940(self):
        result = {
            "movimientos_banco": [{
                "fecha": "2026-09-01",
                "referencia": "8494",
                "numero_transaccion": "8494",
                "monto": "8940.0",
                "fila_origen": 12,
                "datos_originales": {"Referencia": "8494", "archivo": "banco.xlsx", "hoja": "Hoja1"},
            }],
            "movimientos_libros": [{
                "fecha": "2026-09-01",
                "referencia": None,
                "monto": "8940",
                "fila_origen": 44,
                "datos_originales": {"Referencia 1": "", "Referencia 2": "8494", "archivo": "mayor.xlsx", "hoja": "Sheet1"},
            }],
        }
        _dashboard_sections(result)
        matches = result["proceso_conciliacion"]["coincidencias_confirmadas"]
        self.assertEqual(len(matches), 1)
        self.assertIn("8494", matches[0]["referencias_alternativas"])

    def test_ambiguous_and_duplicate_consumption_are_not_confirmed(self):
        result = {
            "movimientos_banco": [
                {"fecha": "2026-09-01", "referencia": "A", "monto": "100", "fila_origen": 1, "datos_originales": {}},
                {"fecha": "2026-09-02", "referencia": "A", "monto": "100", "fila_origen": 2, "datos_originales": {}},
            ],
            "movimientos_libros": [
                {"fecha": "2026-09-01", "referencia": "A", "monto": "100", "fila_origen": 3, "datos_originales": {}},
            ],
        }
        _dashboard_sections(result)
        confirmed = result["proceso_conciliacion"]["coincidencias_confirmadas"]
        ambiguous = result["proceso_conciliacion"]["alternativas_ambiguas"]
        self.assertEqual(confirmed, [])
        self.assertTrue(ambiguous or result["proceso_conciliacion"]["movimientos_sin_contraparte"]["banco"])


class DirectAccountBindingTests(SimpleTestCase):
    def setUp(self):
        self.bac_0386 = {
            "id": "bac-0386",
            "banco_id": "bank-bac",
            "banco": "BAC",
            "numero": "0386",
            "numero_terminacion": "0386",
            "moneda_codigo": "USD",
        }
        self.bac_6715 = {
            "id": "bac-6715",
            "banco_id": "bank-bac",
            "banco": "BAC",
            "numero": "6715",
            "numero_terminacion": "6715",
            "moneda_codigo": "USD",
        }

    def _bank_result(self, *, number=None, ending=None, bank="BAC", currency="USD"):
        context = {"banco": bank, "moneda_codigo": currency}
        if number:
            context["cuenta_bancaria"] = number
        if ending:
            context["terminacion"] = ending
        recognition = {"origen_sugerido": "banco", "contexto": context}
        return {
            "archivos": [{"reconocimiento": recognition}],
            "banco": {"reconocimiento": recognition},
            "libros": {"reconocimiento": {"contexto": {}}},
        }

    def test_selects_between_two_accounts_at_same_bank(self):
        diagnostics, suggestion = _validate_account_result(
            self.bac_6715,
            [self.bac_0386, self.bac_6715],
            self._bank_result(number="200280386"),
            "SV",
        )
        self.assertEqual(diagnostics[0]["codigo"], "cuenta_numero_incompatible")
        self.assertIsNone(suggestion)

    def test_bac_0386_matches_full_document_number_and_suggests_explicit_link(self):
        diagnostics, suggestion = _validate_account_result(
            self.bac_0386,
            [self.bac_0386, self.bac_6715],
            self._bank_result(number="200280386"),
            "SV",
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(suggestion["numero_completo"], "200280386")
        self.assertEqual(suggestion["terminacion"], "0386")

    def test_account_bank_and_currency_mismatches_are_reported(self):
        result = self._bank_result(number="200280386", bank="Banco Agrícola", currency="GTQ")
        diagnostics, _ = _validate_account_result(
            self.bac_0386,
            [self.bac_0386, self.bac_6715],
            result,
            "SV",
        )
        self.assertIn("cuenta_banco_incompatible", {item["codigo"] for item in diagnostics})
        self.assertIn("cuenta_moneda_incompatible", {item["codigo"] for item in diagnostics})

    def test_terminacion_requires_unique_bank_and_currency_match(self):
        diagnostics, _ = _validate_account_result(
            self.bac_0386,
            [self.bac_0386, {**self.bac_0386, "id": "duplicate-0386"}],
            self._bank_result(ending="0386"),
            "SV",
        )
        self.assertIn("cuenta_coincidencia_ambigua", {item["codigo"] for item in diagnostics})

    def test_ledger_without_bank_number_keeps_explicit_account(self):
        result = self._bank_result()
        result["libros"]["reconocimiento"]["contexto"] = {"moneda_codigo": "USD"}
        diagnostics, suggestion = _validate_account_result(
            self.bac_0386,
            [self.bac_0386, self.bac_6715],
            result,
            "SV",
        )
        self.assertEqual(diagnostics, [])
        self.assertIsNone(suggestion)

    def test_ledger_number_conflicting_with_selected_account_is_blocked(self):
        result = self._bank_result(number="200280386")
        result["libros"]["reconocimiento"]["contexto"] = {
            "banco": "BAC",
            "moneda_codigo": "USD",
            "cuenta_bancaria": "2002806715",
        }
        diagnostics, _ = _validate_account_result(
            self.bac_0386,
            [self.bac_0386, self.bac_6715],
            result,
            "SV",
        )
        self.assertIn("cuenta_numero_incompatible", {item["codigo"] for item in diagnostics})

    def test_cuscatlan_file_number_is_matched_to_selected_account_ending(self):
        account = {
            "id": "cusca-2509",
            "banco_id": "bank-cusca",
            "banco": "Banco Cuscatlán",
            "numero": "2509",
            "numero_terminacion": "2509",
            "moneda_codigo": "USD",
        }
        result = {
            "archivos": [],
            "banco": {
                "reconocimiento": {
                    "contexto": {
                        "banco": "Banco Cuscatlán",
                        "moneda_codigo": "USD",
                        "cuenta_bancaria": "008303000002509",
                        "terminacion": "2509",
                    },
                },
            },
            "libros": {"reconocimiento": {"contexto": {}}},
        }

        diagnostics, suggestion = _validate_account_result(
            account, [account], result, "SV",
        )

        self.assertEqual(diagnostics, [])
        self.assertEqual(suggestion["numero_completo"], "008303000002509")

    def test_multiple_bank_accounts_are_not_mixed(self):
        result = self._bank_result(number="200280386")
        second = {
            "origen_sugerido": "banco",
            "contexto": {"banco": "BAC", "moneda_codigo": "USD", "cuenta_bancaria": "2002806715"},
        }
        result["archivos"].append({"reconocimiento": second})
        diagnostics, _ = _validate_account_result(
            self.bac_0386,
            [self.bac_0386, self.bac_6715],
            result,
            "SV",
        )
        self.assertIn("documentos_cuentas_multiples", {item["codigo"] for item in diagnostics})


class DirectDifferenceExplanationTests(SimpleTestCase):
    def _result(self, difference):
        return {
            "resumen": {
                "saldo_final_banco": "100.00",
                "saldo_final_contabilidad": str(Decimal("100.00") - Decimal(difference)),
                "diferencia_original": difference,
                "diferencia_residual_pendiente": difference,
                "partidas_conciliatorias": [],
            },
            "proceso_conciliacion": {
                "movimientos_sin_contraparte": {
                    "banco": [],
                    "contabilidad": [],
                },
                "alternativas_ambiguas": [],
                "diferencias_importe": [],
            },
            "banco": {"reconocimiento": {"contexto": {"moneda_codigo": "USD"}}},
            "libros": {"reconocimiento": {"contexto": {"moneda_codigo": "USD"}}},
        }

    def test_difference_direction_and_zero_with_no_applied_items(self):
        for value, expected_direction, expected_residual in (
            ("60.41", "banco_mayor", "$60.41"),
            ("-60.41", "banco_menor", "-$60.41"),
            ("0.00", "iguales", "$0.00"),
        ):
            result = self._result(value)
            _difference_explanation(result)
            explanation = result["resumen"]["explicacion_diferencia"]
            self.assertEqual(explanation["diferencia_original"]["direccion"], expected_direction)
            self.assertEqual(explanation["residual"]["importe"], value)
            self.assertIn(expected_residual, explanation["residual"]["mensaje_sin_partidas"])

    def test_verified_applied_item_changes_the_shared_residual(self):
        result = self._result("60.41")
        result["resumen"]["partidas_conciliatorias"] = [{
            "concepto": "Ajuste verificado",
            "importe": "10.00",
            "importe_con_signo": "-10.00",
            "diferencia_signo": "-10.00",
            "lado": "Banco",
            "motivo": "Evidencia bancaria confirmada",
            "verificado": True,
            "afecta_residual": True,
            "evidencia_banco": {"archivo": "banco.xlsx", "fila": 7},
        }]
        _difference_explanation(result)
        explanation = result["resumen"]["explicacion_diferencia"]
        self.assertEqual(result["resumen"]["diferencia_residual_pendiente"], "50.41")
        self.assertEqual(len(explanation["residual"]["partidas_aplicadas"]), 1)
        self.assertTrue(explanation["residual"]["partidas_aplicadas"][0]["afecta_residual"])

    def test_explicit_cancellation_pairs_once_and_keeps_date_net(self):
        result = self._result("0.00")
        original = {
            "fecha": "2026-10-15",
            "referencia": "11169",
            "monto": "-1691.79",
            "descripcion": "Pago de servicios",
            "datos_originales": {"Referencia 1": "11169", "archivo": "mayor.xlsx", "hoja": "Sheet1"},
            "fila_origen": 8,
        }
        cancellation = {
            "fecha": "2026-10-15",
            "referencia": "11169",
            "monto": "1691.79",
            "descripcion": "Cancelar entrada para número de pago 11169",
            "datos_originales": {"Referencia 1": "11169", "archivo": "mayor.xlsx", "hoja": "Sheet1"},
            "fila_origen": 19,
        }
        result["proceso_conciliacion"]["movimientos_sin_contraparte"]["contabilidad"] = [
            {"movimiento_id": "books:1", "movimiento": original, "situacion": "Sin contraparte", "evidencia": {"archivo": "mayor.xlsx", "hoja": "Sheet1", "fila": 8}},
            {"movimiento_id": "books:2", "movimiento": cancellation, "situacion": "Sin contraparte", "evidencia": {"archivo": "mayor.xlsx", "hoja": "Sheet1", "fila": 19}},
        ]

        _difference_explanation(result)

        pending = result["resumen"]["explicacion_diferencia"]["pendientes"]["contabilidad"]
        self.assertEqual(pending["neto"], "0.00")
        self.assertEqual(len(pending["reversiones_identificadas"]), 1)
        group = pending["reversiones_identificadas"][0]
        self.assertEqual(group["referencia"], "11169")
        self.assertEqual(group["neto"], "0.00")
        self.assertEqual([item["id"] for item in group["movimientos"]], ["books:1", "books:2"])
        self.assertEqual(len(pending["grupos_fecha"]), 1)
        self.assertEqual(pending["grupos_fecha"][0]["neto"], "0.00")
        self.assertEqual(pending["grupos_fecha"][0]["movimientos"], [])

    def test_duplicate_reversal_counterparts_stay_unmatched_for_review(self):
        result = self._result("0.00")
        movements = [
            ("books:1", "-100.00", "Pago 11169"),
            ("books:2", "100.00", "Cancelar pago 11169"),
            ("books:3", "100.00", "Anulación de pago 11169"),
        ]
        result["proceso_conciliacion"]["movimientos_sin_contraparte"]["contabilidad"] = [
            {
                "movimiento_id": item_id,
                "movimiento": {
                    "fecha": "2026-10-15",
                    "referencia": "11169",
                    "monto": amount,
                    "descripcion": description,
                    "datos_originales": {"Referencia 1": "11169"},
                },
                "situacion": "Sin contraparte",
                "evidencia": {},
            }
            for item_id, amount, description in movements
        ]

        _difference_explanation(result)

        pending = result["resumen"]["explicacion_diferencia"]["pendientes"]["contabilidad"]
        self.assertEqual(pending["reversiones_identificadas"], [])
        self.assertEqual(len(pending["reversiones_por_revisar"]), 1)
        self.assertEqual(
            {item["id"] for item in pending["reversiones_por_revisar"][0]["movimientos"]},
            {"books:1", "books:2", "books:3"},
        )
        self.assertEqual(len(pending["movimientos"]), 3)
        self.assertEqual(pending["neto"], "100.00")

    def test_opening_and_pending_balances_form_a_separate_difference_breakdown(self):
        result = self._result("16006.29")
        result["resumen"].update({
            "saldo_final_banco": "40349.53",
            "saldo_final_contabilidad": "24343.24",
            "saldo_inicial_banco": "16233.93",
            "saldo_inicial_banco_evidencia": {
                "fecha": "2026-09-30", "archivo": "banco.xls", "hoja": "First Sheet", "fila": 2,
            },
        })
        result["banco"]["reconocimiento"]["contexto"].update({
            "saldo_inicial": "16233.93",
            "saldo_inicial_evidencia": result["resumen"]["saldo_inicial_banco_evidencia"],
        })
        result["libros"]["reconocimiento"]["contexto"].update({
            "saldo_inicial": "16212.48",
            "saldo_inicial_evidencia": {
                "archivo": "mayor.xlsx", "hoja": "Sheet1", "fila": 2,
            },
        })
        result["proceso_conciliacion"]["movimientos_sin_contraparte"]["contabilidad"] = [
            {
                "movimiento_id": "books:1",
                "movimiento": {"fecha": "2026-10-15", "referencia": "P1", "monto": "-8931.67"},
                "evidencia": {},
            },
            {
                "movimiento_id": "books:2",
                "movimiento": {"fecha": "2026-10-29", "referencia": "P2", "monto": "-7053.17"},
                "evidencia": {},
            },
        ]

        _difference_explanation(result)

        explanation = result["resumen"]["explicacion_diferencia"]
        self.assertEqual(explanation["diferencia_inicial"]["importe"], "21.45")
        self.assertEqual(explanation["pendientes"]["contabilidad"]["neto"], "-15984.84")
        self.assertEqual(
            [group["neto"] for group in explanation["pendientes"]["contabilidad"]["grupos_fecha"]],
            ["-8931.67", "-7053.17"],
        )
        self.assertEqual(explanation["desglose_diferencia"]["total"], "16006.29")
        self.assertTrue(explanation["desglose_diferencia"]["coincide_con_diferencia_original"])
        self.assertEqual(result["resumen"]["diferencia_residual_pendiente"], "16006.29")

    def test_previous_reconciliation_link_and_unique_later_bank_reflection(self):
        root = Path(tempfile.mkdtemp())
        scope = {"country_code": "SV", "company_id": "company"}
        account = {"id": "account", "moneda_codigo": "USD"}
        prior_job = {
            "id": "previous-job",
            "empresa_id": "company",
            "pais_codigo": "SV",
            "cuenta_id": "account",
            "moneda_codigo": "USD",
            "periodo_sugerido": "2026-09",
            "estado": "finalizado_con_pendientes",
            "titulo": "Conciliación de septiembre",
            "resultado": {"banco": {"reconocimiento": {"contexto": {"moneda_codigo": "USD"}}}},
        }
        later_movement = {
            "fecha": "2026-11-02",
            "referencia": "P-10",
            "monto": "-25.00",
            "fila_origen": 4,
            "datos_originales": {"archivo": "banco-noviembre.xlsx", "hoja": "Estado"},
        }
        later_job = {
            "id": "later-job",
            "empresa_id": "company",
            "pais_codigo": "SV",
            "cuenta_id": "account",
            "moneda_codigo": "USD",
            "periodo_sugerido": "2026-11",
            "estado": "preparado",
            "resultado": {
                "banco": {
                    "reconocimiento": {"contexto": {"moneda_codigo": "USD"}},
                    "movimientos": [later_movement],
                },
            },
        }
        mismatched_account_job = {
            **prior_job,
            "id": "other-account-prior",
            "cuenta_id": "other-account",
        }
        mismatched_currency_job = {
            **prior_job,
            "id": "other-currency-prior",
            "moneda_codigo": "EUR",
        }
        for job in (prior_job, later_job, mismatched_account_job, mismatched_currency_job):
            folder = root / "user" / job["id"]
            folder.mkdir(parents=True)
            (folder / "metadata.json").write_text(json.dumps(job), encoding="utf-8")
        result = self._result("25.00")
        result["contexto_resultado"] = {"periodo": "2026-10"}
        result["banco"]["reconocimiento"]["contexto"]["moneda_codigo"] = "USD"
        result["libros"]["reconocimiento"]["contexto"]["moneda_codigo"] = "USD"
        result["proceso_conciliacion"]["movimientos_sin_contraparte"]["contabilidad"] = [{
            "movimiento_id": "books:1",
            "movimiento": {
                "fecha": "2026-10-15",
                "referencia": "P-10",
                "monto": "-25.00",
            },
            "evidencia": {},
        }]

        try:
            with patch("contared_api.direct_reconciliation._scope_jobs_path", return_value=root):
                _difference_explanation(result)
                _historical_difference_evidence(result, scope, account)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        explanation = result["resumen"]["explicacion_diferencia"]
        self.assertEqual(explanation["conciliacion_anterior"]["id"], "previous-job")
        reflected = explanation["pendientes"]["contabilidad"]["movimientos"][0]["reflejado_en_periodo_posterior"]
        self.assertEqual(reflected["estado"], "Reflejado en el banco en un período posterior")
        self.assertEqual(reflected["fecha"], "2026-11-02")
        self.assertEqual(reflected["evidencia"]["fila"], 4)

    def test_missing_previous_reconciliation_and_duplicate_future_reference_are_not_confirmed(self):
        root = Path(tempfile.mkdtemp())
        scope = {"country_code": "SV", "company_id": "company"}
        account = {"id": "account", "moneda_codigo": "USD"}
        later_job = {
            "id": "later-job",
            "empresa_id": "company",
            "pais_codigo": "SV",
            "cuenta_id": "account",
            "moneda_codigo": "USD",
            "periodo_sugerido": "2026-11",
            "estado": "preparado",
            "resultado": {
                "banco": {
                    "reconocimiento": {"contexto": {"moneda_codigo": "USD"}},
                    "movimientos": [
                        {"fecha": "2026-11-02", "referencia": "P-10", "monto": "-25.00", "fila_origen": 4, "datos_originales": {}},
                        {"fecha": "2026-11-03", "referencia": "P-10", "monto": "-25.00", "fila_origen": 5, "datos_originales": {}},
                    ],
                },
            },
        }
        folder = root / "user" / later_job["id"]
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(json.dumps(later_job), encoding="utf-8")
        result = self._result("25.00")
        result["contexto_resultado"] = {"periodo": "2026-10"}
        result["banco"]["reconocimiento"]["contexto"]["moneda_codigo"] = "USD"
        result["libros"]["reconocimiento"]["contexto"]["moneda_codigo"] = "USD"
        result["banco"]["reconocimiento"]["contexto"]["saldo_inicial"] = "125.00"
        result["banco"]["reconocimiento"]["contexto"]["saldo_inicial_evidencia"] = {
            "archivo": "banco-octubre.xlsx", "hoja": "Estado", "fila": 2,
        }
        result["libros"]["reconocimiento"]["contexto"]["saldo_inicial"] = "100.00"
        result["libros"]["reconocimiento"]["contexto"]["saldo_inicial_evidencia"] = {
            "archivo": "mayor-octubre.xlsx", "hoja": "Mayor", "fila": 2,
        }
        result["proceso_conciliacion"]["movimientos_sin_contraparte"]["contabilidad"] = [{
            "movimiento_id": "books:1",
            "movimiento": {"fecha": "2026-10-15", "referencia": "P-10", "monto": "-25.00"},
            "evidencia": {},
        }]

        try:
            with patch("contared_api.direct_reconciliation._scope_jobs_path", return_value=root):
                _difference_explanation(result)
                _historical_difference_evidence(result, scope, account)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        explanation = result["resumen"]["explicacion_diferencia"]
        self.assertIsNone(explanation["conciliacion_anterior"])
        self.assertEqual(
            explanation["diferencia_inicial"]["origen_pendiente"],
            "Origen pendiente de revisar en el cierre anterior",
        )
        self.assertIsNone(
            explanation["pendientes"]["contabilidad"]["movimientos"][0]["reflejado_en_periodo_posterior"]
        )

    def test_detail_rebuilds_explanation_for_saved_result(self):
        root = Path(tempfile.mkdtemp())
        scope = {"country_code": "SV", "company_id": "company", "user_id": "user"}
        job_id = "saved-october"
        result = self._result("25.00")
        result["contexto_resultado"] = {"periodo": "2026-10"}
        result["banco"]["reconocimiento"]["contexto"].update({
            "moneda_codigo": "USD",
            "saldo_inicial": "125.00",
            "saldo_inicial_evidencia": {"archivo": "banco.xls", "hoja": "Estado", "fila": 2},
        })
        result["libros"]["reconocimiento"]["contexto"].update({
            "moneda_codigo": "USD",
            "saldo_inicial": "100.00",
            "saldo_inicial_evidencia": {"archivo": "mayor.xlsx", "hoja": "Mayor", "fila": 2},
        })
        job = {
            "id": job_id,
            "empresa_id": "company",
            "pais_codigo": "SV",
            "cuenta_id": "account",
            "moneda_codigo": "USD",
            "cuenta_seleccionada": {"id": "account", "moneda_codigo": "USD"},
            "estado": "finalizado_con_pendientes",
            "progreso": [],
            "archivos": [],
            "creado_en": "2026-10-31T00:00:00Z",
            "actualizado_en": "2026-10-31T00:00:00Z",
            "resultado": result,
        }
        job_dir = root / "user" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "metadata.json").write_text(json.dumps(job), encoding="utf-8")
        request = APIRequestFactory().get(f"/api/v1/conciliaciones-directas/trabajos/{job_id}/")
        force_authenticate(request, user=SupabaseUser(uuid4()))

        try:
            with (
                patch("contared_api.direct_reconciliation._access", return_value=scope),
                patch("contared_api.direct_reconciliation._job_path", return_value=job_dir),
                patch("contared_api.direct_reconciliation._scope_jobs_path", return_value=root),
            ):
                response = DirectReconciliationDetailView.as_view()(request, job_id=job_id)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        self.assertEqual(response.status_code, 200)
        explanation = response.data["resultado"]["resumen"]["explicacion_diferencia"]
        self.assertEqual(explanation["diferencia_inicial"]["importe"], "25.00")
        self.assertEqual(
            explanation["diferencia_inicial"]["origen_pendiente"],
            "Origen pendiente de revisar en el cierre anterior",
        )
        self.assertEqual(response.data["resultado"]["resumen"]["diferencia_residual_pendiente"], "25.00")
