import hashlib
import json
import logging
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from django.db import DatabaseError, IntegrityError, connection, transaction
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.conciliaciones import (
    _is_banco_industrial,
    _normalize_original_data,
    _scope_for_reconciliation,
    _transaction_type_label,
    _valid_identifier,
)
from contared_api.cheques import ChequeCrossSync
from contared_api.depositos import _ambiguous_comparison_options
from contared_api.reconciliation import Movement, Proposal, propose_reconciliation
from contared_api.session_access import DatabaseUnavailable


logger = logging.getLogger(__name__)


class ComparisonMigrationRequired(APIException):
    status_code = 503
    default_detail = (
        "La comparación requiere aplicar "
        "supabase/migrations/202610290001_comparacion_movimientos.sql."
    )
    default_code = "comparison_migration_required"


def _database_error(exc):
    cause = exc.__cause__
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if sqlstate in ("42P01", "42703"):
        raise ComparisonMigrationRequired() from exc
    logger.error(
        "ContaRed movement comparison failed (database error type: %s).",
        type(exc).__name__,
    )
    raise DatabaseUnavailable() from exc


def _current_reconciliation(cursor, request, reconciliation_id, *, lock):
    suffix = " FOR UPDATE OF r" if lock else ""
    cursor.execute(
        f"""
        SELECT r.id::text,r.cuenta_id::text,cb.empresa_id::text,cb.pais_codigo,
               cb.moneda_codigo,r.periodo,r.fecha_corte,r.estado,b.nombre
        FROM contared.conciliaciones r
        JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
        JOIN contared.bancos b ON b.id=cb.banco_id
        WHERE r.id=%s
        {suffix}
        """,
        [reconciliation_id],
    )
    row = cursor.fetchone()
    if row is None:
        raise NotFound("La conciliación no existe o su cuenta no está activa.")

    cursor.execute(
        f"""
        SELECT u.id::text,a.rol,u.nombre
        FROM contared.usuarios u
        JOIN contared.accesos a ON a.usuario_id=u.id
        JOIN contared.empresas e ON e.id=a.empresa_id
        WHERE u.auth_user_id=%s AND u.activo IS TRUE
          AND e.activa IS TRUE AND a.activo IS TRUE
          AND a.empresa_id=%s AND a.pais_codigo=%s
        {"FOR SHARE OF u,a,e" if lock else ""}
        """,
        [request.user.auth_user_id, row[2], row[3]],
    )
    access = cursor.fetchone()
    if access is None:
        raise NotFound("La conciliación no existe.")
    return {
        "reconciliation_id": row[0],
        "account_id": row[1],
        "company_id": row[2],
        "country_code": row[3],
        "currency_code": row[4],
        "period": row[5],
        "cutoff": row[6],
        "state": row[7],
        "bank_name": row[8],
        "user_id": access[0],
        "role": access[1],
        "user_name": access[2],
    }


def _require_role(scope, roles):
    if scope["role"] not in roles:
        raise PermissionDenied("Tu rol no permite esta operación.", code="role_not_permitted")


def _decode_json_ids(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise APIException("La propuesta guardada tiene una estructura inválida.")
    try:
        identifiers = [str(UUID(item)) for item in value]
    except ValueError as exc:
        raise APIException("La propuesta guardada contiene un identificador inválido.") from exc
    if len(set(identifiers)) != len(identifiers):
        raise APIException("La propuesta guardada contiene movimientos repetidos.")
    return identifiers


def _movement_rows(cursor, scope, *, lock=False):
    lock_clause = " FOR UPDATE OF m,c" if lock else ""
    cursor.execute(
        f"""
        SELECT m.id::text,m.lado,m.fecha,m.fecha_valor,m.referencia,
               m.numero_transaccion,m.numero_cheque,m.codigo_tt,m.descripcion,
               m.monto::text,m.saldo_origen::text,m.hoja_origen,m.fila_origen,
               m.datos_originales,c.id::text,c.origen,c.metodo,c.nombre_archivo,
               c.estado,c.creada_en,u.nombre,p.configuracion,
               EXISTS (
                 SELECT 1
                 FROM contared.cruce_movimientos cm
                 JOIN contared.cruces x ON x.id=cm.cruce_id
                 WHERE cm.movimiento_id=m.id AND x.estado='confirmado'
               )
        FROM contared.movimientos m
        JOIN contared.cargas c ON c.id=m.carga_id
        JOIN contared.conciliaciones r
          ON r.id=c.conciliacion_id AND r.cuenta_id=c.cuenta_id
        LEFT JOIN contared.perfiles_importacion p ON p.id=c.perfil_id
        LEFT JOIN contared.usuarios u
          ON u.id=c.creada_por AND u.activo IS TRUE
         AND EXISTS (
           SELECT 1 FROM contared.accesos a
           WHERE a.usuario_id=u.id
             AND a.empresa_id=c.empresa_id
             AND a.pais_codigo=c.pais_codigo
         )
        WHERE m.cuenta_id=%s AND c.conciliacion_id=%s
          AND c.empresa_id=%s AND c.pais_codigo=%s
          AND c.estado='validada'
          AND ((c.origen='banco' AND m.lado='banco')
            OR (c.origen='libros' AND m.lado='libros'))
          AND c.origen IN ('banco','libros')
          AND m.fecha >= r.periodo AND m.fecha <= r.fecha_corte
        ORDER BY m.lado,m.fecha,m.id
        {lock_clause}
        """,
        [
            scope["account_id"],
            scope["reconciliation_id"],
            scope["company_id"],
            scope["country_code"],
        ],
    )
    rows = cursor.fetchall()
    return [_movement_from_row(row, scope.get("bank_name")) for row in rows]


def _movement_from_row(row, bank_name):
    original = _normalize_original_data(row[13]) or {}
    profile = row[21] or {}
    if isinstance(profile, str):
        profile = json.loads(profile)
    code = row[7]
    number = row[6]
    if (
        not _valid_identifier(number) and row[1] == "banco" and code == "CQ"
        and _is_banco_industrial(bank_name)
        and _valid_identifier(row[4])
    ):
        number = row[4]
    return {
        "id": row[0],
        "lado": row[1],
        "fecha": row[2].isoformat(),
        "fecha_valor": row[3].isoformat() if row[3] else None,
        "referencia": row[4],
        "numero_transaccion": row[5],
        "numero_cheque": number,
        "codigo_tt": code,
        "tipo_transaccion": _transaction_type_label(code, profile, bank_name),
        "descripcion": row[8],
        "monto": row[9],
        "saldo_origen": row[10],
        "hoja_origen": row[11],
        "fila_origen": row[12],
        "datos_originales": original,
        "fuente": {
            "carga_id": row[14],
            "origen": row[15],
            "metodo": row[16],
            "nombre_archivo": row[17],
            "estado": row[18],
            "creada_en": row[19].isoformat() if row[19] else None,
            "creada_por_nombre": row[20],
        },
        "confirmado": bool(row[22]),
    }


def _engine_movements(movements, side, account_id):
    engine_side = {"banco": "bank", "libros": "books"}[side]
    return [
        Movement(
            id=item["id"],
            account_id=account_id,
            side=engine_side,
            transaction_date=date.fromisoformat(item["fecha"]),
            amount=Decimal(item["monto"]),
            reference=item["referencia"] if _valid_identifier(item["referencia"]) else None,
        )
        for item in movements
        if item["lado"] == side
    ]


def _eligible_movements(movements):
    return [item for item in movements if not item["confirmado"]]


def _dataset_fingerprint(movements, scope):
    payload = [
        [
            item["id"],
            item["lado"],
            item["fecha"],
            item["fuente"]["carga_id"],
            item["monto"],
            item["referencia"],
            item["numero_transaccion"],
            item["numero_cheque"],
            item["codigo_tt"],
            item["confirmado"],
        ]
        for item in movements
    ]
    encoded = json.dumps(
        {
            "account": scope["account_id"],
            "company": scope["company_id"],
            "country": scope["country_code"],
            "currency": scope["currency_code"],
            "period": scope["period"].isoformat(),
            "cutoff": scope["cutoff"].isoformat(),
            "movements": payload,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _proposal_key(proposal: Proposal):
    encoded = json.dumps(
        [
            proposal.rule,
            sorted(proposal.bank_ids),
            sorted(proposal.book_ids),
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_text(values):
    return json.dumps(list(values), separators=(",", ":"))


def _proposal_response(row, movements_by_id):
    bank_ids = _decode_json_ids(row[2])
    book_ids = _decode_json_ids(row[3])
    bank = [movements_by_id[item] for item in bank_ids if item in movements_by_id]
    books = [movements_by_id[item] for item in book_ids if item in movements_by_id]
    if len(bank) != len(bank_ids) or len(books) != len(book_ids):
        raise APIException("No se pudieron cargar todos los movimientos de una propuesta.")
    bank_total = sum((Decimal(item["monto"]) for item in bank), Decimal("0"))
    books_total = sum((Decimal(item["monto"]) for item in books), Decimal("0"))
    alternatives = []
    alternatives_truncated = False
    if row[1] == "ambiguous":
        alternatives, alternatives_truncated = _ambiguous_comparison_options(
            bank, books
        )
    return {
        "id": row[0],
        "regla": row[1],
        "estado": row[4],
        "explicacion": row[5],
        "banco": bank,
        "libros": books,
        "suma_banco": None if row[1] == "ambiguous" else str(bank_total),
        "suma_libros": None if row[1] == "ambiguous" else str(books_total),
        "diferencia": None if row[1] == "ambiguous" else str(bank_total - books_total),
        "alternativas": alternatives,
        "alternativas_truncadas": alternatives_truncated,
        "cruce_id": row[6],
        "resuelta_en": row[7].isoformat() if row[7] else None,
        "motivo_descarte": row[8],
        "resuelta_por_nombre": row[9],
    }


def _latest_comparison(cursor, scope):
    cursor.execute(
        """
        SELECT id::text,huella_datos,creada_en,ultima_ejecucion_en
        FROM contared.ejecuciones_comparacion
        WHERE conciliacion_id=%s AND cuenta_id=%s
        ORDER BY ultima_ejecucion_en DESC,id DESC
        LIMIT 1
        """,
        [scope["reconciliation_id"], scope["account_id"]],
    )
    return cursor.fetchone()


def _results_for_execution(cursor, scope, execution_id):
    cursor.execute(
        """
        SELECT p.id::text,p.regla_codigo,p.banco_ids,p.libros_ids,p.estado,
               p.explicacion,p.cruce_id::text,p.resuelta_en,p.motivo_descarte,
               u.nombre
        FROM contared.propuestas_comparacion p
        LEFT JOIN contared.usuarios u ON u.id=p.resuelta_por
        WHERE p.id IN (
          SELECT ep.propuesta_id
          FROM contared.ejecucion_propuestas ep
          WHERE ep.ejecucion_id=%s AND ep.conciliacion_id=%s
            AND ep.cuenta_id=%s
        )
        OR (
          p.conciliacion_id=%s AND p.cuenta_id=%s
          AND p.estado IN ('confirmada','descartada')
        )
        ORDER BY p.creada_en,p.id
        """,
        [
            execution_id,
            scope["reconciliation_id"],
            scope["account_id"],
            scope["reconciliation_id"],
            scope["account_id"],
        ],
    )
    proposals = cursor.fetchall()
    all_ids = sorted({
        movement_id
        for row in proposals
        for movement_id in (*_decode_json_ids(row[2]), *_decode_json_ids(row[3]))
    })
    movements_by_id = {}
    if all_ids:
        cursor.execute(
            """
            SELECT m.id::text,m.lado,m.fecha,m.fecha_valor,m.referencia,
                   m.numero_transaccion,m.numero_cheque,m.codigo_tt,m.descripcion,
                   m.monto::text,m.saldo_origen::text,m.hoja_origen,m.fila_origen,
                   m.datos_originales,c.id::text,c.origen,c.metodo,c.nombre_archivo,
                   c.estado,c.creada_en,u.nombre,p.configuracion,
                   EXISTS (
                     SELECT 1 FROM contared.cruce_movimientos cm
                     JOIN contared.cruces x ON x.id=cm.cruce_id
                     WHERE cm.movimiento_id=m.id AND x.estado='confirmado'
                   )
            FROM contared.movimientos m
            JOIN contared.cargas c ON c.id=m.carga_id
            LEFT JOIN contared.perfiles_importacion p ON p.id=c.perfil_id
            LEFT JOIN contared.usuarios u ON u.id=c.creada_por AND u.activo IS TRUE
             AND EXISTS (
               SELECT 1 FROM contared.accesos a
               WHERE a.usuario_id=u.id AND a.empresa_id=c.empresa_id
                 AND a.pais_codigo=c.pais_codigo
             )
            WHERE m.id=ANY(%s::uuid[]) AND m.cuenta_id=%s
              AND c.conciliacion_id=%s AND c.empresa_id=%s AND c.pais_codigo=%s
            """,
            [
                all_ids,
                scope["account_id"],
                scope["reconciliation_id"],
                scope["company_id"],
                scope["country_code"],
            ],
        )
        movements_by_id = {
            item["id"]: item for item in (
                _movement_from_row(row, scope.get("bank_name"))
                for row in cursor.fetchall()
            )
        }
    return [_proposal_response(row, movements_by_id) for row in proposals]


class ReconciliationComparisonView(APIView):
    def get(self, request, reconciliation_id):
        _scope_for_reconciliation(request, reconciliation_id)
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    current = _current_reconciliation(
                        cursor, request, reconciliation_id, lock=False
                    )
                    execution = _latest_comparison(cursor, current)
                    if execution is None:
                        return Response({"ejecucion": None, "results": []})
                    return Response({
                        "ejecucion": {
                            "id": execution[0],
                            "huella_datos": execution[1],
                            "creada_en": execution[2].isoformat(),
                            "ultima_ejecucion_en": execution[3].isoformat(),
                        },
                        "results": _results_for_execution(cursor, current, execution[0]),
                    })
        except DatabaseError as exc:
            _database_error(exc)

    def post(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, ("administrador", "auxiliar", "revisor"))
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    current = _current_reconciliation(
                        cursor, request, reconciliation_id, lock=True
                    )
                    _require_role(current, ("administrador", "auxiliar", "revisor"))
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [current["user_id"]])
                    cursor.execute(
                        """
                        SELECT count(*) FILTER (WHERE origen='banco'),
                               count(*) FILTER (WHERE origen='libros')
                        FROM contared.cargas
                        WHERE conciliacion_id=%s AND cuenta_id=%s
                          AND empresa_id=%s AND pais_codigo=%s
                          AND estado='validada'
                        """,
                        [
                            current["reconciliation_id"],
                            current["account_id"],
                            current["company_id"],
                            current["country_code"],
                        ],
                    )
                    bank_loads, books_loads = cursor.fetchone()
                    if not bank_loads or not books_loads:
                        raise ValidationError(
                            "Compara movimientos cuando exista al menos una carga validada de Banco y de Libros."
                        )
                    movements = _movement_rows(cursor, current, lock=True)
                    fingerprint = _dataset_fingerprint(movements, current)
                    cursor.execute(
                        """
                        SELECT id::text
                        FROM contared.ejecuciones_comparacion
                        WHERE conciliacion_id=%s AND huella_datos=%s
                        """,
                        [current["reconciliation_id"], fingerprint],
                    )
                    execution = cursor.fetchone()
                    if execution is None:
                        execution_id = str(uuid4())
                        cursor.execute(
                            """
                            INSERT INTO contared.ejecuciones_comparacion
                              (id,cuenta_id,conciliacion_id,moneda_codigo,periodo,
                               fecha_corte,huella_datos,creada_por)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            [
                                execution_id,
                                current["account_id"],
                                current["reconciliation_id"],
                                current["currency_code"],
                                current["period"],
                                current["cutoff"],
                                fingerprint,
                                current["user_id"],
                            ],
                        )
                        eligible = _eligible_movements(movements)
                        proposals = propose_reconciliation(
                            _engine_movements(
                                eligible, "banco", current["account_id"]
                            ),
                            _engine_movements(
                                eligible, "libros", current["account_id"]
                            ),
                        )
                        for proposal in proposals:
                            key = _proposal_key(proposal)
                            proposal_id = str(uuid4())
                            cursor.execute(
                                """
                                INSERT INTO contared.propuestas_comparacion
                                  (id,ejecucion_origen_id,conciliacion_id,cuenta_id,
                                   regla_codigo,clave_propuesta,banco_ids,libros_ids,
                                   explicacion,creada_por)
                                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                                ON CONFLICT (conciliacion_id,clave_propuesta) DO NOTHING
                                """,
                                [
                                    proposal_id,
                                    execution_id,
                                    current["reconciliation_id"],
                                    current["account_id"],
                                    proposal.rule,
                                    key,
                                    _json_text(proposal.bank_ids),
                                    _json_text(proposal.book_ids),
                                    proposal.explanation,
                                    current["user_id"],
                                ],
                            )
                            cursor.execute(
                                """
                                SELECT id::text FROM contared.propuestas_comparacion
                                WHERE conciliacion_id=%s AND clave_propuesta=%s
                                """,
                                [current["reconciliation_id"], key],
                            )
                            existing_proposal = cursor.fetchone()
                            if existing_proposal is None:
                                raise DatabaseError("No se pudo recuperar una propuesta guardada.")
                            cursor.execute(
                                """
                                INSERT INTO contared.ejecucion_propuestas
                                  (ejecucion_id,propuesta_id,conciliacion_id,cuenta_id)
                                VALUES (%s,%s,%s,%s)
                                ON CONFLICT DO NOTHING
                                """,
                                [
                                    execution_id,
                                    existing_proposal[0],
                                    current["reconciliation_id"],
                                    current["account_id"],
                                ],
                            )
                        execution = (execution_id,)
                    else:
                        cursor.execute(
                            """
                            UPDATE contared.ejecuciones_comparacion
                            SET ultima_ejecucion_en=now()
                            WHERE id=%s
                            """,
                            [execution[0]],
                        )
                    results = _results_for_execution(cursor, current, execution[0])
                    return Response({
                        "ejecucion": {"id": execution[0], "huella_datos": fingerprint},
                        "results": results,
                    })
        except DatabaseError as exc:
            _database_error(exc)


class ReconciliationProposalActionView(APIView):
    action = ""

    def post(self, request, reconciliation_id, proposal_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_role(scope, ("administrador", "revisor"))
        if set(request.data) - {"motivo"}:
            raise ValidationError({"detail": "La solicitud contiene campos no permitidos."})
        reason = request.data.get("motivo", "")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ValidationError({"motivo": "El motivo debe ser texto de hasta 500 caracteres."})
        if self.action == "descartar" and not reason.strip():
            raise ValidationError(
                {"motivo": "Escribe un motivo para descartar la propuesta."}
            )
        try:
            proposal_uuid = UUID(str(proposal_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"propuesta": "La propuesta no es válida."}) from exc
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    current = _current_reconciliation(
                        cursor, request, reconciliation_id, lock=True
                    )
                    _require_role(current, ("administrador", "revisor"))
                    cursor.execute(
                        """
                        SELECT p.id::text,p.cuenta_id::text,p.regla_codigo,p.banco_ids,
                               p.libros_ids,p.estado,p.cruce_id::text,u.nombre,
                               p.resuelta_en,p.motivo_descarte
                        FROM contared.propuestas_comparacion p
                        LEFT JOIN contared.usuarios u ON u.id=p.resuelta_por
                        WHERE p.id=%s AND p.conciliacion_id=%s AND p.cuenta_id=%s
                        FOR UPDATE OF p
                        """,
                        [
                            proposal_uuid,
                            current["reconciliation_id"],
                            current["account_id"],
                        ],
                    )
                    proposal = cursor.fetchone()
                    if proposal is None:
                        raise NotFound("La propuesta no existe.")
                    if proposal[5] == "confirmada":
                        if self.action == "confirmar":
                            return Response({
                                "estado": proposal[5],
                                "cruce_id": proposal[6],
                                "resuelta_por_nombre": proposal[7],
                                "resuelta_en": proposal[8].isoformat() if proposal[8] else None,
                            })
                        raise ValidationError("Una propuesta confirmada no se puede descartar.")
                    if proposal[5] == "descartada":
                        if self.action == "descartar":
                            return Response({
                                "estado": proposal[5],
                                "resuelta_por_nombre": proposal[7],
                                "resuelta_en": proposal[8].isoformat() if proposal[8] else None,
                                "motivo_descarte": proposal[9],
                            })
                        raise ValidationError("La propuesta fue descartada y no se puede confirmar.")
                    cursor.execute("SELECT set_config('contared.actor_id', %s, true)", [current["user_id"]])
                    if self.action == "descartar":
                        cursor.execute(
                            """
                            UPDATE contared.propuestas_comparacion
                            SET estado='descartada',resuelta_por=%s,resuelta_en=now(),
                                motivo_descarte=%s
                            WHERE id=%s AND estado='propuesta'
                            RETURNING resuelta_en
                            """,
                            [
                                current["user_id"],
                                reason.strip() or None,
                                proposal_uuid,
                            ],
                        )
                        resolved_at = cursor.fetchone()[0]
                        return Response({
                            "estado": "descartada",
                            "resuelta_en": resolved_at.isoformat(),
                            "resuelta_por_nombre": current["user_name"],
                            "motivo_descarte": reason.strip(),
                        })
                    return self._confirm(cursor, current, proposal_uuid, proposal)
        except DatabaseError as exc:
            if isinstance(exc, IntegrityError):
                raise ValidationError(
                    "Uno o más movimientos ya forman parte de un cruce confirmado."
                ) from exc
            _database_error(exc)

    def _confirm(self, cursor, scope, proposal_id, proposal):
        proposal_uuid = str(proposal_id)
        cursor.execute(
            """
            SELECT e.moneda_codigo,e.periodo,e.fecha_corte
            FROM contared.ejecucion_propuestas ep
            JOIN contared.ejecuciones_comparacion e
              ON e.id=ep.ejecucion_id AND e.conciliacion_id=ep.conciliacion_id
            WHERE ep.propuesta_id=%s AND ep.conciliacion_id=%s
              AND ep.cuenta_id=%s
              AND ep.ejecucion_id=(
                SELECT id FROM contared.ejecuciones_comparacion
                WHERE conciliacion_id=%s AND cuenta_id=%s
                ORDER BY ultima_ejecucion_en DESC,id DESC
                LIMIT 1
              )
            """,
            [
                proposal_id,
                scope["reconciliation_id"],
                scope["account_id"],
                scope["reconciliation_id"],
                scope["account_id"],
            ],
        )
        comparison_context = cursor.fetchone()
        if comparison_context is None or (
            comparison_context[0] != scope["currency_code"]
            or comparison_context[1] != scope["period"]
            or comparison_context[2] != scope["cutoff"]
        ):
            raise ValidationError(
                "La propuesta no pertenece a la comparación vigente de esta conciliación."
            )
        rule = proposal[2]
        if rule not in ("reference_amount", "amount_date", "reference_date_group"):
            raise ValidationError(
                "Esta propuesta es una diferencia, un caso ambiguo o no tiene contraparte; no se puede confirmar."
            )
        bank_ids = _decode_json_ids(proposal[3])
        books_ids = _decode_json_ids(proposal[4])
        if not bank_ids or not books_ids or len(set(bank_ids + books_ids)) != len(bank_ids) + len(books_ids):
            raise ValidationError({"propuesta": "La estructura de movimientos no es válida."})
        if rule in ("reference_amount", "amount_date") and (len(bank_ids) != 1 or len(books_ids) != 1):
            raise ValidationError({"propuesta": "La regla requiere una partida de cada lado."})
        if rule == "reference_date_group" and (
            max(len(bank_ids), len(books_ids)) > 3
            or min(len(bank_ids), len(books_ids)) != 1
            or max(len(bank_ids), len(books_ids)) < 2
        ):
            raise ValidationError({"propuesta": "El grupo no cumple los límites permitidos."})
        identifiers = bank_ids + books_ids
        cursor.execute(
            """
            SELECT m.id::text,m.lado,m.fecha,m.referencia,m.monto::text,
                   c.origen,c.estado
            FROM contared.movimientos m
            JOIN contared.cargas c ON c.id=m.carga_id
            JOIN contared.conciliaciones r
              ON r.id=c.conciliacion_id AND r.cuenta_id=c.cuenta_id
            WHERE m.id=ANY(%s::uuid[]) AND m.cuenta_id=%s
              AND c.conciliacion_id=%s AND c.empresa_id=%s AND c.pais_codigo=%s
              AND c.estado='validada'
              AND ((c.origen='banco' AND m.lado='banco')
                OR (c.origen='libros' AND m.lado='libros'))
              AND m.fecha >= r.periodo AND m.fecha <= r.fecha_corte
            ORDER BY m.id
            FOR UPDATE OF m,c
            """,
            [
                identifiers,
                scope["account_id"],
                scope["reconciliation_id"],
                scope["company_id"],
                scope["country_code"],
            ],
        )
        rows = cursor.fetchall()
        if len(rows) != len(identifiers) or {row[0] for row in rows} != set(identifiers):
            raise ValidationError(
                "Uno o más movimientos ya no son elegibles para esta conciliación."
            )
        by_id = {row[0]: row for row in rows}
        if any(by_id[item][1] != "banco" for item in bank_ids) or any(
            by_id[item][1] != "libros" for item in books_ids
        ):
            raise ValidationError({"propuesta": "Los lados del grupo cambiaron."})
        bank_total = sum((Decimal(by_id[item][4]) for item in bank_ids), Decimal("0"))
        books_total = sum((Decimal(by_id[item][4]) for item in books_ids), Decimal("0"))
        if bank_total != books_total:
            raise ValidationError("Los importes no son exactamente iguales; no se pueden confirmar.")
        if rule == "reference_amount":
            bank, book = by_id[bank_ids[0]], by_id[books_ids[0]]
            if not bank[3] or bank[3] != book[3] or bank[4] != book[4]:
                raise ValidationError("La referencia o el monto ya no coinciden.")
        elif rule == "amount_date":
            bank, book = by_id[bank_ids[0]], by_id[books_ids[0]]
            if bank[2] != book[2] or bank[4] != book[4]:
                raise ValidationError("La fecha o el monto ya no coinciden.")
        else:
            grouped = [by_id[item] for item in bank_ids + books_ids]
            if (
                not grouped[0][3]
                or any(item[3] != grouped[0][3] for item in grouped)
                or any(item[2] != grouped[0][2] for item in grouped)
            ):
                raise ValidationError("El grupo ya no comparte referencia y fecha.")
        cheque_evidence = False
        if len(bank_ids) == 1 and len(books_ids) == 1:
            cursor.execute(
                """
                SELECT id::text,lado,numero_cheque,fecha,monto::text
                FROM contared.movimientos
                WHERE id=ANY(%s::uuid[])
                ORDER BY lado,id
                """,
                [bank_ids + books_ids],
            )
            cheque_rows = cursor.fetchall()
            if len(cheque_rows) == 2:
                bank_evidence = next((row for row in cheque_rows if row[1] == "banco"), None)
                book_evidence = next((row for row in cheque_rows if row[1] == "libros"), None)
                cheque_evidence = bool(
                    bank_evidence
                    and book_evidence
                    and bank_evidence[2]
                    and bank_evidence[2] == book_evidence[2]
                    and Decimal(bank_evidence[4]) < 0
                    and Decimal(book_evidence[4]) < 0
                    and abs(Decimal(bank_evidence[4])) == abs(Decimal(book_evidence[4]))
                    and bank_evidence[3] >= book_evidence[3]
                )
        cursor.execute(
            """
            SELECT cm.movimiento_id::text
            FROM contared.cruce_movimientos cm
            JOIN contared.cruces x ON x.id=cm.cruce_id
            WHERE cm.movimiento_id=ANY(%s::uuid[])
            """,
            [identifiers],
        )
        if cursor.fetchall():
            raise ValidationError("Uno o más movimientos ya forman parte de un cruce confirmado.")
        cruce_id = str(uuid4())
        cursor.execute(
            """
            INSERT INTO contared.cruces
              (id,cuenta_id,conciliacion_id,tipo,estado,criterio,regla_codigo,
               observacion,creado_por,confirmado_por,confirmado_en)
            VALUES (%s,%s,%s,'coincidencia','confirmado',%s,%s,%s,%s,%s,now())
            """,
            [
                cruce_id,
                scope["account_id"],
                scope["reconciliation_id"],
                "Confirmación manual de propuesta de comparación.",
                rule,
                f"Confirmado desde propuesta {proposal_uuid}",
                scope["user_id"],
                scope["user_id"],
            ],
        )
        for movement_id in identifiers:
            cursor.execute(
                """
                INSERT INTO contared.cruce_movimientos(cruce_id,cuenta_id,movimiento_id)
                VALUES (%s,%s,%s)
                """,
                [cruce_id, scope["account_id"], movement_id],
            )
        cheque_retenido_reportado = False
        if cheque_evidence:
            cheque_retenido_reportado = ChequeCrossSync.reflect(
                cursor, scope, bank_ids, books_ids, cruce_id
            )
        cursor.execute(
            """
            UPDATE contared.propuestas_comparacion
            SET estado='confirmada',cruce_id=%s,resuelta_por=%s,resuelta_en=now()
            WHERE id=%s AND estado='propuesta'
            RETURNING resuelta_en
            """,
            [cruce_id, scope["user_id"], proposal_uuid],
        )
        if cursor.rowcount != 1:
            raise ValidationError("La propuesta cambió mientras se confirmaba.")
        resolved_at = cursor.fetchone()[0]
        return Response({
            "estado": "confirmada",
            "cruce_id": cruce_id,
            "resuelta_en": resolved_at.isoformat(),
            "resuelta_por_nombre": scope["user_name"],
            "cheque_retenido_reportado": cheque_retenido_reportado,
        })


class ReconciliationProposalConfirmView(ReconciliationProposalActionView):
    action = "confirmar"


class ReconciliationProposalDismissView(ReconciliationProposalActionView):
    action = "descartar"
