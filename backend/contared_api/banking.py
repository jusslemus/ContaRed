import logging

from django.db import DatabaseError, IntegrityError, transaction
from django.db import connection
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.session_access import (
    DatabaseUnavailable,
    resolve_authorized_scope,
    require_administrator,
)

logger = logging.getLogger(__name__)


def scope_from_request(request):
    company_id = request.query_params.get("empresa_id")
    country_code = request.query_params.get("pais_codigo")
    if request.method in ("POST", "PATCH"):
        body = request.data if hasattr(request, "data") else {}
        company_id = body.get("empresa_id", company_id)
        country_code = body.get("pais_codigo", country_code)
    if not company_id or not country_code:
        raise ValidationError({"detail": "Se requiere empresa y país autorizados."})
    return resolve_authorized_scope(request.user.auth_user_id, str(company_id), str(country_code).upper())


def paginate(cursor, count_query, rows_query, params, page, page_size):
    cursor.execute(count_query, params)
    total = cursor.fetchone()[0]
    cursor.execute(rows_query, params + [page_size, (page - 1) * page_size])
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": cursor.fetchall(),
    }


class BankListCreateView(APIView):
    def get(self, request):
        scope = scope_from_request(request)
        page = max(int(request.query_params.get("page", 1)), 1)
        page_size = min(max(int(request.query_params.get("page_size", 25)), 1), 100)
        search = (request.query_params.get("search") or "").strip()
        active = request.query_params.get("active")
        clauses = ["pais_codigo = %s"]
        params = [scope["country_code"]]
        if search:
            clauses.append("nombre ILIKE %s")
            params.append(f"%{search}%")
        if active in ("true", "false"):
            clauses.append("activo = %s")
            params.append(active == "true")
        where = " AND ".join(clauses)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    data = paginate(
                        cursor,
                        f"SELECT COUNT(*) FROM contared.bancos WHERE {where}",
                        f"SELECT id::text, nombre, activo FROM contared.bancos WHERE {where} ORDER BY nombre LIMIT %s OFFSET %s",
                        params,
                        page,
                        page_size,
                    )
        except DatabaseError as exc:
            logger.error("ContaRed banks lookup failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            **{k: data[k] for k in ("count", "page", "page_size")},
            "results": [
                {"id": row[0], "nombre": row[1], "activo": row[2]}
                for row in data["results"]
            ],
        })

    def post(self, request):
        scope = scope_from_request(request)
        require_administrator(scope)
        nombre = str(request.data.get("nombre", "")).strip()
        if not nombre:
            raise ValidationError({"nombre": "El nombre del banco es obligatorio."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    cursor.execute(
                        "INSERT INTO contared.bancos(pais_codigo,nombre,activo) VALUES (%s,%s,true) RETURNING id::text,nombre,activo",
                        [scope["country_code"], nombre],
                    )
                    row = cursor.fetchone()
        except IntegrityError:
            raise ValidationError({"nombre": "Ya existe un banco con ese nombre en el país seleccionado."})
        except DatabaseError as exc:
            logger.error("ContaRed bank creation failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({"id": row[0], "nombre": row[1], "activo": row[2]}, status=201)


class ContaredConflict(APIException):
    status_code = 409
    default_detail = "Este registro tiene información asociada. Puedes desactivarlo"
    default_code = "record_in_use"


class BankDetailView(APIView):
    def delete(self, request, bank_id):
        scope = scope_from_request(request)
        require_administrator(scope)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id::text FROM contared.bancos WHERE id=%s AND pais_codigo=%s",
                        [bank_id, scope["country_code"]],
                    )
                    if cursor.fetchone() is None:
                        raise NotFound("El banco no pertenece al país seleccionado.")
                    cursor.execute(
                        """
                        SELECT EXISTS(SELECT 1 FROM contared.cuentas_bancarias WHERE banco_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.perfiles_importacion WHERE banco_id=%s)
                        """,
                        [bank_id, bank_id],
                    )
                    if cursor.fetchone()[0]:
                        raise ContaredConflict()
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    cursor.execute("DELETE FROM contared.bancos WHERE id=%s AND pais_codigo=%s", [bank_id, scope["country_code"]])
                    if cursor.rowcount != 1:
                        raise ContaredConflict()
        except IntegrityError as exc:
            raise ContaredConflict() from exc
        except DatabaseError as exc:
            logger.error("ContaRed bank deletion failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response(status=204)

    def patch(self, request, bank_id):
        scope = scope_from_request(request)
        require_administrator(scope)
        allowed = set(request.data.keys())
        if allowed - {"nombre", "activo", "empresa_id", "pais_codigo"}:
            raise ValidationError({"detail": "Solo se puede cambiar el nombre o el estado del banco."})
        nombre = request.data.get("nombre")
        activo = request.data.get("activo")
        if nombre is not None and not str(nombre).strip():
            raise ValidationError({"nombre": "El nombre del banco es obligatorio."})
        if activo is not None and not isinstance(activo, bool):
            raise ValidationError({"activo": "El estado debe ser verdadero o falso."})
        if nombre is None and activo is None:
            raise ValidationError({"detail": "No se indicó ningún cambio."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id::text FROM contared.bancos WHERE id=%s AND pais_codigo=%s",
                        [bank_id, scope["country_code"]],
                    )
                    if cursor.fetchone() is None:
                        raise NotFound("El banco no pertenece al país seleccionado.")
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    assignments = []
                    params = []
                    if nombre is not None:
                        assignments.append("nombre=%s")
                        params.append(str(nombre).strip())
                    if activo is not None:
                        assignments.append("activo=%s")
                        params.append(activo)
                    params.extend([bank_id, scope["country_code"]])
                    cursor.execute(
                        f"UPDATE contared.bancos SET {','.join(assignments)} WHERE id=%s AND pais_codigo=%s RETURNING id::text,nombre,activo",
                        params,
                    )
                    row = cursor.fetchone()
        except IntegrityError:
            raise ValidationError({"nombre": "Ya existe un banco con ese nombre en el país seleccionado."})
        except DatabaseError as exc:
            logger.error("ContaRed bank update failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({"id": row[0], "nombre": row[1], "activo": row[2]})


class CurrencyListView(APIView):
    def get(self, request):
        scope_from_request(request)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SELECT codigo,nombre FROM contared.monedas ORDER BY codigo")
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("ContaRed currencies lookup failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response([{"codigo": row[0], "nombre": row[1]} for row in rows])


class AccountListCreateView(APIView):
    def get(self, request):
        scope = scope_from_request(request)
        page = max(int(request.query_params.get("page", 1)), 1)
        page_size = min(max(int(request.query_params.get("page_size", 25)), 1), 100)
        search = (request.query_params.get("search") or "").strip()
        active = request.query_params.get("active")
        clauses = ["c.empresa_id = %s", "c.pais_codigo = %s"]
        params = [scope["company_id"], scope["country_code"]]
        if search:
            clauses.append("(c.numero ILIKE %s OR c.alias ILIKE %s OR b.nombre ILIKE %s)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        if active in ("true", "false"):
            clauses.append("c.activa = %s")
            params.append(active == "true")
        where = " AND ".join(clauses)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(f"SELECT COUNT(*) FROM contared.cuentas_bancarias c JOIN contared.bancos b ON b.id=c.banco_id WHERE {where}", params)
                    total = cursor.fetchone()[0]
                    cursor.execute(
                        f"SELECT c.id::text,c.banco_id::text,b.nombre,c.alias,c.numero,c.moneda_codigo,c.activa,b.activo FROM contared.cuentas_bancarias c JOIN contared.bancos b ON b.id=c.banco_id WHERE {where} ORDER BY b.nombre,c.alias LIMIT %s OFFSET %s",
                        params + [page_size, (page - 1) * page_size],
                    )
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("ContaRed accounts lookup failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            "count": total, "page": page, "page_size": page_size,
            "results": [{
                "id": row[0], "banco_id": row[1], "banco": row[2], "alias": row[3], "numero": row[4],
                "moneda_codigo": row[5], "activa": row[6], "banco_activo": row[7],
            } for row in rows],
        })

    def post(self, request):
        scope = scope_from_request(request)
        require_administrator(scope)
        banco_id = request.data.get("banco_id")
        moneda_codigo = request.data.get("moneda_codigo")
        numero = request.data.get("numero")
        alias = str(request.data.get("alias", "")).strip()
        if not banco_id or not moneda_codigo:
            raise ValidationError({"detail": "Se requiere banco y moneda."})
        if not isinstance(numero, str) or numero.strip() == "":
            raise ValidationError({"numero": "El número de cuenta es obligatorio y se conserva como texto."})
        numero = numero.strip()
        if not alias:
            raise ValidationError({"alias": "El alias es obligatorio."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT activo FROM contared.bancos WHERE id=%s AND pais_codigo=%s", [banco_id, scope["country_code"]])
                    bank = cursor.fetchone()
                    if bank is None:
                        raise ValidationError({"banco_id": "El banco no pertenece al país seleccionado."})
                    if not bank[0]:
                        raise ValidationError({"banco_id": "No se pueden crear cuentas en un banco inactivo."})
                    cursor.execute("SELECT codigo FROM contared.monedas WHERE codigo=%s", [moneda_codigo])
                    if cursor.fetchone() is None:
                        raise ValidationError({"moneda_codigo": "La moneda seleccionada no existe en el catálogo."})
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    cursor.execute(
                        "INSERT INTO contared.cuentas_bancarias(empresa_id,pais_codigo,banco_id,moneda_codigo,numero,alias,activa) VALUES (%s,%s,%s,%s,%s,%s,true) RETURNING id::text,banco_id::text,numero,alias,moneda_codigo,activa",
                        [scope["company_id"], scope["country_code"], banco_id, moneda_codigo, numero, alias],
                    )
                    row = cursor.fetchone()
        except IntegrityError:
            raise ValidationError({"numero": "Ya existe una cuenta con ese número en el banco seleccionado."})
        except DatabaseError as exc:
            logger.error("ContaRed account creation failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            "id": row[0], "banco_id": row[1], "numero": row[2], "alias": row[3],
            "moneda_codigo": row[4], "activa": row[5],
        }, status=201)


class AccountDetailView(APIView):
    def delete(self, request, account_id):
        scope = scope_from_request(request)
        require_administrator(scope)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id::text FROM contared.cuentas_bancarias WHERE id=%s AND empresa_id=%s AND pais_codigo=%s",
                        [account_id, scope["company_id"], scope["country_code"]],
                    )
                    if cursor.fetchone() is None:
                        raise NotFound("La cuenta no pertenece al ámbito seleccionado.")
                    cursor.execute(
                        """
                        SELECT EXISTS(SELECT 1 FROM contared.conciliaciones WHERE cuenta_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.cargas WHERE cuenta_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.movimientos WHERE cuenta_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.cruces WHERE cuenta_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.pendientes WHERE cuenta_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.ajustes WHERE cuenta_id=%s)
                           OR EXISTS(SELECT 1 FROM contared.reportes WHERE cuenta_id=%s)
                        """,
                        [account_id] * 7,
                    )
                    if cursor.fetchone()[0]:
                        raise ContaredConflict()
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    cursor.execute(
                        "DELETE FROM contared.cuentas_bancarias WHERE id=%s AND empresa_id=%s AND pais_codigo=%s",
                        [account_id, scope["company_id"], scope["country_code"]],
                    )
                    if cursor.rowcount != 1:
                        raise ContaredConflict()
        except IntegrityError as exc:
            raise ContaredConflict() from exc
        except DatabaseError as exc:
            logger.error("ContaRed account deletion failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response(status=204)

    def patch(self, request, account_id):
        scope = scope_from_request(request)
        require_administrator(scope)
        allowed = set(request.data.keys())
        if allowed - {"alias", "activa", "empresa_id", "pais_codigo"}:
            raise ValidationError({"detail": "Solo se puede cambiar el alias o el estado de la cuenta."})
        alias = request.data.get("alias")
        activa = request.data.get("activa")
        if alias is not None and not str(alias).strip():
            raise ValidationError({"alias": "El alias es obligatorio."})
        if activa is not None and not isinstance(activa, bool):
            raise ValidationError({"activa": "El estado debe ser verdadero o falso."})
        if alias is None and activa is None:
            raise ValidationError({"detail": "No se indicó ningún cambio."})
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT c.id::text,b.activo FROM contared.cuentas_bancarias c JOIN contared.bancos b ON b.id=c.banco_id WHERE c.id=%s AND c.empresa_id=%s AND c.pais_codigo=%s",
                        [account_id, scope["company_id"], scope["country_code"]],
                    )
                    account = cursor.fetchone()
                    if account is None:
                        raise NotFound("La cuenta no pertenece al ámbito seleccionado.")
                    if activa is True and not account[1]:
                        raise ValidationError({"activa": "No puedes activar una cuenta cuyo banco está inactivo."})
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [scope["user_id"]])
                    assignments = []
                    params = []
                    if alias is not None:
                        assignments.append("alias=%s")
                        params.append(str(alias).strip())
                    if activa is not None:
                        assignments.append("activa=%s")
                        params.append(activa)
                    params.extend([account_id, scope["company_id"], scope["country_code"]])
                    cursor.execute(
                        f"UPDATE contared.cuentas_bancarias SET {','.join(assignments)} WHERE id=%s AND empresa_id=%s AND pais_codigo=%s RETURNING id::text,numero,alias,moneda_codigo,activa",
                        params,
                    )
                    row = cursor.fetchone()
        except IntegrityError:
            raise ValidationError({"detail": "No se pudo guardar la cuenta por una restricción existente."})
        except DatabaseError as exc:
            logger.error("ContaRed account update failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({"id": row[0], "numero": row[1], "alias": row[2], "moneda_codigo": row[3], "activa": row[4]})
