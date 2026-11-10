from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import re
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import DatabaseError
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import load_workbook
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseJWTAuthentication, SupabaseUser
from contared_api.reconciliation import Movement, propose_reconciliation
from contared_api.session_access import DatabaseUnavailable, resolve_authorized_scope, require_administrator
from contared_api.views import ApiStatusView, SessionView
from contared_api.reconciliation_summary import (
    _build_summary_pdf,
    _build_summary_xlsx,
    _summary_table_rows,
)
from contared_api.banking import AccountDetailView, BankDetailView
from contared_api.comparison import (
    ReconciliationProposalActionView,
    ReconciliationProposalConfirmView,
    ReconciliationProposalDismissView,
    _dataset_fingerprint,
    _eligible_movements,
    _engine_movements,
    _movement_from_row,
    _proposal_response,
    _require_role,
    _results_for_execution,
)
from contared_api.comparison_reports import ReconciliationReportView
from contared_api.comparison_reports import (
    GROUPS,
    _proposal_groups,
    _report_snapshot,
    _selected_proposals,
    build_pdf,
    build_xlsx,
)
from contared_api.depositos import (
    CLASSIFICATIONS,
    REQUIRED_DEPOSIT_TABLES,
    _ambiguous_comparison_options,
    _case_status_at_cutoff,
    _iso_date as deposit_iso_date,
    _uuid_list as deposit_uuid_list,
)
from contared_api.cheques import (
    ChequesMigrationRequired,
    ChequeAnnulmentView,
    ChequeCashView,
    ChequeCrossSync,
    ChequeDeliveryView,
    ChequeHistoryView,
    ChequeIssuanceCandidatesView,
    ReconciliationChequesView,
    ManualChequeCreateView,
    REQUIRED_CHEQUE_SCHEMA,
    _cheque_counts,
    _cheque_schema_columns_missing,
    _database_error,
    _fetch_cheques,
)
from contared_api.conciliaciones import (
    ReconciliationConflict,
    ReconciliationArchiveView,
    ReconciliationMovementsView,
    _scope_for_reconciliation,
    _archive_reference_specs,
    _apply_banco_industrial_fields,
    _has_reconciliation_references,
    _normalize_original_data,
    _transaction_type_label,
    _valid_identifier,
    _writer,
    _validate_transaction_catalog,
    _manual_import_duplicates,
)


AUTH_USER_ID = uuid4()
INTERNAL_USER_ID = "f42522e0-8d61-45d2-8b17-4cd7da7d7a01"


class ArchiveCursor:
    def __init__(self, *, has_information=False, blockers=(), references=()):
        self.has_information = has_information
        self.blockers = set(blockers)
        self.references = set(references)
        self.state = "borrador"
        self.archived_at = None
        self.deleted = False
        self.events = []
        self.last = None
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "SELECT estado,archivada_en" in query:
            self.last = (self.state, self.archived_at)
        elif "SELECT EXISTS" in query:
            blocker = next(
                (name for name in self.blockers if name in query),
                None,
            )
            if blocker:
                self.last = (True,)
            elif "FROM contared.eventos_archivo_conciliacion" in query:
                self.last = ("eventos_archivo_conciliacion" in self.references,)
            elif "estado='pendiente'" in query:
                self.last = ("carga_pendiente" in self.blockers,)
            elif "estado='propuesto'" in query:
                self.last = ("cruce_propuesto" in self.blockers,)
            elif "contared.pendientes" in query and "estado='abierto'" in query:
                self.last = ("pendiente_abierto" in self.blockers,)
            elif "contared.cheques" in query and "estado IN ('retenido','en_circulacion')" in query:
                self.last = ("cheque_abierto" in self.blockers,)
            elif "contared.casos_deposito" in query and "estado='abierto'" in query:
                self.last = ("deposito_abierto" in self.blockers,)
            elif "conciliacion_resolucion_id" in query and "conciliacion_origen_id<>" in query:
                self.last = ("referencia_otro_periodo" in self.blockers,)
            else:
                self.last = (self.has_information,)
        elif "SET archivada_en=now()" in query:
            self.archived_at = "2026-10-29T12:00:00+00:00"
        elif "SET archivada_en=NULL" in query:
            self.archived_at = None
        elif query.startswith("DELETE FROM contared.conciliaciones"):
            self.deleted = True
        elif query.startswith("INSERT INTO contared.eventos_archivo_conciliacion"):
            self.events.append(params)

    def fetchone(self):
        return self.last

    def fetchall(self):
        return []


class ReconciliationArchiveTests(SimpleTestCase):
    databases = {"default"}
    reconciliation_id = "00000000-0000-0000-0000-000000000001"
    scope = {
        "user_id": INTERNAL_USER_ID,
        "account_id": "00000000-0000-0000-0000-000000000002",
        "role": "administrador",
    }

    def request(self, method, data=None):
        factory = APIRequestFactory()
        request = getattr(factory, method)(
            f"/api/v1/conciliaciones/{self.reconciliation_id}/archivo/",
            data=data or {},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        return request

    def invoke(self, action, method, data=None, *, cursor=None, role="administrador"):
        cursor = cursor or ArchiveCursor()
        scope = {**self.scope, "role": role}
        with (
            patch("contared_api.conciliaciones._scope_for_reconciliation", return_value=scope),
            patch("contared_api.conciliaciones._archive_schema_tables", return_value={
                "conciliaciones": set(), "eventos_archivo_conciliacion": set(),
                "cargas": set(), "cheques": set(), "casos_deposito": set(),
                "pendientes": set(), "cruces": set(),
            }),
            patch("contared_api.conciliaciones.connection.cursor", return_value=cursor),
            patch("contared_api.conciliaciones.transaction.atomic", return_value=nullcontext()),
        ):
            response = ReconciliationArchiveView.as_view()(
                self.request(method, data),
                reconciliation_id=self.reconciliation_id,
            )
        return response, cursor

    def test_legacy_delete_endpoint_requires_the_new_preview_flow(self):
        response, cursor = self.invoke("delete", "delete")
        self.assertEqual(response.status_code, 400)
        self.assertIn("vista previa", response.data["detail"])
        self.assertFalse(cursor.deleted)

    def test_archives_and_restores_with_audit_events(self):
        cursor = ArchiveCursor(has_information=True)
        archived, cursor = self.invoke(
            "post", "post",
            {"accion": "archivar", "motivo": "Revisión completada", "confirmar": True},
            cursor=cursor,
        )
        self.assertEqual(archived.status_code, 200)
        self.assertIsNotNone(cursor.archived_at)
        self.assertEqual(cursor.events[0][1], "archivada")
        restored, cursor = self.invoke(
            "post", "post",
            {"accion": "restaurar", "confirmar": True},
            cursor=cursor,
        )
        self.assertEqual(restored.status_code, 200)
        self.assertIsNone(cursor.archived_at)
        self.assertEqual(cursor.events[1][1], "restaurada")

    def test_active_cheque_or_followup_blocks_archive(self):
        cursor = ArchiveCursor(has_information=True, blockers=("cheque_abierto",))
        response, _ = self.invoke(
            "post", "post",
            {"accion": "archivar", "motivo": "Archivo", "confirmar": True},
            cursor=cursor,
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("cheques retenidos o en circulación", response.data["detail"])
        self.assertIsNone(cursor.archived_at)

    def test_open_pending_and_cross_period_reference_block_archive(self):
        cases = (
            ("pendiente_abierto", "hay pendientes abiertos"),
            ("referencia_otro_periodo", "pendientes de otros períodos"),
        )
        for blocker, message in cases:
            with self.subTest(blocker=blocker):
                response, cursor = self.invoke(
                    "post", "post",
                    {"accion": "archivar", "motivo": "Archivo", "confirmar": True},
                    cursor=ArchiveCursor(
                        has_information=True,
                        blockers=(blocker,),
                    ),
                )
                self.assertEqual(response.status_code, 409)
                self.assertIn(message, response.data["detail"])
                self.assertIsNone(cursor.archived_at)

    def test_archived_reconciliation_rejects_backend_writes(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, query, params=None):
                self.query = query

            def fetchone(self):
                return ("2026-10-29T12:00:00+00:00",)

        scope = {
            "role": "administrador",
            "reconciliation_id": self.reconciliation_id,
            "account_id": self.scope["account_id"],
        }
        with (
            patch("contared_api.conciliaciones._archive_schema_tables", return_value={
                "conciliaciones": {"archivada_en"},
            }),
            patch("contared_api.conciliaciones.connection.cursor", return_value=Cursor()),
        ):
            with self.assertRaises(ReconciliationConflict):
                _writer(scope)

    def test_only_administrators_can_archive_or_delete(self):
        response, _ = self.invoke("delete", "delete", role="auxiliar")
        self.assertEqual(response.status_code, 400)
        response, _ = self.invoke(
            "post", "post",
            {"accion": "archivar", "motivo": "No permitido", "confirmar": True},
            role="revisor",
        )
        self.assertEqual(response.status_code, 403)

    def test_all_known_reconciliation_dependencies_are_checked(self):
        references = _archive_reference_specs({
            "cargas", "movimientos", "cruces", "cruce_movimientos", "pendientes",
            "ajustes", "reportes", "ejecuciones_comparacion",
            "propuestas_comparacion", "ejecucion_propuestas", "cheques",
            "cheque_eventos", "casos_deposito", "deposito_movimientos",
            "deposito_eventos", "eventos_archivo_conciliacion",
        })
        self.assertEqual(
            {name for name, _ in references},
            {
                "cargas", "movimientos", "cruces", "cruce_movimientos",
                "pendientes", "ajustes", "reportes", "ejecuciones_comparacion",
                "propuestas_comparacion", "ejecucion_propuestas", "cheques",
                "cheque_eventos", "casos_deposito", "deposito_movimientos",
                "deposito_eventos", "eventos_archivo_conciliacion",
            },
        )


class DepositTrackingContractTests(SimpleTestCase):
    def test_deposit_schema_requires_distinct_case_relation_and_event_tables(self):
        self.assertEqual(
            set(REQUIRED_DEPOSIT_TABLES),
            {"casos_deposito", "deposito_movimientos", "deposito_eventos"},
        )
        self.assertIn("origen_correcto", REQUIRED_DEPOSIT_TABLES["casos_deposito"])
        self.assertIn("origen_correcto", REQUIRED_DEPOSIT_TABLES["deposito_eventos"])
        self.assertIn("registrado_en", REQUIRED_DEPOSIT_TABLES["deposito_eventos"])

    def test_classifications_are_operational_and_uuid_groups_are_validated(self):
        self.assertEqual(
            CLASSIFICATIONS,
            {"por_investigar", "deposito_transito", "deposito_mal_operado"},
        )
        movement_ids = [uuid4(), uuid4()]
        self.assertEqual(deposit_uuid_list([str(item) for item in movement_ids]), movement_ids)
        with self.assertRaises(ValidationError):
            deposit_uuid_list([str(movement_ids[0]), str(movement_ids[0])])
        with self.assertRaises(ValidationError):
            deposit_uuid_list(["not-a-uuid"])

    def test_effective_date_requires_iso_calendar_date(self):
        self.assertEqual(deposit_iso_date("2026-10-29", "fecha_efectiva"), date(2026, 10, 29))
        with self.assertRaises(ValidationError):
            deposit_iso_date("29/10/2026", "fecha_efectiva")

    def test_later_resolution_remains_open_at_the_earlier_cutoff(self):
        self.assertEqual(
            _case_status_at_cutoff("resuelto", date(2026, 10, 15), date(2026, 9, 30)),
            "abierto",
        )
        self.assertEqual(
            _case_status_at_cutoff("resuelto", date(2026, 10, 15), date(2026, 10, 31)),
            "resuelto",
        )

    def test_ambiguous_deposit_candidates_are_alternatives_not_a_combined_difference(self):
        bank = [{
            "id": "bank-8200", "fecha": "2026-09-10", "referencia": None,
            "descripcion": "Depósito", "monto": "8200.06",
        }]
        books = [
            {
                "id": f"book-{index}", "fecha": "2026-09-10", "referencia": None,
                "descripcion": "Candidato", "monto": "8200.06",
            }
            for index in range(2)
        ]

        options, truncated = _ambiguous_comparison_options(bank, books)

        self.assertFalse(truncated)
        self.assertEqual(len(options), 2)
        self.assertTrue(all(len(item["banco"]) == len(item["libros"]) == 1 for item in options))
        self.assertEqual(
            {item["libros"][0]["id"] for item in options},
            {"book-0", "book-1"},
        )

    def test_two_by_two_830_candidates_do_not_become_a_1660_group(self):
        bank = [
            {"id": f"bank-{index}", "fecha": "2026-09-10", "referencia": None,
             "descripcion": "Depósito", "monto": "830"}
            for index in range(2)
        ]
        books = [
            {"id": f"book-{index}", "fecha": "2026-09-10", "referencia": None,
             "descripcion": "Candidato", "monto": "830"}
            for index in range(2)
        ]

        options, _ = _ambiguous_comparison_options(bank, books)

        self.assertEqual(len(options), 4)
        self.assertTrue(all(len(item["banco"]) == len(item["libros"]) == 1 for item in options))
        self.assertTrue(all(item["suma_banco"] == item["suma_libros"] == "830" for item in options))

    def test_exact_one_to_many_candidate_remains_a_group_option(self):
        bank = [{
            "id": "bank-300", "fecha": "2026-09-10", "referencia": "REF-1",
            "descripcion": "Depósito", "monto": "300",
        }]
        books = [
            {"id": "book-180", "fecha": "2026-09-10", "referencia": "REF-1",
             "descripcion": "Parte 1", "monto": "180"},
            {"id": "book-120", "fecha": "2026-09-10", "referencia": "REF-1",
             "descripcion": "Parte 2", "monto": "120"},
        ]

        options, _ = _ambiguous_comparison_options(bank, books)

        self.assertEqual(len(options), 1)
        self.assertEqual(len(options[0]["banco"]), 1)
        self.assertEqual(len(options[0]["libros"]), 2)
        self.assertEqual(options[0]["suma_banco"], options[0]["suma_libros"])


class ReconciliationSummaryReportTests(SimpleTestCase):
    def make_snapshot(self):
        return {
            "title": "Resumen del proceso de conciliación — Borrador",
            "generated_at": "2026-10-29T14:00:00-06:00",
            "timezone": "America/El_Salvador",
            "generated_by": "Usuario de prueba",
            "version": 1,
            "alcance": "Datos al corte.",
            "firmas": ["Preparado por", "Revisado por", "Fecha"],
            "context": {
                "empresa": "INTELPRUEBA", "pais": "El Salvador", "banco": "Banco de prueba",
                "cuenta": "Cuenta 001", "moneda": "USD", "periodo": "2026-10-01",
                "fecha_corte": "2026-10-29",
            },
            "saldos": {
                "libros": {
                    "inicial_declarado": None, "final_declarado": "0", "entradas": "100",
                    "salidas": "20", "saldo_calculado": None,
                    "final_usado": "0", "final_capturado_manualmente": "0",
                    "final_detectado_en_carga": None, "fuente_final_detectado": None,
                    "conflicto_final": None, "origen_final": "manual",
                    "diferencia_calculado_vs_declarado": None,
                    "ajustes_aprobados": {"importe": "0"},
                },
                "banco": {
                    "inicial_declarado": "0", "final_declarado": "80", "entradas": "100",
                    "salidas": "20", "saldo_calculado": "80",
                    "final_usado": "80", "final_capturado_manualmente": "80",
                    "final_detectado_en_carga": None, "fuente_final_detectado": None,
                    "conflicto_final": None, "origen_final": "manual",
                    "diferencia_calculado_vs_declarado": "0",
                    "ajustes_aprobados": {"importe": "0"},
                },
            },
            "diferencia_pendiente": None,
            "base_diferencia_pendiente": "saldos_finales",
            "pendientes": {
                "cheques": {"retenido": {"cantidad": 1, "importe": "100"}, "en_circulacion": {"cantidad": 0, "importe": "0"}},
                "depositos": [{"clasificacion": "deposito_transito", "lado": "libros", "cantidad": 1, "importe": "450"}],
                "comparaciones": [{"regla": "reference_amount_mismatch", "estado": "descartada", "diferencia": "20"}],
                "otros_movimientos": {"libros": {"cantidad": 0, "importe": "0"}, "banco": {"cantidad": 0, "importe": "0"}},
            },
        }

    def test_summary_marks_missing_initial_as_unavailable_and_preserves_zero(self):
        rows = _summary_table_rows(self.make_snapshot())
        self.assertEqual(rows[1][1], "No disponible")
        self.assertEqual(rows[1][2], "0")

    def test_summary_distinguishes_detected_balance_source_and_manual_conflict(self):
        snapshot = self.make_snapshot()
        source = {
            "archivo": "Estado bancario.xlsx",
            "hoja": "Movimientos",
            "fila": 193,
            "metodo": "Último saldo acumulado no vacío.",
        }
        snapshot["saldos"]["banco"].update({
            "final_declarado": "1460000",
            "final_usado": "1460000",
            "final_capturado_manualmente": "1460000",
            "final_detectado_en_carga": "1462269.13",
            "fuente_final_detectado": source,
            "conflicto_final": {
                "capturado_manualmente": "1460000",
                "detectado_en_carga": "1462269.13",
                "fuente_detectada": source,
            },
        })

        rows = _summary_table_rows(snapshot)
        detected = next(row for row in rows if row[0] == "Saldo final detectado en carga")
        used = next(row for row in rows if row[0] == "Saldo final utilizado")

        self.assertEqual(detected[2], "1462269.13")
        self.assertIn("Estado bancario.xlsx", detected[3])
        self.assertIn("fila 193", detected[3])
        self.assertEqual(used[2], "1460000")
        self.assertIn("difiere del detectado", used[3])

    def test_excel_report_contains_summary_pending_sheet_and_typed_dates(self):
        snapshot = self.make_snapshot()
        snapshot["diferencia_saldos_finales"] = "80"
        snapshot["diferencia_final_pendiente_explicacion"] = True
        workbook = load_workbook(io.BytesIO(_build_summary_xlsx(snapshot)), data_only=True)
        self.assertEqual(workbook.sheetnames, ["Resumen", "Pendientes al corte"])
        self.assertEqual(workbook["Resumen"]["B7"].value.date(), date(2026, 10, 1))
        bank_highlight = next(
            row for row in range(1, workbook["Resumen"].max_row + 1)
            if workbook["Resumen"].cell(row, 1).value == "Saldo final Banco"
        )
        difference_highlight = next(
            row for row in range(1, workbook["Resumen"].max_row + 1)
            if workbook["Resumen"].cell(row, 1).value == "Diferencia entre saldos finales (Banco − Contabilidad)"
            and workbook["Resumen"].cell(row, 2).value is not None
        )
        self.assertEqual(workbook["Resumen"].cell(bank_highlight, 2).value, 80)
        self.assertEqual(workbook["Resumen"].cell(difference_highlight, 2).value, 80)
        initial_row = next(
            row for row in range(1, workbook["Resumen"].max_row + 1)
            if workbook["Resumen"].cell(row, 1).value == "Saldo inicial declarado"
        )
        self.assertEqual(workbook["Resumen"].cell(initial_row, 2).value, "No disponible")
        self.assertEqual(workbook["Pendientes al corte"]["A2"].value, "Cheque")

    def test_pdf_report_has_pdf_signature_and_borrador_title(self):
        content = _build_summary_pdf(self.make_snapshot())
        self.assertTrue(content.startswith(b"%PDF-"))


def application_statements(cursor):
    return [
        statement
        for statement in cursor.statements
        if statement[0] not in {"BEGIN", "COMMIT", "ROLLBACK"}
        and not statement[0].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))
    ]


class FakeSessionCursor:
    def __init__(self, *, profile=None, accesses=(), fail=False):
        self.profile = profile
        self.accesses = accesses
        self.fail = fail
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if self.fail:
            raise DatabaseError("test database failure")

    def fetchone(self):
        return self.profile

    def fetchall(self):
        return list(self.accesses)


class FakeSigningKey:
    def __init__(self, key):
        self.key = key


@override_settings(
    SUPABASE_URL="https://project.supabase.co",
    SUPABASE_JWKS_URL="https://project.supabase.co/auth/v1/.well-known/jwks.json",
    SUPABASE_ISSUER="https://project.supabase.co/auth/v1",
    SUPABASE_PUBLISHABLE_KEY="test-publishable-key",
)
class SupabaseAuthenticationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def make_token(self, **overrides):
        now = datetime.now(timezone.utc)
        claims = {
            "sub": str(uuid4()),
            "iss": "https://project.supabase.co/auth/v1",
            "aud": "authenticated",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def make_legacy_token(self, **overrides):
        now = datetime.now(timezone.utc)
        claims = {
            "sub": str(uuid4()),
            "iss": "https://project.supabase.co/auth/v1",
            "aud": "authenticated",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        claims.update(overrides)
        return jwt.encode(claims, "test-only-signing-key", algorithm="HS256")

    @patch("contared_api.authentication.get_jwks_client")
    def test_verifies_signature_issuer_audience_and_expiration(self, get_client):
        get_client.return_value.get_signing_key_from_jwt.return_value = FakeSigningKey(
            self.public_key
        )
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {self.make_token()}",
        )

        user, token = SupabaseJWTAuthentication().authenticate(request)

        claims = jwt.decode(
            token,
            self.public_key,
            algorithms=["RS256"],
            options={
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
        self.assertEqual(str(user.auth_user_id), claims["sub"])
        self.assertTrue(user.is_authenticated)

    @patch("contared_api.authentication.get_jwks_client")
    def test_rejects_wrong_audience(self, get_client):
        get_client.return_value.get_signing_key_from_jwt.return_value = FakeSigningKey(
            self.public_key
        )
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {self.make_token(aud='anon')}",
        )

        with self.assertRaises(AuthenticationFailed):
            SupabaseJWTAuthentication().authenticate(request)

    @patch("contared_api.authentication.get_jwks_client")
    def test_rejects_wrong_issuer(self, get_client):
        get_client.return_value.get_signing_key_from_jwt.return_value = FakeSigningKey(
            self.public_key
        )
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {self.make_token(iss='https://wrong.example')}",
        )

        with self.assertRaises(AuthenticationFailed):
            SupabaseJWTAuthentication().authenticate(request)

    @patch("contared_api.authentication.get_jwks_client")
    def test_rejects_expired_token(self, get_client):
        get_client.return_value.get_signing_key_from_jwt.return_value = FakeSigningKey(
            self.public_key
        )
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {self.make_token(exp=datetime.now(timezone.utc) - timedelta(seconds=1))}",
        )

        with self.assertRaises(AuthenticationFailed):
            SupabaseJWTAuthentication().authenticate(request)

    @patch("contared_api.authentication.get_jwks_client")
    def test_rejects_invalid_signature(self, get_client):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        get_client.return_value.get_signing_key_from_jwt.return_value = FakeSigningKey(
            self.public_key
        )
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(uuid4()),
                "iss": "https://project.supabase.co/auth/v1",
                "aud": "authenticated",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            other_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        with self.assertRaises(AuthenticationFailed):
            SupabaseJWTAuthentication().authenticate(request)

    @patch(
        "contared_api.authentication.urlopen",
        side_effect=lambda request, timeout: io.BytesIO(
            json.dumps(
                {
                    "id": jwt.decode(
                        request.headers["Authorization"].removeprefix("Bearer "),
                        options={
                            "verify_signature": False,
                            "verify_exp": False,
                            "verify_aud": False,
                            "verify_iss": False,
                        },
                    )["sub"]
                }
            ).encode()
        ),
    )
    def test_verifies_legacy_hs256_token_with_auth_server(self, open_url):
        token = self.make_legacy_token()
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        user, verified_token = SupabaseJWTAuthentication().authenticate(request)

        self.assertEqual(verified_token, token)
        self.assertEqual(str(user.auth_user_id), jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )["sub"])
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)

    @patch("contared_api.authentication.urlopen")
    def test_rejects_legacy_token_with_wrong_audience_before_auth_request(self, open_url):
        request = APIRequestFactory().get(
            "/api/v1/session/",
            HTTP_AUTHORIZATION=f"Bearer {self.make_legacy_token(aud='anon')}",
        )

        with self.assertRaises(AuthenticationFailed):
            SupabaseJWTAuthentication().authenticate(request)

        open_url.assert_not_called()

    def test_requires_bearer_token_for_protected_session(self):
        response = SessionView.as_view()(APIRequestFactory().get("/api/v1/session/"))

        self.assertEqual(response.status_code, 401)


class SessionAuthorizationTests(SimpleTestCase):
    databases = {"default"}

    def make_session_request(self):
        request = APIRequestFactory().get("/api/v1/session/")
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        return request

    def test_status_checks_database_connection(self):
        cursor = FakeSessionCursor()
        with patch("contared_api.session_access.connection.cursor", return_value=cursor):
            response = ApiStatusView.as_view()(
                APIRequestFactory().get("/api/v1/status/")
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"status": "ok"})
        self.assertEqual(
            application_statements(cursor),
            [
                ("SET TRANSACTION READ ONLY", None),
                ("SELECT 1", None),
            ],
        )

    def test_status_reports_database_failure(self):
        with patch(
            "contared_api.session_access.connection.cursor",
            return_value=FakeSessionCursor(fail=True),
        ):
            response = ApiStatusView.as_view()(
                APIRequestFactory().get("/api/v1/status/")
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "database_unavailable")

    def test_session_returns_only_active_authorized_company_country_roles(self):
        cursor = FakeSessionCursor(
            profile=(INTERNAL_USER_ID, "Administradora", True, False),
            accesses=[
                ("company-sv", "Empresa SV", "SV", "El Salvador", "administrador"),
                ("company-gt", "Empresa GT", "GT", "Guatemala", "administrador"),
            ],
        )
        with patch("contared_api.session_access.connection.cursor", return_value=cursor):
            response = SessionView.as_view()(self.make_session_request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            {
                "usuario": {"id": INTERNAL_USER_ID, "nombre": "Administradora"},
                "cambio_clave_obligatorio": False,
                "accesos": [
                    {
                        "empresa_id": "company-sv",
                        "empresa": "Empresa SV",
                        "pais_codigo": "SV",
                        "pais": "El Salvador",
                        "rol": "administrador",
                    },
                    {
                        "empresa_id": "company-gt",
                        "empresa": "Empresa GT",
                        "pais_codigo": "GT",
                        "pais": "Guatemala",
                        "rol": "administrador",
                    },
                ],
                "paises": [
                    {"codigo": "SV", "nombre": "El Salvador"},
                    {"codigo": "GT", "nombre": "Guatemala"},
                ],
            },
        )
        statements = application_statements(cursor)
        self.assertEqual(statements[0], ("SET TRANSACTION READ ONLY", None))
        self.assertEqual(statements[1][1], [AUTH_USER_ID])
        self.assertEqual(statements[2][1], [INTERNAL_USER_ID])
        self.assertIn("e.activa IS TRUE", statements[2][0])
        self.assertIn("p.codigo IN ('SV', 'GT')", statements[2][0])
        self.assertIn("a.usuario_id = %s", statements[2][0])

    def test_session_ignores_client_identifiers_and_returns_only_bound_user_access(
        self,
    ):
        cursor = FakeSessionCursor(
            profile=(INTERNAL_USER_ID, "Auxiliar", True, False),
            accesses=[
                ("company-sv", "Empresa SV", "SV", "El Salvador", "auxiliar"),
            ],
        )
        request = APIRequestFactory().get(
            "/api/v1/session/?usuario_id=otro&empresa_id=otra&pais_codigo=GT"
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        with patch("contared_api.session_access.connection.cursor", return_value=cursor):
            response = SessionView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["paises"],
            [{"codigo": "SV", "nombre": "El Salvador"}],
        )
        self.assertEqual(response.data["accesos"][0]["empresa_id"], "company-sv")
        statements = application_statements(cursor)
        self.assertEqual(statements[1][1], [AUTH_USER_ID])
        self.assertNotIn("request.query_params", statements[1][0])

    def assert_session_forbidden(self, cursor, expected_code):
        with (
            patch(
                "contared_api.session_access.connection.cursor",
                return_value=cursor,
            ),
        ):
            response = SessionView.as_view()(self.make_session_request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], expected_code)
        cursor.statements = application_statements(cursor)
        return response

    def test_session_rejects_missing_profile(self):
        cursor = FakeSessionCursor(profile=None)

        self.assert_session_forbidden(cursor, "profile_not_found")
        self.assertEqual(len(cursor.statements), 2)

    def test_session_rejects_inactive_profile_without_querying_accesses(self):
        cursor = FakeSessionCursor(
            profile=(INTERNAL_USER_ID, "Administradora", False, False),
        )

        self.assert_session_forbidden(cursor, "profile_inactive")
        self.assertEqual(len(cursor.statements), 2)

    def test_session_rejects_profile_without_active_authorizations(self):
        cursor = FakeSessionCursor(
            profile=(INTERNAL_USER_ID, "Administradora", True, False),
            accesses=[],
        )

        self.assert_session_forbidden(cursor, "no_access")
        self.assertEqual(len(cursor.statements), 3)

    def test_session_surfaces_database_failure_without_database_details(self):
        with patch(
            "contared_api.session_access.connection.cursor",
            return_value=FakeSessionCursor(fail=True),
        ):
            response = SessionView.as_view()(self.make_session_request())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "database_unavailable")
        self.assertNotIn("test database failure", response.data["detail"])


class ReconciliationRulesTests(SimpleTestCase):
    def movement(self, movement_id, side, amount, reference="R-01", day=27):
        from datetime import date

        return Movement(
            id=movement_id,
            account_id="account-1",
            side=side,
            transaction_date=date(2026, 10, day),
            amount=amount,
            reference=reference,
        )

    def test_matches_exact_reference_and_decimal_amount(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("12.34"))],
            [self.movement("book-1", "books", Decimal("12.34"))],
        )

        self.assertEqual([proposal.rule for proposal in proposals], ["reference_amount"])

    def test_matches_unique_amount_and_date_without_reference(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("-20.00"), reference=None)],
            [self.movement("book-1", "books", Decimal("-20.00"), reference=None)],
        )

        self.assertEqual([proposal.rule for proposal in proposals], ["amount_date"])

    def test_marks_duplicate_amount_and_date_as_ambiguous(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("15.00"), reference=None)],
            [
                self.movement("book-1", "books", Decimal("15.00"), reference=None),
                self.movement("book-2", "books", Decimal("15.00"), reference=None),
            ],
        )

        self.assertIn("ambiguous", [proposal.rule for proposal in proposals])
        self.assertNotIn("amount_date", [proposal.rule for proposal in proposals])

    def test_proposes_bounded_group_for_exact_reference_and_date(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("10.00"))],
            [
                self.movement("book-1", "books", Decimal("4.25")),
                self.movement("book-2", "books", Decimal("5.75")),
            ],
        )

        group = next(proposal for proposal in proposals if proposal.rule == "reference_date_group")
        self.assertEqual(group.bank_ids, ("bank-1",))
        self.assertEqual(set(group.book_ids), {"book-1", "book-2"})

    def test_flags_matching_reference_with_different_amounts(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("10.00"))],
            [self.movement("book-1", "books", Decimal("12.00"))],
        )

        self.assertEqual(
            [proposal.rule for proposal in proposals],
            ["reference_amount_mismatch"],
        )

    def test_reports_movements_without_counterparts(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("-10.00"), reference=None)],
            [],
        )

        self.assertEqual(
            [(proposal.rule, proposal.bank_ids, proposal.book_ids) for proposal in proposals],
            [("unmatched", ("bank-1",), ())],
        )

    def test_does_not_group_more_than_three_movements(self):
        proposals = propose_reconciliation(
            [self.movement("bank-1", "bank", Decimal("10.00"))],
            [
                self.movement("book-1", "books", Decimal("1.00")),
                self.movement("book-2", "books", Decimal("2.00")),
                self.movement("book-3", "books", Decimal("3.00")),
                self.movement("book-4", "books", Decimal("4.00")),
            ],
        )

        self.assertNotIn(
            "reference_date_group",
            [proposal.rule for proposal in proposals],
        )

    def test_never_coerces_float_money(self):
        with self.assertRaises(ValueError):
            propose_reconciliation(
                [self.movement("bank-1", "bank", 10.0)],
                [],
            )

    def test_rejects_movements_from_different_accounts(self):
        bank = self.movement("bank-1", "bank", Decimal("10.00"))
        book = self.movement("book-1", "books", Decimal("10.00"))
        book = Movement(**{**book.__dict__, "account_id": "account-2"})

        with self.assertRaises(ValueError):
            propose_reconciliation([bank], [book])

    def test_adapter_translates_both_sides_for_rule_engine_matches_and_groups(self):
        source_movements = [
            {
                "id": "bank-match",
                "lado": "banco",
                "fecha": "2026-09-01",
                "monto": "10.0000",
                "referencia": "MATCH",
            },
            {
                "id": "bank-group",
                "lado": "banco",
                "fecha": "2026-09-02",
                "monto": "30.0000",
                "referencia": "GROUP",
            },
            {
                "id": "book-match",
                "lado": "libros",
                "fecha": "2026-09-01",
                "monto": "10.0000",
                "referencia": "MATCH",
            },
            {
                "id": "book-group-a",
                "lado": "libros",
                "fecha": "2026-09-02",
                "monto": "12.0000",
                "referencia": "GROUP",
            },
            {
                "id": "book-group-b",
                "lado": "libros",
                "fecha": "2026-09-02",
                "monto": "18.0000",
                "referencia": "GROUP",
            },
        ]

        bank = _engine_movements(source_movements, "banco", "account-1")
        books = _engine_movements(source_movements, "libros", "account-1")
        proposals = propose_reconciliation(bank, books)

        self.assertTrue(all(movement.side == "bank" for movement in bank))
        self.assertTrue(all(movement.side == "books" for movement in books))
        self.assertIn("reference_amount", [proposal.rule for proposal in proposals])
        group = next(
            proposal for proposal in proposals
            if proposal.rule == "reference_date_group"
        )
        self.assertEqual(group.bank_ids, ("bank-group",))
        self.assertEqual(set(group.book_ids), {"book-group-a", "book-group-b"})

    def test_intelprueba_control_totals_and_proposals(self):
        test_date = date(2026, 9, 1)

        def item(movement_id, side, amount, reference, movement_date=test_date):
            return Movement(
                id=movement_id,
                account_id="intelprueba-account",
                side=side,
                transaction_date=movement_date,
                amount=Decimal(amount),
                reference=reference,
            )

        bank = [
            item(f"bank-direct-{index}", "bank", f"{index * 10}.00", f"INT-{index:03}")
            for index in range(1, 8)
        ]
        bank.append(item("bank-cheque-000120", "bank", "100.00", "000120"))
        bank.extend([
            item("bank-group-000003", "bank", "300.00", "000003", date(2026, 9, 3)),
            item("bank-deposit-009004", "bank", "220.00", "009004", date(2026, 9, 7)),
            item("bank-mismatch-000005", "bank", "450.00", "000005", date(2026, 9, 5)),
            item("bank-fee-000006", "bank", "25.00", "000006", date(2026, 9, 6)),
        ])
        books = [
            item(f"book-direct-{index}", "books", f"{index * 10}.00", f"INT-{index:03}")
            for index in range(1, 8)
        ]
        books.extend([
            item("book-cheque-000120", "books", "100.00", "000120"),
            item("book-group-000003-a", "books", "180.00", "000003", date(2026, 9, 3)),
            item("book-group-000003-b", "books", "120.00", "000003", date(2026, 9, 3)),
            item("book-deposit-008004", "books", "220.00", "008004", date(2026, 9, 7)),
            item("book-mismatch-000005", "books", "430.00", "000005", date(2026, 9, 5)),
            item("book-cheque-000121", "books", "100.00", "000121", date(2026, 9, 9)),
        ])

        source_movements = [
            {
                "id": movement.id,
                "lado": "banco" if movement.side == "bank" else "libros",
                "fecha": movement.transaction_date.isoformat(),
                "monto": str(movement.amount),
                "referencia": movement.reference,
            }
            for movement in (*bank, *books)
        ]
        bank = _engine_movements(
            source_movements, "banco", "intelprueba-account"
        )
        books = _engine_movements(
            source_movements, "libros", "intelprueba-account"
        )
        proposals = propose_reconciliation(bank, books)
        direct = [proposal for proposal in proposals if proposal.rule == "reference_amount"]
        grouped = [proposal for proposal in proposals if proposal.rule == "reference_date_group"]
        date_matches = [proposal for proposal in proposals if proposal.rule == "amount_date"]
        mismatch = [proposal for proposal in proposals if proposal.rule == "reference_amount_mismatch"]
        unmatched = [proposal for proposal in proposals if proposal.rule == "unmatched"]

        self.assertEqual((len(bank), len(books)), (12, 13))
        self.assertEqual(len(direct), 8)
        self.assertEqual(len(grouped), 1)
        self.assertEqual((grouped[0].bank_ids, set(grouped[0].book_ids)), (
            ("bank-group-000003",),
            {"book-group-000003-a", "book-group-000003-b"},
        ))
        self.assertEqual(len(date_matches), 1)
        self.assertEqual((date_matches[0].bank_ids, date_matches[0].book_ids), (
            ("bank-deposit-009004",),
            ("book-deposit-008004",),
        ))
        self.assertEqual(len(mismatch), 1)
        bank_by_id = {movement.id: movement for movement in bank}
        books_by_id = {movement.id: movement for movement in books}
        self.assertEqual(
            bank_by_id[mismatch[0].bank_ids[0]].amount
            - books_by_id[mismatch[0].book_ids[0]].amount,
            Decimal("20.00"),
        )
        self.assertEqual(len(unmatched), 2)
        self.assertTrue(any("bank-fee-000006" in proposal.bank_ids for proposal in unmatched))
        self.assertTrue(any("book-cheque-000121" in proposal.book_ids for proposal in unmatched))
        cheque_match = [
            proposal for proposal in direct
            if "bank-cheque-000120" in proposal.bank_ids
        ]
        self.assertEqual(len(cheque_match), 1)
        participants = [
            movement_id
            for proposal in proposals
            for movement_id in (*proposal.bank_ids, *proposal.book_ids)
        ]
        self.assertEqual(len(participants), len(set(participants)))

    def test_repeated_references_create_one_ambiguous_component(self):
        day = date(2026, 9, 7)
        bank = [
            Movement("bank-1", "account-1", "bank", day, Decimal("10"), "DUP"),
            Movement("bank-2", "account-1", "bank", day, Decimal("10"), "DUP"),
        ]
        books = [
            Movement("book-1", "account-1", "books", day, Decimal("10"), "DUP"),
            Movement("book-2", "account-1", "books", day, Decimal("10"), "DUP"),
        ]

        proposals = propose_reconciliation(bank, books)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].rule, "ambiguous")
        self.assertEqual(set(proposals[0].bank_ids), {"bank-1", "bank-2"})
        self.assertEqual(set(proposals[0].book_ids), {"book-1", "book-2"})

    def test_unknown_transaction_codes_are_not_interpreted(self):
        self.assertEqual(
            _transaction_type_label("ZZ", {"catalogo_tipo_transaccion": {"CQ": "Cheque cobrado"}}),
            "Tipo de transacción sin identificar",
        )
        self.assertEqual(
            _transaction_type_label("CQ", {"catalogo_tipo_transaccion": {"CQ": "Cheque cobrado"}}),
            "Cheque cobrado",
        )
        self.assertEqual(
            _validate_transaction_catalog('{"CQ":"Cheque cobrado","DE":"Depósito"}'),
            {"CQ": "Cheque cobrado", "DE": "Depósito"},
        )
        self.assertEqual(
            _validate_transaction_catalog(None),
            {},
        )

    def test_banco_industrial_catalog_is_scoped_and_keeps_cq_reference(self):
        movement = {
            "codigo_tt": "CQ",
            "numero_cheque": None,
            "referencia": "0013715",
        }
        _apply_banco_industrial_fields(
            [movement], origin="banco", bank_name="Banco Industrial de Guatemala"
        )

        self.assertEqual(movement["numero_cheque"], "0013715")
        self.assertEqual(movement["referencia"], "0013715")
        self.assertEqual(_transaction_type_label("CQ", {}, "Banco Industrial"), "Pago de Cheque")
        self.assertEqual(_transaction_type_label("NC", {}, "Banco Industrial"), "Nota de crédito")
        self.assertEqual(_transaction_type_label("ND", {}, "Banco Industrial"), "Nota de débito")
        self.assertEqual(_transaction_type_label("DE", {}, "Banco Industrial"), "Depósito")
        self.assertEqual(
            _transaction_type_label("CQ", {}, "Banco de prueba"),
            "Tipo de transacción sin identificar",
        )
        zero_cheque = {"codigo_tt": "CQ", "numero_cheque": None, "referencia": "000"}
        _apply_banco_industrial_fields(
            [zero_cheque], origin="banco", bank_name="Banco Industrial"
        )
        self.assertIsNone(zero_cheque["numero_cheque"])
        self.assertFalse(_valid_identifier("0"))
        self.assertFalse(_valid_identifier("000"))
        self.assertFalse(_valid_identifier(" "))
        self.assertTrue(_valid_identifier("0013715"))

    def test_comparison_movement_uses_banco_industrial_cheque_reference(self):
        movement = _movement_from_row(
            (
                "movement-id", "banco", date(2026, 9, 1), None, "0013715", None,
                None, "CQ", "Pago cheque", "-100", None, "Hoja1", 7, {},
                "load-id", "banco", "excel", "Estado.xlsx", "validada",
                datetime(2026, 9, 2), "Usuario", {}, False,
            ),
            "Banco Industrial",
        )

        self.assertEqual(movement["numero_cheque"], "0013715")
        self.assertEqual(movement["referencia"], "0013715")
        self.assertEqual(movement["tipo_transaccion"], "Pago de Cheque")

    def test_ambiguous_proposal_response_does_not_publish_component_totals(self):
        bank_id, book_a_id, book_b_id = (str(uuid4()) for _ in range(3))
        movements = {
            bank_id: {
                "id": bank_id, "fecha": "2026-09-10", "referencia": None,
                "descripcion": "Banco", "monto": "8200.06",
            },
            book_a_id: {
                "id": book_a_id, "fecha": "2026-09-10", "referencia": None,
                "descripcion": "Candidato A", "monto": "8200.06",
            },
            book_b_id: {
                "id": book_b_id, "fecha": "2026-09-10", "referencia": None,
                "descripcion": "Candidato B", "monto": "8200.06",
            },
        }
        proposal = _proposal_response(
            (
                "proposal-id", "ambiguous", [bank_id], [book_a_id, book_b_id],
                "propuesta", "Varios candidatos", None, None, None, None,
            ),
            movements,
        )

        self.assertIsNone(proposal["suma_banco"])
        self.assertIsNone(proposal["suma_libros"])
        self.assertIsNone(proposal["diferencia"])
        self.assertEqual(len(proposal["alternativas"]), 2)

    def test_invalid_or_ambiguous_transaction_catalog_is_rejected(self):
        for value in ('["CQ"]', '{"CQ":""}', '{"":"Sin código"}', "{"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                _validate_transaction_catalog(value)


class ChequeQueryTests(TestCase):
    class Cursor:
        def __init__(self, *, rows=(), one=None, counts=()):
            self.rows = list(rows)
            self.one = one
            self.counts = list(counts)
            self.query = ""
            self.params = []
            self.statements = []

        def execute(self, query, params=None):
            self.query = " ".join(query.split())
            self.params = params or []
            self.statements.append((self.query,self.params))

        def fetchall(self):
            if "SELECT state, count(*)" in self.query:
                return self.counts
            return self.rows

        def fetchone(self):
            return self.one

    def setUp(self):
        self.scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "reconciliation_id": "reconciliation-1",
            "period": date(2026, 10, 1),
            "cutoff": date(2026, 10, 31),
        }

    def test_candidate_query_preserves_check_number_and_scopes_historical_account(self):
        cursor = self.Cursor(rows=[(
            "movement-121", "candidato", "candidato", "movement-121",
            "000121", date(2026, 9, 9), "Cheque emitido", None, "100.0000",
            "USD", date(2026, 9, 1), None, None, None, None, None, None,
            None, None, False, "REF-121", "TX-121", 1,
        )])

        results, total = _fetch_cheques(
            cursor,self.scope,"candidatos","000121","anteriores",1,50
        )

        self.assertEqual(total, 1)
        self.assertEqual(results[0]["numero_cheque"], "000121")
        self.assertEqual(results[0]["periodo_origen"], "2026-09-01")
        self.assertIn("m.cuenta_id=%s", cursor.query)
        self.assertIn("m.monto<0", cursor.query)
        self.assertIn("m.fecha<=%s", cursor.query)
        self.assertIn(
            "COALESCE(NULLIF(ch.descripcion,''),m.descripcion) AS descripcion",
            cursor.query,
        )
        self.assertIn("ORDER BY i.fecha_emision DESC,i.numero_cheque,i.id", cursor.query)
        self.assertEqual(cursor.query.count("%s"), len(cursor.params))
        self.assertIn("account-1", cursor.params)
        self.assertIn(date(2026, 10, 31), cursor.params)
        self.assertIn(date(2026, 10, 1), cursor.params)
        self.assertIn("e.fecha_efectiva<=%s", cursor.query)
        event_projection = re.search(
            r"LEFT JOIN LATERAL \( SELECT (.*?) FROM contared\.cheque_eventos e",
            cursor.query,
        )
        self.assertIsNotNone(event_projection)
        selected_fields = set(re.findall(r"\be\.(\w+)", event_projection.group(1)))
        outer_select = cursor.query.split("FROM contared.cheques ch", 1)[0]
        referenced_fields = set(re.findall(r"\bev\.(\w+)", outer_select))
        self.assertIn("motivo", selected_fields)
        self.assertTrue(referenced_fields.issubset(selected_fields))

    def test_cutoff_counts_separate_historical_states_from_unclassified_candidates(self):
        cursor = self.Cursor(counts=[
            ("retenido", 2),
            ("en_circulacion", 1),
            ("cobrado", 3),
            ("anulado", 1),
        ], one=(4,))

        counts = _cheque_counts(cursor, self.scope)

        self.assertEqual(counts, {
            "candidatos": 4,
            "retenido": 2,
            "en_circulacion": 1,
            "cobrado": 3,
            "anulado": 1,
        })
        self.assertTrue(all(
            query.count("%s") == len(params)
            for query, params in cursor.statements
        ))


class ChequeDatabaseErrorTests(SimpleTestCase):
    @staticmethod
    def database_error(sqlstate):
        class DriverError(Exception):
            pass

        error = DatabaseError("internal detail that must not reach logs")
        cause = DriverError()
        cause.sqlstate = sqlstate
        error.__cause__ = cause
        return error

    def test_missing_required_column_is_classified_as_migration_required(self):
        rows = [
            (table, column)
            for table, columns in REQUIRED_CHEQUE_SCHEMA.items()
            for column in columns
            if not (table == "cheque_eventos" and column == "motivo")
        ]
        self.assertTrue(_cheque_schema_columns_missing(rows))
        with patch("contared_api.cheques._required_cheque_schema_missing", return_value=True):
            with self.assertRaises(ChequesMigrationRequired):
                _database_error(self.database_error("42703"))

    def test_query_column_error_with_complete_schema_is_not_migration_required(self):
        rows = [
            (table, column)
            for table, columns in REQUIRED_CHEQUE_SCHEMA.items()
            for column in columns
        ]
        self.assertFalse(_cheque_schema_columns_missing(rows))
        with self.assertLogs("contared_api.cheques", level="ERROR") as captured:
            with patch("contared_api.cheques._required_cheque_schema_missing", return_value=False):
                with self.assertRaises(DatabaseUnavailable):
                    _database_error(self.database_error("42703"))
        self.assertIn("SQLSTATE: 42703", captured.output[0])
        self.assertNotIn("internal detail", captured.output[0])


class ManualChequeTests(TestCase):
    def setUp(self):
        self.reconciliation_id = uuid4()
        self.scope = {
            "account_id":"account-1","company_id":"company-1","country_code":"SV",
            "currency_code":"USD","reconciliation_id":str(self.reconciliation_id),
            "period":date(2026,9,1),"cutoff":date(2026,9,30),
            "state":"borrador","user_id":INTERNAL_USER_ID,
            "user_name":"Auxiliar","role":"auxiliar",
        }

    def _post(self, payload=None, *, related=(), role="auxiliar"):
        database = {"statements":[]}

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(cursor, query, params=None):
                normalized = " ".join(query.split())
                database["statements"].append((normalized,params))
                cursor.query = normalized

            def fetchone(cursor):
                return ("account-1",) if "SELECT id FROM contared.cuentas_bancarias" in cursor.query else None

            def fetchall(cursor):
                return list(related) if "JOIN contared.cheques ch ON ch.movimiento_emision_id=m.id" in cursor.query else []

        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/cheques/manuales/",
            payload or {
                "numero_cheque":"000121","fecha_emision":"2026-09-09",
                "beneficiario":"Proveedor","descripcion":"Pago de servicios",
                "monto":"100.0000","referencia":"REF-000121","estado":"retenido",
            },
            format="json",
        )
        force_authenticate(request,user=SupabaseUser(AUTH_USER_ID))
        scope = {**self.scope,"role":role}
        with (
            patch("contared_api.cheques._scope_for_reconciliation",return_value=scope),
            patch("contared_api.cheques._locked_scope",return_value=scope),
            patch("contared_api.cheques.connection.cursor",return_value=Cursor()),
            patch("contared_api.cheques.transaction.atomic",nullcontext),
        ):
            response = ManualChequeCreateView.as_view()(
                request,reconciliation_id=self.reconciliation_id
            )
        return response,database

    def test_manual_issuance_preserves_legacy_load_and_movement(self):
        response,database = self._post()
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.data["estado"],"retenido")
        statements = database["statements"]
        load = next((params for query,params in statements if "INSERT INTO contared.cargas" in query),None)
        movement = next((params for query,params in statements if "INSERT INTO contared.movimientos" in query),None)
        cheque = next((params for query,params in statements if "INSERT INTO contared.cheques" in query),None)
        event = next((params for query,params in statements if "INSERT INTO contared.cheque_eventos" in query),None)
        self.assertIsNotNone(load)
        self.assertIsNotNone(movement)
        self.assertIn("'libros','manual'",next(query for query,_ in statements if "INSERT INTO contared.cargas" in query))
        self.assertEqual(load[5],"Cheque manual 000121")
        self.assertEqual(tuple(movement[5:8]),("000121","Pago de servicios",Decimal("-100.0000")))
        self.assertEqual(movement[4],"REF-000121")
        self.assertEqual(cheque[5],"000121")
        self.assertEqual(cheque[11],Decimal("100.0000"))
        cheque_query = next(query for query,_ in statements if "INSERT INTO contared.cheques" in query)
        self.assertEqual(cheque_query.count("%s"),len(cheque))
        self.assertEqual(event[1],"retenido")
        self.assertTrue(any("contared.actor_id" in query for query,_ in statements))
        self.assertFalse(any("lado='banco'" in query for query,_ in statements))

    def test_independent_cheque_creates_no_load_or_movement(self):
        response,database = self._post({
            "numero_cheque":"000121","fecha_emision":"2026-09-09",
            "beneficiario":"Proveedor","descripcion":"Pago de servicios",
            "monto":"100.0000","referencia":"REF-000121","estado":"retenido",
            "registro_independiente":True,
        })
        self.assertEqual(response.status_code,201)
        self.assertIsNone(response.data["movimiento_emision_id"])
        self.assertIsNone(response.data["carga_id"])
        statements = database["statements"]
        self.assertFalse(any("INSERT INTO contared.cargas" in query for query,_ in statements))
        self.assertFalse(any("INSERT INTO contared.movimientos" in query for query,_ in statements))
        insert = next((params for query,params in statements if "INSERT INTO contared.cheques" in query),None)
        self.assertIsNotNone(insert)
        self.assertIsNone(insert[3])
        self.assertEqual(insert[8],"Pago de servicios")
        self.assertEqual(insert[9],"REF-000121")
        insert_query = next(query for query,_ in statements if "INSERT INTO contared.cheques" in query)
        self.assertEqual(insert_query.count("%s"),len(insert))

    def test_manual_issuance_rejects_cash_status_invalid_date_amount_and_read_only_role(self):
        for payload in (
            {"estado":"cobrado"},
            {"fecha_emision":"2026-10-01"},
            {"monto":"0"},
            {"monto":"1.00001"},
        ):
            response,_ = self._post({**{
                "numero_cheque":"000121","fecha_emision":"2026-09-09",
                "beneficiario":"","descripcion":"Concepto","monto":"100",
                "estado":"retenido",
            },**payload})
            self.assertEqual(response.status_code,400)
        forbidden,_ = self._post(role="consulta")
        self.assertEqual(forbidden.status_code,403)

    def test_manual_issuance_requires_delivery_confirmation_and_flags_duplicates(self):
        circulating = {
            "numero_cheque":"000121","fecha_emision":"2026-09-09",
            "descripcion":"Concepto","monto":"100","estado":"en_circulacion",
            "fecha_entrega":"2026-09-12",
        }
        response,database = self._post(circulating)
        self.assertEqual(response.status_code,400)
        self.assertFalse(any("INSERT INTO contared.cargas" in q for q,_ in database["statements"]))

        repeated = ("existing-movement","000121",date(2026,9,9),"Pago existente",
                    "100.0000","manual","existing-cheque","retenido")
        duplicate,database = self._post(related=[repeated])
        self.assertEqual(duplicate.status_code,400)
        self.assertIn("duplicado",duplicate.data)
        self.assertFalse(any("INSERT INTO contared.cargas" in q for q,_ in database["statements"]))

    def test_issuance_candidates_show_registered_cheque_and_preserve_number(self):
        candidate = (
            "movement-1","000121",date(2026,9,9),"Cheque", "100.0000","USD",
            date(2026,9,1),"Proveedor","manual","cheque-1","retenido",False,1,
        )

        class Cursor:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,query,params=None): self.query=" ".join(query.split())
            def fetchall(self): return [candidate]

        request = APIRequestFactory().get("/api/v1/conciliaciones/cheques/movimientos-emision/")
        force_authenticate(request,user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation",return_value=self.scope),
            patch("contared_api.cheques._locked_scope",return_value=self.scope),
            patch("contared_api.cheques.connection.cursor",return_value=Cursor()),
        ):
            response = ChequeIssuanceCandidatesView.as_view()(
                request,reconciliation_id=self.reconciliation_id
            )
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data["results"][0]["numero_cheque"],"000121")
        self.assertEqual(response.data["results"][0]["cheque_id"],"cheque-1")


class ManualImportDuplicateTests(SimpleTestCase):
    def test_import_confirmation_detects_same_manual_emission(self):
        class Cursor:
            def execute(self,query,params):
                self.query=" ".join(query.split())
                self.params=params

            def fetchall(self):
                return [(
                    "manual-movement","000121",date(2026,9,9),
                    "Cheque a proveedor","100.0000","manual",
                )]

        cursor=Cursor()
        duplicates=_manual_import_duplicates(cursor,"account-1",[{
            "numero_cheque":"000121","fecha":date(2026,9,9),
            "monto":Decimal("-105.0000"),"fila_origen":12,
        }])
        self.assertIn("c.metodo='manual'",cursor.query)
        self.assertEqual(cursor.params,["account-1","000121",date(2026,9,9)])
        self.assertEqual(duplicates[0]["fila_importacion"],12)
        self.assertEqual(duplicates[0]["movimiento_existente_id"],"manual-movement")
        self.assertEqual(duplicates[0]["metodo"],"manual")


class ChequeCrossSyncTests(SimpleTestCase):
    def test_confirmed_cross_records_retained_cheque_inconsistency(self):
        class Cursor:
            def __init__(self):
                self.query = ""
                self.statements = []
                self.responses = iter([
                    ("contared.cheques",),
                    ("000121",date(2026,9,9),"-100.0000",date(2026,10,10),
                     "-100.0000","000121","origin-id","USD","Beneficiario"),
                    (1,),
                    (1,),
                    ("cheque-id","retenido"),
                ])

            def execute(self, query, params=None):
                self.query = " ".join(query.split())
                self.statements.append((self.query,params))

            def fetchone(self):
                return next(self.responses)

        cursor = Cursor()
        retained = ChequeCrossSync.reflect(
            cursor,
            {"account_id":"account-1","reconciliation_id":"current-id","user_id":"user-id"},
            ["bank-movement"],
            ["book-movement"],
            "cross-id",
        )
        self.assertTrue(retained)
        event_query,event_params = next(
            (query,params) for query,params in cursor.statements
            if "INSERT INTO contared.cheque_eventos" in query
        )
        self.assertIn("observacion",event_query)
        self.assertIn("documenta lo ocurrido",event_params[5])

    def test_missing_optional_cheque_schema_does_not_break_cross_confirmation(self):
        class Cursor:
            query = ""

            def execute(self, query, params=None):
                self.query = " ".join(query.split())

            def fetchone(self):
                return None

        cursor = Cursor()
        ChequeCrossSync.reflect(
            cursor,
            {"account_id":"account-1"},
            ["bank-movement"],
            ["book-movement"],
            "cross-1",
        )
        self.assertIn("to_regclass", cursor.query)

    def test_non_one_to_one_crosses_do_not_query_cheque_registry(self):
        class Cursor:
            def execute(self, query, params=None):
                raise AssertionError("Grouped crosses cannot auto-classify a cheque.")

        ChequeCrossSync.reflect(
            Cursor(),
            {"account_id":"account-1"},
            ["bank-1","bank-2"],
            ["book-1"],
            "cross-1",
        )


class ChequeClassificationTests(TestCase):
    def setUp(self):
        self.reconciliation_id = uuid4()
        self.cheque_id = str(uuid4())
        self.movement_id = str(uuid4())
        self.scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "reconciliation_id": str(self.reconciliation_id),
            "period": date(2026, 10, 1),
            "cutoff": date(2026, 10, 31),
            "user_id": INTERNAL_USER_ID,
            "user_name": "Auxiliar",
            "role": "auxiliar",
        }

    def _post(self, payload, *, role="auxiliar", duplicate_number_count=1, existing=None,
              resolved_status="retenido"):
        scope = {**self.scope, "role": role}
        database = {"cheques": [], "events": []}

        class Cursor:
            query = ""
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(cursor, query, params=None):
                cursor.query = " ".join(query.split())
                cursor.params = params or []
                if cursor.query.startswith("INSERT INTO contared.cheques"):
                    database["cheques"].append(tuple(cursor.params))
                elif cursor.query.startswith("INSERT INTO contared.cheque_eventos"):
                    database["events"].append(tuple(cursor.params))
                elif cursor.query.startswith("UPDATE contared.cheques"):
                    cursor.rowcount = 1

            def fetchone(cursor):
                if cursor.query.startswith("SELECT m.id::text,m.numero_cheque"):
                    return (
                        self.movement_id,"000121",date(2026,9,9),"Cheque emitido",
                        "-100.0000","account-1",{"beneficiario":"Persona de prueba"},
                        str(self.reconciliation_id),"USD",date(2026,9,1),
                    )
                if cursor.query.startswith("SELECT (SELECT count(*) FROM contared.movimientos m"):
                    return (duplicate_number_count,)
                if cursor.query.startswith("SELECT id::text,estado FROM contared.cheques"):
                    return existing
                if cursor.query.startswith("SELECT estado FROM contared.cheques WHERE id="):
                    return (resolved_status,)
                return None

        cursor = Cursor()
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/cheques/",
            payload,
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation", return_value=scope),
            patch("contared_api.cheques._locked_scope", return_value=scope),
            patch("contared_api.cheques.connection.cursor", return_value=cursor),
            patch("contared_api.cheques.transaction.atomic", nullcontext),
        ):
            response = ReconciliationChequesView.as_view()(
                request,reconciliation_id=self.reconciliation_id
            )
        return response, database

    def test_candidate_classifies_as_held_and_retry_does_not_duplicate(self):
        payload = {
            "movimiento_emision_id": self.movement_id,
            "estado": "retenido",
            "observacion": "Pendiente de entrega",
            "beneficiario": "Beneficiario corregido",
        }
        response, database = self._post(payload)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(database["cheques"][0][10], "retenido")
        self.assertEqual(database["cheques"][0][7], date(2026, 9, 9))
        self.assertEqual(database["cheques"][0][9], Decimal("100"))
        self.assertEqual(database["cheques"][0][8], "Beneficiario corregido")
        self.assertEqual(database["events"][0][1], "retenido")
        self.assertEqual(database["events"][0][0], response.data["id"])

        repeated, repeated_database = self._post(
            payload,existing=(self.cheque_id,"retenido")
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.data["repetido"])
        self.assertEqual(repeated_database["cheques"], [])

    def test_existing_confirmed_cross_returns_resolved_cash_state(self):
        response,database=self._post(
            {"movimiento_emision_id":self.movement_id,"estado":"retenido"},
            resolved_status="cobrado",
        )
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.data["estado"],"cobrado")
        self.assertEqual(len(database["cheques"]),1)

    def test_ambiguous_candidate_requires_explicit_review(self):
        payload = {
            "movimiento_emision_id": self.movement_id,
            "estado": "retenido",
        }
        response, database = self._post(payload,duplicate_number_count=2)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(database["cheques"], [])

        payload["revision_ambiguedad"] = True
        reviewed, reviewed_database = self._post(payload,duplicate_number_count=2)
        self.assertEqual(reviewed.status_code, 201)
        self.assertEqual(len(reviewed_database["cheques"]), 1)

    def test_initial_circulation_requires_confirmation_and_known_delivery_date(self):
        payload = {
            "movimiento_emision_id": self.movement_id,
            "estado": "en_circulacion",
            "fecha_entrega": "2026-09-10",
        }
        response, database = self._post(payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(database["cheques"], [])

        payload["confirmacion_entrega"] = True
        delivered, delivered_database = self._post(payload,resolved_status="en_circulacion")
        self.assertEqual(delivered.status_code, 201)
        self.assertEqual(delivered.data["estado"], "en_circulacion")
        self.assertEqual(delivered_database["cheques"][0][10], "en_circulacion")

    def test_consultation_role_cannot_classify(self):
        response, database = self._post(
            {"movimiento_emision_id":self.movement_id,"estado":"retenido"},
            role="consulta",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(database["cheques"], [])


class ChequeDeliveryTests(TestCase):
    def setUp(self):
        self.reconciliation_id = uuid4()
        self.cheque_id = str(uuid4())
        self.scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "reconciliation_id": str(self.reconciliation_id),
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 10, 31),
            "user_id": INTERNAL_USER_ID,
            "user_name": "Auxiliar",
            "role": "auxiliar",
        }

    def _post(self, *, status="retenido", role="auxiliar", delivery_date="2026-10-10"):
        database = {"updates": [], "events": []}
        cheque = (
            self.cheque_id,status,"account-1",str(uuid4()),"000121",
            date(2026,9,9),"100.0000","USD","origin-1",None,None,
            date(2026,10,10) if status=="en_circulacion" else None,None,None,
        )

        class Cursor:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(cursor, query, params=None):
                statement = " ".join(query.split())
                if statement.startswith("UPDATE contared.cheques"):
                    database["updates"].append(tuple(params))
                    cursor.rowcount = 1
                elif statement.startswith("INSERT INTO contared.cheque_eventos"):
                    database["events"].append(tuple(params))

        scope = {**self.scope, "role": role}
        cursor = Cursor()
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/cheques/entrega/",
            {"fecha_entrega": delivery_date,"observacion":"Entregado en sucursal"},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation", return_value=scope),
            patch("contared_api.cheques._locked_scope", return_value=scope),
            patch("contared_api.cheques._locked_cheque", return_value=cheque),
            patch("contared_api.cheques.connection.cursor", return_value=cursor),
            patch("contared_api.cheques.transaction.atomic", nullcontext),
        ):
            response = ChequeDeliveryView.as_view()(
                request,reconciliation_id=self.reconciliation_id,cheque_id=self.cheque_id
            )
        return response, database

    def test_delivery_records_effective_date_actor_and_note(self):
        response, database = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estado"], "en_circulacion")
        self.assertEqual(database["updates"][0][0], date(2026,10,10))
        self.assertEqual(database["updates"][0][1], INTERNAL_USER_ID)
        self.assertEqual(database["events"][0][1], date(2026,10,10))
        self.assertEqual(database["events"][0][2], INTERNAL_USER_ID)
        self.assertEqual(database["events"][0][3], "Entregado en sucursal")

    def test_delivery_rejects_dates_before_issue_and_wrong_roles(self):
        too_early, database = self._post(delivery_date="2026-09-08")
        self.assertEqual(too_early.status_code, 400)
        self.assertEqual(database["updates"], [])

        forbidden, database = self._post(role="revisor")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(database["updates"], [])

    def test_delivery_retry_cannot_silently_change_effective_date(self):
        repeated, database = self._post(status="en_circulacion")
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.data["repetido"])
        self.assertEqual(database["updates"], [])

        response, database = self._post(
            status="en_circulacion",delivery_date="2026-10-11"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(database["updates"], [])


class ChequeCashTests(TestCase):
    def setUp(self):
        self.reconciliation_id = uuid4()
        self.cheque_id = str(uuid4())
        self.book_id = str(uuid4())
        self.bank_id = str(uuid4())
        self.cross_id = str(uuid4())
        self.scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "reconciliation_id": str(self.reconciliation_id),
            "period": date(2026, 10, 1),
            "cutoff": date(2026, 10, 31),
            "user_id": INTERNAL_USER_ID,
            "user_name": "Revisora",
            "role": "revisor",
        }

    def _post(self, *, status="en_circulacion", bank_amount="-100.0000",
              bank_number="000121",bank_date=date(2026,10,5),
              ambiguity_count=1, existing_crosses=(), movement_cobro_id=None,
              role="revisor", independent=False):
        database = {"crosses": [], "members": [], "updates": [], "events": []}
        cheque = (
            self.cheque_id,status,"account-1",None if independent else self.book_id,"000121",
            date(2026,9,9),"100.0000","USD","origin-1",movement_cobro_id,
            None,date(2026,9,10),None,None,
        )

        class Cursor:
            query = ""
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(cursor, query, params=None):
                cursor.query = " ".join(query.split())
                cursor.params = params or []
                if cursor.query.startswith("INSERT INTO contared.cruces"):
                    database["crosses"].append(tuple(cursor.params))
                elif cursor.query.startswith("INSERT INTO contared.cruce_movimientos"):
                    database["members"].append(tuple(cursor.params))
                elif cursor.query.startswith("UPDATE contared.cheques"):
                    database["updates"].append(tuple(cursor.params))
                    cursor.rowcount = 1
                elif cursor.query.startswith("INSERT INTO contared.cheque_eventos"):
                    database["events"].append(tuple(cursor.params))

            def fetchone(cursor):
                if cursor.query.startswith("SELECT b.id::text, CASE"):
                    return (
                        self.bank_id,bank_number,bank_date,bank_amount,
                        str(self.reconciliation_id),"banco","validada",{},
                        "BANK-REF","Cheque 000121",{},
                    )
                if cursor.query.startswith("SELECT count(*) FROM contared.movimientos b"):
                    return (ambiguity_count,)
                return None

            def fetchall(cursor):
                if cursor.query.startswith("SELECT cm.cruce_id::text,x.estado"):
                    return list(existing_crosses)
                return []

        cursor = Cursor()
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/cheques/cobrar/",
            {"movimiento_banco_id": self.bank_id},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation", return_value={**self.scope,"role":role}),
            patch("contared_api.cheques._locked_scope", return_value={**self.scope,"role":role}),
            patch("contared_api.cheques._locked_cheque", return_value=cheque),
            patch("contared_api.cheques.connection.cursor", return_value=cursor),
            patch("contared_api.cheques.transaction.atomic", nullcontext),
        ):
            response = ChequeCashView.as_view()(
                request,reconciliation_id=self.reconciliation_id,cheque_id=self.cheque_id
            )
        return response, database

    def test_cash_records_cross_once_and_documents_held_inconsistency(self):
        response, database = self._post(status="retenido")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["inconsistencia_retenido"])
        self.assertEqual(len(database["crosses"]), 1)
        self.assertEqual(
            {item[2] for item in database["members"]},
            {self.book_id,self.bank_id},
        )
        self.assertEqual(database["updates"][0][0], self.bank_id)
        self.assertEqual(database["events"][0][1], "retenido")

    def test_independent_cheque_cash_does_not_create_a_fake_cross(self):
        response, database = self._post(independent=True)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["cruce_id"])
        self.assertEqual(database["crosses"], [])
        self.assertEqual(database["members"], [])
        self.assertIsNone(database["events"][0][-1])
        self.assertEqual(database["events"][0][8], response.data["cruce_id"])

    def test_cash_rejects_mismatched_amount_and_ambiguous_candidate(self):
        mismatch, mismatch_db = self._post(bank_amount="-99.99")
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch_db["crosses"], [])

        ambiguous, ambiguous_db = self._post(ambiguity_count=2)
        self.assertEqual(ambiguous.status_code, 400)
        self.assertEqual(ambiguous_db["crosses"], [])

        wrong_number, wrong_number_db = self._post(bank_number="000120")
        self.assertEqual(wrong_number.status_code, 400)
        self.assertEqual(wrong_number_db["crosses"], [])

        before_issue, before_issue_db = self._post(bank_date=date(2026,9,8))
        self.assertEqual(before_issue.status_code, 400)
        self.assertEqual(before_issue_db["crosses"], [])

    def test_idempotent_cash_does_not_create_another_cross(self):
        response, database = self._post(
            status="cobrado",movement_cobro_id=self.bank_id
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["repetido"])
        self.assertEqual(database["crosses"], [])
        self.assertEqual(database["members"], [])

    def test_idempotent_cash_retry_must_reference_the_original_bank_movement(self):
        other_bank_id = str(uuid4())
        database = {"crosses": [], "members": [], "updates": [], "events": []}
        original_post = APIRequestFactory().post(
            "/api/v1/conciliaciones/cheques/cobrar/",
            {"movimiento_banco_id":other_bank_id},
            format="json",
        )
        force_authenticate(original_post,user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation", return_value=self.scope),
            patch("contared_api.cheques._locked_scope", return_value=self.scope),
            patch("contared_api.cheques._locked_cheque", return_value=(
                self.cheque_id,"cobrado","account-1",self.book_id,"000121",
                date(2026,9,9),"100.0000","USD","origin-1",self.bank_id,
                str(self.reconciliation_id),date(2026,9,10),None,None,
            )),
            patch("contared_api.cheques.connection.cursor"),
            patch("contared_api.cheques.transaction.atomic", nullcontext),
        ):
            response = ChequeCashView.as_view()(
                original_post,reconciliation_id=self.reconciliation_id,
                cheque_id=self.cheque_id,
            )
        self.assertEqual(response.status_code, 400)

    def test_auxiliary_cannot_confirm_cheque_cash(self):
        response, database = self._post(role="auxiliar")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(database["crosses"], [])

    def test_existing_confirmed_pair_is_reused_without_duplicate_cross(self):
        existing = [(self.cross_id,"confirmado",str(self.reconciliation_id),"coincidencia",2)] * 2
        response, database = self._post(existing_crosses=existing)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cruce_id"], self.cross_id)
        self.assertEqual(database["crosses"], [])
        self.assertEqual(database["members"], [])


class ChequeAnnulmentTests(TestCase):
    def setUp(self):
        self.reconciliation_id = uuid4()
        self.cheque_id = str(uuid4())
        self.scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "reconciliation_id": str(self.reconciliation_id),
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
            "user_id": INTERNAL_USER_ID,
            "user_name": "Revisora",
            "role": "revisor",
        }

    def _post(self, *, status="en_circulacion", include_support=True):
        database = {"updates": [], "events": []}
        cheque = (
            self.cheque_id,status,"account-1",str(uuid4()),"000121",
            date(2026,9,9),"100.0000","USD","origin-1",None,None,
            date(2026,9,10),None,None,
        )
        class Cursor:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(cursor, query, params=None):
                statement = " ".join(query.split())
                if statement.startswith("UPDATE contared.cheques"):
                    database["updates"].append(tuple(params))
                    cursor.rowcount = 1
                elif statement.startswith("INSERT INTO contared.cheque_eventos"):
                    database["events"].append(tuple(params))

        payload = {
            "fecha_anulacion": "2026-09-20",
            "motivo": "Cheque sustituido con autorización",
        }
        if include_support:
            payload["soporte"] = SimpleUploadedFile(
                "soporte.pdf",b"%PDF-1.4 test document",content_type="application/pdf"
            )
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/cheques/anular/",payload,format="multipart"
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation", return_value=self.scope),
            patch("contared_api.cheques._locked_scope", return_value=self.scope),
            patch("contared_api.cheques._locked_cheque", return_value=cheque),
            patch("contared_api.cheques.connection.cursor", return_value=Cursor()),
            patch("contared_api.cheques.transaction.atomic", nullcontext),
            patch("contared_api.cheques._storage_request") as upload,
            patch("contared_api.cheques._delete_storage_object") as cleanup,
        ):
            response = ChequeAnnulmentView.as_view()(
                request,reconciliation_id=self.reconciliation_id,cheque_id=self.cheque_id
            )
        return response, database, upload, cleanup

    def test_annulment_requires_support_and_records_operational_only_event(self):
        missing, database, upload, cleanup = self._post(include_support=False)
        self.assertEqual(missing.status_code, 400)
        upload.assert_not_called()
        self.assertEqual(database["updates"], [])

        response, database, upload, cleanup = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estado"], "anulado")
        self.assertEqual(response.data["ajuste_contable"], "pendiente")
        upload.assert_called_once()
        cleanup.assert_not_called()
        self.assertEqual(database["updates"][0][0], date(2026,9,20))
        self.assertIn("cheques/", database["updates"][0][2])
        self.assertEqual(database["events"][0][1], "en_circulacion")
        self.assertEqual(database["events"][0][2], date(2026,9,20))

    def test_cashed_cheque_cannot_be_annulled_and_uploaded_support_is_cleaned(self):
        response, database, upload, cleanup = self._post(status="cobrado")
        self.assertEqual(response.status_code, 400)
        upload.assert_called_once()
        cleanup.assert_called_once()
        self.assertEqual(database["updates"], [])


class ChequeHistoryTests(TestCase):
    def setUp(self):
        self.reconciliation_id = uuid4()
        self.cheque_id = str(uuid4())
        self.event_id = str(uuid4())
        self.cross_id = str(uuid4())
        self.scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "reconciliation_id": str(self.reconciliation_id),
            "period": date(2026, 10, 1),
            "cutoff": date(2026, 10, 31),
            "user_id": INTERNAL_USER_ID,
            "user_name": "Consulta",
            "role": "consulta",
        }

    def test_history_includes_effective_registered_actor_and_cross_references(self):
        classified = (
            str(uuid4()),None,"retenido",date(2026,9,9),
            datetime(2026,9,9,10,30,tzinfo=timezone.utc),"Auxiliar",
            "Clasificación inicial desde movimiento explícito de cheque",None,
            None,None,None,None,
        )
        delivered = (
            str(uuid4()),"retenido","en_circulacion",date(2026,9,12),
            datetime(2026,9,12,12,30,tzinfo=timezone.utc),"Auxiliar",
            "Entrega registrada","Confirmó entrega",None,None,None,None,
        )
        event = (
            self.event_id,"en_circulacion","cobrado",date(2026,10,5),
            datetime(2026,10,5,12,30,tzinfo=timezone.utc),"Revisora",
            "Cobro confirmado",None,str(uuid4()),str(self.reconciliation_id),
            self.cross_id,None,
        )
        links = (str(uuid4()),event[8],str(uuid4()),str(self.reconciliation_id))

        class Cursor:
            query = ""

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(cursor, query, params=None):
                cursor.query = " ".join(query.split())

            def fetchall(cursor):
                return [classified,delivered,event] if cursor.query.startswith("SELECT ev.id::text,ev.estado_anterior") else []

            def fetchone(cursor):
                return links if cursor.query.startswith("SELECT ch.movimiento_emision_id::text") else None

        request = APIRequestFactory().get("/api/v1/conciliaciones/cheques/historial/")
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        with (
            patch("contared_api.cheques._scope_for_reconciliation", return_value=self.scope),
            patch("contared_api.cheques._locked_scope", return_value=self.scope),
            patch("contared_api.cheques._locked_cheque", return_value=(
                self.cheque_id,"cobrado","account-1",links[0],"000121",
                date(2026,9,9),"100","USD",links[2],links[1],links[3],
                date(2026,9,10),None,None,
            )),
            patch("contared_api.cheques.connection.cursor", return_value=Cursor()),
        ):
            response = ChequeHistoryView.as_view()(
                request,reconciliation_id=self.reconciliation_id,cheque_id=self.cheque_id
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["events"][0]["estado_anterior"])
        self.assertEqual(response.data["events"][0]["estado_nuevo"], "retenido")
        self.assertEqual(response.data["events"][1]["estado_anterior"], "retenido")
        self.assertEqual(response.data["events"][1]["estado_nuevo"], "en_circulacion")
        self.assertEqual(response.data["events"][2]["fecha_efectiva"], "2026-10-05")
        self.assertEqual(response.data["events"][2]["usuario"], "Revisora")
        self.assertEqual(response.data["events"][2]["cruce_id"], self.cross_id)


class ComparisonPolicyTests(TestCase):
    def _confirmation_fixture(self, *, fail_on_membership=False):
        proposal_id = uuid4()
        bank_id = str(uuid4())
        book_id = str(uuid4())
        reconciliation_id = uuid4()
        cruce_id = str(uuid4())
        scope = {
            "account_id": "account-sv",
            "reconciliation_id": str(reconciliation_id),
            "company_id": "company-sv",
            "country_code": "SV",
            "user_id": INTERNAL_USER_ID,
            "user_name": "Revisora",
            "role": "revisor",
            "currency_code": "USD",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
        }
        database = {
            "proposal_state": "propuesta",
            "cruce_id": None,
            "crosses": [],
            "members": [],
        }

        class Cursor:
            def __init__(self):
                self.query = ""
                self.params = []
                self.rowcount = 0
                self.in_transaction = False
                self.commits = 0
                self.rollbacks = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, query, params=None):
                self.query = " ".join(query.split())
                self.params = params or []
                if self.query.startswith(("INSERT INTO", "UPDATE contared.propuestas_comparacion")):
                    if not self.in_transaction:
                        raise AssertionError("Confirmation writes must be transactional.")
                if self.query.startswith("INSERT INTO contared.cruces"):
                    database["crosses"].append(tuple(self.params))
                    database["cruce_id"] = self.params[0]
                elif self.query.startswith("INSERT INTO contared.cruce_movimientos"):
                    if fail_on_membership:
                        raise DatabaseError("simulated membership insert failure")
                    database["members"].append(tuple(self.params))
                elif self.query.startswith("UPDATE contared.propuestas_comparacion"):
                    database["proposal_state"] = "confirmada"
                    database["cruce_id"] = self.params[0]
                    self.rowcount = 1

            def fetchone(self):
                if "FROM contared.ejecucion_propuestas ep" in self.query:
                    return ("USD", date(2026, 9, 1), date(2026, 9, 30))
                if "RETURNING resuelta_en" in self.query:
                    return (datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc),)
                if "FOR UPDATE OF p" in self.query:
                    return (
                        str(proposal_id),
                        scope["account_id"],
                        "reference_amount",
                        [bank_id],
                        [book_id],
                        database["proposal_state"],
                        database["cruce_id"],
                        "Revisora",
                        datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
                        if database["proposal_state"] == "confirmada" else None,
                        None,
                    )
                return None

            def fetchall(self):
                if self.query.startswith("SELECT m.id::text,m.lado"):
                    return [
                        (
                            bank_id, "banco", date(2026, 9, 1), "REF-1",
                            "10.0000", "banco", "validada",
                        ),
                        (
                            book_id, "libros", date(2026, 9, 1), "REF-1",
                            "10.0000", "libros", "validada",
                        ),
                    ]
                return []

        cursor = Cursor()

        @contextmanager
        def atomic():
            previous = deepcopy(database)
            cursor.in_transaction = True
            try:
                yield
            except Exception:
                database.clear()
                database.update(previous)
                cursor.rollbacks += 1
                raise
            else:
                cursor.commits += 1
            finally:
                cursor.in_transaction = False

        def confirm():
            request = APIRequestFactory().post(
                "/api/v1/conciliaciones/comparaciones/propuestas/confirmar/",
                {},
                format="json",
            )
            force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
            with (
                patch(
                    "contared_api.comparison._scope_for_reconciliation",
                    return_value=scope,
                ),
                patch(
                    "contared_api.comparison._current_reconciliation",
                    return_value=scope,
                ),
                patch(
                    "contared_api.comparison.connection.cursor",
                    return_value=cursor,
                ),
                patch(
                    "contared_api.comparison.transaction",
                    SimpleNamespace(atomic=atomic),
                ),
            ):
                return ReconciliationProposalConfirmView.as_view()(
                    request,
                    reconciliation_id=reconciliation_id,
                    proposal_id=proposal_id,
                )

        return confirm, cursor, database, proposal_id, bank_id, book_id

    def test_confirmed_movements_change_the_comparison_fingerprint(self):
        movement = {
            "id": "movement-1",
            "lado": "banco",
            "fecha": "2026-09-01",
            "fuente": {"carga_id": "load-1"},
            "monto": "10.0000",
            "referencia": "REF-1",
            "numero_transaccion": None,
            "numero_cheque": None,
            "codigo_tt": None,
            "confirmado": False,
        }
        scope = {
            "account_id": "account-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
        }

        before_confirmation = _dataset_fingerprint([movement], scope)
        movement["confirmado"] = True

        self.assertNotEqual(
            before_confirmation,
            _dataset_fingerprint([movement], scope),
        )

    def test_confirmation_with_uuid_is_atomic_and_idempotent(self):
        confirm, cursor, database, proposal_id, bank_id, book_id = (
            self._confirmation_fixture()
        )

        first_response = confirm()
        second_response = confirm()

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.data["estado"], "confirmada")
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.data["cruce_id"], first_response.data["cruce_id"])
        self.assertEqual(len(database["crosses"]), 1)
        self.assertEqual(
            {item[2] for item in database["members"]},
            {bank_id, book_id},
        )
        self.assertEqual(database["proposal_state"], "confirmada")
        self.assertEqual(
            database["crosses"][0][5],
            f"Confirmado desde propuesta {proposal_id}",
        )
        self.assertEqual(cursor.commits, 2)
        self.assertEqual(cursor.rollbacks, 0)

    def test_confirmation_failure_rolls_back_cross_members_and_proposal(self):
        confirm, cursor, database, _, _, _ = self._confirmation_fixture(
            fail_on_membership=True
        )

        response = confirm()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(database["crosses"], [])
        self.assertEqual(database["members"], [])
        self.assertEqual(database["proposal_state"], "propuesta")
        self.assertEqual(cursor.commits, 0)
        self.assertEqual(cursor.rollbacks, 1)

    def test_confirmed_movements_are_not_proposed_again(self):
        movements = [
            {
                "id": "confirmed-bank",
                "lado": "banco",
                "fecha": "2026-09-01",
                "monto": "10.00",
                "referencia": "REF-1",
                "confirmado": True,
            },
            {
                "id": "confirmed-books",
                "lado": "libros",
                "fecha": "2026-09-01",
                "monto": "10.00",
                "referencia": "REF-1",
                "confirmado": True,
            },
        ]

        eligible = _eligible_movements(movements)

        self.assertEqual(eligible, [])
        self.assertEqual(
            propose_reconciliation(
                _engine_movements(eligible, "banco", "account-1"),
                _engine_movements(eligible, "libros", "account-1"),
            ),
            [],
        )

    def test_confirmed_proposals_remain_in_comparison_results(self):
        bank_id = str(uuid4())
        book_id = str(uuid4())
        proposal_id = str(uuid4())
        cruce_id = str(uuid4())

        class Cursor:
            def __init__(self):
                self.query = ""
                self.queries = []

            def execute(self, query, params=None):
                self.query = " ".join(query.split())
                self.queries.append(self.query)

            def fetchall(self):
                if self.query.startswith("SELECT p.id::text,p.regla_codigo"):
                    return [
                        (
                            proposal_id, "reference_amount", [bank_id], [book_id],
                            "confirmada", "Referencia y monto exactos.",
                            cruce_id, None, None, "Revisora",
                        ),
                        (
                            str(uuid4()), "unmatched", [bank_id], [],
                            "descartada", "Sin contraparte identificada.",
                            None, None, "Descartada para investigar.",
                            "Revisora",
                        ),
                    ]
                if self.query.startswith("SELECT m.id::text,m.lado"):
                    def movement_row(movement_id, side, origin):
                        return (
                            movement_id, side, date(2026, 9, 1), None, "REF-1",
                            None, None, None, "", "10.0000", None, "Hoja1", 1,
                            {}, str(uuid4()), origin, "excel", "archivo.xlsx",
                            "validada", None, None, {}, True,
                        )

                    return [
                        movement_row(bank_id, "banco", "banco"),
                        movement_row(book_id, "libros", "libros"),
                    ]
                return []

        scope = {
            "account_id": "account-1",
            "reconciliation_id": "reconciliation-1",
            "company_id": "company-1",
            "country_code": "SV",
        }
        cursor = Cursor()
        results = _results_for_execution(cursor, scope, str(uuid4()))

        self.assertEqual(len(results), 2)
        confirmed = next(item for item in results if item["estado"] == "confirmada")
        dismissed = next(item for item in results if item["estado"] == "descartada")
        self.assertEqual(confirmed["cruce_id"], cruce_id)
        self.assertTrue(confirmed["banco"][0]["confirmado"])
        self.assertTrue(confirmed["libros"][0]["confirmado"])
        self.assertEqual(dismissed["motivo_descarte"], "Descartada para investigar.")
        self.assertTrue(any("p.estado IN ('confirmada','descartada')" in query for query in cursor.queries))

    def test_comparison_role_matrix(self):
        for role in ("administrador", "auxiliar", "revisor"):
            with self.subTest(role=role):
                _require_role({"role": role}, ("administrador", "auxiliar", "revisor"))
        with self.assertRaises(PermissionDenied):
            _require_role({"role": "consulta"}, ("administrador", "auxiliar", "revisor"))
        for role in ("administrador", "revisor"):
            with self.subTest(role=role):
                _require_role({"role": role}, ("administrador", "revisor"))
        for role in ("auxiliar", "consulta"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                _require_role({"role": role}, ("administrador", "revisor"))

    def test_discard_requires_a_non_empty_reason(self):
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/reconciliation-id/comparaciones/propuestas/proposal-id/descartar/",
            {"motivo": "   "},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        scope = {
            "role": "revisor",
            "account_id": "account-1",
            "reconciliation_id": "reconciliation-1",
        }

        with patch(
            "contared_api.comparison._scope_for_reconciliation",
            return_value=scope,
        ):
            response = ReconciliationProposalDismissView.as_view()(
                request,
                reconciliation_id=uuid4(),
                proposal_id=uuid4(),
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("motivo", response.data)

    def test_confirmation_rejects_unequal_decimals_before_writing_a_cross(self):
        bank_id = str(uuid4())
        book_id = str(uuid4())
        rows = [
            (bank_id, "banco", date(2026, 9, 1), "000005", "450.0000", "banco", "validada"),
            (book_id, "libros", date(2026, 9, 1), "000005", "430.0000", "libros", "validada"),
        ]

        class Cursor:
            def __init__(self):
                self.statements = []

            def execute(self, query, params=None):
                self.statements.append(query)

            def fetchall(self):
                return rows

            def fetchone(self):
                return ("USD", date(2026, 9, 1), date(2026, 9, 30))

        cursor = Cursor()
        scope = {
            "account_id": "account-sv",
            "reconciliation_id": "reconciliation-sv",
            "company_id": "company-sv",
            "country_code": "SV",
            "user_id": INTERNAL_USER_ID,
            "currency_code": "USD",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
        }
        proposal = (
            "proposal-id",
            "account-sv",
            "reference_amount",
            [bank_id],
            [book_id],
            "propuesta",
            None,
        )

        with self.assertRaises(ValidationError) as raised:
            ReconciliationProposalActionView()._confirm(
                cursor, scope, "proposal-id", proposal
            )

        self.assertIn("exactamente iguales", str(raised.exception.detail))
        self.assertFalse(any("INSERT INTO contared.cruces" in query for query in cursor.statements))


class ComparisonReportTests(TestCase):
    def setUp(self):
        self.report_id = str(uuid4())
        self.scope = {
            "account_id": "account-1",
            "reconciliation_id": "reconciliation-1",
            "company_id": "company-1",
            "country_code": "SV",
            "currency_code": "USD",
            "period": date(2026, 9, 1),
            "cutoff": date(2026, 9, 30),
            "user_id": INTERNAL_USER_ID,
            "user_name": "Revisora",
            "role": "consulta",
        }
        bank = {
            "id": str(uuid4()),
            "fecha": "2026-09-03",
            "referencia": "000003",
            "numero_cheque": "000120",
            "numero_transaccion": "0000042",
            "descripcion": "Depósito de prueba",
            "monto": "300.0000",
        }
        book_a = {
            "id": str(uuid4()),
            "fecha": "2026-09-03",
            "referencia": "000003",
            "numero_cheque": None,
            "numero_transaccion": None,
            "descripcion": "Partida uno",
            "monto": "180.0000",
        }
        book_b = {
            "id": str(uuid4()),
            "fecha": "2026-09-03",
            "referencia": "000003",
            "numero_cheque": None,
            "numero_transaccion": None,
            "descripcion": "Partida dos",
            "monto": "120.0000",
        }
        self.proposals = [
            {
                "id": str(uuid4()),
                "regla": "reference_date_group",
                "estado": "propuesta",
                "explicacion": "Grupo exacto por referencia y fecha.",
                "banco": [bank],
                "libros": [book_a, book_b],
                "suma_banco": "300.0000",
                "suma_libros": "300.0000",
                "diferencia": "0.0000",
                "resuelta_por_nombre": None,
                "resuelta_en": None,
            },
            {
                "id": str(uuid4()),
                "regla": "reference_amount",
                "estado": "confirmada",
                "explicacion": "Referencia y monto exactos.",
                "banco": [dict(bank, id=str(uuid4()), descripcion="Confirmado")],
                "libros": [dict(book_a, id=str(uuid4()), descripcion="Confirmado")],
                "suma_banco": "10.0000",
                "suma_libros": "10.0000",
                "diferencia": "0.0000",
                "resuelta_por_nombre": "Revisora",
                "resuelta_en": "2026-10-29T10:00:00-06:00",
                "cruce_id": str(uuid4()),
            },
            {
                "id": str(uuid4()),
                "regla": "unmatched",
                "estado": "descartada",
                "explicacion": "Sin contraparte identificada.",
                "banco": [dict(bank, id=str(uuid4()), referencia="=1+1",
                               descripcion="=HYPERLINK(\"https://example.invalid\")",
                               monto="-25.0000")],
                "libros": [],
                "suma_banco": "-25.0000",
                "suma_libros": "0",
                "diferencia": "-25.0000",
                "resuelta_por_nombre": "Revisora",
                "resuelta_en": "2026-10-29T10:01:00-06:00",
                "motivo_descarte": "No hay soporte; revisar luego.",
            },
            {
                "id": str(uuid4()),
                "regla": "reference_amount_mismatch",
                "estado": "propuesta",
                "explicacion": "La referencia coincide, pero difiere el monto.",
                "banco": [dict(bank, id=str(uuid4()), monto="450.0000")],
                "libros": [dict(book_a, id=str(uuid4()), monto="430.0000")],
                "suma_banco": "450.0000",
                "suma_libros": "430.0000",
                "diferencia": "20.0000",
                "resuelta_por_nombre": None,
                "resuelta_en": None,
            },
        ]
        self.metadata = {
            "company": "Empresa de prueba",
            "country": "El Salvador (SV)",
            "bank": "Banco de prueba",
            "account": "Cuenta principal · 001234",
            "currency": "Dólares estadounidenses (USD)",
            "period": "2026-09-01",
            "cutoff": "2026-09-30",
        }

    def snapshot(self, groups=GROUPS, report_scope="all"):
        proposals = _selected_proposals(self.proposals, groups)
        return _report_snapshot(
            self.report_id, self.scope, self.metadata, proposals, groups, report_scope
        )

    def request_report(self, data, *, fail_insert=False, fail_storage=False):
        test_case = self

        class Cursor:
            def __init__(self):
                self.query = ""
                self.params = []
                self.report_inserted = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, query, params=None):
                self.query = " ".join(query.split())
                self.params = params or []
                if self.query.startswith("INSERT INTO contared.reportes"):
                    if fail_insert:
                        raise DatabaseError("report registration failure")
                    self.report_inserted = True
                    test_case.persisted_snapshot = json.loads(self.params[5])

            def fetchone(self):
                if self.query.startswith("SELECT e.nombre"):
                    return (
                        test_case.metadata["company"],
                        "El Salvador",
                        test_case.metadata["bank"],
                        "Cuenta principal",
                        "001234",
                        "Dólares estadounidenses",
                        "USD",
                    )
                if self.query.startswith("SELECT COALESCE(MAX(version),0)+1"):
                    return (3,)
                return None

        self.persisted_snapshot = None
        cursor = Cursor()
        request = APIRequestFactory().post(
            "/api/v1/conciliaciones/reconciliation-id/comparaciones/reportes/",
            data,
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        from contared_api.conciliaciones import ImportStorageUnavailable

        storage_patch = patch(
            "contared_api.comparison_reports._storage_request",
            side_effect=ImportStorageUnavailable() if fail_storage else None,
        )
        with (
            patch(
                "contared_api.comparison_reports._scope_for_reconciliation",
                return_value=self.scope,
            ),
            patch(
                "contared_api.comparison_reports._current_reconciliation",
                return_value=self.scope,
            ),
            patch(
                "contared_api.comparison_reports._latest_comparison",
                return_value=("execution-1", "f" * 64, None, None),
            ),
            patch(
                "contared_api.comparison_reports._results_for_execution",
                return_value=self.proposals,
            ),
            patch(
                "contared_api.comparison_reports.connection.cursor",
                return_value=cursor,
            ),
            patch(
                "contared_api.comparison_reports.transaction",
                SimpleNamespace(atomic=nullcontext),
            ),
            storage_patch as storage,
            patch("contared_api.comparison_reports._delete_storage_object") as cleanup,
        ):
            response = ReconciliationReportView.as_view()(
                request,
                reconciliation_id=uuid4(),
            )
        return response, cursor, storage, cleanup

    def test_group_classification_keeps_dismissed_differences_visible(self):
        dismissed_difference = self.proposals[2]
        self.assertEqual(
            set(_proposal_groups(dismissed_difference)),
            {"dismissed", "differences"},
        )
        self.assertEqual(
            len(_selected_proposals(self.proposals, ["differences"])),
            2,
        )
        self.assertEqual(
            len(_selected_proposals(self.proposals, ["dismissed"])),
            1,
        )

    def test_excel_preserves_all_group_movements_literal_text_and_typed_values(self):
        output = build_xlsx(self.snapshot())
        workbook = load_workbook(io.BytesIO(output), data_only=False)

        self.assertEqual(
            set(workbook.sheetnames),
            {"Resumen", "Por revisar", "Confirmados", "Diferencias y sin contraparte", "Descartadas"},
        )
        summary = workbook["Resumen"]
        self.assertEqual(summary["B6"].value, "Cuenta principal · 001234")
        self.assertEqual(summary["B8"].value.date(), date(2026, 9, 1))
        self.assertEqual(summary["A1"].value, "Reporte del proceso de conciliación — Borrador")
        self.assertEqual(workbook["Por revisar"].freeze_panes, "A2")
        self.assertTrue(workbook["Por revisar"].auto_filter.ref.startswith("A1:R"))

        review = workbook["Por revisar"]
        movement_rows = [
            row for row in review.iter_rows(min_row=2, values_only=True)
            if row[1] == "Movimiento"
        ]
        self.assertEqual(len(movement_rows), 3)
        self.assertEqual(
            {row[5] for row in movement_rows},
            {"Banco", "Libros"},
        )
        self.assertTrue(all(isinstance(row[6], date) for row in movement_rows))
        self.assertTrue(all(isinstance(row[11], (int, float)) for row in movement_rows))

        differences = workbook["Diferencias y sin contraparte"]
        formula_row = next(
            row for row in differences.iter_rows(min_row=2)
            if row[1].value == "Movimiento" and row[7].value == "=1+1"
        )
        self.assertEqual(formula_row[7].data_type, "s")
        self.assertEqual(formula_row[10].data_type, "s")
        self.assertEqual(formula_row[10].value, '=HYPERLINK("https://example.invalid")')
        self.assertEqual(formula_row[11].value, -25)

    def test_pdf_builds_valid_document_with_full_group_details(self):
        output = build_pdf(self.snapshot())

        self.assertTrue(output.startswith(b"%PDF-"))
        self.assertGreater(len(output), 1500)

    def test_snapshot_counts_statuses_groups_and_filters(self):
        snapshot = self.snapshot()

        self.assertEqual(snapshot["counts"], {
            "review": 1,
            "confirmed": 1,
            "differences": 2,
            "dismissed": 1,
        })
        self.assertEqual(snapshot["proposal_count"], 4)
        self.assertEqual(snapshot["context"]["currency"], "Dólares estadounidenses (USD)")
        self.assertIn("No se aplicaron filtros de búsqueda ni paginación.", snapshot["filters"]["description"])
        self.assertFalse(snapshot["closed"])

    def test_report_endpoint_registers_persisted_snapshot_for_all_selected_groups(self):
        response, cursor, storage, _ = self.request_report({
            "formato": "xlsx",
            "alcance": "all",
            "grupos": list(GROUPS),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(cursor.params[2:4], [3, "xlsx"])
        self.assertIsNotNone(self.persisted_snapshot)
        self.assertEqual(self.persisted_snapshot["proposal_count"], 4)
        self.assertEqual(
            self.persisted_snapshot["counts"]["differences"],
            2,
        )
        self.assertIn("reports/company-1/account-1/reconciliation-1/", cursor.params[4])
        self.assertEqual(storage.call_args.kwargs["body"], response.content)

    def test_current_tab_export_uses_only_that_group(self):
        response, _, _, _ = self.request_report({
            "formato": "pdf",
            "alcance": "current",
            "grupo_actual": "dismissed",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            self.persisted_snapshot["scope"]["groups"],
            [{"key": "dismissed", "label": "Descartadas"}],
        )
        self.assertEqual(self.persisted_snapshot["proposal_count"], 1)

    def test_failed_storage_upload_does_not_register_a_report(self):
        response, cursor, storage, cleanup = self.request_report({
            "formato": "xlsx",
            "alcance": "all",
            "grupos": ["review"],
        }, fail_storage=True)

        self.assertEqual(response.status_code, 503)
        self.assertFalse(cursor.report_inserted)
        storage.assert_called_once()
        cleanup.assert_called_once()

    def test_failed_report_registration_cleans_private_object_and_returns_error(self):
        response, cursor, storage, cleanup = self.request_report({
            "formato": "xlsx",
            "alcance": "all",
            "grupos": ["review"],
        }, fail_insert=True)

        self.assertEqual(response.status_code, 503)
        self.assertFalse(cursor.report_inserted)
        storage.assert_called_once()
        cleanup.assert_called_once_with(storage.call_args.args[1])

    def test_report_endpoint_rejects_empty_or_invalid_group_selection(self):
        response, _, storage, _ = self.request_report({
            "formato": "pdf",
            "alcance": "all",
            "grupos": [],
        })

        self.assertEqual(response.status_code, 400)
        storage.assert_not_called()


class BankingAuthorizationTests(SimpleTestCase):
    def test_reconciliation_scope_includes_authorized_profile_name(self):
        company_id = str(uuid4())
        account_id = str(uuid4())
        reconciliation_id = str(uuid4())
        profile_id = str(uuid4())
        cutoff = date(2026, 9, 30)

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                return None

            def fetchone(self):
                return (
                    company_id, "GT", reconciliation_id, account_id, str(uuid4()),
                    date(2026, 9, 1), cutoff, "borrador", None, None, None, None,
                    "0040040592", "GTQ", "Banco Industrial", "1101030103",
                )

        session = {
            "usuario": {"id": profile_id, "nombre": "Perfil autorizado"},
            "accesos": [{
                "empresa_id": company_id,
                "pais_codigo": "GT",
                "rol": "administrador",
            }],
        }
        request = SimpleNamespace(user=SimpleNamespace(auth_user_id=AUTH_USER_ID))
        with (
            patch("contared_api.conciliaciones.load_session_access", return_value=session),
            patch("contared_api.conciliaciones.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.conciliaciones.connection", SimpleNamespace(cursor=Cursor)),
        ):
            scope = _scope_for_reconciliation(request, reconciliation_id)

        self.assertEqual(scope["user_id"], profile_id)
        self.assertEqual(scope["user_name"], "Perfil autorizado")
        self.assertEqual(scope["company_id"], company_id)
        self.assertEqual(scope["reconciliation_id"], reconciliation_id)

    def test_administrator_is_required_for_writes(self):
        for role in ("auxiliar", "revisor", "consulta"):
            with self.subTest(role=role):
                with self.assertRaises(PermissionDenied) as raised:
                    require_administrator({"role": role})
                self.assertEqual(raised.exception.detail.code, "administrator_required")

    def test_administrator_can_write(self):
        self.assertIsNone(require_administrator({"role": "administrador"}))

    @patch("contared_api.session_access.load_session_access")
    def test_cross_company_and_country_scope_is_rejected(self, load_session):
        load_session.return_value = {
            "usuario": {"id": INTERNAL_USER_ID, "nombre": "Usuario"},
            "accesos": [{
                "empresa_id": "company-sv",
                "empresa": "Empresa SV",
                "pais_codigo": "SV",
                "pais": "El Salvador",
                "rol": "administrador",
            }],
        }
        with self.assertRaises(PermissionDenied) as raised:
            resolve_authorized_scope(AUTH_USER_ID, "company-sv", "GT")
        self.assertEqual(raised.exception.detail.code, "scope_not_authorized")

    def test_account_number_is_preserved_as_text(self):
        numero = "00123456"
        self.assertIsInstance(numero, str)
        self.assertEqual(numero, "00123456")

    @patch("contared_api.banking.scope_from_request")
    def test_account_deletion_rejects_non_administrators(self, scope_from_request):
        scope_from_request.return_value = {
            "company_id": "company-sv",
            "country_code": "SV",
            "role": "consulta",
            "user_id": INTERNAL_USER_ID,
        }
        request = APIRequestFactory().delete("/api/v1/cuentas/00000000-0000-0000-0000-000000000001/?empresa_id=company-sv&pais_codigo=SV")
        with self.assertRaises(PermissionDenied):
            AccountDetailView().delete(request, "00000000-0000-0000-0000-000000000001")


class ReconciliationMovementSourceTests(SimpleTestCase):
    def test_normalizes_json_string_original_data(self):
        self.assertEqual(
            _normalize_original_data('{"Referencia":"000012","Monto":0}'),
            {"Referencia": "000012", "Monto": 0},
        )

    def test_keeps_missing_original_data_distinct_from_empty_values(self):
        self.assertIsNone(_normalize_original_data(None))
        self.assertEqual(_normalize_original_data({"Monto": 0, "Referencia": ""}), {
            "Monto": 0,
            "Referencia": "",
        })

    def test_rejects_invalid_original_data_instead_of_replacing_it(self):
        with self.assertRaises(APIException) as raised:
            _normalize_original_data("{not valid json")
        self.assertEqual(raised.exception.detail.code, "invalid_original_data")

    def test_returns_source_method_and_original_data_for_bank_and_books(self):
        scope = {
            "account_id": "account-sv",
            "reconciliation_id": "reconciliation-id",
            "company_id": "company-sv",
            "country_code": "SV",
            "period": date(2026, 1, 1),
            "cutoff": date(2026, 1, 31),
        }
        source_user = SupabaseUser(AUTH_USER_ID)
        for side, original_data in (
            ("banco", '{"Referencia":"000012","Monto":0}'),
            ("libros", {"Referencia": "000012", "Monto": 0}),
        ):
            with self.subTest(side=side):
                row = (
                    "movement-id",
                    side,
                    date(2026, 1, 15),
                    None,
                    "000012",
                    None,
                    "000007",
                    "CQ" if side == "banco" else None,
                    "Pago",
                    Decimal("25.00"),
                    None,
                    "Hoja1",
                    4,
                    original_data,
                    side,
                    "excel",
                    "movimientos.xlsx",
                    "validada",
                    datetime(2026, 1, 16, 10, 30, tzinfo=timezone.utc),
                    "Ana",
                    {"catalogo_tipo_transaccion": {"CQ": "Cheque cobrado"}},
                )
                cursor = FakeSessionCursor()
                request = APIRequestFactory().get(
                    f"/api/v1/conciliaciones/reconciliation-id/movimientos/?lado={side}"
                )
                force_authenticate(request, user=source_user)
                with (
                    patch(
                        "contared_api.conciliaciones._scope_for_reconciliation",
                        return_value=scope,
                    ),
                    patch(
                        "contared_api.conciliaciones.transaction",
                        SimpleNamespace(atomic=lambda: nullcontext()),
                    ),
                    patch(
                        "contared_api.conciliaciones.connection",
                        SimpleNamespace(cursor=lambda: cursor),
                    ),
                    patch(
                        "contared_api.conciliaciones.paginate",
                        return_value={
                            "count": 1,
                            "page": 1,
                            "page_size": 25,
                            "results": [row],
                        },
                    ) as paginate,
                ):
                    response = ReconciliationMovementsView.as_view()(
                        request,
                        reconciliation_id="reconciliation-id",
                    )

                self.assertEqual(response.status_code, 200)
                movement = response.data["results"][0]
                self.assertEqual(movement["referencia"], "000012")
                self.assertEqual(movement["numero_cheque"], "000007")
                self.assertEqual(movement["tipo_transaccion"], "Cheque cobrado" if side == "banco" else "Tipo de transacción sin identificar")
                self.assertEqual(movement["datos_originales"], {
                    "Referencia": "000012",
                    "Monto": 0,
                })
                self.assertEqual(movement["fuente"]["origen"], side)
                self.assertEqual(movement["fuente"]["metodo"], "excel")
                self.assertEqual(movement["fuente"]["creada_por_nombre"], "Ana")
                self.assertNotIn("sha256", movement["fuente"])
                self.assertNotIn("carga_id", movement["fuente"])
                rows_query = paginate.call_args.args[2]
                self.assertIn("c.metodo", rows_query)
                self.assertIn("u.activo IS TRUE", rows_query)
                self.assertIn("a.usuario_id=u.id", rows_query)
                self.assertIn("a.empresa_id=c.empresa_id", rows_query)
                self.assertIn("a.pais_codigo=c.pais_codigo", rows_query)
                self.assertEqual(paginate.call_args.args[3][1], side)
