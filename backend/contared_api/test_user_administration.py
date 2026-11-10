from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, override_settings
from django.db import IntegrityError
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseUser
from contared_api.cheques import (
    CLASSIFY_ROLES,
    READ_ROLES,
    REVIEW_ROLES,
    _require_role as require_cheque_role,
)
from contared_api.comparison import _require_role as require_comparison_role
from contared_api.conciliaciones import _writer
from contared_api.depositos import _role as require_deposit_role
from contared_api.direct_reconciliation import (
    DirectReconciliationDetailView,
    DirectReconciliationListCreateView,
    _job_path,
)
from contared_api.user_administration import (
    UserAdministrationDeleteView,
    UserAdministrationListInviteView,
    UserAdministrationPasswordResetView,
    UserAdministrationPermissionsView,
    UserAdministrationResendInvitationView,
    UserAdministrationTemporaryPasswordView,
    UserAdministrationUserView,
    PasswordSetupView,
    SupabaseAdminRequestError,
    _admin_request,
    _create_auth_user,
    _log_auth_failure,
    _provider_error_details,
    _resend_invitation,
    _safe_provider_text,
    _ensure_another_administrator,
    _scope,
    _validate_accesses,
)
from contared_api.session_access import load_session_access


ADMIN_SCOPE = {
    "user_id": str(uuid4()),
    "company_id": str(uuid4()),
    "country_code": "GT",
    "role": "administrador",
}


class UserAdministrationPolicyTests(SimpleTestCase):
    @override_settings(
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-test-key",
    )
    def test_auth_failure_preserves_status_code_and_sanitized_message(self):
        body = (
            b'{"code":"email_exists","message":"user@example.test already exists: '
            b'https://example.test/secret"}'
        )
        error = HTTPError(
            "https://project.supabase.co/auth/v1/admin/users",
            422,
            "Unprocessable Entity",
            {},
            BytesIO(body),
        )
        with patch("contared_api.user_administration.urlopen", side_effect=error):
            with self.assertRaises(SupabaseAdminRequestError) as raised:
                _admin_request("GET", "/admin/users?page=1&per_page=1")

        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(raised.exception.code, "email_exists")
        self.assertEqual(raised.exception.operation, "identity lookup")
        self.assertNotIn("user@example.test", raised.exception.message)
        self.assertNotIn("https://", raised.exception.message)
        with self.assertLogs("contared_api.user_administration", level="WARNING") as captured:
            _log_auth_failure(raised.exception)
        self.assertIn("HTTP 422", captured.output[0])
        self.assertIn("email_exists", captured.output[0])
        self.assertNotIn("user@example.test", captured.output[0])
        self.assertNotIn("https://", captured.output[0])

    @override_settings(
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-test-key",
        SUPABASE_AUTH_REDIRECT_URL="https://app.example.test/?establecer-clave=1",
    )
    def test_resend_uses_signup_resend_endpoint_and_preserves_redirect(self):
        with patch("contared_api.user_administration._admin_request", return_value={}) as request:
            _resend_invitation("pending@example.test")

        args = request.call_args.args
        self.assertEqual(args[0:2], ("POST", "/resend"))
        self.assertEqual(args[2]["type"], "signup")
        self.assertEqual(args[2]["email"], "pending@example.test")
        self.assertEqual(
            args[2]["options"]["emailRedirectTo"],
            "https://app.example.test/?establecer-clave=1",
        )

    @override_settings(
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-test-key",
    )
    def test_auth_identity_creation_uses_admin_api_and_confirms_email(self):
        auth_id = str(uuid4())
        with patch(
            "contared_api.user_administration._admin_request",
            return_value={
                "id": auth_id,
                "email": "persona@example.test",
                "email_confirmed_at": "2026-01-01T00:00:00Z",
            },
        ) as admin_request:
            identity = _create_auth_user(
                "persona@example.test",
                "Persona",
                "Temporary-Pass-42!",
            )

        self.assertEqual(identity["id"], auth_id)
        admin_request.assert_called_once_with(
            "POST",
            "/admin/users",
            {
                "email": "persona@example.test",
                "password": "Temporary-Pass-42!",
                "email_confirm": True,
                "user_metadata": {"nombre": "Persona"},
            },
        )

    @override_settings(
        SUPABASE_URL="https://project.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-test-key",
    )
    def test_auth_provider_error_never_returns_or_logs_password(self):
        password = "Temporary-Pass-42!"
        error = HTTPError(
            "https://project.supabase.co/auth/v1/admin/users",
            422,
            "Unprocessable Entity",
            {},
            BytesIO(
                (
                    '{"code":"weak_password","message":"Rejected '
                    + password
                    + '"}'
                ).encode(),
            ),
        )
        with patch("contared_api.user_administration.urlopen", side_effect=error):
            with self.assertRaises(SupabaseAdminRequestError) as raised:
                _admin_request(
                    "POST",
                    "/admin/users",
                    {"email": "persona@example.test", "password": password},
                )
        self.assertNotIn(password, raised.exception.message or "")
        with self.assertLogs("contared_api.user_administration", level="WARNING") as captured:
            _log_auth_failure(raised.exception)
        self.assertNotIn(password, captured.output[0])

    def test_guatemala_administrator_cannot_grant_el_salvador(self):
        with self.assertRaises(PermissionDenied):
            _validate_accesses(
                [{"pais_codigo": "SV", "rol": "consulta"}],
                {"GT"},
            )

    def test_single_country_access_is_valid(self):
        accesses = _validate_accesses(
            [{"pais_codigo": "GT", "rol": "auxiliar"}],
            {"GT"},
        )
        self.assertEqual(accesses, [("GT", "auxiliar", True)])

    def test_distinct_roles_can_be_assigned_to_both_authorized_countries(self):
        accesses = _validate_accesses(
            [
                {"pais_codigo": "GT", "rol": "auxiliar"},
                {"pais_codigo": "SV", "rol": "consulta"},
            ],
            {"GT", "SV"},
        )
        self.assertEqual(
            accesses,
            [("GT", "auxiliar", True), ("SV", "consulta", True)],
        )

    def test_last_administrator_cannot_be_removed(self):
        cursor = SimpleNamespace(
            execute=lambda query, params: None,
            fetchone=lambda: (0,),
        )
        with self.assertRaises(ValidationError):
            _ensure_another_administrator(cursor, ADMIN_SCOPE["company_id"], "GT", str(uuid4()))

    def test_administrator_can_be_removed_when_another_is_active(self):
        cursor = SimpleNamespace(
            execute=lambda query, params: None,
            fetchone=lambda: (1,),
        )
        _ensure_another_administrator(cursor, ADMIN_SCOPE["company_id"], "GT", str(uuid4()))

    @patch("contared_api.user_administration.scope_from_request")
    def test_only_administrators_can_open_user_management_api(self, mock_scope):
        mock_scope.return_value = {**ADMIN_SCOPE, "role": "auxiliar"}
        with self.assertRaises(PermissionDenied):
            _scope(SimpleNamespace(
                method="GET",
                query_params={"empresa_id": ADMIN_SCOPE["company_id"]},
            ))


class ActiveAccessSessionTests(SimpleTestCase):
    def test_next_protected_request_excludes_deactivated_country_access(self):
        cursor = _SessionAccessCursor()
        with (
            patch("contared_api.session_access.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.session_access.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            result = load_session_access(uuid4())

        self.assertEqual(len(result["accesos"]), 1)
        self.assertEqual(result["accesos"][0]["pais_codigo"], "GT")
        access_query = next(query for query, _ in cursor.statements if "FROM contared.accesos" in query)
        self.assertIn("a.activo IS TRUE", access_query)

    def test_required_password_change_blocks_protected_requests_but_is_returned_for_setup(self):
        cursor = _SessionAccessCursor(password_change_required=True)
        with (
            patch("contared_api.session_access.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.session_access.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            with self.assertRaises(PermissionDenied) as raised:
                load_session_access(uuid4())
            session = load_session_access(uuid4(), allow_password_change=True)

        self.assertEqual(raised.exception.get_codes(), "password_change_required")
        self.assertTrue(session["cambio_clave_obligatorio"])


class UserCreationEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_id = ADMIN_SCOPE["company_id"]
        self.admin_id = ADMIN_SCOPE["user_id"]
        self.target_id = str(uuid4())
        self.auth_id = str(uuid4())
        self.scope = {**ADMIN_SCOPE, "role": "administrador"}

    def post_user(self, *, accesses=None, user_id=None, password="Temporary-Pass-42!"):
        payload = {
            "empresa_id": self.company_id,
            "pais_codigo": "GT",
            "nombre": "Persona de prueba",
            "correo": "persona@example.test",
            "contrasena_inicial": password,
            "accesos": accesses or [{"pais_codigo": "GT", "rol": "auxiliar"}],
        }
        if user_id:
            payload["usuario_id"] = user_id
        request = self.factory.post(
            "/api/v1/administracion/usuarios/",
            payload,
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        return UserAdministrationListInviteView.as_view()(request)

    def run_with_cursor(
        self,
        profile=None,
        *,
        auth_id=None,
        auth_confirmed=False,
        creator=None,
        accesses=("GT",),
        access_roles=None,
        user_id=None,
        password="Temporary-Pass-42!",
    ):
        cursor = _InvitationCursor(
            company_id=self.company_id,
            profile=profile,
            target_id=self.target_id,
            allowed_countries=accesses,
        )
        auth_user = None if auth_id is None else {
            "id": auth_id,
            "email": "persona@example.test",
            "email_confirmed_at": "2026-01-01T00:00:00Z" if auth_confirmed else None,
        }
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration._find_auth_user", return_value=auth_user),
            patch("contared_api.user_administration._create_auth_user", side_effect=creator) as create_auth,
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = self.post_user(
                accesses=[
                    {
                        "pais_codigo": country,
                        "rol": (access_roles or {}).get(country, "consulta"),
                    }
                    for country in accesses
                ],
                user_id=user_id,
                password=password,
            )
        return response, cursor, create_auth

    def test_creates_confirmed_auth_identity_with_initial_password(self):
        response, cursor, create_auth = self.run_with_cursor(
            creator=lambda *args: {
                "id": self.auth_id,
                "email": "persona@example.test",
                "email_confirmed_at": "2026-01-01T00:00:00Z",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(cursor.inserted_profiles, 1)
        self.assertEqual(cursor.inserted_accesses, 1)
        create_auth.assert_called_once_with(
            "persona@example.test",
            "Persona de prueba",
            "Temporary-Pass-42!",
        )
        profile_insert = next(
            (query, params)
            for query, params in cursor.statements
            if "INSERT INTO contared.usuarios" in query
        )
        self.assertIn("cambio_clave_obligatorio", profile_insert[0])
        self.assertTrue(profile_insert[1][-1])
        self.assertFalse(any("auth.users" in query for query, _ in cursor.statements))
        self.assertNotIn("Temporary-Pass-42!", str(cursor.statements))
        self.assertEqual(response.data["estado_invitacion"], "no_requerida")
        self.assertTrue(response.data["cambio_clave_obligatorio"])

    def test_existing_email_reuses_identity_without_overwriting_password(self):
        response, cursor, create_auth = self.run_with_cursor(
            auth_id=self.auth_id,
            auth_confirmed=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cursor.inserted_profiles, 1)
        self.assertEqual(cursor.inserted_accesses, 1)
        create_auth.assert_not_called()
        self.assertIn("no se modificó la contraseña", response.data["mensaje"])

    def test_failed_profile_reuses_access_and_completes_missing_auth_identity(self):
        profile = (
            self.target_id,
            None,
            "persona@example.test",
            "fallida",
            "Persona de prueba",
            True,
        )
        response, cursor, create_auth = self.run_with_cursor(
            profile=profile,
            creator=lambda *args: {"id": self.auth_id, "email": "persona@example.test"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cursor.inserted_profiles, 0)
        self.assertEqual(cursor.inserted_accesses, 1)
        create_auth.assert_called_once()
        self.assertTrue(any("cambio_clave_obligatorio=TRUE" in query for query, _ in cursor.statements))

    def test_partial_auth_failure_keeps_profile_for_retry_without_duplicate(self):
        def fail_creation(*args):
            raise SupabaseAdminRequestError(503)

        response, cursor, create_auth = self.run_with_cursor(creator=fail_creation)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(cursor.inserted_profiles, 1)
        self.assertTrue(any("cambio_clave_obligatorio=TRUE" in query for query, _ in cursor.statements))
        create_auth.assert_called_once()

    def test_existing_identity_on_failed_profile_is_not_reset_automatically(self):
        profile = (
            self.target_id,
            self.auth_id,
            "persona@example.test",
            "fallida",
            "Persona de prueba",
            True,
        )
        response, cursor, create_auth = self.run_with_cursor(
            profile=profile,
            auth_id=self.auth_id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cursor.inserted_profiles, 0)
        self.assertEqual(cursor.inserted_accesses, 1)
        create_auth.assert_not_called()
        self.assertIn("Asignar contraseña temporal", response.data["mensaje"])

    def test_retry_after_auth_created_before_profile_link_reuses_identity(self):
        profile = (
            self.target_id,
            None,
            "persona@example.test",
            "fallida",
            "Persona de prueba",
            True,
        )
        response, cursor, create_auth = self.run_with_cursor(
            profile=profile,
            auth_id=self.auth_id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cursor.inserted_profiles, 0)
        self.assertEqual(cursor.inserted_accesses, 1)
        create_auth.assert_not_called()
        self.assertTrue(any("auth_user_id=COALESCE" in query for query, _ in cursor.statements))

    def test_all_authorized_country_roles_can_be_assigned_together(self):
        response, cursor, create_auth = self.run_with_cursor(
            creator=lambda *args: {"id": self.auth_id, "email": "persona@example.test"},
            accesses=("GT", "SV"),
            access_roles={"GT": "auxiliar", "SV": "consulta"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(cursor.inserted_accesses, 2)
        self.assertEqual(
            {(params[2], params[3]) for params in cursor.inserted_access_rows},
            {("GT", "auxiliar"), ("SV", "consulta")},
        )

    def test_non_authorized_country_is_rejected(self):
        request = self.factory.post(
            "/api/v1/administracion/usuarios/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "nombre": "Persona",
                "correo": "persona@example.test",
                "contrasena_inicial": "Temporary-Pass-42!",
                "accesos": [{"pais_codigo": "SV", "rol": "consulta"}],
            },
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _InvitationCursor(
            company_id=self.company_id,
            profile=None,
            target_id=self.target_id,
        )
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration._find_auth_user", return_value=None),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationListInviteView.as_view()(request)
        self.assertEqual(response.status_code, 403)

    def test_provider_error_parser_redacts_email_and_url(self):
        code, message = _provider_error_details(
            b'{"code":"email_exists","message":"user@example.test already exists: https://example.test/secret"}'
        )
        self.assertEqual(code, "email_exists")
        safe_message = _safe_provider_text(message)
        self.assertNotIn("user@example.test", safe_message)
        self.assertNotIn("https://", safe_message)
        self.assertIn("[email redacted]", safe_message)


class UserInvitationResendTests(SimpleTestCase):
    def test_existing_unconfirmed_invitation_resends_without_creating_identity(self):
        company_id = ADMIN_SCOPE["company_id"]
        user_id = str(uuid4())
        auth_id = str(uuid4())
        scope = {**ADMIN_SCOPE, "role": "administrador"}
        request = APIRequestFactory().post(
            f"/api/v1/administracion/usuarios/{user_id}/invitacion/",
            {"empresa_id": company_id, "pais_codigo": "GT"},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _ResendInvitationCursor(
            company_id=company_id,
            profile=(
                user_id,
                "pending@example.test",
                "Pending User",
                auth_id,
                "fallida",
                True,
            ),
        )
        with (
            patch("contared_api.user_administration._scope", return_value=scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch(
                "contared_api.user_administration._find_auth_user",
                return_value={"id": auth_id, "email": "pending@example.test"},
            ),
            patch("contared_api.user_administration._resend_invitation") as resend,
            patch("contared_api.user_administration._send_invitation") as invite,
            patch("contared_api.user_administration._save_invitation_state") as save_state,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationResendInvitationView.as_view()(request, user_id=user_id)

        self.assertEqual(response.status_code, 200)
        resend.assert_called_once_with("pending@example.test")
        invite.assert_not_called()
        self.assertEqual(save_state.call_args.kwargs["auth_user_id"], auth_id)
        self.assertEqual(save_state.call_args.kwargs["state"], "enviada")
        self.assertFalse(any("INSERT INTO contared.usuarios" in query for query, _ in cursor.statements))


class UserAccessUpdateEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_id = ADMIN_SCOPE["company_id"]
        self.user_id = str(uuid4())
        self.scope = {**ADMIN_SCOPE, "role": "administrador"}

    def patch_access(self, country, role, active, *, current, allowed=("GT",), admin_count=1):
        request = self.factory.patch(
            f"/api/v1/administracion/usuarios/{self.user_id}/accesos/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "accesos": [{"pais_codigo": country, "rol": role, "activo": active}],
            },
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _AccessUpdateCursor(
            company_id=self.company_id,
            user_id=self.user_id,
            current=current,
            allowed=allowed,
            admin_count=admin_count,
        )
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationPermissionsView.as_view()(request, user_id=self.user_id)
        return response, cursor

    def test_deactivating_guatemala_does_not_change_el_salvador_access(self):
        current = [("GT", "auxiliar", True), ("SV", "consulta", True)]
        response, cursor = self.patch_access(
            "GT",
            "auxiliar",
            False,
            current=current,
            allowed=("GT", "SV"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(cursor.access_updates), 1)
        self.assertEqual(tuple(cursor.access_updates[0][2:]), ("GT", "auxiliar", False))
        self.assertFalse(any("DELETE FROM contared.accesos" in query for query, _ in cursor.statements))

    def test_last_administrator_access_is_protected_inside_locked_transaction(self):
        response, cursor = self.patch_access(
            "GT",
            "consulta",
            False,
            current=[("GT", "administrador", True)],
            admin_count=0,
        )
        response.render()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(cursor.access_updates)
        company_lock = next(index for index, (query, _) in enumerate(cursor.statements) if "FROM contared.empresas" in query)
        admin_count = next(index for index, (query, _) in enumerate(cursor.statements) if "SELECT count(*)" in query)
        self.assertIn("FOR UPDATE", cursor.statements[company_lock][0])
        self.assertLess(company_lock, admin_count)

    def test_guatemala_administrator_cannot_update_el_salvador(self):
        response, cursor = self.patch_access(
            "SV",
            "consulta",
            True,
            current=[("GT", "auxiliar", True)],
            allowed=("GT",),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(cursor.access_updates)


class UserProfileEditTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_id = ADMIN_SCOPE["company_id"]
        self.user_id = str(uuid4())
        self.auth_id = str(uuid4())
        self.scope = {**ADMIN_SCOPE, "role": "administrador"}
        self.current_email = "justin@example.test"

    def patch_user(self, data, *, current=None, allowed=("GT",), has_outside=False, admin_count=1, duplicate=None):
        payload = {
            "empresa_id": self.company_id,
            "pais_codigo": "GT",
            "accesos": [{"pais_codigo": "GT", "rol": "auxiliar", "activo": True}],
            **data,
        }
        request = self.factory.patch(
            f"/api/v1/administracion/usuarios/{self.user_id}/",
            payload,
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _UserEditCursor(
            company_id=self.company_id,
            user_id=self.user_id,
            auth_id=self.auth_id,
            current=current or [("GT", "auxiliar", True)],
            allowed=allowed,
            has_outside=has_outside,
            admin_count=admin_count,
            duplicate=duplicate,
        )
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationUserView.as_view()(request, user_id=self.user_id)
        return response, cursor

    def test_name_and_email_update_and_pending_email_are_saved_separately(self):
        with patch(
            "contared_api.user_administration._get_auth_user",
            return_value={"id": self.auth_id, "email": self.current_email},
        ), patch(
            "contared_api.user_administration._update_auth_user_email",
            return_value={"id": self.auth_id, "email": self.current_email, "new_email": "nuevo@example.test"},
        ) as update_auth:
            response, cursor = self.patch_user({
                "nombre": "Justin Actualizado",
                "correo": "nuevo@example.test",
            })

        self.assertEqual(response.status_code, 200)
        update_auth.assert_called_once_with(self.auth_id, "nuevo@example.test")
        profile_update = next(params for query, params in cursor.statements if "UPDATE contared.usuarios" in query)
        self.assertEqual(profile_update[:3], ["Justin Actualizado", self.current_email, "nuevo@example.test"])
        self.assertEqual(str(profile_update[3]), self.user_id)
        self.assertEqual(response.data["correo"], self.current_email)
        self.assertEqual(response.data["correo_pendiente"], "nuevo@example.test")

    def test_name_only_update_does_not_read_or_update_auth_identity(self):
        with patch("contared_api.user_administration._get_auth_user") as get_auth, patch(
            "contared_api.user_administration._update_auth_user_email"
        ) as update_auth:
            response, cursor = self.patch_user({"nombre": "Nombre actualizado"})

        self.assertEqual(response.status_code, 200)
        get_auth.assert_not_called()
        update_auth.assert_not_called()
        profile_update = next(params for query, params in cursor.statements if "UPDATE contared.usuarios" in query)
        self.assertEqual(profile_update[0], "Nombre actualizado")

    def test_confirmed_email_update_moves_new_address_to_current_email(self):
        with patch(
            "contared_api.user_administration._get_auth_user",
            return_value={"id": self.auth_id, "email": self.current_email},
        ), patch(
            "contared_api.user_administration._update_auth_user_email",
            return_value={"id": self.auth_id, "email": "nuevo@example.test"},
        ):
            response, cursor = self.patch_user({"correo": "nuevo@example.test"})

        self.assertEqual(response.status_code, 200)
        profile_update = next(params for query, params in cursor.statements if "UPDATE contared.usuarios" in query)
        self.assertEqual(profile_update[1:3], ["nuevo@example.test", None])
        self.assertEqual(response.data["correo"], "nuevo@example.test")
        self.assertIsNone(response.data["correo_pendiente"])

    def test_recovering_missing_local_email_uses_linked_auth_identity(self):
        scope = self.scope
        cursor = _UsersListCursor(self.company_id, self.user_id, self.auth_id)
        with (
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch(
                "contared_api.user_administration._get_auth_user",
                return_value={"id": self.auth_id, "email": self.current_email},
            ) as get_auth,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            from contared_api.user_administration import _query_users

            users = _query_users(scope)

        self.assertEqual(users[0]["correo"], self.current_email)
        self.assertNotIn("auth_user_id", users[0])
        get_auth.assert_called_once_with(self.auth_id)

    def test_duplicate_email_is_rejected_without_calling_auth_update(self):
        with patch(
            "contared_api.user_administration._get_auth_user",
            return_value={"id": self.auth_id, "email": self.current_email},
        ), patch("contared_api.user_administration._update_auth_user_email") as update_auth:
            response, _ = self.patch_user({"correo": "existente@example.test"}, duplicate=True)

        self.assertEqual(response.status_code, 400)
        update_auth.assert_not_called()

    def test_auth_duplicate_email_is_reported_as_validation_error(self):
        with patch(
            "contared_api.user_administration._get_auth_user",
            return_value={"id": self.auth_id, "email": self.current_email},
        ), patch(
            "contared_api.user_administration._update_auth_user_email",
            side_effect=SupabaseAdminRequestError(422),
        ):
            response, cursor = self.patch_user({"correo": "existente@example.test"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("UPDATE contared.usuarios" in query for query, _ in cursor.statements))

    def test_auth_provider_failure_does_not_report_success_or_update_profile(self):
        with patch(
            "contared_api.user_administration._get_auth_user",
            side_effect=SupabaseAdminRequestError(503),
        ):
            response, cursor = self.patch_user({"correo": "nuevo@example.test"})

        self.assertEqual(response.status_code, 503)
        self.assertFalse(any("UPDATE contared.usuarios" in query for query, _ in cursor.statements))

    def test_auth_email_update_failure_does_not_update_local_profile(self):
        with patch(
            "contared_api.user_administration._get_auth_user",
            return_value={"id": self.auth_id, "email": self.current_email},
        ), patch(
            "contared_api.user_administration._update_auth_user_email",
            side_effect=SupabaseAdminRequestError(503),
        ):
            response, cursor = self.patch_user({"correo": "nuevo@example.test"})

        self.assertEqual(response.status_code, 503)
        self.assertFalse(any("UPDATE contared.usuarios" in query for query, _ in cursor.statements))

    def test_global_profile_fields_are_blocked_when_user_has_out_of_scope_access(self):
        with patch("contared_api.user_administration._get_auth_user") as get_auth:
            response, cursor = self.patch_user(
                {"nombre": "Cambio no permitido"},
                has_outside=True,
            )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(any("UPDATE contared.usuarios" in query for query, _ in cursor.statements))
        get_auth.assert_not_called()

    def test_access_updates_remain_available_when_global_profile_is_out_of_scope(self):
        response, cursor = self.patch_user({}, has_outside=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("INSERT INTO contared.accesos" in query for query, _ in cursor.statements))
        self.assertFalse(any("UPDATE contared.usuarios" in query for query, _ in cursor.statements))

    def test_self_edit_cannot_remove_the_last_active_administrator(self):
        request = self.factory.patch(
            f"/api/v1/administracion/usuarios/{self.scope['user_id']}/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "accesos": [{"pais_codigo": "GT", "rol": "consulta", "activo": False}],
            },
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _UserEditCursor(
            company_id=self.company_id,
            user_id=self.scope["user_id"],
            auth_id=self.auth_id,
            current=[("GT", "administrador", True)],
            allowed=("GT",),
            has_outside=False,
            admin_count=0,
            duplicate=None,
        )
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationUserView.as_view()(request, user_id=self.scope["user_id"])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("INSERT INTO contared.accesos" in query for query, _ in cursor.statements))


class UserDeletionEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_id = ADMIN_SCOPE["company_id"]
        self.admin_id = ADMIN_SCOPE["user_id"]
        self.user_id = str(uuid4())
        self.scope = {**ADMIN_SCOPE, "role": "administrador"}

    def call_endpoint(
        self,
        method,
        *,
        cursor,
        action=None,
        countries=None,
        user_id=None,
        confirmed=True,
    ):
        target_id = user_id or self.user_id
        url = (
            f"/api/v1/administracion/usuarios/{target_id}/eliminacion/"
            f"?empresa_id={self.company_id}&pais_codigo=GT"
        )
        if method == "GET":
            request = self.factory.get(url)
        else:
            request = self.factory.delete(
                url,
                {"accion": action, "confirmar": confirmed, **({"paises": countries} if countries is not None else {})},
                format="json",
            )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch(
                "contared_api.user_administration.transaction.atomic",
                side_effect=lambda: nullcontext(),
            ),
            patch("contared_api.user_administration.connection") as mock_connection,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            mock_connection.ops.quote_name.side_effect = lambda name: f'"{name}"'
            response = UserAdministrationDeleteView.as_view()(request, user_id=target_id)
        return response

    def test_pending_profile_without_auth_or_history_can_be_removed(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "auxiliar", True)],
            admin_scopes={(self.company_id, "GT")},
        )

        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("DELETE FROM contared.usuarios" in query for query, _ in cursor.statements))
        self.assertFalse(any("DELETE FROM auth.users" in query for query, _ in cursor.statements))

    def test_unlinked_auth_identity_is_found_by_email_and_deleted(self):
        auth_id = str(uuid4())
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "auxiliar", True)],
            admin_scopes={(self.company_id, "GT")},
            auth_candidates=[(auth_id, False)],
        )
        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")

        self.assertEqual(response.status_code, 200)
        self.assertIn(("DELETE FROM auth.users WHERE id=%s", [auth_id]), cursor.statements)

    def test_email_linked_to_another_profile_blocks_permanent_deletion(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "auxiliar", True)],
            admin_scopes={(self.company_id, "GT")},
            auth_candidates=[(str(uuid4()), True)],
        )
        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any("DELETE FROM" in query for query, _ in cursor.statements))

    def test_deletes_identity_and_inactive_accesses_but_preserves_attribution(self):
        auth_id = str(uuid4())
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            auth_id,
            [("GT", "auxiliar", False)],
            admin_scopes={(self.company_id, "GT")},
            references=[("contared", "conciliaciones", "usuario_id")],
            reference_count=1,
        )
        preview = self.call_endpoint("GET", cursor=cursor)
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.data["puede_eliminar_perfil"])
        self.assertTrue(preview.data["referencias_historicas"])
        self.assertNotIn("_auth_user_id", preview.data)

        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")
        self.assertEqual(response.status_code, 200)
        self.assertIn(("DELETE FROM auth.users WHERE id=%s", [auth_id]), cursor.statements)
        self.assertTrue(any("DELETE FROM contared.accesos" in query for query, _ in cursor.statements))
        self.assertTrue(any("SET activo=FALSE, auth_user_id=NULL, correo=NULL" in query for query, _ in cursor.statements))
        self.assertFalse(any("DELETE FROM contared.usuarios" in query for query, _ in cursor.statements))

    def test_requires_explicit_confirmation(self):
        response = self.call_endpoint("DELETE", cursor=None, action="eliminar_perfil", confirmed=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirmar", response.data)

    def test_country_retirement_preserves_access_outside_admin_scope(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "auxiliar", True), ("SV", "consulta", True)],
            admin_scopes={(self.company_id, "GT")},
        )
        denied = self.call_endpoint(
            "DELETE",
            cursor=cursor,
            action="eliminar_perfil",
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(any("DELETE FROM contared.usuarios" in query for query, _ in cursor.statements))

        response = self.call_endpoint(
            "DELETE",
            cursor=cursor,
            action="retirar_accesos",
            countries=["GT"],
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("DELETE FROM contared.usuarios" in query for query, _ in cursor.statements))
        update = next(params for query, params in cursor.statements if "UPDATE contared.accesos" in query)
        self.assertEqual(update[-1], ["GT"])

        denied = self.call_endpoint(
            "DELETE",
            cursor=cursor,
            action="retirar_accesos",
            countries=["SV"],
        )
        self.assertEqual(denied.status_code, 403)

    def test_last_active_administrator_cannot_be_removed(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "administrador", True)],
            admin_scopes={(self.company_id, "GT")},
            admin_count=0,
        )
        response = self.call_endpoint(
            "DELETE",
            cursor=cursor,
            action="retirar_accesos",
            countries=["GT"],
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("UPDATE contared.accesos" in query for query, _ in cursor.statements))

    def test_last_active_administrator_profile_cannot_be_deleted(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "administrador", True)],
            admin_scopes={(self.company_id, "GT")},
            admin_count=0,
        )
        response = self.call_endpoint(
            "DELETE",
            cursor=cursor,
            action="eliminar_perfil",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("DELETE FROM contared.usuarios" in query for query, _ in cursor.statements))

    def test_self_deletion_is_rejected(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.admin_id,
            None,
            [("GT", "administrador", True)],
            admin_scopes={(self.company_id, "GT")},
        )
        response = self.call_endpoint(
            "DELETE",
            cursor=cursor,
            action="eliminar_perfil",
            user_id=self.admin_id,
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any("DELETE FROM contared.usuarios" in query for query, _ in cursor.statements))

    def test_identity_linked_during_deletion_is_protected(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            None,
            [("GT", "auxiliar", True)],
            admin_scopes={(self.company_id, "GT")},
            auth_candidates=[(str(uuid4()), False)],
            concurrently_linked=True,
        )
        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any("DELETE FROM" in query for query, _ in cursor.statements))

    def test_auth_constraint_failure_returns_a_conflict(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            str(uuid4()),
            [("GT", "auxiliar", True)],
            admin_scopes={(self.company_id, "GT")},
            auth_deletion_error=True,
        )
        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")
        self.assertEqual(response.status_code, 409)
        self.assertIn("se conservaron", str(response.data["detail"]))

    def test_owned_files_block_permanent_deletion(self):
        cursor = _UserDeletionCursor(
            self.company_id,
            self.user_id,
            str(uuid4()),
            [("GT", "auxiliar", True)],
            admin_scopes={(self.company_id, "GT")},
            owns_files=True,
        )
        response = self.call_endpoint("DELETE", cursor=cursor, action="eliminar_perfil")
        self.assertEqual(response.status_code, 409)
        self.assertIn("archivos", str(response.data["detail"]))
        self.assertFalse(any("DELETE FROM auth.users" in query for query, _ in cursor.statements))


class PasswordResetEndpointTests(SimpleTestCase):
    def test_password_reset_uses_current_auth_email_and_official_recovery_api(self):
        company_id = ADMIN_SCOPE["company_id"]
        user_id = str(uuid4())
        auth_id = str(uuid4())
        scope = {**ADMIN_SCOPE, "role": "administrador"}
        request = APIRequestFactory().post(
            f"/api/v1/administracion/usuarios/{user_id}/restablecer-clave/",
            {"empresa_id": company_id, "pais_codigo": "GT"},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _PasswordResetCursor(company_id, auth_id)
        with (
            patch("contared_api.user_administration._scope", return_value=scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch(
                "contared_api.user_administration._get_auth_user",
                return_value={"id": auth_id, "email": "justin@example.test", "new_email": "pending@example.test"},
            ),
            patch("contared_api.user_administration._send_password_recovery") as send_recovery,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationPasswordResetView.as_view()(request, user_id=user_id)

        self.assertEqual(response.status_code, 200)
        send_recovery.assert_called_once_with("justin@example.test")

    def test_password_reset_is_blocked_for_a_profile_with_out_of_scope_access(self):
        company_id = ADMIN_SCOPE["company_id"]
        user_id = str(uuid4())
        auth_id = str(uuid4())
        scope = {**ADMIN_SCOPE, "role": "administrador"}
        request = APIRequestFactory().post(
            f"/api/v1/administracion/usuarios/{user_id}/restablecer-clave/",
            {"empresa_id": company_id, "pais_codigo": "GT"},
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        cursor = _PasswordResetCursor(company_id, auth_id, has_outside=True)
        with (
            patch("contared_api.user_administration._scope", return_value=scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch("contared_api.user_administration._get_auth_user") as get_auth,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationPasswordResetView.as_view()(request, user_id=user_id)

        self.assertEqual(response.status_code, 403)
        get_auth.assert_not_called()


@override_settings(
    SUPABASE_ISSUER="https://project.supabase.co/auth/v1",
    SUPABASE_JWKS_URL="https://project.supabase.co/auth/v1/.well-known/jwks.json",
)
class PasswordSetupEndpointTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.factory = APIRequestFactory()
        self.auth_id = uuid4()
        self.profile_id = str(uuid4())
        self.password = "Temporary-Pass-42!"
        self.new_password = "New-Passphrase-53!"

    def make_access_token(self):
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "sub": str(self.auth_id),
                "iss": "https://project.supabase.co/auth/v1",
                "aud": "authenticated",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def post_password_setup(self, current_password=None, new_password=None, *, authenticated=True):
        request = self.factory.post(
            "/api/v1/session/password/",
            {
                "contrasena_actual": current_password or self.password,
                "nueva_contrasena": new_password or self.new_password,
            },
            format="json",
        )
        if authenticated:
            request.META["HTTP_AUTHORIZATION"] = f"Bearer {self.make_access_token()}"
        with patch("contared_api.authentication.get_jwks_client") as get_jwks_client:
            get_jwks_client.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(
                key=self.public_key,
            )
            return PasswordSetupView.as_view()(request)

    def execute_setup(self, *, password_matches=True, rowcount=1, update_error=False):
        cursor = _PasswordSetupCursor(
            profile_id=self.profile_id,
            email="persona@example.test",
            rowcount=rowcount,
        )
        with (
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch(
                "contared_api.user_administration._get_auth_user",
                return_value={"id": str(self.auth_id), "email": "persona@example.test"},
            ),
            patch(
                "contared_api.user_administration._verify_auth_password",
                return_value=password_matches,
            ) as verify_password,
            patch(
                "contared_api.user_administration._set_auth_password",
                side_effect=(
                    SupabaseAdminRequestError(status=503, operation="password update")
                    if update_error else None
                ),
            ) as set_password,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = self.post_password_setup()
        return response, cursor, verify_password, set_password

    def test_password_change_requires_a_bearer_token(self):
        response = self.post_password_setup(authenticated=False)

        self.assertEqual(response.status_code, 401)

    def test_temporary_password_cannot_be_reused(self):
        response = self.post_password_setup(self.password, self.password)
        self.assertEqual(response.status_code, 400)
        self.assertIn("distinta", str(response.data["nueva_contrasena"]))

    def test_wrong_current_password_does_not_update_auth_or_clear_flag(self):
        response, cursor, verify_password, set_password = self.execute_setup(password_matches=False)
        self.assertEqual(response.status_code, 400)
        verify_password.assert_called_once_with(
            "persona@example.test",
            self.password,
            self.auth_id,
        )
        set_password.assert_not_called()
        self.assertFalse(any("SET cambio_clave_obligatorio=FALSE" in query for query, _ in cursor.statements))

    def test_verified_auth_update_clears_flag_and_returns_no_password(self):
        response, cursor, verify_password, set_password = self.execute_setup()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["cambio_clave_obligatorio"])
        set_password.assert_called_once_with(self.auth_id, self.new_password)
        self.assertTrue(any("SET cambio_clave_obligatorio=FALSE" in query for query, _ in cursor.statements))
        self.assertNotIn(self.password, str(response.data))
        self.assertNotIn(self.new_password, str(response.data))
        self.assertNotIn(self.password, str(cursor.statements))
        self.assertNotIn(self.new_password, str(cursor.statements))

    def test_auth_success_with_database_failure_keeps_change_required(self):
        response, cursor, _, set_password = self.execute_setup(rowcount=0)
        self.assertEqual(response.status_code, 503)
        set_password.assert_called_once()
        self.assertNotIn(self.new_password, str(response.data))
        self.assertTrue(any("SET cambio_clave_obligatorio=FALSE" in query for query, _ in cursor.statements))

    def test_auth_password_update_failure_keeps_required_change_flag(self):
        response, cursor, _, set_password = self.execute_setup(update_error=True)

        self.assertEqual(response.status_code, 503)
        set_password.assert_called_once_with(self.auth_id, self.new_password)
        self.assertFalse(any("SET cambio_clave_obligatorio=FALSE" in query for query, _ in cursor.statements))


class TemporaryPasswordAssignmentTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_id = ADMIN_SCOPE["company_id"]
        self.user_id = str(uuid4())
        self.auth_id = str(uuid4())
        self.password = "Temporary-Pass-42!"
        self.scope = {**ADMIN_SCOPE, "role": "administrador"}

    def post_assignment(self):
        request = self.factory.post(
            f"/api/v1/administracion/usuarios/{self.user_id}/clave-temporal/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "contrasena_temporal": self.password,
            },
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        return UserAdministrationTemporaryPasswordView.as_view()(request, user_id=self.user_id)

    def run_assignment(self, *, has_outside=False, auth_id=None):
        cursor = _TemporaryPasswordCursor(
            company_id=self.company_id,
            auth_id=auth_id,
            user_id=self.user_id,
            has_outside=has_outside,
        )
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch(
                "contared_api.user_administration._get_auth_user",
                return_value={"id": self.auth_id, "email": "persona@example.test"},
            ) as get_auth,
            patch("contared_api.user_administration._set_auth_password") as set_password,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = self.post_assignment()
        return response, cursor, get_auth, set_password

    def test_temporary_password_assignment_requires_admin_scope(self):
        response, cursor, get_auth, set_password = self.run_assignment(
            has_outside=True,
            auth_id=self.auth_id,
        )
        self.assertEqual(response.status_code, 403)
        get_auth.assert_not_called()
        set_password.assert_not_called()
        self.assertFalse(any("cambio_clave_obligatorio=TRUE" in query for query, _ in cursor.statements))

    def test_explicit_temporary_assignment_sets_auth_and_required_flag(self):
        response, cursor, _, set_password = self.run_assignment(auth_id=self.auth_id)
        self.assertEqual(response.status_code, 200)
        set_password.assert_called_once_with(self.auth_id, self.password, confirm_email=True)
        self.assertTrue(any("cambio_clave_obligatorio=TRUE" in query for query, _ in cursor.statements))
        self.assertNotIn(self.password, str(response.data))
        self.assertNotIn(self.password, str(cursor.statements))

    def test_profile_without_auth_identity_must_use_account_creation(self):
        cursor = _TemporaryPasswordCursor(
            company_id=self.company_id,
            auth_id=None,
            user_id=self.user_id,
        )
        request = self.factory.post(
            f"/api/v1/administracion/usuarios/{self.user_id}/clave-temporal/",
            {
                "empresa_id": self.company_id,
                "pais_codigo": "GT",
                "contrasena_temporal": self.password,
            },
            format="json",
        )
        force_authenticate(request, user=SupabaseUser(uuid4()))
        with (
            patch("contared_api.user_administration._scope", return_value=self.scope),
            patch("contared_api.user_administration.transaction.atomic", return_value=nullcontext()),
            patch("contared_api.user_administration.connection") as mock_connection,
            patch("contared_api.user_administration._find_auth_user", return_value=None),
            patch("contared_api.user_administration._set_auth_password") as set_password,
        ):
            mock_connection.cursor.return_value.__enter__.return_value = cursor
            response = UserAdministrationTemporaryPasswordView.as_view()(
                request,
                user_id=self.user_id,
            )
        self.assertEqual(response.status_code, 409)
        set_password.assert_not_called()


class _UserDeletionCursor:
    def __init__(
        self,
        company_id,
        user_id,
        auth_id,
        accesses,
        *,
        admin_scopes,
        references=(),
        reference_count=0,
        admin_count=1,
        auth_candidates=(),
        owns_files=False,
        concurrently_linked=False,
        auth_deletion_error=False,
    ):
        self.company_id = company_id
        self.user_id = str(user_id)
        self.auth_id = auth_id
        self.accesses = accesses
        self.admin_scopes = list(admin_scopes)
        self.references = list(references)
        self.reference_count = reference_count
        self.admin_count = admin_count
        self.auth_candidates = list(auth_candidates)
        self.owns_files = owns_files
        self.concurrently_linked = concurrently_linked
        self.auth_deletion_error = auth_deletion_error
        self.row = None
        self.rows = []
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query and "JOIN contared.usuarios u" in query:
            self.rows = [(country,) for company_id, country in self.admin_scopes if company_id == self.company_id]
        elif "SELECT u.id::text,u.auth_user_id::text" in query:
            self.row = (self.user_id, self.auth_id, "usuario@example.test")
        elif "SELECT u.id::text,u.nombre,u.correo,u.auth_user_id::text" in query:
            self.row = (self.user_id, "Usuario de prueba", "usuario@example.test", self.auth_id)
        elif "SELECT empresa_id::text FROM contared.accesos" in query:
            self.rows = [(self.company_id,) for _ in self.accesses]
        elif "SELECT a.empresa_id::text,e.nombre,a.pais_codigo,p.nombre,a.rol,a.activo" in query:
            self.rows = [
                (self.company_id, "Empresa de prueba", country, country, role, active)
                for country, role, active in self.accesses
            ]
        elif "SELECT a.empresa_id::text,a.pais_codigo" in query:
            self.rows = self.admin_scopes
        elif "FROM auth.users au" in query:
            self.rows = self.auth_candidates
        elif "FROM auth.users WHERE id=%s FOR UPDATE" in query:
            self.row = (True,)
        elif "FROM contared.usuarios WHERE auth_user_id=%s" in query:
            self.row = (self.concurrently_linked,)
        elif "DELETE FROM auth.users" in query and self.auth_deletion_error:
            raise IntegrityError()
        elif "FROM storage.objects" in query:
            self.row = (self.owns_files,)
        elif "FROM pg_constraint fk" in query:
            self.rows = self.references
        elif "SELECT count(*)" in query and "JOIN contared.usuarios u" in query:
            self.row = (self.admin_count,)
        elif "SELECT count(*) FROM" in query:
            self.row = (self.reference_count,)
        elif "SELECT pais_codigo,rol,activo" in query and "FROM contared.accesos" in query:
            countries = params[2]
            self.rows = [
                (country, role, active)
                for country, role, active in self.accesses
                if country in countries
            ]
        elif "SELECT empresa_id::text,pais_codigo,rol,activo" in query:
            self.rows = [
                (self.company_id, country, role, active)
                for country, role, active in self.accesses
            ]

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _UserEditCursor:
    def __init__(
        self,
        *,
        company_id,
        user_id,
        auth_id,
        current,
        allowed,
        has_outside,
        admin_count,
        duplicate,
    ):
        self.company_id = company_id
        self.user_id = user_id
        self.auth_id = auth_id
        self.current = current
        self.allowed = [(country,) for country in allowed]
        self.has_outside = has_outside
        self.admin_count = admin_count
        self.duplicate = duplicate
        self.row = None
        self.rows = []
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = self.allowed
        elif "SELECT u.id::text,u.auth_user_id::text,u.correo" in query:
            self.row = (
                self.user_id,
                self.auth_id,
                "justin@example.test",
                None,
                "Justin",
                True,
            )
        elif "SELECT EXISTS (" in query:
            self.row = (self.has_outside,)
        elif "SELECT pais_codigo,rol,activo" in query:
            self.rows = self.current
        elif "SELECT id FROM contared.usuarios" in query and "id<>%s" in query:
            self.row = (str(uuid4()),) if self.duplicate else None
        elif "SELECT count(*)" in query:
            self.row = (self.admin_count,)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _UsersListCursor:
    def __init__(self, company_id, user_id, auth_id):
        self.company_id = company_id
        self.user_id = user_id
        self.auth_id = auth_id
        self.row = None
        self.rows = []

    def execute(self, query, params=None):
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = [("GT",)]
        elif "SELECT u.id::text,u.nombre,u.correo,u.activo" in query:
            self.rows = [(
                self.user_id,
                "Justin",
                None,
                True,
                "no_requerida",
                None,
                self.auth_id,
                None,
                False,
                True,
                "GT",
                "Guatemala",
                "auxiliar",
                True,
            )]

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _PasswordResetCursor:
    def __init__(self, company_id, auth_id, *, has_outside=False):
        self.company_id = company_id
        self.auth_id = auth_id
        self.has_outside = has_outside
        self.row = None
        self.rows = []

    def execute(self, query, params=None):
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = [("GT",)]
        elif "SELECT u.auth_user_id::text,u.correo," in query:
            self.row = (self.auth_id, "stale@example.test", not self.has_outside)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _ResendInvitationCursor:
    def __init__(self, *, company_id, profile):
        self.company_id = company_id
        self.profile = profile
        self.row = None
        self.rows = []
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = [("GT",)]
        elif "SELECT u.id::text,u.correo,u.nombre,u.auth_user_id::text" in query:
            self.row = self.profile

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _InvitationCursor:
    def __init__(self, *, company_id, profile, target_id, allowed_countries=("GT",)):
        self.company_id = company_id
        self.profile = profile
        self.target_id = target_id
        self.allowed_countries = allowed_countries
        self.row = None
        self.rows = []
        self.rowcount = 1
        self.statements = []
        self.inserted_profiles = 0
        self.inserted_accesses = 0
        self.inserted_access_rows = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = [(country,) for country in self.allowed_countries]
        elif "SELECT id::text,auth_user_id::text,correo,estado_invitacion,nombre" in query:
            self.row = self.profile
        elif "SELECT NOT EXISTS (" in query:
            self.row = (True,)
        elif "INSERT INTO contared.usuarios" in query:
            self.inserted_profiles += 1
            self.row = (self.target_id,)
        elif "INSERT INTO contared.accesos" in query:
            self.inserted_accesses += 1
            self.inserted_access_rows.append(params)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _PasswordSetupCursor:
    def __init__(self, *, profile_id, email, rowcount=1):
        self.profile_id = profile_id
        self.email = email
        self.rowcount = rowcount
        self.row = None
        self.rows = []
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id::text,correo,cambio_clave_obligatorio,activo" in query:
            self.row = (self.profile_id, self.email, True, True)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _TemporaryPasswordCursor:
    def __init__(self, *, company_id, auth_id, user_id, has_outside=False):
        self.company_id = company_id
        self.auth_id = auth_id
        self.user_id = user_id
        self.has_outside = has_outside
        self.row = None
        self.rows = []
        self.rowcount = 1
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = [("GT",)]
        elif "SELECT u.auth_user_id::text,u.correo," in query:
            self.row = (self.auth_id, "persona@example.test", not self.has_outside)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _SessionAccessCursor:
    def __init__(self, *, password_change_required=False):
        self.row = None
        self.rows = []
        self.statements = []
        self.password_change_required = password_change_required

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id::text, nombre, activo" in query:
            self.row = (
                str(uuid4()),
                "Administradora",
                True,
                self.password_change_required,
            )
        elif "FROM contared.accesos" in query:
            self.rows = [("company-gt", "Empresa GT", "GT", "Guatemala", "administrador")]

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _AccessUpdateCursor:
    def __init__(self, *, company_id, user_id, current, allowed, admin_count):
        self.company_id = company_id
        self.user_id = user_id
        self.current = current
        self.allowed = [(country,) for country in allowed]
        self.admin_count = admin_count
        self.row = None
        self.rows = []
        self.statements = []
        self.access_updates = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self.row = None
        self.rows = []
        if "SELECT id FROM contared.empresas" in query:
            self.row = (self.company_id,)
        elif "SELECT a.pais_codigo" in query:
            self.rows = self.allowed
        elif "SELECT id,activo FROM contared.usuarios" in query:
            self.row = (self.user_id, True)
        elif "SELECT pais_codigo,rol,activo" in query:
            self.rows = self.current
        elif "SELECT count(*)" in query:
            self.row = (self.admin_count,)
        elif "INSERT INTO contared.accesos" in query:
            self.access_updates.append(params)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class ExistingRoleMatrixTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.company_id = str(uuid4())
        self.user_id = str(uuid4())

    def test_saved_data_is_readable_for_all_four_roles(self):
        for role in ("administrador", "auxiliar", "revisor", "consulta"):
            with self.subTest(role=role), patch(
                "contared_api.direct_reconciliation.scope_from_request",
                return_value={
                    "role": role,
                    "company_id": self.company_id,
                    "country_code": "GT",
                    "user_id": self.user_id,
                },
            ), patch("contared_api.direct_reconciliation._scope_jobs_path") as jobs_path:
                jobs_path.return_value.exists.return_value = False
                request = self.factory.get(
                    "/api/v1/conciliaciones-directas/trabajos/",
                    {"empresa_id": self.company_id, "pais_codigo": "GT"},
                )
                force_authenticate(request, user=SupabaseUser(uuid4()))
                response = DirectReconciliationListCreateView.as_view()(request)
                self.assertEqual(response.status_code, 200)

    def test_company_history_resolves_jobs_created_by_other_users_in_same_scope(self):
        job_id = str(uuid4())
        scope = {
            "country_code": "GT",
            "company_id": self.company_id,
            "user_id": self.user_id,
        }
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "GT" / self.company_id / str(uuid4()) / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "metadata.json").write_text(
                json.dumps({"empresa_id": self.company_id, "pais_codigo": "GT"}),
                encoding="utf-8",
            )
            with patch("contared_api.direct_reconciliation._storage_root", return_value=Path(temporary)):
                self.assertEqual(_job_path(scope, job_id), job_dir)

    def test_direct_analysis_is_limited_to_admin_and_accounting_auxiliary(self):
        for role, expected_status in (
            ("administrador", 400),
            ("auxiliar", 400),
            ("revisor", 403),
            ("consulta", 403),
        ):
            with self.subTest(role=role), patch(
                "contared_api.direct_reconciliation.scope_from_request",
                return_value={
                    "role": role,
                    "company_id": self.company_id,
                    "country_code": "GT",
                    "user_id": self.user_id,
                },
            ):
                request = self.factory.post(
                    "/api/v1/conciliaciones-directas/trabajos/",
                    {"empresa_id": self.company_id, "pais_codigo": "GT"},
                    format="multipart",
                )
                force_authenticate(request, user=SupabaseUser(uuid4()))
                response = DirectReconciliationListCreateView.as_view()(request)
                self.assertEqual(response.status_code, expected_status)
                if expected_status == 403:
                    self.assertEqual(response.data["code"], "writer_required")

    def test_only_administrators_can_permanently_delete_direct_jobs(self):
        for role, expected_status in (
            ("administrador", 404),
            ("auxiliar", 403),
            ("revisor", 403),
            ("consulta", 403),
        ):
            with self.subTest(role=role), patch(
                "contared_api.direct_reconciliation.scope_from_request",
                return_value={
                    "role": role,
                    "company_id": self.company_id,
                    "country_code": "GT",
                    "user_id": self.user_id,
                },
            ):
                job_id = uuid4()
                request = self.factory.delete(
                    f"/api/v1/conciliaciones-directas/trabajos/{job_id}/"
                    f"?empresa_id={self.company_id}&pais_codigo=GT",
                )
                force_authenticate(request, user=SupabaseUser(uuid4()))
                with patch(
                    "contared_api.direct_reconciliation._job_path",
                    return_value=Path("missing-direct-job"),
                ):
                    response = DirectReconciliationDetailView.as_view()(
                        request,
                        job_id=job_id,
                    )
                self.assertEqual(response.status_code, expected_status)

    def test_other_role_guards_match_read_write_and_review_matrix(self):
        for role in ("administrador", "auxiliar"):
            _writer({"role": role})
            require_deposit_role({"role": role}, ("administrador", "auxiliar"))
        for role in ("revisor", "consulta"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                _writer({"role": role})
        for role in READ_ROLES:
            require_cheque_role({"role": role}, READ_ROLES)
        for role in ("revisor", "consulta"):
            with self.assertRaises(PermissionDenied):
                require_cheque_role({"role": role}, CLASSIFY_ROLES)
        for role in ("auxiliar", "consulta"):
            with self.assertRaises(PermissionDenied):
                require_cheque_role({"role": role}, REVIEW_ROLES)
        require_comparison_role({"role": "revisor"}, ("administrador", "auxiliar", "revisor"))
        with self.assertRaises(PermissionDenied):
            require_comparison_role({"role": "consulta"}, ("administrador", "auxiliar", "revisor"))
