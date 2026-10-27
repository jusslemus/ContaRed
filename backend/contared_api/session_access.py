import logging
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from rest_framework.exceptions import APIException, PermissionDenied


logger = logging.getLogger(__name__)


class DatabaseUnavailable(APIException):
    status_code = 503
    default_detail = (
        "No se pudo consultar la base de ContaRed. Verifica la conexión del backend."
    )
    default_code = "database_unavailable"


def load_session_access(auth_user_id: UUID) -> dict:
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    """
                    SELECT id::text, nombre, activo
                    FROM contared.usuarios
                    WHERE auth_user_id = %s
                    """,
                    [auth_user_id],
                )
                profile = cursor.fetchone()
                if profile is None:
                    raise PermissionDenied(
                        "Tu cuenta de Supabase no tiene un perfil vinculado en ContaRed.",
                        code="profile_not_found",
                    )

                user_id, user_name, active = profile
                if not active:
                    raise PermissionDenied(
                        "Tu perfil de ContaRed está inactivo. Contacta al administrador.",
                        code="profile_inactive",
                    )

                cursor.execute(
                    """
                    SELECT
                        a.empresa_id::text,
                        e.nombre,
                        a.pais_codigo,
                        p.nombre,
                        a.rol
                    FROM contared.accesos AS a
                    INNER JOIN contared.empresas AS e
                        ON e.id = a.empresa_id
                    INNER JOIN contared.paises AS p
                        ON p.codigo = a.pais_codigo
                    WHERE a.usuario_id = %s
                      AND e.activa IS TRUE
                      AND p.codigo IN ('SV', 'GT')
                    ORDER BY p.codigo, e.nombre, a.rol
                    """,
                    [user_id],
                )
                accesses = [
                    {
                        "empresa_id": row[0],
                        "empresa": row[1],
                        "pais_codigo": row[2],
                        "pais": row[3],
                        "rol": row[4],
                    }
                    for row in cursor.fetchall()
                ]

    except DatabaseError as exc:
        logger.error(
            "ContaRed session lookup failed (database error type: %s).",
            type(exc).__name__,
        )
        raise DatabaseUnavailable() from exc

    if not accesses:
        raise PermissionDenied(
            "Tu perfil no tiene accesos activos a empresas de El Salvador o Guatemala.",
            code="no_access",
        )

    authorized_countries = {}
    for access in accesses:
        authorized_countries[access["pais_codigo"]] = access["pais"]

    return {
        "usuario": {"id": user_id, "nombre": user_name},
        "accesos": accesses,
        "paises": [
            {"codigo": code, "nombre": authorized_countries[code]}
            for code in ("SV", "GT")
            if code in authorized_countries
        ],
    }


def resolve_authorized_scope(auth_user_id: UUID, company_id: str, country_code: str) -> dict:
    """Resuelve el perfil y confirma el acceso exacto sin aceptar el ámbito del cliente."""
    try:
        session = load_session_access(auth_user_id)
    except (DatabaseUnavailable, PermissionDenied):
        raise

    if country_code not in ("SV", "GT"):
        raise PermissionDenied("El país seleccionado no es válido.", code="invalid_scope")

    access = next(
        (
            item
            for item in session["accesos"]
            if item["empresa_id"] == company_id
            and item["pais_codigo"] == country_code
        ),
        None,
    )
    if access is None:
        raise PermissionDenied(
            "No tienes acceso autorizado a esta empresa en el país seleccionado.",
            code="scope_not_authorized",
        )

    return {
        "user_id": session["usuario"]["id"],
        "company_id": company_id,
        "country_code": country_code,
        "role": access["rol"],
    }


def require_administrator(scope: dict) -> None:
    if scope["role"] != "administrador":
        raise PermissionDenied(
            "Esta operación requiere el rol administrador.",
            code="administrator_required",
        )
