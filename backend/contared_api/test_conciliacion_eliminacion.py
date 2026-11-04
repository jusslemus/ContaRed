from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from django.db import DatabaseError, IntegrityError, OperationalError, ProgrammingError
from django.test import SimpleTestCase
from rest_framework.exceptions import APIException, NotFound
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseUser
from contared_api.conciliacion_eliminacion import (
    CROSS_PERIOD_QUERY,
    DELETE_ORDER,
    DeletionDatabaseOperationError,
    DeletionDatabasePermissionError,
    DeletionDatabaseQueryError,
    ReconciliationDeletionView,
    _raise_deletion_database_error,
    _allowed_storage_path,
    _cross_period_references,
    _preview,
    _retry_storage_cleanup,
)
from contared_api.conciliaciones import ImportStorageUnavailable
from contared_api.session_access import DatabaseUnavailable


AUTH_USER_ID = UUID("b1e54dc1-9c2f-4d15-baca-15a4f8d7ac2a")
USER_ID = "f42522e0-8d61-45d2-8b17-4cd7da7d7a01"
COMPANY_ID = "10000000-0000-0000-0000-000000000001"
ACCOUNT_ID = "20000000-0000-0000-0000-000000000002"
RECONCILIATION_ID = UUID("30000000-0000-0000-0000-000000000003")
STORAGE_PATH = f"imports/{COMPANY_ID}/{ACCOUNT_ID}/synthetic-source.xlsx"


class SyntheticCursor:
    def __init__(
        self, *, cross_period=(), storage_paths=(), shared_paths=(),
        shared_references=(),
        fail_on_delete=None, fail_error=None, enforce_cheque_event_cruce_fk=False,
        lock_missing=False, existing_receipt=False,
    ):
        self.cross_period = list(cross_period)
        self.storage_paths = list(storage_paths)
        self.shared_paths = list(shared_paths)
        self.shared_references = list(shared_references) or [
            (
                path, "Caso de depósito", "40000000-0000-0000-0000-000000000004",
                "2026-10-01", False,
            )
            for path in self.shared_paths
        ]
        self.fail_on_delete = fail_on_delete
        self.fail_error = fail_error
        self.enforce_cheque_event_cruce_fk = enforce_cheque_event_cruce_fk
        self.lock_missing = lock_missing
        self.existing_receipt = existing_receipt
        self.statements = []
        self.last = None
        self.last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.last_query = query
        if "FOR UPDATE OF r" in query:
            self.last = None if self.lock_missing else (
                str(RECONCILIATION_ID), ACCOUNT_ID, COMPANY_ID, "SV",
                "Prueba", "001", "Banco sintético", "USD",
                "2026-09-01", "2026-09-30", None,
            )
        elif "SELECT b.nombre,cb.alias" in query:
            self.last = (
                "Banco sintético", "Cuenta aislada", "001", "USD",
                "2026-09-01", "2026-09-30", None,
            )
        elif "FROM contared.eliminaciones_conciliacion e" in query:
            self.last = ("60000000-0000-0000-0000-000000000006",) if self.existing_receipt else None
        elif "SELECT DISTINCT tipo,periodo" in query:
            self.last = None
        elif "SELECT DISTINCT ruta" in query:
            self.last = None
        elif "WITH referencias(ruta,tipo,registro_id,conciliacion_id)" in query:
            self.last = None
        elif query.startswith("WITH scoped(tabla,registro_id)"):
            self.last = (4,)
        elif query.lstrip().startswith("SELECT count(*)"):
            self.last = (1,)
        elif (
            self.enforce_cheque_event_cruce_fk
            and query.lstrip().startswith("DELETE FROM contared.cruces")
            and not any(
                statement.lstrip().startswith("DELETE FROM contared.cheque_eventos")
                for statement, _ in self.statements[:-1]
            )
        ):
            error = IntegrityError("synthetic cheque_eventos.cruce_id foreign key")
            error.__cause__ = SyntheticPostgresError(
                "foreign key violation",
                sqlstate="23503",
                constraint="cheque_eventos_cruce_id_fkey",
            )
            raise error
        elif self.fail_on_delete and query.lstrip().startswith(f"DELETE FROM contared.{self.fail_on_delete}"):
            if self.fail_error is not None:
                raise self.fail_error
            raise DatabaseError("synthetic intermediate failure")
        else:
            self.last = None

    def fetchone(self):
        return self.last

    def fetchall(self):
        if "SELECT tipo,periodo,count(*)" in self.last_query:
            return self.cross_period
        if "WITH referencias(ruta,tipo,registro_id,conciliacion_id)" in self.last_query:
            return self.shared_references
        if "SELECT DISTINCT ruta" in self.last_query:
            return [(path,) for path in self.storage_paths]
        return []


class SyntheticPostgresError(Exception):
    def __init__(self, message, *, sqlstate=None, constraint=None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint)


class AtomicTracker:
    def __init__(self):
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.rolled_back = exc_type is not None
        return False


class ReconciliationDeletionTests(SimpleTestCase):
    databases = {"default"}
    scope = {
        "user_id": USER_ID,
        "company_id": COMPANY_ID,
        "country_code": "SV",
        "account_id": ACCOUNT_ID,
        "role": "administrador",
    }

    def test_delete_order_respects_all_declared_foreign_key_dependencies(self):
        expected = (
            "ejecucion_propuestas",
            "propuestas_comparacion",
            "cheque_eventos",
            "deposito_eventos",
            "cruce_movimientos",
            "deposito_movimientos",
            "cheques",
            "casos_deposito",
            "ajustes",
            "pendientes",
            "reportes",
            "ejecuciones_comparacion",
            "cruces",
            "movimientos",
            "cargas",
            "eventos_archivo_conciliacion",
            "conciliaciones",
        )
        self.assertEqual(DELETE_ORDER, expected)
        position = {table: index for index, table in enumerate(DELETE_ORDER)}
        foreign_key_edges = (
            ("ejecucion_propuestas", "propuestas_comparacion"),
            ("ejecucion_propuestas", "ejecuciones_comparacion"),
            ("propuestas_comparacion", "ejecuciones_comparacion"),
            ("propuestas_comparacion", "cruces"),
            ("cheque_eventos", "cheques"),
            ("cheque_eventos", "cruces"),
            ("cheque_eventos", "movimientos"),
            ("deposito_eventos", "casos_deposito"),
            ("cruce_movimientos", "cruces"),
            ("cruce_movimientos", "movimientos"),
            ("deposito_movimientos", "casos_deposito"),
            ("deposito_movimientos", "movimientos"),
            ("cheques", "movimientos"),
            ("casos_deposito", "conciliaciones"),
            ("ajustes", "pendientes"),
            ("pendientes", "movimientos"),
            ("reportes", "conciliaciones"),
            ("ejecuciones_comparacion", "conciliaciones"),
            ("cruces", "conciliaciones"),
            ("movimientos", "cargas"),
            ("cargas", "conciliaciones"),
            ("eventos_archivo_conciliacion", "conciliaciones"),
        )
        for child, parent in foreign_key_edges:
            with self.subTest(child=child, parent=parent):
                self.assertLess(position[child], position[parent])

    def test_paid_cheque_event_referencing_confirmed_cross_deletes_before_cross(self):
        cursor = SyntheticCursor(enforce_cheque_event_cruce_fk=True)
        body = {
            "motivo": "Caso sintético de cheque cobrado",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 200, response.data)
        deleted = [
            query for query, _ in cursor.statements
            if query.lstrip().startswith("DELETE FROM")
        ]
        event_delete = next(i for i, query in enumerate(deleted)
                            if "contared.cheque_eventos" in query)
        cross_delete = next(i for i, query in enumerate(deleted)
                            if "contared.cruces" in query)
        self.assertLess(event_delete, cross_delete)

        migrations = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
        cheque_sql = (migrations / "202610300001_cheques.sql").read_text(encoding="utf-8")
        self.assertIn("cruce_id uuid REFERENCES contared.cruces(id)", cheque_sql)
        cleanup.assert_called_once_with(RECONCILIATION_ID)

    def request(self, method, data=None, *, path=None):
        factory = APIRequestFactory()
        request = getattr(factory, method)(
            path or f"/api/v1/conciliaciones/{RECONCILIATION_ID}/eliminacion/",
            data=data or {},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(AUTH_USER_ID))
        return request

    def invoke_delete(self, cursor, *, body=None, role="administrador", atomic=None):
        scope = {**self.scope, "role": role}
        with (
            patch("contared_api.conciliacion_eliminacion._require_deletion_schema"),
            patch("contared_api.conciliacion_eliminacion._receipt_scope", side_effect=NotFound()),
            patch("contared_api.conciliacion_eliminacion._scope_for_reconciliation", return_value=scope),
            patch("contared_api.conciliacion_eliminacion.connection.cursor", return_value=cursor),
            patch(
                "contared_api.conciliacion_eliminacion.transaction.atomic",
                return_value=atomic or nullcontext(),
            ),
            patch(
                "contared_api.conciliacion_eliminacion._retry_storage_cleanup",
                return_value={
                    "id": str(RECONCILIATION_ID),
                    "eliminada": True,
                    "limpieza_completa": True,
                    "archivos_pendientes": 0,
                },
            ) as cleanup,
        ):
            response = ReconciliationDeletionView.as_view()(
                self.request("delete", body or {}),
                reconciliation_id=RECONCILIATION_ID,
            )
        return response, cleanup

    def test_preview_counts_synthetic_financial_records_and_names_exact_scope(self):
        cursor = SyntheticCursor()
        preview = _preview(cursor, RECONCILIATION_ID, self.scope)
        self.assertEqual(preview["banco"], "Banco sintético")
        self.assertEqual(preview["cuenta_alias"], "Cuenta aislada")
        self.assertEqual(preview["periodo"], "2026-09-01")
        self.assertEqual(
            preview["frase_requerida"],
            f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
        )
        self.assertTrue(preview["libera_cuenta_periodo"])
        self.assertIn("propuestas_comparacion", preview["cantidades"])
        self.assertIn("cruces", preview["cantidades"])
        self.assertIn("cheques", preview["cantidades"])
        self.assertIn("casos_deposito", preview["cantidades"])
        self.assertIn("archivos_privados", preview["cantidades"])
        self.assertIn("auditoria_financiera", preview["cantidades"])

    def test_other_period_reference_blocks_purge_and_prevents_storage_cleanup(self):
        cursor = SyntheticCursor(
            cross_period=[("Cheque originado o cobrado en otro período", "2026-10-01", 1)]
        )
        body = {
            "motivo": "Datos sintéticos de prueba",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn("2026-10-01", response.data["detail"])
        self.assertFalse(any(query.lstrip().startswith("DELETE FROM") for query, _ in cursor.statements))
        cleanup.assert_not_called()

    def test_shared_private_object_blocks_storage_deletion(self):
        cursor = SyntheticCursor(
            storage_paths=[STORAGE_PATH],
            shared_paths=[STORAGE_PATH],
        )
        body = {
            "motivo": "No borrar objeto compartido",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Archivos referenciados fuera", response.data["detail"])
        self.assertFalse(any(query.lstrip().startswith("DELETE FROM") for query, _ in cursor.statements))
        cleanup.assert_not_called()

    def test_origin_owned_support_with_null_resolution_is_not_shared(self):
        cursor = SyntheticCursor(storage_paths=[STORAGE_PATH])
        preview = _preview(cursor, RECONCILIATION_ID, self.scope)
        self.assertEqual(preview["bloqueos"], [])

        query = next(
            query for query, _ in cursor.statements
            if "WITH referencias(ruta,tipo,registro_id,conciliacion_id)" in query
        )
        self.assertIn("conciliacion_resolucion_id IS NOT NULL", query)
        self.assertIn(
            "conciliacion_origen_id IS NULL AND conciliacion_resolucion_id IS NULL",
            query,
        )
        self.assertNotIn("IS DISTINCT FROM", query)

    def test_repeated_support_references_in_target_conciliation_are_not_shared(self):
        cursor = SyntheticCursor(storage_paths=[STORAGE_PATH, STORAGE_PATH])
        preview = _preview(cursor, RECONCILIATION_ID, self.scope)
        self.assertEqual(preview["cantidades"]["archivos_privados"], 1)
        self.assertEqual(preview["bloqueos"], [])
        query = next(
            query for query, _ in cursor.statements
            if "WITH referencias(ruta,tipo,registro_id,conciliacion_id)" in query
        )
        self.assertIn("WHERE ref.conciliacion_id IS NULL OR ref.conciliacion_id<>%s", query)

    def test_external_support_preview_identifies_record_and_authorized_reconciliation(self):
        related_id = "40000000-0000-0000-0000-000000000004"
        cursor = SyntheticCursor(
            storage_paths=[STORAGE_PATH],
            shared_references=[(
                STORAGE_PATH, "Caso de depósito", related_id, "2026-10-01", False,
            )],
        )
        preview = _preview(cursor, RECONCILIATION_ID, self.scope)
        blocker = preview["bloqueos"][0]
        self.assertEqual(blocker["tipo"], "Archivos referenciados fuera de esta conciliación")
        self.assertEqual(blocker["cantidad"], 1)
        self.assertEqual(blocker["referencias"], [{
            "tipo_registro": "Caso de depósito",
            "cantidad": 1,
            "conciliacion_id": related_id,
            "periodo": "2026-10-01",
        }])
        self.assertNotIn("ruta", str(preview))
        query = next(
            query for query, _ in cursor.statements
            if "WITH referencias(ruta,tipo,registro_id,conciliacion_id)" in query
        )
        self.assertIn("account.empresa_id=%s AND account.pais_codigo=%s", query)

    def test_indeterminate_support_ownership_blocks_without_exposing_private_details(self):
        cursor = SyntheticCursor(
            storage_paths=[STORAGE_PATH],
            shared_references=[(
                STORAGE_PATH, "Evento de cheque", None, None, True,
            )],
        )
        preview = _preview(cursor, RECONCILIATION_ID, self.scope)
        blocker = preview["bloqueos"][0]
        self.assertEqual(blocker["tipo"], "Archivos con propiedad indeterminada")
        self.assertEqual(blocker["cantidad"], 1)
        self.assertEqual(blocker["referencias"], [{
            "tipo_registro": "Evento de cheque",
            "cantidad": 1,
            "indeterminada": True,
        }])
        self.assertNotIn(STORAGE_PATH, str(preview))
        body = {
            "motivo": "Propiedad sintética indeterminada",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 409)
        self.assertIn("propiedad indeterminada", response.data["detail"])
        self.assertFalse(any(query.lstrip().startswith("DELETE FROM") for query, _ in cursor.statements))
        cleanup.assert_not_called()

    def test_open_proposals_checks_and_deposits_are_not_cross_period_blockers(self):
        cursor = SyntheticCursor(storage_paths=[STORAGE_PATH])
        body = {
            "motivo": "Limpieza de conjunto sintético",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 200, response.data)
        deleted = [query for query, _ in cursor.statements if query.lstrip().startswith("DELETE FROM")]
        self.assertEqual(len(deleted), len(DELETE_ORDER))
        self.assertIn("contared.conciliaciones", deleted[len(DELETE_ORDER) - 1])
        self.assertTrue(any(
            "eliminar_auditoria_purga" in query
            for query, _ in cursor.statements
        ))
        self.assertTrue(any(
            query.startswith("INSERT INTO contared.eliminacion_conciliacion_storage")
            for query, _ in cursor.statements
        ))
        self.assertFalse(any("cuentas_bancarias" in query for query in deleted))
        self.assertFalse(any("bancos" in query or "empresas" in query for query in deleted))
        self.assertTrue(any("FOR UPDATE OF r" in query for query, _ in cursor.statements))
        cleanup.assert_called_once_with(RECONCILIATION_ID)

    def test_revoked_admin_access_is_rechecked_under_lock(self):
        cursor = SyntheticCursor(lock_missing=True)
        body = {
            "motivo": "Conjunto sintético",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 403)
        lock_query = next(query for query, _ in cursor.statements if "FOR UPDATE OF r" in query)
        self.assertIn("u.auth_user_id", lock_query)
        self.assertIn("a.rol='administrador'", lock_query)
        self.assertFalse(any(query.lstrip().startswith("DELETE FROM") for query, _ in cursor.statements))
        cleanup.assert_not_called()

    def test_double_click_after_commit_reuses_receipt_without_deleting_again(self):
        cursor = SyntheticCursor(lock_missing=True, existing_receipt=True)
        body = {
            "motivo": "Reintento sintético",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(query.lstrip().startswith("DELETE FROM") for query, _ in cursor.statements))
        cleanup.assert_called_once_with(RECONCILIATION_ID)

    def test_invalid_confirmation_phrase_cannot_start_database_deletes(self):
        cursor = SyntheticCursor()
        body = {
            "motivo": "Conjunto sintético",
            "frase_confirmacion": "ELIMINAR PRUEBA 1 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any(query.lstrip().startswith("DELETE FROM") for query, _ in cursor.statements))
        cleanup.assert_not_called()

    def test_intermediate_failure_rolls_back_all_database_deletes(self):
        cursor = SyntheticCursor(fail_on_delete="cruces")
        atomic = AtomicTracker()
        body = {
            "motivo": "Conjunto sintético para rollback",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        response, cleanup = self.invoke_delete(cursor, body=body, atomic=atomic)
        self.assertEqual(response.status_code, 500, response.data)
        self.assertTrue(atomic.rolled_back)
        cleanup.assert_not_called()

    def test_foreign_key_violation_rolls_back_and_logs_safe_database_diagnostics(self):
        failure = IntegrityError("synthetic FK rejection")
        failure.__cause__ = SyntheticPostgresError(
            "foreign key violation",
            sqlstate="23503",
            constraint="synthetic_cheque_event_fk",
        )
        cursor = SyntheticCursor(fail_on_delete="cruces", fail_error=failure)
        atomic = AtomicTracker()
        body = {
            "motivo": "Rollback por clave foránea sintética",
            "frase_confirmacion": f"ELIMINAR {str(RECONCILIATION_ID).upper()} 2026-09",
            "confirmar_irreversible": True,
        }
        with self.assertLogs("contared_api.conciliacion_eliminacion", level="ERROR") as logs:
            response, cleanup = self.invoke_delete(cursor, body=body, atomic=atomic)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(atomic.rolled_back)
        self.assertIn("stage=delete:cruces", logs.output[0])
        self.assertIn("sqlstate=23503", logs.output[0])
        self.assertIn("constraint=synthetic_cheque_event_fk", logs.output[0])
        self.assertNotIn("synthetic FK rejection", logs.output[0])
        cleanup.assert_not_called()

    def test_database_failures_are_not_all_reported_as_connection_errors(self):
        cases = (
            (SyntheticPostgresError("connection", sqlstate="08006"), DatabaseUnavailable, 503),
            (SyntheticPostgresError("foreign key", sqlstate="23503"), APIException, 409),
            (SyntheticPostgresError("permission", sqlstate="42501"), DeletionDatabasePermissionError, 500),
            (SyntheticPostgresError("bad query", sqlstate="42601"), DeletionDatabaseQueryError, 500),
            (DatabaseError("other database failure"), DeletionDatabaseOperationError, 500),
            (OperationalError("connection refused"), DatabaseUnavailable, 503),
            (ProgrammingError("query failure"), DeletionDatabaseQueryError, 500),
        )
        for failure, expected_type, expected_status in cases:
            with self.subTest(failure=type(failure).__name__, sqlstate=getattr(failure, "sqlstate", None)):
                with self.assertRaises(expected_type) as caught:
                    _raise_deletion_database_error(failure, "test_stage")
                self.assertEqual(caught.exception.status_code, expected_status)

    def test_non_administrator_cannot_even_request_preview(self):
        request = self.request("get")
        with patch(
            "contared_api.conciliacion_eliminacion._scope_for_reconciliation",
            return_value={**self.scope, "role": "revisor"},
        ), patch("contared_api.conciliacion_eliminacion._require_deletion_schema") as schema:
            response = ReconciliationDeletionView.as_view()(
                request,
                reconciliation_id=RECONCILIATION_ID,
            )
        self.assertEqual(response.status_code, 403)
        schema.assert_not_called()

    def test_storage_path_must_be_exclusive_to_the_selected_account_prefix(self):
        self.assertTrue(_allowed_storage_path(STORAGE_PATH, self.scope))
        self.assertFalse(
            _allowed_storage_path(
                f"imports/{COMPANY_ID}/{ACCOUNT_ID}/../other-account/file.xlsx",
                self.scope,
            )
        )
        self.assertFalse(
            _allowed_storage_path(
                f"imports/{COMPANY_ID}/another-account/file.xlsx",
                self.scope,
            )
        )

    def test_preview_counts_only_unique_private_objects_and_blocks_shared_files(self):
        cursor = SyntheticCursor(
            storage_paths=[STORAGE_PATH],
            shared_paths=[STORAGE_PATH],
        )
        preview = _preview(cursor, RECONCILIATION_ID, self.scope)
        self.assertEqual(preview["cantidades"]["archivos_privados"], 1)
        self.assertFalse(preview["libera_cuenta_periodo"])
        self.assertEqual(preview["bloqueos"][0]["cantidad"], 1)

    def test_migration_preserves_account_period_uniqueness_and_reusable_records(self):
        root = Path(__file__).resolve().parents[2]
        initial = (root / "supabase" / "migrations" / "202610270001_inicial.sql").read_text(
            encoding="utf-8"
        )
        migration = (
            root / "supabase" / "migrations"
            / "202610300004_eliminacion_definitiva_conciliaciones.sql"
        ).read_text(encoding="utf-8").upper()
        self.assertIn("UNIQUE (CUENTA_ID,PERIODO)", initial.upper())
        self.assertNotIn("DROP CONSTRAINT", migration)
        self.assertNotIn("DELETE FROM CONTARED.CUENTAS_BANCARIAS", migration)

    def test_cross_period_query_binds_every_reference_to_selected_id(self):
        cursor = SyntheticCursor(
            cross_period=[("Pendiente resuelto en otro período", "2026-10-01", 2)]
        )
        references = _cross_period_references(cursor, RECONCILIATION_ID)
        self.assertEqual(references[0]["periodo"], "2026-10-01")
        query, params = cursor.statements[0]
        self.assertEqual(query, CROSS_PERIOD_QUERY)
        self.assertEqual(len(params), query.count("%s"))


class StorageCleanupRetryTests(TestCase):
    class Cursor:
        def __init__(self, pending, result):
            self.pending = pending
            self.result = result
            self.query = ""
            self.updates = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query, params=None):
            self.query = query
            if query.startswith("UPDATE contared.eliminacion_conciliacion_storage"):
                self.updates.append((query, params))

        def fetchall(self):
            if "SELECT id::text,object_path" in self.query:
                return self.pending
            return []

        def fetchone(self):
            if "SELECT e.id::text,e.cantidades" in self.query:
                return self.result
            return None

    def test_storage_failure_is_persisted_and_later_retry_completes(self):
        cursor = self.Cursor(
            pending=[("40000000-0000-0000-0000-000000000004", STORAGE_PATH)],
            result=("50000000-0000-0000-0000-000000000005", {}, 1, 1, 0),
        )
        with (
            patch("contared_api.conciliacion_eliminacion.connection.cursor", return_value=cursor),
            patch(
                "contared_api.conciliacion_eliminacion._storage_request",
                side_effect=[ImportStorageUnavailable(), True],
            ) as storage,
        ):
            result = _retry_storage_cleanup(RECONCILIATION_ID)
        self.assertTrue(result["limpieza_completa"])
        self.assertEqual(result["archivos_pendientes"], 0)
        self.assertEqual(storage.call_count, 2)
        self.assertEqual(len(cursor.updates), 2)

    def test_three_failed_attempts_remain_verifiably_pending(self):
        cursor = self.Cursor(
            pending=[("40000000-0000-0000-0000-000000000004", STORAGE_PATH)],
            result=("50000000-0000-0000-0000-000000000005", {}, 1, 0, 1),
        )
        with (
            patch("contared_api.conciliacion_eliminacion.connection.cursor", return_value=cursor),
            patch(
                "contared_api.conciliacion_eliminacion._storage_request",
                side_effect=ImportStorageUnavailable(),
            ) as storage,
        ):
            result = _retry_storage_cleanup(RECONCILIATION_ID)
        self.assertFalse(result["limpieza_completa"])
        self.assertEqual(result["archivos_pendientes"], 1)
        self.assertEqual(storage.call_count, 3)
        self.assertEqual(len(cursor.updates), 3)
