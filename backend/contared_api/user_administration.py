import hmac
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, IntegrityError, connection, transaction
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.banking import scope_from_request
from contared_api.session_access import DatabaseUnavailable, require_administrator


logger = logging.getLogger(__name__)
VALID_ROLES = ("administrador", "auxiliar", "revisor", "consulta")
COUNTRIES = {"SV": "El Salvador", "GT": "Guatemala"}


class UserAdministrationMigrationRequired(APIException):
    status_code = 503
    default_detail = (
        "La administración de usuarios requiere aplicar las migraciones "
        "supabase/migrations/202611090001_administracion_usuarios.sql y "
        "supabase/migrations/202611100001_perfil_correo_pendiente.sql y "
        "supabase/migrations/202611100002_cambio_clave_obligatorio.sql."
    )
    default_code = "user_administration_migration_required"


class SupabaseAdminUnavailable(APIException):
    status_code = 503
    default_detail = (
        "No se pudo completar la operación en Supabase Auth. No se confirmó el cambio; "
        "puedes reintentar."
    )
    default_code = "supabase_auth_unavailable"

    def __init__(self, detail=None):
        super().__init__(detail or self.default_detail, code=self.default_code)


class UserDeletionConflict(APIException):
    status_code = 409
    default_code = "user_deletion_conflict"


class UserIdentityConflict(APIException):
    status_code = 409
    default_code = "user_identity_conflict"


class SupabaseAdminRequestError(Exception):
    def __init__(self, status=None, *, code=None, message=None, operation=None):
        self.status = status
        self.code = _safe_provider_text(code)
        self.message = _safe_provider_text(message)
        self.operation = operation or "Auth administrative request"
        super().__init__("Supabase Auth administrative request failed")


def _safe_provider_text(value, sensitive_values=()):
    if not isinstance(value, str):
        return None
    value = value[:1000]
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", value)
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email redacted]", value, flags=re.I)
    value = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "[id redacted]", value, flags=re.I)
    value = re.sub(r"https?://\S+", "[URL redacted]", value, flags=re.I)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[token redacted]", value)
    value = re.sub(r"(?i)(password|token|secret|authorization|apikey|credential|key)\s*[:=]\s*\S+", r"\1=[redacted]", value)
    value = re.sub(r"(?<!\d)(?:\+?\d[\s().-]*){8,15}(?!\d)", "[number redacted]", value)
    for sensitive in sorted(
        {item for item in sensitive_values if isinstance(item, str) and len(item) >= 3},
        key=len,
        reverse=True,
    ):
        value = re.sub(re.escape(sensitive), "[personal data redacted]", value, flags=re.I)
    return value[:300]


def _provider_error_details(raw, sensitive_values=()):
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    code = next(
        (payload[key] for key in ("code", "error_code", "error") if isinstance(payload.get(key), str)),
        None,
    )
    message = next(
        (
            payload[key]
            for key in ("message", "msg", "error_description", "error")
            if isinstance(payload.get(key), str) and payload.get(key) != code
        ),
        None,
    )
    return (
        _safe_provider_text(code, sensitive_values),
        _safe_provider_text(message, sensitive_values),
    )


def _auth_operation(method, path):
    route = path.split("?", 1)[0].rstrip("/")
    if route == "/admin/users":
        return "identity lookup" if method == "GET" else "identity creation"
    if route.startswith("/admin/users/"):
        if method == "GET":
            return "identity lookup"
        if method == "PUT":
            return "email change"
        if method == "DELETE":
            return "identity deletion"
        return "identity update"
    if route == "/invite":
        return "invitation delivery"
    if route == "/resend":
        return "invitation resend"
    if route == "/recover":
        return "password recovery"
    return "Auth administrative request"


def _log_auth_failure(exc):
    logger.warning(
        "Supabase Auth %s failed: HTTP %s, code=%s, message=%s",
        exc.operation,
        exc.status or "unavailable",
        exc.code or "unavailable",
        exc.message or "unavailable",
    )


def _provider_failure_detail(exc):
    detail = (
        f"Falló la operación «{exc.operation}» de Supabase Auth "
        f"(HTTP {exc.status or 'sin respuesta'})."
    )
    if exc.code:
        detail += f" Código: {exc.code}."
    if exc.message:
        detail += f" {exc.message}"
    return detail


def _admin_request(method, path, payload=None, *, operation=None):
    operation = operation or _auth_operation(method, path)
    base_url = getattr(settings, "SUPABASE_URL", "")
    service_key = getattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise SupabaseAdminRequestError(
            code="backend_configuration",
            message="Supabase Auth server configuration is incomplete",
            operation=operation,
        )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    sensitive_values = []
    if isinstance(payload, dict):
        sensitive_keys = {
            "email", "name", "nombre", "full_name", "phone",
            "user_id", "id", "emailredirectto", "password",
            "new_password", "current_password",
        }
        for key, value in payload.items():
            if isinstance(value, str) and key.casefold() in sensitive_keys:
                sensitive_values.append(value)
            elif isinstance(value, dict):
                sensitive_values.extend(
                    child for child_key, child in value.items()
                    if isinstance(child, str) and child_key.casefold() in sensitive_keys
                )
    request = Request(
        f"{base_url}/auth/v1{path}",
        data=body,
        method=method,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise SupabaseAdminRequestError(response.status, operation=operation)
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        code, message = _provider_error_details(exc.read(8192), sensitive_values)
        raise SupabaseAdminRequestError(
            exc.code,
            code=code,
            message=message,
            operation=operation,
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SupabaseAdminRequestError(operation=operation) from exc


def _find_auth_user(email):
    for page in range(1, 101):
        query = urlencode({"page": page, "per_page": 100})
        data = _admin_request("GET", f"/admin/users?{query}")
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, list):
            raise SupabaseAdminRequestError(
                code="invalid_response",
                message="Supabase Auth returned an invalid identity list",
                operation="identity lookup",
            )
        for user in users:
            if (
                isinstance(user, dict)
                and isinstance(user.get("email"), str)
                and user["email"].casefold() == email.casefold()
            ):
                return user
        if len(users) < 100:
            return None
    raise SupabaseAdminRequestError(
        code="pagination_limit",
        message="Supabase Auth identity search exceeded its safe page limit",
        operation="identity lookup",
    )


def _get_auth_user(auth_user_id):
    try:
        normalized_id = str(UUID(str(auth_user_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SupabaseAdminRequestError(
            code="invalid_identity_id",
            operation="identity lookup",
        ) from exc
    result = _admin_request("GET", f"/admin/users/{normalized_id}")
    auth_user = result.get("user", result) if isinstance(result, dict) else None
    if (
        not isinstance(auth_user, dict)
        or _auth_user_id(auth_user) != normalized_id
        or not isinstance(auth_user.get("email"), str)
    ):
        raise SupabaseAdminRequestError(
            code="invalid_response",
            message="Supabase Auth returned an invalid identity record",
            operation="identity lookup",
        )
    return auth_user


def _create_auth_user(email, name, password):
    result = _admin_request(
        "POST",
        "/admin/users",
        {
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"nombre": name},
        },
    )
    auth_user = result.get("user", result) if isinstance(result, dict) else None
    if (
        not isinstance(auth_user, dict)
        or not _auth_user_id(auth_user)
        or not isinstance(auth_user.get("email"), str)
        or auth_user["email"].casefold() != email.casefold()
        or not auth_user.get("email_confirmed_at")
    ):
        raise SupabaseAdminRequestError(
            code="invalid_response",
            message="Supabase Auth returned an invalid identity creation record",
            operation="identity creation",
        )
    return auth_user


def _set_auth_password(auth_user_id, password, *, confirm_email=False):
    payload = {"password": password}
    if confirm_email:
        payload["email_confirm"] = True
    result = _admin_request(
        "PUT",
        f"/admin/users/{UUID(str(auth_user_id))}",
        payload,
        operation="password update",
    )
    auth_user = result.get("user", result) if isinstance(result, dict) else None
    if (
        not isinstance(auth_user, dict)
        or _auth_user_id(auth_user) != str(UUID(str(auth_user_id)))
        or (confirm_email and not auth_user.get("email_confirmed_at"))
    ):
        raise SupabaseAdminRequestError(
            code="invalid_response",
            message="Supabase Auth returned an invalid password update record",
            operation="password update",
        )
    return auth_user


def _verify_auth_password(email, password, auth_user_id):
    base_url = getattr(settings, "SUPABASE_URL", "")
    publishable_key = getattr(settings, "SUPABASE_PUBLISHABLE_KEY", "")
    if not base_url or not publishable_key:
        raise SupabaseAdminRequestError(
            code="backend_configuration",
            message="Supabase Auth server configuration is incomplete",
            operation="password verification",
        )
    request = Request(
        f"{base_url}/auth/v1/token?grant_type=password",
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        method="POST",
        headers={
            "apikey": publishable_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(2_000_001))
    except HTTPError as exc:
        if exc.code in (400, 401, 403):
            return False
        raise SupabaseAdminRequestError(
            exc.code,
            operation="password verification",
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SupabaseAdminRequestError(operation="password verification") from exc

    auth_user = result.get("user") if isinstance(result, dict) else None
    return isinstance(auth_user, dict) and _auth_user_id(auth_user) == str(auth_user_id)


def _update_auth_user_email(auth_user_id, email):
    result = _admin_request(
        "PUT",
        f"/admin/users/{UUID(str(auth_user_id))}",
        {"email": email},
    )
    auth_user = result.get("user", result) if isinstance(result, dict) else None
    if (
        not isinstance(auth_user, dict)
        or _auth_user_id(auth_user) != str(UUID(str(auth_user_id)))
        or not isinstance(auth_user.get("email"), str)
    ):
        raise SupabaseAdminRequestError(
            code="invalid_response",
            message="Supabase Auth returned an invalid email update record",
            operation="email change",
        )
    return auth_user


def _send_password_recovery(email):
    return _admin_request(
        "POST",
        f"/recover{_redirect_query()}",
        {"email": email},
    )


def _redirect_query():
    redirect_url = getattr(settings, "SUPABASE_AUTH_REDIRECT_URL", "")
    if not redirect_url:
        raise SupabaseAdminRequestError(
            code="redirect_configuration",
            message="SUPABASE_AUTH_REDIRECT_URL is not configured",
            operation="invitation delivery",
        )
    return f"?{urlencode({'redirect_to': redirect_url})}"


def _send_invitation(email, name):
    return _admin_request(
        "POST",
        f"/invite{_redirect_query()}",
        {"email": email, "data": {"nombre": name}},
    )


def _resend_invitation(email):
    redirect_url = getattr(settings, "SUPABASE_AUTH_REDIRECT_URL", "")
    if not redirect_url:
        raise SupabaseAdminRequestError(
            code="backend_configuration",
            message="SUPABASE_AUTH_REDIRECT_URL is not configured",
            operation="invitation resend",
        )
    return _admin_request(
        "POST",
        "/resend",
        {
            "type": "signup",
            "email": email,
            "options": {"emailRedirectTo": redirect_url},
        },
    )


def _auth_user_id(auth_user):
    if not isinstance(auth_user, dict):
        return None
    user_id = auth_user.get("id")
    try:
        return str(UUID(str(user_id))) if user_id else None
    except (ValueError, TypeError, AttributeError):
        return None


def _scope(request):
    body = request.data if request.method in ("POST", "PATCH") else {}
    company_id = body.get("empresa_id", request.query_params.get("empresa_id"))
    try:
        UUID(str(company_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({"empresa_id": "La empresa seleccionada no es válida."}) from exc
    scope = scope_from_request(request)
    require_administrator(scope)
    return scope


def _set_actor(cursor, scope):
    cursor.execute(
        "SELECT set_config('contared.actor_id', %s, true)",
        [scope["user_id"]],
    )


def _lock_company_and_admin_countries(cursor, scope, *, lock=True):
    lock_clause = " FOR UPDATE" if lock else ""
    cursor.execute(
        "SELECT id FROM contared.empresas WHERE id=%s AND activa IS TRUE" + lock_clause,
        [scope["company_id"]],
    )
    if cursor.fetchone() is None:
        raise PermissionDenied("La empresa no está disponible en tu ámbito autorizado.")
    cursor.execute(
        """
        SELECT a.pais_codigo
        FROM contared.accesos a
        JOIN contared.usuarios u ON u.id=a.usuario_id
        WHERE a.usuario_id=%s AND a.empresa_id=%s
          AND a.rol='administrador' AND a.activo IS TRUE AND u.activo IS TRUE
        ORDER BY a.pais_codigo
        """,
        [scope["user_id"], scope["company_id"]],
    )
    countries = {row[0] for row in cursor.fetchall()}
    if scope["country_code"] not in countries:
        raise PermissionDenied("No puedes administrar usuarios en el país seleccionado.")
    return countries


def _validate_accesses(value, allowed_countries, *, allow_active=True):
    if not isinstance(value, list) or not value:
        raise ValidationError({"accesos": "Selecciona al menos un país y un rol."})
    result = []
    seen = set()
    for access in value:
        if not isinstance(access, dict) or set(access) - {"pais_codigo", "rol", "activo"}:
            raise ValidationError({"accesos": "La configuración de accesos no es válida."})
        if not allow_active and "activo" in access:
            raise ValidationError({"accesos": "Los accesos nuevos se crean activos."})
        country = access.get("pais_codigo")
        role = access.get("rol")
        active = access.get("activo", True)
        if country not in COUNTRIES or country not in allowed_countries:
            raise PermissionDenied("No puedes asignar permisos en uno de esos países.")
        if country in seen:
            raise ValidationError({"accesos": "No repitas un país en la configuración."})
        if role not in VALID_ROLES:
            raise ValidationError({"accesos": "Selecciona un rol válido para cada país."})
        if not isinstance(active, bool):
            raise ValidationError({"accesos": "El estado del acceso no es válido."})
        seen.add(country)
        result.append((country, role, active))
    return result


def _ensure_another_administrator(cursor, company_id, country, target_id):
    cursor.execute(
        """
        SELECT count(*)
        FROM contared.accesos a
        JOIN contared.usuarios u ON u.id=a.usuario_id
        JOIN contared.empresas e ON e.id=a.empresa_id
        WHERE a.empresa_id=%s AND a.pais_codigo=%s
          AND a.rol='administrador' AND a.activo IS TRUE
          AND u.activo IS TRUE AND e.activa IS TRUE
          AND a.usuario_id<>%s
        """,
        [company_id, country, target_id],
    )
    if cursor.fetchone()[0] == 0:
        raise ValidationError({
            "accesos": f"No se puede retirar o desactivar al último administrador de {COUNTRIES[country]}."
        })


def _profile_reference_counts(cursor, user_id):
    cursor.execute(
        """
        SELECT source_schema.nspname,source_table.relname,source_column.attname
        FROM pg_constraint fk
        JOIN pg_class source_table ON source_table.oid=fk.conrelid
        JOIN pg_namespace source_schema ON source_schema.oid=source_table.relnamespace
        CROSS JOIN LATERAL unnest(fk.conkey) WITH ORDINALITY AS local_key(attnum,position)
        CROSS JOIN LATERAL unnest(fk.confkey) WITH ORDINALITY AS remote_key(attnum,position)
        JOIN pg_attribute source_column
          ON source_column.attrelid=source_table.oid
         AND source_column.attnum=local_key.attnum
        JOIN pg_attribute target_column
          ON target_column.attrelid=fk.confrelid
         AND target_column.attnum=remote_key.attnum
        WHERE fk.contype='f'
          AND fk.confrelid='contared.usuarios'::regclass
          AND local_key.position=remote_key.position
          AND target_column.attname='id'
          AND NOT (source_schema.nspname='contared' AND source_table.relname='accesos')
        ORDER BY source_schema.nspname,source_table.relname,source_column.attname
        """
    )
    references = cursor.fetchall()
    blockers = []
    for schema_name, table_name, column_name in references:
        qualified_table = ".".join(
            connection.ops.quote_name(part)
            for part in (schema_name, table_name)
        )
        quoted_column = connection.ops.quote_name(column_name)
        cursor.execute(
            f"SELECT count(*) FROM {qualified_table} WHERE {quoted_column}=%s",
            [user_id],
        )
        count = cursor.fetchone()[0]
        if count:
            blockers.append((schema_name, table_name, count))
    return blockers


def _authorized_admin_scopes(cursor, user_id):
    cursor.execute(
        """
        SELECT a.empresa_id::text,a.pais_codigo
        FROM contared.accesos a
        JOIN contared.usuarios u ON u.id=a.usuario_id
        JOIN contared.empresas e ON e.id=a.empresa_id
        WHERE a.usuario_id=%s AND a.rol='administrador'
          AND a.activo IS TRUE AND u.activo IS TRUE AND e.activa IS TRUE
        """,
        [user_id],
    )
    return {(row[0], row[1]) for row in cursor.fetchall()}


def _user_deletion_preview(cursor, scope, user_id, admin_countries):
    cursor.execute(
        """
        SELECT u.id::text,u.nombre,u.correo,u.auth_user_id::text
        FROM contared.usuarios u
        WHERE u.id=%s AND EXISTS (
          SELECT 1 FROM contared.accesos a
          WHERE a.usuario_id=u.id AND a.empresa_id=%s
            AND a.pais_codigo=ANY(%s)
        )
        """,
        [user_id, scope["company_id"], list(admin_countries)],
    )
    profile = cursor.fetchone()
    if profile is None:
        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")

    cursor.execute(
        """
        SELECT a.empresa_id::text,e.nombre,a.pais_codigo,p.nombre,a.rol,a.activo
        FROM contared.accesos a
        JOIN contared.empresas e ON e.id=a.empresa_id
        JOIN contared.paises p ON p.codigo=a.pais_codigo
        WHERE a.usuario_id=%s
        ORDER BY e.nombre,p.codigo
        """,
        [user_id],
    )
    all_accesses = cursor.fetchall()
    visible_accesses = [
        {
            "pais_codigo": row[2],
            "pais": row[3],
            "empresa": row[1],
            "rol": row[4],
            "activo": row[5],
        }
        for row in all_accesses
        if row[0] == scope["company_id"] and row[2] in admin_countries
    ]
    all_scopes = {(row[0], row[2]) for row in all_accesses}
    external_accesses = not all_scopes.issubset(_authorized_admin_scopes(cursor, scope["user_id"]))
    self_delete = str(user_id) == str(scope["user_id"])
    references = _profile_reference_counts(cursor, user_id)
    auth_user_id = profile[3]
    identity_conflict = False
    if not auth_user_id and profile[2] and not (self_delete or external_accesses):
        cursor.execute(
            """
            SELECT au.id::text, linked.id IS NOT NULL
            FROM auth.users au
            LEFT JOIN contared.usuarios linked ON linked.auth_user_id=au.id
            WHERE lower(au.email)=lower(%s)
            LIMIT 2
            """,
            [profile[2]],
        )
        candidates = cursor.fetchall()
        identity_conflict = len(candidates) > 1 or bool(candidates and candidates[0][1])
        if candidates and not identity_conflict:
            auth_user_id = candidates[0][0]
    auth_check_incomplete = not auth_user_id and not profile[2]
    if self_delete:
        reason = "No puedes eliminar tu propio perfil desde este módulo."
    elif external_accesses:
        reason = "El usuario tiene accesos fuera de tu ámbito. Solo puedes retirar los accesos autorizados que se muestran."
    elif identity_conflict:
        reason = "El correo coincide con una identidad ambigua o vinculada a otro perfil. Revisa el vínculo antes de eliminar."
    elif auth_check_incomplete:
        reason = "No se puede confirmar que el perfil no tenga una identidad Auth porque no hay un correo asociado. El perfil se conserva."
    else:
        reason = ""
    return {
        "usuario": {
            "nombre": profile[1],
            "correo": profile[2] or "",
        },
        "accesos_afectados": visible_accesses,
        "tiene_accesos_fuera_del_ambito": external_accesses,
        "tiene_identidad_auth": bool(auth_user_id),
        "_auth_user_id": auth_user_id,
        "es_propia": self_delete,
        "puede_eliminar_perfil": not (self_delete or external_accesses or identity_conflict or auth_check_incomplete),
        "motivo_bloqueo": reason,
        "referencias_historicas": bool(references),
    }


def _database_failure(exc):
    cause = exc.__cause__ or exc
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if sqlstate in ("42P01", "42703"):
        raise UserAdministrationMigrationRequired() from exc
    logger.error(
        "ContaRed user-administration query failed (database error type: %s).",
        type(exc).__name__,
    )
    raise DatabaseUnavailable() from exc


def _query_users(scope):
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                countries = _lock_company_and_admin_countries(cursor, scope, lock=False)
                cursor.execute(
                    """
                    SELECT u.id::text,u.nombre,u.correo,u.activo,
                           u.estado_invitacion,u.invitacion_enviada_en,
                           u.auth_user_id::text,u.correo_pendiente,
                           u.cambio_clave_obligatorio,
                           NOT EXISTS (
                             SELECT 1 FROM contared.accesos fuera
                             WHERE fuera.usuario_id=u.id
                               AND (fuera.empresa_id<>%s
                                    OR NOT (fuera.pais_codigo=ANY(%s)))
                           ),
                           a.pais_codigo,p.nombre,a.rol,a.activo
                    FROM contared.usuarios u
                    JOIN contared.accesos a ON a.usuario_id=u.id
                    JOIN contared.paises p ON p.codigo=a.pais_codigo
                    WHERE a.empresa_id=%s AND a.pais_codigo=ANY(%s)
                    ORDER BY lower(u.nombre),lower(u.correo),a.pais_codigo
                    """,
                    [
                        scope["company_id"],
                        list(countries),
                        scope["company_id"],
                        list(countries),
                    ],
                )
                rows = cursor.fetchall()
    except DatabaseError as exc:
        _database_failure(exc)

    users = {}
    for row in rows:
        user = users.setdefault(
            row[0],
            {
                "id": row[0],
                "nombre": row[1],
                "correo": row[2] or "",
                "perfil_activo": row[3],
                "estado_invitacion": row[4],
                "invitacion_enviada_en": row[5].isoformat() if row[5] else None,
                "auth_user_id": row[6],
                "correo_pendiente": row[7] or None,
                "cambio_clave_obligatorio": row[8],
                "perfil_global_editable": row[9],
                "accesos": [],
            },
        )
        user["accesos"].append({
            "pais_codigo": row[10],
            "pais": row[11],
            "rol": row[12],
            "activo": row[13],
        })
    for user in users.values():
        auth_user_id = user.pop("auth_user_id")
        user["tiene_identidad_auth"] = bool(auth_user_id)
        if not user["correo"] and auth_user_id:
            try:
                auth_user = _get_auth_user(auth_user_id)
            except SupabaseAdminRequestError as exc:
                _log_auth_failure(exc)
                raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
            user["correo"] = auth_user["email"]
            user["correo_pendiente"] = auth_user.get("new_email") or user["correo_pendiente"]
        elif (
            not auth_user_id
            and user["correo"]
            and user["estado_invitacion"] in ("pendiente", "fallida")
        ):
            try:
                user["tiene_identidad_auth"] = bool(_find_auth_user(user["correo"]))
            except SupabaseAdminRequestError as exc:
                _log_auth_failure(exc)
                raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
    return list(users.values())


def _profile_query(cursor, *, email, auth_user_id):
    if auth_user_id:
        cursor.execute(
            """
            SELECT id::text,auth_user_id::text,correo,estado_invitacion,nombre,activo
            FROM contared.usuarios WHERE auth_user_id=%s FOR UPDATE
            """,
            [auth_user_id],
        )
        profile = cursor.fetchone()
        if profile:
            return profile
    cursor.execute(
        """
        SELECT id::text,auth_user_id::text,correo,estado_invitacion,nombre,activo
        FROM contared.usuarios WHERE lower(correo)=lower(%s) FOR UPDATE
        """,
        [email],
    )
    return cursor.fetchone()


def _validate_initial_password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise ValidationError({"contrasena_inicial": "Usa una contraseña de 8 a 128 caracteres."})
    return value


def _save_created_identity(user_id, scope, auth_user_id, *, state="no_requerida"):
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ WRITE")
                _set_actor(cursor, scope)
                cursor.execute(
                    """
                    UPDATE contared.usuarios
                    SET auth_user_id=COALESCE(auth_user_id,%s),
                        estado_invitacion=%s
                    WHERE id=%s
                      AND (auth_user_id IS NULL OR auth_user_id=%s)
                    """,
                    [auth_user_id, state, user_id, auth_user_id],
                )
                if cursor.rowcount != 1:
                    raise UserIdentityConflict(
                        "El perfil ya está vinculado a otra identidad Auth."
                    )
    except DatabaseError as exc:
        logger.error(
            "Supabase Auth identity created but its ContaRed profile could not be linked "
            "(database error type: %s).",
            type(exc).__name__,
        )
        raise SupabaseAdminUnavailable(
            "La identidad existe en Auth, pero no se pudo vincular con ContaRed. "
            "Reintenta completar la creación; no se generará una identidad duplicada."
        ) from exc


def _add_missing_accesses(cursor, user_id, company_id, accesses):
    for country, role, _ in accesses:
        cursor.execute(
            """
            INSERT INTO contared.accesos(usuario_id,empresa_id,pais_codigo,rol,activo)
            VALUES (%s,%s,%s,%s,TRUE)
            ON CONFLICT (usuario_id,empresa_id,pais_codigo) DO NOTHING
            """,
            [user_id, company_id, country, role],
        )


def _save_invitation_state(user_id, scope, *, auth_user_id, state, sent=False):
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ WRITE")
                _set_actor(cursor, scope)
                cursor.execute(
                    """
                    UPDATE contared.usuarios
                    SET auth_user_id=COALESCE(%s,auth_user_id),
                        estado_invitacion=%s,
                        invitacion_enviada_en=CASE WHEN %s THEN now()
                                                   ELSE invitacion_enviada_en END
                    WHERE id=%s
                    """,
                    [auth_user_id, state, sent, user_id],
                )
    except DatabaseError as exc:
        _database_failure(exc)


class UserAdministrationListInviteView(APIView):
    def get(self, request):
        scope = _scope(request)
        return Response({"usuarios": _query_users(scope)})

    def post(self, request):
        scope = _scope(request)
        name = request.data.get("nombre")
        email = request.data.get("correo")
        password = _validate_initial_password(request.data.get("contrasena_inicial"))
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise ValidationError({"nombre": "Escribe un nombre de hasta 200 caracteres."})
        if (
            not isinstance(email, str)
            or len(email.strip()) > 320
        ):
            raise ValidationError({"correo": "Escribe un correo electrónico válido."})
        name = name.strip()
        email = email.strip().casefold()
        try:
            validate_email(email)
        except DjangoValidationError as exc:
            raise ValidationError({"correo": "Escribe un correo electrónico válido."}) from exc
        requested = request.data.get("accesos")
        requested_profile_id = request.data.get("usuario_id")
        if requested_profile_id:
            try:
                requested_profile_id = str(UUID(str(requested_profile_id)))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError({"usuario": "El perfil seleccionado no es válido."}) from exc
        try:
            auth_user = _find_auth_user(email)
        except SupabaseAdminRequestError as exc:
            _log_auth_failure(exc)
            raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc

        auth_user_id = _auth_user_id(auth_user)
        if auth_user and not auth_user_id:
            exc = SupabaseAdminRequestError(
                code="invalid_response",
                message="Supabase Auth identity search did not return a valid identity",
                operation="identity lookup",
            )
            _log_auth_failure(exc)
            raise SupabaseAdminUnavailable(_provider_failure_detail(exc))
        profile_created = False
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(lower(%s)))",
                        [email],
                    )
                    countries = _lock_company_and_admin_countries(cursor, scope)
                    accesses = _validate_accesses(requested, countries, allow_active=False)
                    _set_actor(cursor, scope)
                    if requested_profile_id:
                        cursor.execute(
                            """
                            SELECT id::text,auth_user_id::text,correo,estado_invitacion,nombre,activo
                            FROM contared.usuarios WHERE id=%s FOR UPDATE
                            """,
                            [requested_profile_id],
                        )
                        profile = cursor.fetchone()
                        if profile is None:
                            raise NotFound("El perfil no está disponible en tu ámbito autorizado.")
                    else:
                        profile = _profile_query(
                            cursor,
                            email=email,
                            auth_user_id=auth_user_id,
                        )
                    if profile and profile[2] and profile[2].casefold() != email:
                        raise ValidationError({
                            "correo": "El correo no coincide con el perfil seleccionado."
                        })
                    if profile and profile[1] and not auth_user:
                        try:
                            auth_user = _get_auth_user(profile[1])
                        except SupabaseAdminRequestError as exc:
                            if exc.status == 404:
                                raise UserIdentityConflict(
                                    "La identidad vinculada ya no existe en Supabase Auth."
                                ) from exc
                            _log_auth_failure(exc)
                            raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
                        else:
                            auth_user_id = profile[1]
                    if profile:
                        cursor.execute(
                            """
                            SELECT NOT EXISTS (
                              SELECT 1 FROM contared.accesos
                              WHERE usuario_id=%s
                                AND (empresa_id<>%s OR NOT (pais_codigo=ANY(%s)))
                            )
                            """,
                            [profile[0], scope["company_id"], list(countries)],
                        )
                        if not cursor.fetchone()[0]:
                            raise PermissionDenied(
                                "No puedes completar la cuenta de un perfil con accesos "
                                "fuera de tu ámbito."
                            )
                    if profile is None:
                        cursor.execute(
                            """
                            INSERT INTO contared.usuarios
                                (auth_user_id,nombre,correo,estado_invitacion,
                                 cambio_clave_obligatorio)
                            VALUES (%s,%s,%s,%s,%s)
                            RETURNING id::text
                            """,
                            [
                                auth_user_id,
                                name,
                                email,
                                "no_requerida" if auth_user else "fallida",
                                not bool(auth_user),
                            ],
                        )
                        user_id = cursor.fetchone()[0]
                        profile_created = True
                    else:
                        user_id = profile[0]
                        if not profile[5]:
                            raise ValidationError({
                                "correo": "El perfil global está inactivo y no puede recibir nuevos accesos."
                            })
                        stored_auth_id = profile[1]
                        if stored_auth_id and auth_user_id and stored_auth_id != auth_user_id:
                            raise ValidationError({
                                "correo": "No se pudo asociar este correo a un perfil existente."
                            })
                        cursor.execute(
                            """
                            UPDATE contared.usuarios
                            SET correo=COALESCE(correo,%s),
                                auth_user_id=COALESCE(auth_user_id,%s)
                            WHERE id=%s
                            """,
                            [email, auth_user_id, user_id],
                        )
                    _add_missing_accesses(
                        cursor,
                        user_id,
                        scope["company_id"],
                        accesses,
                    )
                    if not auth_user_id:
                        cursor.execute(
                            """
                            UPDATE contared.usuarios
                            SET cambio_clave_obligatorio=TRUE,
                                estado_invitacion='fallida'
                            WHERE id=%s
                            """,
                            [user_id],
                        )
        except IntegrityError as exc:
            raise ValidationError({
                "correo": "Ya existe un perfil vinculado a este correo."
            }) from exc
        except DatabaseError as exc:
            _database_failure(exc)

        if auth_user:
            state = "no_requerida" if auth_user.get("email_confirmed_at") else "fallida"
            _save_created_identity(user_id, scope, auth_user_id, state=state)
            return Response({
                "mensaje": (
                    "Se reutilizó el perfil y la identidad existentes; no se modificó la "
                    "contraseña. Usa «Asignar contraseña temporal» si necesita una nueva."
                ),
                "estado_invitacion": state,
                "tiene_identidad_auth": True,
            }, status=200)

        try:
            created_identity = _create_auth_user(email, name, password)
        except SupabaseAdminRequestError as exc:
            if exc.code == "email_exists":
                try:
                    existing_identity = _find_auth_user(email)
                except SupabaseAdminRequestError as lookup_exc:
                    _log_auth_failure(lookup_exc)
                    raise SupabaseAdminUnavailable(_provider_failure_detail(lookup_exc)) from lookup_exc
                if existing_identity:
                    existing_id = _auth_user_id(existing_identity)
                    state = "no_requerida" if existing_identity.get("email_confirmed_at") else "fallida"
                    _save_created_identity(user_id, scope, existing_id, state=state)
                    return Response({
                        "mensaje": (
                            "La identidad ya existía y no se cambió su contraseña. Usa "
                            "«Asignar contraseña temporal» para asignar una explícitamente."
                        ),
                        "estado_invitacion": state,
                        "tiene_identidad_auth": True,
                    }, status=200)
            _log_auth_failure(exc)
            raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc

        _save_created_identity(user_id, scope, _auth_user_id(created_identity))
        return Response({
            "mensaje": "Se creó la cuenta. La persona deberá cambiar la contraseña al ingresar.",
            "estado_invitacion": "no_requerida",
            "cambio_clave_obligatorio": True,
        }, status=201 if profile_created else 200)


class UserAdministrationPermissionsView(APIView):
    def patch(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc
        requested = request.data.get("accesos")
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    countries = _lock_company_and_admin_countries(cursor, scope)
                    accesses = _validate_accesses(requested, countries)
                    cursor.execute(
                        """
                        SELECT id,activo FROM contared.usuarios
                        WHERE id=%s AND EXISTS (
                          SELECT 1 FROM contared.accesos a
                          WHERE a.usuario_id=contared.usuarios.id
                            AND a.empresa_id=%s AND a.pais_codigo=ANY(%s)
                        )
                        FOR UPDATE
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    target_profile = cursor.fetchone()
                    if target_profile is None:
                        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")
                    if not target_profile[1]:
                        raise ValidationError({
                            "accesos": "El perfil global está inactivo; no se pueden cambiar sus accesos."
                        })

                    cursor.execute(
                        """
                        SELECT pais_codigo,rol,activo
                        FROM contared.accesos
                        WHERE usuario_id=%s AND empresa_id=%s AND pais_codigo=ANY(%s)
                        FOR UPDATE
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    current = {
                        row[0]: {"rol": row[1], "activo": row[2]}
                        for row in cursor.fetchall()
                    }
                    for country, role, active in accesses:
                        previous = current.get(country)
                        if (
                            previous
                            and previous["rol"] == "administrador"
                            and previous["activo"]
                            and (role != "administrador" or not active)
                        ):
                            _ensure_another_administrator(
                                cursor,
                                scope["company_id"],
                                country,
                                target_id,
                            )

                    _set_actor(cursor, scope)
                    for country, role, active in accesses:
                        cursor.execute(
                            """
                            INSERT INTO contared.accesos
                                (usuario_id,empresa_id,pais_codigo,rol,activo)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (usuario_id,empresa_id,pais_codigo)
                            DO UPDATE SET rol=EXCLUDED.rol,activo=EXCLUDED.activo
                            """,
                            [target_id, scope["company_id"], country, role, active],
                        )
        except DatabaseError as exc:
            _database_failure(exc)
        return Response({"mensaje": "Los permisos se actualizaron."})


class UserAdministrationUserView(APIView):
    def patch(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc

        name_supplied = "nombre" in request.data
        email_supplied = "correo" in request.data
        accesses_supplied = "accesos" in request.data
        if not (name_supplied or email_supplied or accesses_supplied):
            raise ValidationError({"detail": "No hay cambios para guardar."})

        name = request.data.get("nombre")
        if name_supplied:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                raise ValidationError({"nombre": "Escribe un nombre de hasta 200 caracteres."})
            name = name.strip()

        email = request.data.get("correo")
        if email_supplied:
            if not isinstance(email, str) or len(email.strip()) > 320:
                raise ValidationError({"correo": "Escribe un correo electrónico válido."})
            email = email.strip().casefold()
            try:
                validate_email(email)
            except DjangoValidationError as exc:
                raise ValidationError({"correo": "Escribe un correo electrónico válido."}) from exc

        auth_update = None
        auth_user_id = None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    countries = _lock_company_and_admin_countries(cursor, scope)
                    cursor.execute(
                        """
                        SELECT u.id::text,u.auth_user_id::text,u.correo,
                               u.correo_pendiente,u.nombre,u.activo
                        FROM contared.usuarios u
                        WHERE u.id=%s AND EXISTS (
                          SELECT 1 FROM contared.accesos a
                          WHERE a.usuario_id=u.id AND a.empresa_id=%s
                            AND a.pais_codigo=ANY(%s)
                        )
                        FOR UPDATE
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    profile = cursor.fetchone()
                    if profile is None:
                        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")
                    if not profile[5]:
                        raise ValidationError({
                            "usuario": "El perfil global está inactivo y no se puede editar."
                        })

                    cursor.execute(
                        """
                        SELECT EXISTS (
                          SELECT 1 FROM contared.accesos
                          WHERE usuario_id=%s
                            AND (empresa_id<>%s OR NOT (pais_codigo=ANY(%s)))
                        )
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    has_out_of_scope_access = cursor.fetchone()[0]
                    if has_out_of_scope_access and (name_supplied or email_supplied):
                        raise PermissionDenied(
                            "No puedes modificar el nombre o correo global porque este usuario "
                            "también tiene accesos fuera de tu ámbito. Puedes editar únicamente "
                            "los accesos autorizados."
                        )

                    accesses = []
                    if accesses_supplied:
                        accesses = _validate_accesses(request.data.get("accesos"), countries)

                    cursor.execute(
                        """
                        SELECT pais_codigo,rol,activo
                        FROM contared.accesos
                        WHERE usuario_id=%s AND empresa_id=%s AND pais_codigo=ANY(%s)
                        FOR UPDATE
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    current = {
                        row[0]: {"rol": row[1], "activo": row[2]}
                        for row in cursor.fetchall()
                    }
                    for country, role, active in accesses:
                        previous = current.get(country)
                        if (
                            previous
                            and previous["rol"] == "administrador"
                            and previous["activo"]
                            and (role != "administrador" or not active)
                        ):
                            _ensure_another_administrator(
                                cursor,
                                scope["company_id"],
                                country,
                                target_id,
                            )

                    updated_name = name if name_supplied else profile[4]
                    updated_email = profile[2]
                    pending_email = profile[3]
                    if email_supplied:
                        auth_user_id = profile[1]
                        if not auth_user_id:
                            if email != (profile[2] or "").casefold():
                                raise ValidationError({
                                    "correo": "No se puede cambiar el correo hasta vincular "
                                    "la identidad de Supabase Auth."
                                })
                            auth_user = None
                        else:
                            cursor.execute(
                                "SELECT pg_advisory_xact_lock(hashtext(lower(%s)))",
                                [email],
                            )
                            cursor.execute(
                                """
                                SELECT id FROM contared.usuarios
                                WHERE id<>%s
                                  AND (lower(correo)=lower(%s)
                                       OR lower(correo_pendiente)=lower(%s))
                                LIMIT 1
                                """,
                                [target_id, email, email],
                            )
                            if cursor.fetchone():
                                raise ValidationError({
                                    "correo": "Ese correo ya está asociado a otro perfil."
                                })
                            try:
                                auth_user = _get_auth_user(auth_user_id)
                            except SupabaseAdminRequestError as exc:
                                _log_auth_failure(exc)
                                raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
                            current_auth_email = auth_user["email"]
                            current_pending_email = auth_user.get("new_email")
                            if email not in {
                                current_auth_email.casefold(),
                                current_pending_email.casefold()
                                if isinstance(current_pending_email, str)
                                else "",
                            }:
                                try:
                                    auth_user = _update_auth_user_email(auth_user_id, email)
                                except SupabaseAdminRequestError as exc:
                                    if exc.status in (409, 422):
                                        raise ValidationError({
                                            "correo": "Ese correo ya está asociado a otra identidad de Supabase Auth."
                                        }) from exc
                                    _log_auth_failure(exc)
                                    raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
                                auth_update = (auth_user_id, current_auth_email)
                                updated_pending_email = auth_user.get("new_email")
                                if (
                                    auth_user["email"].casefold() != email
                                    and (
                                        not isinstance(updated_pending_email, str)
                                        or updated_pending_email.casefold() != email
                                    )
                                ):
                                    logger.warning(
                                        "Supabase Auth email update did not confirm the requested address."
                                    )
                                    raise SupabaseAdminUnavailable()
                            if auth_user.get("new_email"):
                                updated_email = auth_user["email"]
                                pending_email = auth_user["new_email"]
                            else:
                                updated_email = auth_user["email"]
                                pending_email = None

                    _set_actor(cursor, scope)
                    if name_supplied or email_supplied:
                        cursor.execute(
                            """
                            UPDATE contared.usuarios
                            SET nombre=%s,correo=%s,correo_pendiente=%s
                            WHERE id=%s
                            """,
                            [updated_name, updated_email, pending_email, target_id],
                        )
                    for country, role, active in accesses:
                        cursor.execute(
                            """
                            INSERT INTO contared.accesos
                                (usuario_id,empresa_id,pais_codigo,rol,activo)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (usuario_id,empresa_id,pais_codigo)
                            DO UPDATE SET rol=EXCLUDED.rol,activo=EXCLUDED.activo
                            """,
                            [target_id, scope["company_id"], country, role, active],
                        )
        except IntegrityError as exc:
            if auth_update:
                logger.error(
                    "Supabase Auth email changed but the ContaRed profile update failed "
                    "(database error type: %s).",
                    type(exc).__name__,
                )
                raise SupabaseAdminUnavailable(
                    "Supabase Auth actualizó el correo, pero ContaRed no pudo sincronizar "
                    "el perfil. No vuelvas a intentarlo; contacta al administrador."
                ) from exc
            raise ValidationError({
                "correo": "Ese correo ya está asociado a otro perfil."
            }) from exc
        except DatabaseError as exc:
            if auth_update:
                logger.error(
                    "Supabase Auth email changed but the ContaRed profile update failed "
                    "(database error type: %s).",
                    type(exc).__name__,
                )
                raise SupabaseAdminUnavailable(
                    "Supabase Auth actualizó el correo, pero ContaRed no pudo sincronizar "
                    "el perfil. No vuelvas a intentarlo; contacta al administrador."
                ) from exc
            _database_failure(exc)

        return Response({
            "mensaje": "Se guardaron los cambios del usuario.",
            "correo": updated_email if email_supplied else profile[2],
            "correo_pendiente": pending_email if email_supplied else profile[3],
        })


class UserAdministrationPasswordResetView(APIView):
    def post(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    countries = _lock_company_and_admin_countries(cursor, scope, lock=False)
                    cursor.execute(
                        """
                        SELECT u.auth_user_id::text,u.correo,
                               NOT EXISTS (
                                 SELECT 1 FROM contared.accesos fuera
                                 WHERE fuera.usuario_id=u.id
                                   AND (fuera.empresa_id<>%s
                                        OR NOT (fuera.pais_codigo=ANY(%s)))
                               )
                        FROM contared.usuarios u
                        WHERE u.id=%s AND u.activo IS TRUE AND EXISTS (
                          SELECT 1 FROM contared.accesos a
                          WHERE a.usuario_id=u.id AND a.empresa_id=%s
                            AND a.pais_codigo=ANY(%s)
                        )
                        """,
                        [
                            scope["company_id"],
                            list(countries),
                            target_id,
                            scope["company_id"],
                            list(countries),
                        ],
                    )
                    profile = cursor.fetchone()
                    if profile is None:
                        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")
                    if not profile[2]:
                        raise PermissionDenied(
                            "No puedes restablecer la contraseña de un usuario con accesos "
                            "fuera de tu ámbito."
                        )
        except DatabaseError as exc:
            _database_failure(exc)

        if not profile[0]:
            raise ValidationError({
                "usuario": "No se puede enviar el enlace porque la identidad de Supabase Auth "
                "aún no está vinculada."
            })
        try:
            auth_user = _get_auth_user(profile[0])
            _send_password_recovery(auth_user["email"])
        except SupabaseAdminRequestError as exc:
            _log_auth_failure(exc)
            raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
        return Response({"mensaje": "Se envió el enlace para restablecer la contraseña."})


class PasswordSetupView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current_password = request.data.get("contrasena_actual")
        new_password = request.data.get("nueva_contrasena")
        if not isinstance(current_password, str) or not current_password:
            raise ValidationError({"contrasena_actual": "Escribe tu contraseña actual."})
        new_password = _validate_initial_password(new_password)
        if hmac.compare_digest(
            current_password.encode("utf-8"),
            new_password.encode("utf-8"),
        ):
            raise ValidationError({
                "nueva_contrasena": "La contraseña nueva debe ser distinta de la temporal."
            })

        auth_updated = False
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ WRITE")
                    cursor.execute(
                        """
                        SELECT id::text,correo,cambio_clave_obligatorio,activo
                        FROM contared.usuarios
                        WHERE auth_user_id=%s
                        FOR UPDATE
                        """,
                        [request.user.auth_user_id],
                    )
                    profile = cursor.fetchone()
                    if profile is None or not profile[3]:
                        raise PermissionDenied(
                            "No se encontró un perfil activo vinculado a esta cuenta."
                        )
                    if not profile[2]:
                        return Response({
                            "mensaje": "No hay un cambio de contraseña pendiente.",
                            "cambio_clave_obligatorio": False,
                        })

                    try:
                        auth_user = _get_auth_user(request.user.auth_user_id)
                    except SupabaseAdminRequestError as exc:
                        _log_auth_failure(exc)
                        raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc

                    try:
                        password_matches = _verify_auth_password(
                            auth_user["email"],
                            current_password,
                            request.user.auth_user_id,
                        )
                    except SupabaseAdminRequestError as exc:
                        _log_auth_failure(exc)
                        raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
                    if not password_matches:
                        raise ValidationError({
                            "contrasena_actual": "La contraseña actual no es correcta."
                        })
                    try:
                        _set_auth_password(request.user.auth_user_id, new_password)
                    except SupabaseAdminRequestError as exc:
                        _log_auth_failure(exc)
                        raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
                    auth_updated = True
                    cursor.execute(
                        "SELECT set_config('contared.actor_id', %s, true)",
                        [profile[0]],
                    )
                    cursor.execute(
                        """
                        UPDATE contared.usuarios
                        SET cambio_clave_obligatorio=FALSE
                        WHERE id=%s AND cambio_clave_obligatorio IS TRUE
                        """,
                        [profile[0]],
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseError("The password-change requirement was not cleared.")
        except DatabaseError as exc:
            if auth_updated:
                logger.error(
                    "Supabase Auth password changed but ContaRed could not clear the "
                    "required-change flag (database error type: %s).",
                    type(exc).__name__,
                )
                raise SupabaseAdminUnavailable(
                    "Auth actualizó la contraseña, pero ContaRed no pudo completar el cambio. "
                    "Reintenta con la contraseña que acabas de establecer y elige una distinta."
                ) from exc
            _database_failure(exc)

        return Response({
            "mensaje": "La contraseña se actualizó correctamente.",
            "cambio_clave_obligatorio": False,
        })


class UserAdministrationTemporaryPasswordView(APIView):
    def post(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc
        password = _validate_initial_password(request.data.get("contrasena_temporal"))

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ WRITE")
                    countries = _lock_company_and_admin_countries(cursor, scope, lock=False)
                    cursor.execute(
                        """
                        SELECT u.auth_user_id::text,u.correo,
                               NOT EXISTS (
                                 SELECT 1 FROM contared.accesos fuera
                                 WHERE fuera.usuario_id=u.id
                                   AND (fuera.empresa_id<>%s
                                        OR NOT (fuera.pais_codigo=ANY(%s)))
                               )
                        FROM contared.usuarios u
                        WHERE u.id=%s AND u.activo IS TRUE AND EXISTS (
                          SELECT 1 FROM contared.accesos a
                          WHERE a.usuario_id=u.id AND a.empresa_id=%s
                            AND a.pais_codigo=ANY(%s)
                        )
                        """,
                        [
                            scope["company_id"],
                            list(countries),
                            target_id,
                            scope["company_id"],
                            list(countries),
                        ],
                    )
                    profile = cursor.fetchone()
                    if profile is None:
                        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")
                    if not profile[2]:
                        raise PermissionDenied(
                            "No puedes asignar una contraseña a un usuario con accesos "
                            "fuera de tu ámbito."
                        )
        except DatabaseError as exc:
            _database_failure(exc)

        auth_user_id = profile[0]
        try:
            auth_user = _get_auth_user(auth_user_id) if auth_user_id else (
                _find_auth_user(profile[1]) if profile[1] else None
            )
        except SupabaseAdminRequestError as exc:
            if exc.status == 404:
                auth_user = None
            else:
                _log_auth_failure(exc)
                raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc
        auth_user_id = _auth_user_id(auth_user)
        if not auth_user_id:
            raise UserIdentityConflict(
                "Este perfil aún no tiene identidad Auth. Usa «Crear usuario» para "
                "completar su creación con una contraseña inicial."
            )

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ WRITE")
                    _set_actor(cursor, scope)
                    cursor.execute(
                        """
                        UPDATE contared.usuarios
                        SET auth_user_id=COALESCE(auth_user_id,%s),
                            cambio_clave_obligatorio=TRUE
                        WHERE id=%s
                          AND (auth_user_id IS NULL OR auth_user_id=%s)
                        """,
                        [auth_user_id, target_id, auth_user_id],
                    )
                    if cursor.rowcount != 1:
                        raise UserIdentityConflict(
                            "El perfil ya está vinculado a otra identidad Auth."
                        )
        except DatabaseError as exc:
            _database_failure(exc)

        try:
            _set_auth_password(auth_user_id, password, confirm_email=True)
        except SupabaseAdminRequestError as exc:
            _log_auth_failure(exc)
            raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ WRITE")
                    _set_actor(cursor, scope)
                    cursor.execute(
                        """
                        UPDATE contared.usuarios
                        SET estado_invitacion='no_requerida'
                        WHERE id=%s
                        """,
                        [target_id],
                    )
        except DatabaseError as exc:
            logger.error(
                "Supabase Auth temporary password was assigned but its ContaRed profile "
                "could not be updated (database error type: %s).",
                type(exc).__name__,
            )
            raise SupabaseAdminUnavailable(
                "Auth actualizó la contraseña, pero ContaRed no pudo guardar el estado. "
                "La cuenta deberá cambiarla al ingresar; no repitas la asignación."
            ) from exc

        return Response({
            "mensaje": "Se asignó la contraseña temporal. La persona deberá cambiarla al ingresar.",
            "cambio_clave_obligatorio": True,
            "estado_invitacion": "no_requerida",
        })


class UserAdministrationResendInvitationView(APIView):
    def post(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    countries = _lock_company_and_admin_countries(cursor, scope)
                    cursor.execute(
                        """
                        SELECT u.id::text,u.correo,u.nombre,u.auth_user_id::text,
                               u.estado_invitacion,u.activo
                        FROM contared.usuarios u
                        WHERE u.id=%s AND EXISTS (
                          SELECT 1 FROM contared.accesos a
                          WHERE a.usuario_id=u.id AND a.empresa_id=%s
                            AND a.pais_codigo=ANY(%s)
                        )
                        FOR UPDATE
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    profile = cursor.fetchone()
                    if profile is None or not profile[1]:
                        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")
                    if not profile[5]:
                        raise ValidationError({
                            "usuario": "El perfil global está inactivo; no se puede reenviar la invitación."
                        })
                    _set_actor(cursor, scope)
        except DatabaseError as exc:
            _database_failure(exc)

        try:
            auth_user = _find_auth_user(profile[1])
            if auth_user and auth_user.get("email_confirmed_at"):
                return Response(
                    {"detail": "El usuario ya estableció su cuenta y puede iniciar sesión."},
                    status=409,
                )
            if auth_user is None:
                result = _send_invitation(profile[1], profile[2])
                invited_id = _auth_user_id(result.get("user", result))
                if invited_id is None:
                    raise SupabaseAdminRequestError(
                        code="invalid_response",
                        message="Supabase Auth invitation response did not include an identity",
                        operation="invitation delivery",
                    )
            else:
                _resend_invitation(profile[1])
                invited_id = _auth_user_id(auth_user)
            if invited_id is None:
                raise SupabaseAdminRequestError(
                    code="invalid_response",
                    message="Supabase Auth identity response did not include an identity",
                    operation="invitation resend",
                )
        except SupabaseAdminRequestError as exc:
            _save_invitation_state(
                profile[0],
                scope,
                auth_user_id=profile[3],
                state="fallida",
            )
            _log_auth_failure(exc)
            raise SupabaseAdminUnavailable(_provider_failure_detail(exc)) from exc

        _save_invitation_state(
            profile[0],
            scope,
            auth_user_id=invited_id,
            state="enviada",
            sent=True,
        )
        return Response({"mensaje": "Se envió nuevamente la invitación."})


class UserAdministrationDeleteView(APIView):
    def get(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    countries = _lock_company_and_admin_countries(cursor, scope, lock=False)
                    preview = _user_deletion_preview(cursor, scope, target_id, countries)
                    preview.pop("_auth_user_id", None)
        except DatabaseError as exc:
            _database_failure(exc)
        return Response(preview)

    def delete(self, request, user_id):
        scope = _scope(request)
        try:
            target_id = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"usuario": "El usuario seleccionado no es válido."}) from exc

        action = request.data.get("accion")
        if action not in ("retirar_accesos", "eliminar_perfil"):
            raise ValidationError({"accion": "Selecciona una acción válida."})
        if action == "eliminar_perfil" and request.data.get("confirmar") is not True:
            raise ValidationError({"confirmar": "Confirma la eliminación definitiva del usuario."})
        countries_requested = request.data.get("paises")
        if action == "retirar_accesos" and (
            not isinstance(countries_requested, list)
            or not countries_requested
            or any(not isinstance(country, str) for country in countries_requested)
            or len(countries_requested) != len(set(countries_requested))
            or any(country not in COUNTRIES for country in countries_requested)
        ):
            raise ValidationError({"paises": "Selecciona al menos un país válido, sin repetirlo."})

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    countries = _lock_company_and_admin_countries(cursor, scope)
                    cursor.execute(
                        """
                        SELECT u.id::text,u.auth_user_id::text,u.correo
                        FROM contared.usuarios u
                        WHERE u.id=%s AND EXISTS (
                          SELECT 1 FROM contared.accesos a
                          WHERE a.usuario_id=u.id AND a.empresa_id=%s
                            AND a.pais_codigo=ANY(%s)
                        )
                        FOR UPDATE
                        """,
                        [target_id, scope["company_id"], list(countries)],
                    )
                    profile = cursor.fetchone()
                    if profile is None:
                        raise NotFound("El usuario no está disponible en tu ámbito autorizado.")
                    if str(target_id) == str(scope["user_id"]):
                        raise PermissionDenied("No puedes eliminar tu propia cuenta desde este módulo.")

                    if action == "retirar_accesos":
                        if not set(countries_requested).issubset(countries):
                            raise PermissionDenied("No puedes retirar accesos fuera de tu ámbito autorizado.")
                        cursor.execute(
                            """
                            SELECT pais_codigo,rol,activo
                            FROM contared.accesos
                            WHERE usuario_id=%s AND empresa_id=%s
                              AND pais_codigo=ANY(%s)
                            FOR UPDATE
                            """,
                            [target_id, scope["company_id"], countries_requested],
                        )
                        target_accesses = cursor.fetchall()
                        if len(target_accesses) != len(countries_requested):
                            raise ValidationError({"paises": "Uno de los accesos seleccionados ya no está disponible."})
                        for country, role, active in target_accesses:
                            if role == "administrador" and active:
                                _ensure_another_administrator(
                                    cursor,
                                    scope["company_id"],
                                    country,
                                    target_id,
                                )
                        _set_actor(cursor, scope)
                        cursor.execute(
                            """
                            UPDATE contared.accesos
                            SET activo=FALSE
                            WHERE usuario_id=%s AND empresa_id=%s
                              AND pais_codigo=ANY(%s) AND activo IS TRUE
                            """,
                            [target_id, scope["company_id"], countries_requested],
                        )
                    else:
                        cursor.execute(
                            "SELECT empresa_id::text FROM contared.accesos WHERE usuario_id=%s",
                            [target_id],
                        )
                        target_company_ids = sorted({row[0] for row in cursor.fetchall()})
                        for company_id in target_company_ids:
                            cursor.execute(
                                "SELECT id FROM contared.empresas WHERE id=%s FOR UPDATE",
                                [company_id],
                            )
                            if cursor.fetchone() is None:
                                raise PermissionDenied(
                                    "No se pueden verificar todos los ámbitos autorizados del usuario."
                                )
                        preview = _user_deletion_preview(cursor, scope, target_id, countries)
                        if not preview["puede_eliminar_perfil"]:
                            if preview["tiene_accesos_fuera_del_ambito"]:
                                raise PermissionDenied(preview["motivo_bloqueo"])
                            raise UserDeletionConflict(preview["motivo_bloqueo"])
                        cursor.execute(
                            """
                            SELECT empresa_id::text,pais_codigo,rol,activo
                            FROM contared.accesos
                            WHERE usuario_id=%s
                            FOR UPDATE
                            """,
                            [target_id],
                        )
                        all_accesses = cursor.fetchall()
                        for company_id, country, role, active in all_accesses:
                            if role == "administrador" and active:
                                _ensure_another_administrator(
                                    cursor,
                                    company_id,
                                    country,
                                    target_id,
                                )
                        auth_user_id = profile[1] or preview["_auth_user_id"]
                        if auth_user_id and not profile[1]:
                            cursor.execute(
                                "SELECT lower(email)=lower(%s) FROM auth.users WHERE id=%s FOR UPDATE",
                                [profile[2], auth_user_id],
                            )
                            identity = cursor.fetchone()
                            if identity is None:
                                auth_user_id = None
                            else:
                                cursor.execute(
                                    "SELECT EXISTS (SELECT 1 FROM contared.usuarios WHERE auth_user_id=%s)",
                                    [auth_user_id],
                                )
                                if not identity[0] or cursor.fetchone()[0]:
                                    raise UserDeletionConflict(
                                        "La identidad cambió durante la operación. Revisa el vínculo antes de eliminar."
                                    )
                        if auth_user_id:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM storage.objects WHERE owner_id=%s)",
                                [auth_user_id],
                            )
                            if cursor.fetchone()[0]:
                                raise UserDeletionConflict(
                                    "La identidad es propietaria de archivos en Supabase. "
                                    "Transfiere su propiedad antes de eliminar el usuario."
                                )
                        _set_actor(cursor, scope)
                        cursor.execute(
                            "DELETE FROM contared.accesos WHERE usuario_id=%s",
                            [target_id],
                        )
                        if preview["referencias_historicas"]:
                            cursor.execute(
                                """
                                UPDATE contared.usuarios
                                SET activo=FALSE, auth_user_id=NULL, correo=NULL, correo_pendiente=NULL,
                                    estado_invitacion='no_requerida', invitacion_enviada_en=NULL,
                                    cambio_clave_obligatorio=FALSE
                                WHERE id=%s
                                """,
                                [target_id],
                            )
                        else:
                            cursor.execute(
                                "DELETE FROM contared.usuarios WHERE id=%s",
                                [target_id],
                            )
                        if auth_user_id:
                            # Use the same transaction as the profile and access changes.
                            cursor.execute("DELETE FROM auth.users WHERE id=%s", [auth_user_id])
        except IntegrityError as exc:
            raise UserDeletionConflict(
                "La base de datos rechazó la eliminación por un registro vinculado. "
                "El usuario y sus accesos se conservaron."
            ) from exc
        except DatabaseError as exc:
            _database_failure(exc)

        if action == "retirar_accesos":
            return Response({"mensaje": "Se desactivaron los accesos seleccionados; el historial y los demás accesos se conservaron."})
        message = "Se eliminó definitivamente la cuenta de acceso."
        if preview["referencias_historicas"]:
            message += " El historial conserva el nombre para identificar las operaciones anteriores."
        return Response({"mensaje": message})
