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

CUENTAS_GT_CONFIRMADAS = {
    "0040040592": ("Banco Industrial", "BIQ", "GTQ"),
    "0040040584": ("Banco Industrial", "BI$3", "GTQ"),
    "090003566": ("Banco GYT Continental", "GYT", "GTQ"),
    "901020461": ("Banco de América Central", "BAC", "GTQ"),
    "3445256003": ("Banco de Desarrollo Rural", "BANRURAL", "GTQ"),
}

CUENTAS_SV_CONFIRMADAS = {
    "9596": ("Banco Agrícola", "AGRICOLA", "USD"),
    "2509": ("Banco Cuscatlán", "CUSCA 2509", "USD"),
    "4978": ("Banco Cuscatlán", "CUSCA 4978", "USD"),
    "0386": ("BAC", "BAC 386", "USD"),
    "6715": ("BAC", "BAC 6715", "USD"),
    "0327": ("BAC", "BAC 327", "USD"),
    "0049": ("Promerica", "PROM 49", "USD"),
    "8202": ("Davivienda", "DAV 68202", "USD"),
    "1962": ("Davivienda", "DAV 962", "USD"),
}


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
        catalogo_id = request.data.get("catalogo_id")
        if catalogo_id:
            if scope["country_code"] not in ("GT", "SV"):
                raise ValidationError({"catalogo_id": "El catálogo completo está disponible para Guatemala y El Salvador."})
            moneda_codigo = str(request.data.get("moneda_codigo") or "").strip().upper() or None
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION READ WRITE")
                        catalog_record_id = catalogo_id.replace("catalogo:", "", 1) if str(catalogo_id).startswith("catalogo:") else catalogo_id
                        if scope["country_code"] == "GT":
                            cursor.execute(
                                "SELECT numero,banco_nombre,alias,moneda_codigo FROM contared.catalogo_cuentas_bancarias WHERE id=%s AND pais_codigo='GT'",
                                [catalog_record_id],
                            )
                        else:
                            cursor.execute(
                                "SELECT terminacion,banco_nombre,alias,moneda_codigo FROM contared.catalogo_cuentas_sv WHERE id=%s AND pais_codigo='SV'",
                                [catalog_record_id],
                            )
                        catalog = cursor.fetchone()
                        if catalog is None:
                            raise ValidationError({"catalogo_id": "La cuenta no pertenece al catálogo autorizado."})
                        numero, banco_nombre, alias, catalog_currency = catalog
                        canonical = CUENTAS_GT_CONFIRMADAS.get(str(numero).strip()) if scope["country_code"] == "GT" else CUENTAS_SV_CONFIRMADAS.get(str(numero).strip())
                        if canonical:
                            banco_nombre, alias, catalog_currency = canonical
                        moneda_codigo = catalog_currency or moneda_codigo
                        if not moneda_codigo:
                            raise ValidationError({"moneda_codigo": "Selecciona una moneda antes de activar esta cuenta."})
                        cursor.execute("SELECT codigo FROM contared.monedas WHERE codigo=%s", [moneda_codigo])
                        if cursor.fetchone() is None:
                            raise ValidationError({"moneda_codigo": "La moneda seleccionada no existe."})
                        if banco_nombre.lower().startswith("banco industrial"):
                            cursor.execute(
                                "SELECT id::text FROM contared.bancos WHERE empresa_id=%s AND pais_codigo=%s AND lower(nombre) LIKE 'banco industrial%' ORDER BY nombre LIMIT 1",
                                [scope["company_id"], scope["country_code"]],
                            )
                        else:
                            cursor.execute(
                                "SELECT id::text FROM contared.bancos WHERE empresa_id=%s AND pais_codigo=%s AND regexp_replace(lower(nombre),'[^a-z0-9]','','g')=regexp_replace(lower(%s),'[^a-z0-9]','','g') LIMIT 1",
                                [scope["company_id"], scope["country_code"], banco_nombre],
                            )
                        bank = cursor.fetchone()
                        if bank is None:
                            cursor.execute(
                                "INSERT INTO contared.bancos(empresa_id,pais_codigo,nombre,activo) VALUES(%s,%s,%s,true) ON CONFLICT (pais_codigo,nombre) DO UPDATE SET activo=true RETURNING id::text",
                                [scope["company_id"], scope["country_code"], banco_nombre],
                            )
                            bank = cursor.fetchone()
                        banco_id = bank[0]
                        cursor.execute(
                            "SELECT id::text,banco_id::text,numero,alias,moneda_codigo,activa,codigo_contable FROM contared.cuentas_bancarias WHERE empresa_id=%s AND pais_codigo=%s AND btrim(numero)=btrim(%s)",
                            [scope["company_id"], scope["country_code"], numero],
                        )
                        existing = cursor.fetchone()
                        if existing:
                            cuenta_id = existing[0]
                            cursor.execute(
                                "UPDATE contared.cuentas_bancarias SET banco_id=%s,moneda_codigo=%s,alias=%s,activa=true WHERE id=%s",
                                [banco_id, moneda_codigo, alias, cuenta_id],
                            )
                            row = (cuenta_id, banco_id, numero, alias, moneda_codigo, True, existing[6])
                        else:
                            cursor.execute(
                                "INSERT INTO contared.cuentas_bancarias(empresa_id,pais_codigo,banco_id,moneda_codigo,numero,alias,activa) VALUES(%s,%s,%s,%s,%s,%s,true) RETURNING id::text,banco_id::text,numero,alias,moneda_codigo,activa,codigo_contable",
                                [scope["company_id"], scope["country_code"], banco_id, moneda_codigo, numero, alias],
                            )
                            row = cursor.fetchone()
                        if scope["country_code"] == "GT":
                            cursor.execute(
                                "INSERT INTO contared.catalogo_cuenta_empresa(catalogo_id,empresa_id) SELECT id,%s FROM contared.catalogo_cuentas_bancarias WHERE id=%s ON CONFLICT DO NOTHING",
                                [scope["company_id"], catalog_record_id],
                            )
            except DatabaseError as exc:
                logger.error("ContaRed catalog account activation failed: %s", type(exc).__name__)
                raise DatabaseUnavailable() from exc
            return Response({
                "id": row[0], "banco_id": row[1], "numero": row[2], "alias": row[3],
                "moneda_codigo": row[4], "activa": row[5], "codigo_contable": row[6],
            }, status=200)
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
                        f"SELECT c.id::text,c.banco_id::text,b.nombre,c.alias,c.numero,c.moneda_codigo,c.activa,b.activo,c.codigo_contable FROM contared.cuentas_bancarias c JOIN contared.bancos b ON b.id=c.banco_id WHERE {where} ORDER BY b.nombre,c.alias LIMIT %s OFFSET %s",
                        params + [page_size, (page - 1) * page_size],
                    )
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("ContaRed accounts lookup failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        operational_results = [{
                "id": row[0], "banco_id": row[1], "banco": row[2], "alias": row[3], "numero": row[4],
                "moneda_codigo": row[5], "activa": row[6], "banco_activo": row[7],
                "codigo_contable": row[8],
            } for row in rows]

        existing_numbers = {str(item["numero"]).strip() for item in operational_results}
        catalog_results = []
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT to_regclass('contared.catalogo_cuentas_retiradas')")
                retired_table = cursor.fetchone()[0] is not None
                if scope["country_code"] == "GT":
                    cursor.execute("SELECT to_regclass('contared.catalogo_cuentas_bancarias')")
                    if cursor.fetchone()[0]:
                        cursor.execute("""
                            SELECT id::text, numero, banco_nombre, alias, moneda_codigo
                            FROM contared.catalogo_cuentas_bancarias
                            WHERE pais_codigo='GT' ORDER BY numero
                        """)
                        for row in cursor.fetchall():
                            catalog_id, numero, banco, alias, moneda = row[:5]
                            if numero in existing_numbers:
                                continue
                            if retired_table:
                                cursor.execute(
                                    "SELECT 1 FROM contared.catalogo_cuentas_retiradas WHERE catalogo_id=%s AND empresa_id=%s AND pais_codigo=%s LIMIT 1",
                                    [catalog_id, scope["company_id"], scope["country_code"]],
                                )
                                if cursor.fetchone():
                                    continue
                            canonical = CUENTAS_GT_CONFIRMADAS.get(str(numero).strip())
                            if canonical:
                                banco, alias, moneda = canonical
                            catalog_results.append({
                                "id": f"catalogo:{catalog_id}", "banco_id": None, "banco": banco,
                                "alias": alias, "numero": numero, "numero_completo": numero,
                                "moneda_codigo": moneda or "Pendiente", "activa": False,
                                "banco_activo": True, "codigo_contable": None,
                                "pendiente_configuracion": moneda is None,
                            })
                else:
                    cursor.execute("SELECT to_regclass('contared.catalogo_cuentas_sv')")
                    if cursor.fetchone()[0]:
                        cursor.execute("""
                            SELECT id::text, banco_nombre, terminacion, alias, moneda_codigo, confirmado
                            FROM contared.catalogo_cuentas_sv
                            WHERE pais_codigo='SV' ORDER BY banco_nombre, terminacion
                        """)
                        for row in cursor.fetchall():
                            catalog_id, banco, terminacion, alias, moneda, confirmado = row[:6]
                            if terminacion in existing_numbers:
                                continue
                            if retired_table:
                                cursor.execute(
                                    "SELECT 1 FROM contared.catalogo_cuentas_retiradas WHERE catalogo_id=%s AND empresa_id=%s AND pais_codigo=%s LIMIT 1",
                                    [catalog_id, scope["company_id"], scope["country_code"]],
                                )
                                if cursor.fetchone():
                                    continue
                            canonical = CUENTAS_SV_CONFIRMADAS.get(str(terminacion).strip())
                            if canonical:
                                banco, alias, moneda = canonical
                            catalog_results.append({
                                "id": f"catalogo:{catalog_id}", "banco_id": None, "banco": banco,
                                "alias": alias, "numero": f"••••{terminacion}", "terminacion": terminacion,
                                "numero_completo": None, "moneda_codigo": moneda or "Pendiente",
                                "activa": False, "banco_activo": True, "codigo_contable": None,
                                "pendiente_configuracion": not bool(confirmado) or moneda in (None, "", "Pendiente"),
                            })
        except DatabaseError:
            catalog_results = []

        if active in ("true", "false"):
            catalog_results = [item for item in catalog_results if item["activa"] == (active == "true")]
        return Response({
            "count": total + len(catalog_results),
            "page": page,
            "page_size": page_size,
            "results": operational_results + catalog_results,
        })

    def post(self, request):
        scope = scope_from_request(request)
        require_administrator(scope)
        catalogo_id = request.data.get("catalogo_id")
        if catalogo_id:
            if scope["country_code"] not in ("GT", "SV"):
                raise ValidationError({"catalogo_id": "El catálogo completo está disponible para Guatemala y El Salvador."})
            catalog_record_id = str(catalogo_id).replace("catalogo:", "", 1)
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION READ WRITE")
                        if scope["country_code"] == "GT":
                            cursor.execute(
                                "SELECT numero,banco_nombre,alias,moneda_codigo FROM contared.catalogo_cuentas_bancarias WHERE id=%s AND pais_codigo='GT'",
                                [catalog_record_id],
                            )
                        else:
                            cursor.execute(
                                "SELECT terminacion,banco_nombre,alias,moneda_codigo FROM contared.catalogo_cuentas_sv WHERE id=%s AND pais_codigo='SV'",
                                [catalog_record_id],
                            )
                        catalog = cursor.fetchone()
                        if catalog is None:
                            raise ValidationError({"catalogo_id": "La cuenta no pertenece al catálogo autorizado."})

                        numero, banco_nombre, alias, catalog_currency = catalog
                        canonical = (
                            CUENTAS_GT_CONFIRMADAS.get(str(numero).strip())
                            if scope["country_code"] == "GT"
                            else CUENTAS_SV_CONFIRMADAS.get(str(numero).strip())
                        )
                        if canonical:
                            banco_nombre, alias, catalog_currency = canonical
                        moneda_codigo = catalog_currency or str(request.data.get("moneda_codigo") or "").strip().upper()
                        if moneda_codigo != ("GTQ" if scope["country_code"] == "GT" else "USD"):
                            raise ValidationError({"moneda_codigo": "La moneda de esta cuenta no coincide con la configuración autorizada."})
                        cursor.execute("SELECT codigo FROM contared.monedas WHERE codigo=%s", [moneda_codigo])
                        if cursor.fetchone() is None:
                            raise ValidationError({"moneda_codigo": "La moneda seleccionada no existe."})

                        if banco_nombre.lower().startswith("banco industrial"):
                            cursor.execute(
                                """
                                SELECT id::text FROM contared.bancos
                                WHERE empresa_id=%s AND pais_codigo=%s
                                  AND lower(nombre) LIKE 'banco industrial%'
                                ORDER BY nombre LIMIT 1
                                """,
                                [scope["company_id"], scope["country_code"]],
                            )
                        else:
                            cursor.execute(
                                """
                                SELECT id::text FROM contared.bancos
                                WHERE empresa_id=%s AND pais_codigo=%s
                                  AND regexp_replace(lower(nombre),'[^a-z0-9]','','g')=regexp_replace(lower(%s),'[^a-z0-9]','','g')
                                LIMIT 1
                                """,
                                [scope["company_id"], scope["country_code"], banco_nombre],
                            )
                        bank = cursor.fetchone()
                        if bank is None:
                            cursor.execute(
                                """
                                INSERT INTO contared.bancos(empresa_id,pais_codigo,nombre,activo)
                                VALUES(%s,%s,%s,true)
                                ON CONFLICT (empresa_id,pais_codigo,nombre) DO UPDATE SET activo=true
                                RETURNING id::text
                                """,
                                [scope["company_id"], scope["country_code"], banco_nombre],
                            )
                            bank = cursor.fetchone()
                        banco_id = bank[0]

                        cursor.execute(
                            """
                            SELECT id::text,banco_id::text,numero,alias,moneda_codigo,activa,codigo_contable
                            FROM contared.cuentas_bancarias
                            WHERE empresa_id=%s AND pais_codigo=%s AND btrim(numero)=btrim(%s)
                            LIMIT 1
                            """,
                            [scope["company_id"], scope["country_code"], numero],
                        )
                        existing = cursor.fetchone()
                        if existing:
                            cuenta_id = existing[0]
                            cursor.execute(
                                """
                                UPDATE contared.cuentas_bancarias
                                SET banco_id=%s, moneda_codigo=%s, alias=%s, activa=true
                                WHERE id=%s AND empresa_id=%s AND pais_codigo=%s
                                """,
                                [banco_id, moneda_codigo, alias, cuenta_id, scope["company_id"], scope["country_code"]],
                            )
                            row = (cuenta_id, banco_id, numero, alias, moneda_codigo, True, existing[6])
                        else:
                            cursor.execute(
                                """
                                INSERT INTO contared.cuentas_bancarias
                                (empresa_id,pais_codigo,banco_id,moneda_codigo,numero,alias,activa)
                                VALUES(%s,%s,%s,%s,%s,%s,true)
                                RETURNING id::text,banco_id::text,numero,alias,moneda_codigo,activa,codigo_contable
                                """,
                                [scope["company_id"], scope["country_code"], banco_id, moneda_codigo, numero, alias],
                            )
                            row = cursor.fetchone()

                        cursor.execute(
                            "DELETE FROM contared.catalogo_cuentas_retiradas WHERE catalogo_id=%s AND empresa_id=%s AND pais_codigo=%s",
                            [catalog_record_id, scope["company_id"], scope["country_code"]],
                        )
                        cursor.execute("SELECT to_regclass('contared.catalogo_cuenta_empresa')")
                        if scope["country_code"] == "GT" and cursor.fetchone()[0]:
                            cursor.execute(
                                "INSERT INTO contared.catalogo_cuenta_empresa(catalogo_id,empresa_id) SELECT id,%s FROM contared.catalogo_cuentas_bancarias WHERE id=%s ON CONFLICT DO NOTHING",
                                [scope["company_id"], catalog_record_id],
                            )
            except DatabaseError as exc:
                logger.error("ContaRed catalog account activation failed: %s", type(exc).__name__)
                raise DatabaseUnavailable() from exc
            return Response({
                "id": row[0], "banco_id": row[1], "numero": row[2], "alias": row[3],
                "moneda_codigo": row[4], "activa": row[5], "codigo_contable": row[6],
            }, status=200)
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
        codigo_contable = request.data.get("codigo_contable")
        if codigo_contable is not None:
            if not isinstance(codigo_contable, str) or not codigo_contable.strip():
                raise ValidationError({"codigo_contable": "El código contable debe ser texto no vacío o nulo."})
            codigo_contable = codigo_contable.strip()
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
                        "INSERT INTO contared.cuentas_bancarias(empresa_id,pais_codigo,banco_id,moneda_codigo,numero,alias,codigo_contable,activa) VALUES (%s,%s,%s,%s,%s,%s,%s,true) RETURNING id::text,banco_id::text,numero,alias,moneda_codigo,activa,codigo_contable",
                        [scope["company_id"], scope["country_code"], banco_id, moneda_codigo, numero, alias, codigo_contable],
                    )
                    row = cursor.fetchone()
        except IntegrityError:
            raise ValidationError({"numero": "Ya existe una cuenta con ese número en el banco seleccionado."})
        except DatabaseError as exc:
            logger.error("ContaRed account creation failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({
            "id": row[0], "banco_id": row[1], "numero": row[2], "alias": row[3],
            "moneda_codigo": row[4], "activa": row[5], "codigo_contable": row[6],
        }, status=201)


class CatalogAccountRetirementView(APIView):
    def delete(self, request, catalog_id):
        scope = scope_from_request(request)
        require_administrator(scope)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ WRITE")
                    cursor.execute("SELECT to_regclass('contared.catalogo_cuentas_retiradas')")
                    if cursor.fetchone()[0] is None:
                        raise ContaredConflict()

                    if scope["country_code"] == "GT":
                        cursor.execute(
                            "SELECT id::text FROM contared.catalogo_cuentas_bancarias WHERE id=%s AND pais_codigo='GT'",
                            [catalog_id],
                        )
                    else:
                        cursor.execute(
                            "SELECT id::text FROM contared.catalogo_cuentas_sv WHERE id=%s AND pais_codigo='SV'",
                            [catalog_id],
                        )
                    if cursor.fetchone() is None:
                        raise NotFound("La entrada no pertenece al catálogo del país seleccionado.")

                    cursor.execute(
                        """
                        INSERT INTO contared.catalogo_cuentas_retiradas
                          (catalogo_id, empresa_id, pais_codigo, retirada_por)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (catalogo_id, empresa_id, pais_codigo) DO UPDATE
                        SET retirada_en=now(), retirada_por=EXCLUDED.retirada_por
                        """,
                        [catalog_id, scope["company_id"], scope["country_code"], scope["user_id"]],
                    )
        except IntegrityError as exc:
            raise ContaredConflict() from exc
        except DatabaseError as exc:
            logger.error("ContaRed catalog account retirement failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response(status=204)


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
                    dependent_tables = (
                        "conciliaciones", "cargas", "movimientos", "cruces", "pendientes",
                        "ajustes", "reportes", "cheques", "casos_deposito",
                        "trabajos_conciliacion_directa",
                    )
                    has_dependencies = False
                    for table_name in dependent_tables:
                        cursor.execute("SELECT to_regclass(%s)", [f"contared.{table_name}"])
                        regclass = cursor.fetchone()[0]
                        if not regclass:
                            continue
                        cursor.execute(
                            "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='contared' AND table_name=%s AND column_name='cuenta_id')",
                            [table_name],
                        )
                        if not cursor.fetchone()[0]:
                            continue
                        cursor.execute(
                            f'SELECT EXISTS(SELECT 1 FROM contared.{table_name} WHERE cuenta_id=%s LIMIT 1)',
                            [account_id],
                        )
                        if cursor.fetchone()[0]:
                            has_dependencies = True
                            break
                    if has_dependencies:
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
        if allowed - {"alias", "activa", "codigo_contable", "empresa_id", "pais_codigo"}:
            raise ValidationError({"detail": "Solo se puede cambiar el alias, el código contable o el estado de la cuenta."})
        alias = request.data.get("alias")
        activa = request.data.get("activa")
        codigo_contable = request.data.get("codigo_contable")
        if alias is not None and not str(alias).strip():
            raise ValidationError({"alias": "El alias es obligatorio."})
        if codigo_contable is not None:
            if not isinstance(codigo_contable, str) or not codigo_contable.strip():
                raise ValidationError({"codigo_contable": "El código contable debe ser texto no vacío o nulo."})
            codigo_contable = codigo_contable.strip()
        if activa is not None and not isinstance(activa, bool):
            raise ValidationError({"activa": "El estado debe ser verdadero o falso."})
        if alias is None and activa is None and "codigo_contable" not in request.data:
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
                    if "codigo_contable" in request.data:
                        assignments.append("codigo_contable=%s")
                        params.append(codigo_contable)
                    params.extend([account_id, scope["company_id"], scope["country_code"]])
                    cursor.execute(
                        f"UPDATE contared.cuentas_bancarias SET {','.join(assignments)} WHERE id=%s AND empresa_id=%s AND pais_codigo=%s RETURNING id::text,numero,alias,moneda_codigo,activa,codigo_contable",
                        params,
                    )
                    row = cursor.fetchone()
        except IntegrityError:
            raise ValidationError({"detail": "No se pudo guardar la cuenta por una restricción existente."})
        except DatabaseError as exc:
            logger.error("ContaRed account update failed (database error type: %s).", type(exc).__name__)
            raise DatabaseUnavailable() from exc
        return Response({"id": row[0], "numero": row[1], "alias": row[2], "moneda_codigo": row[3], "activa": row[4], "codigo_contable": row[5]})
