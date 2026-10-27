from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from unittest.mock import patch
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import DatabaseError
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from contared_api.authentication import SupabaseJWTAuthentication, SupabaseUser
from contared_api.reconciliation import Movement, propose_reconciliation
from contared_api.session_access import resolve_authorized_scope, require_administrator
from contared_api.views import ApiStatusView, SessionView
from contared_api.banking import AccountDetailView, BankDetailView


AUTH_USER_ID = uuid4()
INTERNAL_USER_ID = "f42522e0-8d61-45d2-8b17-4cd7da7d7a01"


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
            profile=(INTERNAL_USER_ID, "Administradora", True),
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
            profile=(INTERNAL_USER_ID, "Auxiliar", True),
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
            profile=(INTERNAL_USER_ID, "Administradora", False),
        )

        self.assert_session_forbidden(cursor, "profile_inactive")
        self.assertEqual(len(cursor.statements), 2)

    def test_session_rejects_profile_without_active_authorizations(self):
        cursor = FakeSessionCursor(
            profile=(INTERNAL_USER_ID, "Administradora", True),
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


class BankingAuthorizationTests(SimpleTestCase):
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


