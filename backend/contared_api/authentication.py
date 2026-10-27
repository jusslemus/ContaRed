from dataclasses import dataclass
import json
from functools import lru_cache
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

import jwt
from django.conf import settings
from jwt import InvalidTokenError
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import APIException, AuthenticationFailed


@dataclass(frozen=True)
class SupabaseUser:
    auth_user_id: UUID

    @property
    def is_authenticated(self):
        return True


class IdentityProviderUnavailable(APIException):
    status_code = 503
    default_detail = "No se pudo verificar la identidad con Supabase Auth."
    default_code = "identity_provider_unavailable"


@lru_cache(maxsize=1)
def get_jwks_client():
    if not settings.SUPABASE_JWKS_URL:
        raise IdentityProviderUnavailable(
            "Configure SUPABASE_URL o SUPABASE_JWKS_URL en el backend."
        )
    return jwt.PyJWKClient(settings.SUPABASE_JWKS_URL, lifespan=600)


class SupabaseJWTAuthentication(BaseAuthentication):
    algorithms = ("ES256", "RS256")

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts:
            return None
        if parts[0].lower() != b"bearer":
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("El encabezado Bearer no es válido.")

        try:
            token = parts[1].decode("ascii")
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm in self.algorithms:
                signing_key = get_jwks_client().get_signing_key_from_jwt(token)
                claims = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=list(self.algorithms),
                    audience="authenticated",
                    issuer=settings.SUPABASE_ISSUER,
                    options={"require": ["exp", "iss", "aud", "sub"]},
                )
            elif algorithm == "HS256":
                claims = self._verify_legacy_token_with_auth_server(token)
            else:
                raise AuthenticationFailed(
                    "El algoritmo de firma del token no está permitido."
                )
            auth_user_id = UUID(claims["sub"])
        except PyJWKClientConnectionError as exc:
            raise IdentityProviderUnavailable() from exc
        except (InvalidTokenError, PyJWKClientError, UnicodeDecodeError, ValueError) as exc:
            raise AuthenticationFailed("El token de acceso no es válido.") from exc

        return SupabaseUser(auth_user_id=auth_user_id), token

    def _verify_legacy_token_with_auth_server(self, token):
        if not settings.SUPABASE_URL or not settings.SUPABASE_PUBLISHABLE_KEY:
            raise IdentityProviderUnavailable(
                "Configure SUPABASE_URL y SUPABASE_PUBLISHABLE_KEY para validar tokens HS256."
            )

        claims = jwt.decode(
            token,
            algorithms=["HS256"],
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
        if claims.get("iss") != settings.SUPABASE_ISSUER:
            raise AuthenticationFailed("El emisor del token no es válido.")
        audience = claims.get("aud")
        if "authenticated" not in (
            audience if isinstance(audience, list) else [audience]
        ):
            raise AuthenticationFailed("La audiencia del token no es válida.")
        expiration = claims.get("exp")
        if (
            isinstance(expiration, bool)
            or not isinstance(expiration, (int, float))
            or expiration <= time.time()
        ):
            raise AuthenticationFailed("El token ha vencido.")
        if not isinstance(claims.get("sub"), str):
            raise AuthenticationFailed("La identidad del token no es válida.")

        request = Request(
            f"{settings.SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": settings.SUPABASE_PUBLISHABLE_KEY,
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                auth_user = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthenticationFailed("El token de acceso no es válido.") from exc
            raise IdentityProviderUnavailable() from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdentityProviderUnavailable() from exc

        if not isinstance(auth_user, dict) or auth_user.get("id") != claims["sub"]:
            raise AuthenticationFailed("La identidad confirmada por Supabase no coincide.")
        return claims

    def authenticate_header(self, request):
        return "Bearer"
