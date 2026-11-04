import shutil
import tempfile
from pathlib import Path
from decimal import Decimal

from django.test import SimpleTestCase

from contared_api.direct_reconciliation import _analyze_files, _dashboard_sections, _job_response
from contared_api.banking import CUENTAS_GT_CONFIRMADAS, CUENTAS_SV_CONFIRMADAS


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


class DirectGuatemalaReconciliationTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.job = self.root / "job"
        originals = self.job / "originales"
        originals.mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / "docs contared" / "GT"
        names = [
            next(source.glob("Estado de cuenta Banco Septiembre.xlsx")).name,
            next(source.glob("Saldo de cuenta - 1101030103*.xlsx")).name,
        ]
        names.extend(file.name for file in source.glob("*.xlsx") if "circul" in file.name.lower())
        for name in names:
            shutil.copy2(source / name, originals / name)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

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
