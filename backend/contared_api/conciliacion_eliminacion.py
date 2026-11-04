import json
import logging
from uuid import UUID, uuid4

from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    connection,
    transaction,
)
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.banking import scope_from_request
from contared_api.conciliaciones import (
    ImportStorageUnavailable,
    ReconciliationConflict,
    _scope_for_reconciliation,
    _storage_request,
)
from contared_api.session_access import DatabaseUnavailable, load_session_access


logger = logging.getLogger(__name__)


class DeletionDatabasePermissionError(APIException):
    status_code = 500
    default_detail = (
        "La base de datos no tiene los permisos necesarios para completar la eliminación."
    )
    default_code = "deletion_database_permission_error"


class DeletionDatabaseQueryError(APIException):
    status_code = 500
    default_detail = (
        "Falló una consulta de eliminación. No se completó la operación."
    )
    default_code = "deletion_database_query_error"


class DeletionDatabaseOperationError(APIException):
    status_code = 500
    default_detail = (
        "Falló una operación de base de datos. No se completó la eliminación."
    )
    default_code = "deletion_database_operation_error"


def _database_diagnostics(exc):
    sqlstate = None
    constraint = None
    causes = []
    current = exc
    while current is not None and current not in causes:
        causes.append(current)
        diagnostic = getattr(current, "diag", None)
        sqlstate = (
            sqlstate
            or getattr(current, "sqlstate", None)
            or getattr(current, "pgcode", None)
            or getattr(diagnostic, "sqlstate", None)
        )
        constraint = constraint or getattr(diagnostic, "constraint_name", None)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return sqlstate, constraint, causes


def _raise_deletion_database_error(exc, stage):
    sqlstate, constraint, causes = _database_diagnostics(exc)
    if sqlstate and sqlstate.startswith("08"):
        category = "connection"
        error = DatabaseUnavailable()
    elif (sqlstate and sqlstate.startswith("23")) or isinstance(exc, IntegrityError):
        category = "integrity"
        error = ReconciliationConflict(
            "La base de datos rechazó la eliminación por una restricción de integridad. "
            "No se completó la operación."
        )
    elif (sqlstate and (sqlstate.startswith("28") or sqlstate == "42501")):
        category = "permission"
        error = DeletionDatabasePermissionError()
    elif (sqlstate and sqlstate.startswith("42")) or any(
        isinstance(cause, ProgrammingError) for cause in causes
    ):
        category = "query"
        error = DeletionDatabaseQueryError()
    elif not sqlstate and any(isinstance(cause, OperationalError) for cause in causes):
        category = "connection"
        error = DatabaseUnavailable()
    else:
        category = "database_operation"
        error = DeletionDatabaseOperationError()

    logger.error(
        "Permanent reconciliation deletion database failure "
        "(stage=%s, category=%s, sqlstate=%s, constraint=%s, error_type=%s).",
        stage,
        category,
        sqlstate or "unavailable",
        constraint or "unavailable",
        type(exc).__name__,
    )
    raise error from exc


PURGE_TABLES = (
    "eliminaciones_conciliacion",
    "eliminacion_conciliacion_storage",
)

COUNT_QUERIES = {
    "conciliaciones": """
        SELECT count(*) FROM contared.conciliaciones WHERE id=%s
    """,
    "cargas": """
        SELECT count(*) FROM contared.cargas WHERE conciliacion_id=%s
    """,
    "movimientos": """
        SELECT count(*) FROM contared.movimientos m
        JOIN contared.cargas c ON c.id=m.carga_id WHERE c.conciliacion_id=%s
    """,
    "ejecuciones_comparacion": """
        SELECT count(*) FROM contared.ejecuciones_comparacion WHERE conciliacion_id=%s
    """,
    "propuestas_comparacion": """
        SELECT count(*) FROM contared.propuestas_comparacion WHERE conciliacion_id=%s
    """,
    "propuestas_descartadas": """
        SELECT count(*) FROM contared.propuestas_comparacion
        WHERE conciliacion_id=%s AND estado='descartada'
    """,
    "asociaciones_ejecucion": """
        SELECT count(*) FROM contared.ejecucion_propuestas WHERE conciliacion_id=%s
    """,
    "cruces": """
        SELECT count(*) FROM contared.cruces WHERE conciliacion_id=%s
    """,
    "asociaciones_cruce": """
        SELECT count(*) FROM contared.cruce_movimientos cm
        JOIN contared.cruces x ON x.id=cm.cruce_id WHERE x.conciliacion_id=%s
    """,
    "cheques": """
        SELECT count(*) FROM contared.cheques
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s
    """,
    "eventos_cheque": """
        SELECT count(*) FROM contared.cheque_eventos e
        LEFT JOIN contared.cheques ch ON ch.id=e.cheque_id
        WHERE e.conciliacion_id=%s OR ch.conciliacion_origen_id=%s
           OR ch.conciliacion_resolucion_id=%s
    """,
    "casos_deposito": """
        SELECT count(*) FROM contared.casos_deposito
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s
    """,
    "asociaciones_deposito": """
        SELECT count(*) FROM contared.deposito_movimientos dm
        JOIN contared.casos_deposito d ON d.id=dm.caso_id
        WHERE d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s
    """,
    "eventos_deposito": """
        SELECT count(*) FROM contared.deposito_eventos e
        LEFT JOIN contared.casos_deposito d ON d.id=e.caso_id
        WHERE e.conciliacion_id=%s OR d.conciliacion_origen_id=%s
           OR d.conciliacion_resolucion_id=%s
    """,
    "pendientes": """
        SELECT count(*) FROM contared.pendientes
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s
    """,
    "ajustes": """
        SELECT count(*) FROM contared.ajustes WHERE conciliacion_id=%s
    """,
    "reportes_snapshots": """
        SELECT count(*) FROM contared.reportes WHERE conciliacion_id=%s
    """,
    "historial_archivo": """
        SELECT count(*) FROM contared.eventos_archivo_conciliacion
        WHERE conciliacion_id=%s
    """,
}

TARGET_RECORDS = (
    ("conciliaciones", "SELECT id::text FROM contared.conciliaciones WHERE id=%s"),
    ("cargas", "SELECT id::text FROM contared.cargas WHERE conciliacion_id=%s"),
    ("movimientos", """SELECT m.id::text FROM contared.movimientos m
        JOIN contared.cargas c ON c.id=m.carga_id WHERE c.conciliacion_id=%s"""),
    ("cruces", "SELECT id::text FROM contared.cruces WHERE conciliacion_id=%s"),
    ("cruce_movimientos", """SELECT concat_ws(':',cm.cruce_id,cm.movimiento_id)
        FROM contared.cruce_movimientos cm JOIN contared.cruces x ON x.id=cm.cruce_id
        WHERE x.conciliacion_id=%s"""),
    ("pendientes", """SELECT id::text FROM contared.pendientes
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s"""),
    ("ajustes", "SELECT id::text FROM contared.ajustes WHERE conciliacion_id=%s"),
    ("reportes", "SELECT id::text FROM contared.reportes WHERE conciliacion_id=%s"),
    ("ejecuciones_comparacion", """SELECT id::text FROM contared.ejecuciones_comparacion
        WHERE conciliacion_id=%s"""),
    ("propuestas_comparacion", """SELECT id::text FROM contared.propuestas_comparacion
        WHERE conciliacion_id=%s"""),
    ("ejecucion_propuestas", """SELECT concat_ws(':',ejecucion_id,propuesta_id)
        FROM contared.ejecucion_propuestas WHERE conciliacion_id=%s"""),
    ("cheques", """SELECT id::text FROM contared.cheques
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s"""),
    ("cheque_eventos", """SELECT e.id::text FROM contared.cheque_eventos e
        LEFT JOIN contared.cheques ch ON ch.id=e.cheque_id
        WHERE e.conciliacion_id=%s OR ch.conciliacion_origen_id=%s
           OR ch.conciliacion_resolucion_id=%s"""),
    ("casos_deposito", """SELECT id::text FROM contared.casos_deposito
        WHERE conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s"""),
    ("deposito_movimientos", """SELECT concat_ws(':',dm.caso_id,dm.movimiento_id)
        FROM contared.deposito_movimientos dm
        JOIN contared.casos_deposito d ON d.id=dm.caso_id
        WHERE d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s"""),
    ("deposito_eventos", """SELECT e.id::text FROM contared.deposito_eventos e
        LEFT JOIN contared.casos_deposito d ON d.id=e.caso_id
        WHERE e.conciliacion_id=%s OR d.conciliacion_origen_id=%s
           OR d.conciliacion_resolucion_id=%s"""),
    ("eventos_archivo_conciliacion", """SELECT id::text
        FROM contared.eventos_archivo_conciliacion WHERE conciliacion_id=%s"""),
)

DELETE_ORDER = (
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

DELETE_SQL = {
    "ejecucion_propuestas": """
        DELETE FROM contared.ejecucion_propuestas t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='ejecucion_propuestas'
          AND p.registro_id=concat_ws(':',t.ejecucion_id,t.propuesta_id)
    """,
    "propuestas_comparacion": """
        DELETE FROM contared.propuestas_comparacion t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='propuestas_comparacion' AND p.registro_id=t.id::text
    """,
    "cruce_movimientos": """
        DELETE FROM contared.cruce_movimientos t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='cruce_movimientos'
          AND p.registro_id=concat_ws(':',t.cruce_id,t.movimiento_id)
    """,
    "cruces": """
        DELETE FROM contared.cruces t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='cruces' AND p.registro_id=t.id::text
    """,
    "cheque_eventos": """
        DELETE FROM contared.cheque_eventos t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='cheque_eventos' AND p.registro_id=t.id::text
    """,
    "deposito_eventos": """
        DELETE FROM contared.deposito_eventos t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='deposito_eventos' AND p.registro_id=t.id::text
    """,
    "deposito_movimientos": """
        DELETE FROM contared.deposito_movimientos t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='deposito_movimientos'
          AND p.registro_id=concat_ws(':',t.caso_id,t.movimiento_id)
    """,
    "cheques": """
        DELETE FROM contared.cheques t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='cheques' AND p.registro_id=t.id::text
    """,
    "casos_deposito": """
        DELETE FROM contared.casos_deposito t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='casos_deposito' AND p.registro_id=t.id::text
    """,
    "ajustes": """
        DELETE FROM contared.ajustes t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='ajustes' AND p.registro_id=t.id::text
    """,
    "pendientes": """
        DELETE FROM contared.pendientes t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='pendientes' AND p.registro_id=t.id::text
    """,
    "reportes": """
        DELETE FROM contared.reportes t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='reportes' AND p.registro_id=t.id::text
    """,
    "ejecuciones_comparacion": """
        DELETE FROM contared.ejecuciones_comparacion t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='ejecuciones_comparacion' AND p.registro_id=t.id::text
    """,
    "movimientos": """
        DELETE FROM contared.movimientos t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='movimientos' AND p.registro_id=t.id::text
    """,
    "cargas": """
        DELETE FROM contared.cargas t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='cargas' AND p.registro_id=t.id::text
    """,
    "eventos_archivo_conciliacion": """
        DELETE FROM contared.eventos_archivo_conciliacion t
        USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='eventos_archivo_conciliacion' AND p.registro_id=t.id::text
    """,
    "conciliaciones": """
        DELETE FROM contared.conciliaciones t USING pg_temp.contared_purga_autorizada p
        WHERE p.tabla='conciliaciones' AND p.registro_id=t.id::text
    """,
}

CROSS_PERIOD_QUERY = """
    SELECT tipo,periodo,count(*) AS cantidad
    FROM (
      SELECT 'Pendiente resuelto en otro período'::text AS tipo,
             r.periodo::text AS periodo
      FROM contared.pendientes p
      JOIN contared.conciliaciones r
        ON r.id=CASE WHEN p.conciliacion_origen_id=%s
                     THEN p.conciliacion_resolucion_id ELSE p.conciliacion_origen_id END
      WHERE (p.conciliacion_origen_id=%s OR p.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Cheque originado o cobrado en otro período',r.periodo::text
      FROM contared.cheques ch
      JOIN contared.conciliaciones r
        ON r.id=CASE WHEN ch.conciliacion_origen_id=%s
                     THEN ch.conciliacion_resolucion_id ELSE ch.conciliacion_origen_id END
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Depósito originado o resuelto en otro período',r.periodo::text
      FROM contared.casos_deposito d
      JOIN contared.conciliaciones r
        ON r.id=CASE WHEN d.conciliacion_origen_id=%s
                     THEN d.conciliacion_resolucion_id ELSE d.conciliacion_origen_id END
      WHERE (d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Movimiento incluido en un cruce de otro período',r.periodo::text
      FROM contared.cruces x
      JOIN contared.cruce_movimientos cm ON cm.cruce_id=x.id
      JOIN contared.movimientos m ON m.id=cm.movimiento_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE x.conciliacion_id=%s AND r.id<>%s
      UNION ALL
      SELECT 'Movimiento de origen o cobro del cheque en otro período',r.periodo::text
      FROM contared.cheques ch
      JOIN contared.movimientos m
        ON m.id IN (ch.movimiento_emision_id,ch.movimiento_cobro_id)
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Pendiente con movimiento fuente de otro período',r.periodo::text
      FROM contared.pendientes p
      JOIN contared.movimientos m ON m.id=p.movimiento_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE (p.conciliacion_origen_id=%s OR p.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Movimiento de esta conciliación usado por cheque de otro período',r.periodo::text
      FROM contared.cheques ch
      JOIN contared.movimientos m
        ON m.id IN (ch.movimiento_emision_id,ch.movimiento_cobro_id)
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r
        ON r.id=coalesce(ch.conciliacion_origen_id,ch.conciliacion_resolucion_id)
      WHERE c.conciliacion_id=%s AND r.id<>%s
      UNION ALL
      SELECT 'Movimiento de esta conciliación usado por otro cruce',r.periodo::text
      FROM contared.cruce_movimientos cm
      JOIN contared.cruces x ON x.id=cm.cruce_id
      JOIN contared.movimientos m ON m.id=cm.movimiento_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=x.conciliacion_id
      WHERE x.conciliacion_id<>%s AND c.conciliacion_id=%s
      UNION ALL
      SELECT 'Caso de depósito de otro período usa un movimiento de esta conciliación',r.periodo::text
      FROM contared.casos_deposito d
      JOIN contared.deposito_movimientos dm ON dm.caso_id=d.id
      JOIN contared.movimientos m ON m.id=dm.movimiento_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=d.conciliacion_origen_id
      WHERE c.conciliacion_id=%s AND d.conciliacion_origen_id<>%s
      UNION ALL
      SELECT 'Pendiente de otro período usa un movimiento de esta conciliación',r.periodo::text
      FROM contared.pendientes p
      JOIN contared.movimientos m ON m.id=p.movimiento_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=p.conciliacion_origen_id
      WHERE c.conciliacion_id=%s AND p.conciliacion_origen_id<>%s
      UNION ALL
      SELECT 'Movimiento de depósito perteneciente a otro período',r.periodo::text
      FROM contared.casos_deposito d
      JOIN contared.deposito_movimientos dm ON dm.caso_id=d.id
      JOIN contared.movimientos m ON m.id=dm.movimiento_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE (d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Pendiente con movimiento de resolución de otro período',r.periodo::text
      FROM contared.pendientes p
      JOIN contared.movimientos m ON m.id=p.movimiento_resolucion_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE (p.conciliacion_origen_id=%s OR p.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Ajuste de otro período vinculado a un pendiente',r.periodo::text
      FROM contared.pendientes p
      JOIN contared.ajustes a ON a.pendiente_id=p.id
      JOIN contared.conciliaciones r ON r.id=a.conciliacion_id
      WHERE (p.conciliacion_origen_id=%s OR p.conciliacion_resolucion_id=%s)
        AND a.conciliacion_id<>%s
      UNION ALL
      SELECT 'Evento de cheque relacionado con otro período',r.periodo::text
      FROM contared.cheque_eventos e
      JOIN contared.cheques ch ON ch.id=e.cheque_id
      JOIN contared.conciliaciones r
        ON r.id=coalesce(e.conciliacion_id,ch.conciliacion_resolucion_id)
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND coalesce(e.conciliacion_id,ch.conciliacion_resolucion_id)<>%s
      UNION ALL
      SELECT 'Evento de cheque enlazado a un cruce de otro período',r.periodo::text
      FROM contared.cheque_eventos e
      JOIN contared.cheques ch ON ch.id=e.cheque_id
      JOIN contared.cruces x ON x.id=e.cruce_id
      JOIN contared.conciliaciones r ON r.id=x.conciliacion_id
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND x.conciliacion_id<>%s
      UNION ALL
      SELECT 'Evento de cheque con período explícito distinto',r.periodo::text
      FROM contared.cheque_eventos e
      JOIN contared.cheques ch ON ch.id=e.cheque_id
      JOIN contared.conciliaciones r ON r.id=e.conciliacion_id
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND e.conciliacion_id<>%s
      UNION ALL
      SELECT 'Evento del cheque pertenece a otro período',r.periodo::text
      FROM contared.cheque_eventos e
      JOIN contared.cheques ch ON ch.id=e.cheque_id
      JOIN contared.conciliaciones r ON r.id=ch.conciliacion_origen_id
      WHERE e.conciliacion_id=%s
        AND ch.conciliacion_origen_id<>%s
      UNION ALL
      SELECT 'Evento de depósito relacionado con otro período',r.periodo::text
      FROM contared.deposito_eventos e
      JOIN contared.casos_deposito d ON d.id=e.caso_id
      JOIN contared.conciliaciones r
        ON r.id=coalesce(e.conciliacion_id,d.conciliacion_resolucion_id)
      WHERE (d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s)
        AND coalesce(e.conciliacion_id,d.conciliacion_resolucion_id)<>%s
      UNION ALL
      SELECT 'Evento de depósito con período explícito distinto',r.periodo::text
      FROM contared.deposito_eventos e
      JOIN contared.casos_deposito d ON d.id=e.caso_id
      JOIN contared.conciliaciones r ON r.id=e.conciliacion_id
      WHERE (d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s)
        AND e.conciliacion_id<>%s
      UNION ALL
      SELECT 'Evento del depósito pertenece a otro período',r.periodo::text
      FROM contared.deposito_eventos e
      JOIN contared.casos_deposito d ON d.id=e.caso_id
      JOIN contared.conciliaciones r ON r.id=d.conciliacion_origen_id
      WHERE e.conciliacion_id=%s
        AND d.conciliacion_origen_id<>%s
      UNION ALL
      SELECT 'Movimiento de evento de cheque perteneciente a otro período',r.periodo::text
      FROM contared.cheque_eventos e
      JOIN contared.cheques ch ON ch.id=e.cheque_id
      JOIN contared.movimientos m ON m.id=e.movimiento_banco_id
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND r.id<>%s
      UNION ALL
      SELECT 'Movimiento de evento de cheque usado por un cruce de otro período',r.periodo::text
      FROM contared.cheque_eventos e
      JOIN contared.cheques ch ON ch.id=e.cheque_id
      JOIN contared.movimientos m ON m.id=e.movimiento_banco_id
      JOIN contared.cruce_movimientos cm ON cm.movimiento_id=m.id
      JOIN contared.cruces x ON x.id=cm.cruce_id
      JOIN contared.conciliaciones r ON r.id=x.conciliacion_id
      WHERE (ch.conciliacion_origen_id=%s OR ch.conciliacion_resolucion_id=%s)
        AND x.conciliacion_id<>%s
      UNION ALL
      SELECT 'Propuesta con movimiento de otro período',r.periodo::text
      FROM contared.propuestas_comparacion p
      CROSS JOIN LATERAL jsonb_array_elements_text(p.banco_ids || p.libros_ids) ids(value)
      JOIN contared.movimientos m ON m.id=ids.value::uuid
      JOIN contared.cargas c ON c.id=m.carga_id
      JOIN contared.conciliaciones r ON r.id=c.conciliacion_id
      WHERE p.conciliacion_id=%s AND r.id<>%s
    ) referencias
    GROUP BY tipo,periodo
    ORDER BY tipo,periodo
"""


class ReconciliationDeletionMigrationRequired(APIException):
    status_code = 503
    default_detail = (
        "La eliminación definitiva requiere aplicar "
        "supabase/migrations/202610300004_eliminacion_definitiva_conciliaciones.sql."
    )
    default_code = "reconciliation_deletion_migration_required"


def _deletion_schema_available():
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='contared'
                 AND table_name IN (%s,%s)""",
            list(PURGE_TABLES),
        )
        return {row[0] for row in cursor.fetchall()} == set(PURGE_TABLES)


def _require_deletion_schema():
    try:
        if not _deletion_schema_available():
            raise ReconciliationDeletionMigrationRequired()
    except DatabaseError as exc:
        _raise_deletion_database_error(exc, "schema_preflight")


def _require_administrator(scope):
    if scope["role"] != "administrador":
        raise PermissionDenied(
            "Solo un administrador puede eliminar definitivamente una conciliación.",
            code="administrator_required",
        )


def _deletion_counts(cursor, reconciliation_id):
    counts = {}
    for label, query in COUNT_QUERIES.items():
        cursor.execute(query, [reconciliation_id] * query.count("%s"))
        counts[label] = int(cursor.fetchone()[0])
    return counts


def _storage_paths(cursor, reconciliation_id):
    cursor.execute(
        """
        SELECT DISTINCT ruta FROM (
          SELECT storage_ruta AS ruta FROM contared.cargas
            WHERE conciliacion_id=%s AND storage_ruta IS NOT NULL
          UNION ALL
          SELECT storage_ruta FROM contared.reportes
            WHERE conciliacion_id=%s AND storage_ruta IS NOT NULL
          UNION ALL
          SELECT soporte_anulacion_ruta FROM contared.cheques
            WHERE (conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s)
              AND soporte_anulacion_ruta IS NOT NULL
          UNION ALL
          SELECT e.soporte_ruta FROM contared.cheque_eventos e
            LEFT JOIN contared.cheques ch ON ch.id=e.cheque_id
            WHERE (e.conciliacion_id=%s OR ch.conciliacion_origen_id=%s
                   OR ch.conciliacion_resolucion_id=%s)
              AND e.soporte_ruta IS NOT NULL
          UNION ALL
          SELECT soporte_ruta FROM contared.casos_deposito
            WHERE (conciliacion_origen_id=%s OR conciliacion_resolucion_id=%s)
              AND soporte_ruta IS NOT NULL
          UNION ALL
          SELECT e.soporte_ruta FROM contared.deposito_eventos e
            LEFT JOIN contared.casos_deposito d ON d.id=e.caso_id
            WHERE (e.conciliacion_id=%s OR d.conciliacion_origen_id=%s
                   OR d.conciliacion_resolucion_id=%s)
              AND e.soporte_ruta IS NOT NULL
          UNION ALL
          SELECT soporte_ruta FROM contared.ajustes
            WHERE conciliacion_id=%s AND soporte_ruta IS NOT NULL
        ) paths
        WHERE ruta IS NOT NULL AND btrim(ruta)<>''
        ORDER BY ruta
        """,
        [reconciliation_id] * 13,
    )
    return sorted({row[0] for row in cursor.fetchall()})


def _shared_storage_paths(cursor, reconciliation_id, paths, scope):
    if not paths:
        return {
            "externos": {"cantidad": 0, "referencias": []},
            "indeterminados": {"cantidad": 0, "referencias": []},
        }
    cursor.execute(
        """
        WITH referencias(ruta,tipo,registro_id,conciliacion_id) AS (
          SELECT storage_ruta,'Carga'::text,id::text,conciliacion_id
            FROM contared.cargas
            WHERE storage_ruta=ANY(%s)
          UNION
          SELECT storage_ruta,'Reporte'::text,id::text,conciliacion_id
            FROM contared.reportes
            WHERE storage_ruta=ANY(%s)
          UNION
          SELECT soporte_anulacion_ruta,'Cheque'::text,id::text,conciliacion_origen_id
            FROM contared.cheques
            WHERE soporte_anulacion_ruta=ANY(%s) AND conciliacion_origen_id IS NOT NULL
          UNION
          SELECT soporte_anulacion_ruta,'Cheque'::text,id::text,conciliacion_resolucion_id
            FROM contared.cheques
            WHERE soporte_anulacion_ruta=ANY(%s) AND conciliacion_resolucion_id IS NOT NULL
          UNION
          SELECT soporte_anulacion_ruta,'Cheque'::text,id::text,NULL::uuid
            FROM contared.cheques
            WHERE soporte_anulacion_ruta=ANY(%s)
              AND conciliacion_origen_id IS NULL AND conciliacion_resolucion_id IS NULL
          UNION
          SELECT e.soporte_ruta,'Evento de cheque'::text,e.id::text,
                 e.conciliacion_id
            FROM contared.cheque_eventos e
            WHERE e.soporte_ruta=ANY(%s) AND e.conciliacion_id IS NOT NULL
          UNION
          SELECT e.soporte_ruta,'Evento de cheque'::text,e.id::text,
                 ch.conciliacion_origen_id
            FROM contared.cheque_eventos e
            JOIN contared.cheques ch ON ch.id=e.cheque_id
            WHERE e.soporte_ruta=ANY(%s) AND ch.conciliacion_origen_id IS NOT NULL
          UNION
          SELECT e.soporte_ruta,'Evento de cheque'::text,e.id::text,
                 ch.conciliacion_resolucion_id
            FROM contared.cheque_eventos e
            JOIN contared.cheques ch ON ch.id=e.cheque_id
            WHERE e.soporte_ruta=ANY(%s) AND ch.conciliacion_resolucion_id IS NOT NULL
          UNION
          SELECT e.soporte_ruta,'Evento de cheque'::text,e.id::text,NULL::uuid
            FROM contared.cheque_eventos e
            LEFT JOIN contared.cheques ch ON ch.id=e.cheque_id
            WHERE e.soporte_ruta=ANY(%s) AND e.conciliacion_id IS NULL
              AND ch.conciliacion_origen_id IS NULL
              AND ch.conciliacion_resolucion_id IS NULL
          UNION
          SELECT soporte_ruta,'Caso de depósito'::text,id::text,conciliacion_origen_id
            FROM contared.casos_deposito
            WHERE soporte_ruta=ANY(%s) AND conciliacion_origen_id IS NOT NULL
          UNION
          SELECT soporte_ruta,'Caso de depósito'::text,id::text,conciliacion_resolucion_id
            FROM contared.casos_deposito
            WHERE soporte_ruta=ANY(%s) AND conciliacion_resolucion_id IS NOT NULL
          UNION
          SELECT soporte_ruta,'Caso de depósito'::text,id::text,NULL::uuid
            FROM contared.casos_deposito
            WHERE soporte_ruta=ANY(%s)
              AND conciliacion_origen_id IS NULL AND conciliacion_resolucion_id IS NULL
          UNION
          SELECT e.soporte_ruta,'Evento de depósito'::text,e.id::text,e.conciliacion_id
            FROM contared.deposito_eventos e
            WHERE e.soporte_ruta=ANY(%s) AND e.conciliacion_id IS NOT NULL
          UNION
          SELECT e.soporte_ruta,'Evento de depósito'::text,e.id::text,
                 d.conciliacion_origen_id
            FROM contared.deposito_eventos e
            JOIN contared.casos_deposito d ON d.id=e.caso_id
            WHERE e.soporte_ruta=ANY(%s) AND d.conciliacion_origen_id IS NOT NULL
          UNION
          SELECT e.soporte_ruta,'Evento de depósito'::text,e.id::text,
                 d.conciliacion_resolucion_id
            FROM contared.deposito_eventos e
            JOIN contared.casos_deposito d ON d.id=e.caso_id
            WHERE e.soporte_ruta=ANY(%s) AND d.conciliacion_resolucion_id IS NOT NULL
          UNION
          SELECT e.soporte_ruta,'Evento de depósito'::text,e.id::text,NULL::uuid
            FROM contared.deposito_eventos e
            LEFT JOIN contared.casos_deposito d ON d.id=e.caso_id
            WHERE e.soporte_ruta=ANY(%s) AND e.conciliacion_id IS NULL
              AND d.conciliacion_origen_id IS NULL
              AND d.conciliacion_resolucion_id IS NULL
          UNION
          SELECT soporte_ruta,'Ajuste'::text,id::text,conciliacion_id
            FROM contared.ajustes
            WHERE soporte_ruta=ANY(%s)
        )
        SELECT DISTINCT ref.ruta,ref.tipo,
               CASE WHEN account.empresa_id=%s AND account.pais_codigo=%s
                    THEN ref.conciliacion_id::text END AS conciliacion_visible,
               CASE WHEN account.empresa_id=%s AND account.pais_codigo=%s
                    THEN related.periodo::text END AS periodo_visible,
               ref.conciliacion_id IS NULL AS indeterminada
        FROM referencias ref
        LEFT JOIN contared.conciliaciones related ON related.id=ref.conciliacion_id
        LEFT JOIN contared.cuentas_bancarias account ON account.id=related.cuenta_id
        WHERE ref.conciliacion_id IS NULL OR ref.conciliacion_id<>%s
        ORDER BY ref.ruta,ref.tipo,conciliacion_visible
        """,
        [paths] * 17 + [
            scope["company_id"], scope["country_code"],
            scope["company_id"], scope["country_code"],
            reconciliation_id,
        ],
    )
    grouped = {
        "externos": {},
        "indeterminados": {},
    }
    all_paths = {"externos": set(), "indeterminados": set()}
    for path, record_type, related_id, period, indeterminate in cursor.fetchall():
        category = "indeterminados" if indeterminate else "externos"
        all_paths[category].add(path)
        key = (record_type, related_id, period)
        grouped[category].setdefault(key, set()).add(path)

    return {
        category: {
            "cantidad": len(all_paths[category]),
            "referencias": [
                {
                    "tipo_registro": record_type,
                    "cantidad": len(referenced_paths),
                    **({"conciliacion_id": related_id} if related_id else {}),
                    **({"periodo": period} if period else {}),
                    **({"indeterminada": True} if category == "indeterminados" else {}),
                }
                for (record_type, related_id, period), referenced_paths
                in sorted(grouped[category].items())
            ],
        }
        for category in ("externos", "indeterminados")
    }


def _allowed_storage_path(path, scope):
    company_id = scope["company_id"]
    account_id = scope["account_id"]
    prefixes = (
        f"imports/{company_id}/{account_id}/",
        f"cheques/{company_id}/{account_id}/",
        f"depositos/{company_id}/{account_id}/",
        f"reports/{company_id}/{account_id}/",
        f"reports/summary/{company_id}/{account_id}/",
    )
    return isinstance(path, str) and path.startswith(prefixes) and ".." not in path.split("/")


def _cross_period_references(cursor, reconciliation_id):
    cursor.execute(
        CROSS_PERIOD_QUERY,
        [reconciliation_id] * CROSS_PERIOD_QUERY.count("%s"),
    )
    return [
        {"tipo": row[0], "periodo": row[1], "cantidad": int(row[2])}
        for row in cursor.fetchall()
    ]


def _audit_count(cursor, reconciliation_id):
    selects = []
    params = []
    for table, query in TARGET_RECORDS:
        selects.append(f"SELECT %s::text AS tabla,registro_id FROM (")
        # Reuse the same ownership query as the purge whitelist. Composite keys
        # use their stable two-column key so prior audit snapshots are removed too.
        if table == "cruce_movimientos":
            inner = """SELECT concat_ws(':',cm.cruce_id,cm.movimiento_id) AS registro_id
                FROM contared.cruce_movimientos cm
                JOIN contared.cruces x ON x.id=cm.cruce_id WHERE x.conciliacion_id=%s"""
        elif table == "ejecucion_propuestas":
            inner = """SELECT concat_ws(':',ejecucion_id,propuesta_id) AS registro_id
                FROM contared.ejecucion_propuestas WHERE conciliacion_id=%s"""
        elif table == "deposito_movimientos":
            inner = """SELECT concat_ws(':',dm.caso_id,dm.movimiento_id) AS registro_id
                FROM contared.deposito_movimientos dm
                JOIN contared.casos_deposito d ON d.id=dm.caso_id
                WHERE d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s"""
        else:
            inner = query.replace("id::text", "id::text AS registro_id", 1)
        selects[-1] += inner + ") scoped"
        params.append(table)
        params.extend([reconciliation_id] * inner.count("%s"))
    cursor.execute(
        """WITH scoped(tabla,registro_id) AS (""" + " UNION ALL ".join(selects) + """)
           SELECT count(*) FROM contared.auditoria a JOIN scoped s ON a.tabla=s.tabla
           AND (a.registro_id=s.registro_id OR
             (a.registro_id IS NULL AND contared.clave_purga(
                a.tabla,coalesce(a.antes,a.despues))=s.registro_id))""",
        params,
    )
    return int(cursor.fetchone()[0])


def _preview(cursor, reconciliation_id, scope):
    cursor.execute(
        """
        SELECT b.nombre,cb.alias,cb.numero,cb.moneda_codigo,r.periodo::text,
               r.fecha_corte::text,r.archivada_en
        FROM contared.conciliaciones r
        JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
        JOIN contared.bancos b ON b.id=cb.banco_id
        WHERE r.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s
        """,
        [reconciliation_id, scope["company_id"], scope["country_code"]],
    )
    identity = cursor.fetchone()
    if identity is None:
        raise NotFound("La conciliación no existe.")
    counts = _deletion_counts(cursor, reconciliation_id)
    paths = _storage_paths(cursor, reconciliation_id)
    blockers = _cross_period_references(cursor, reconciliation_id)
    shared = _shared_storage_paths(cursor, reconciliation_id, paths, scope)
    unknown_paths = [path for path in paths if not _allowed_storage_path(path, scope)]
    if shared["externos"]["cantidad"]:
        blockers.append({
            "tipo": "Archivos referenciados fuera de esta conciliación",
            **shared["externos"],
        })
    if shared["indeterminados"]["cantidad"]:
        blockers.append({
            "tipo": "Archivos con propiedad indeterminada",
            **shared["indeterminados"],
        })
    if unknown_paths:
        blockers.append({
            "tipo": "Archivos con una ruta no reconocida; requiere revisión",
            "cantidad": len(unknown_paths),
        })
    counts["archivos_privados"] = len(paths)
    counts["auditoria_financiera"] = _audit_count(cursor, reconciliation_id)
    return {
        "id": str(reconciliation_id),
        "banco": identity[0],
        "cuenta_alias": identity[1],
        "cuenta_numero": identity[2],
        "moneda_codigo": identity[3],
        "periodo": identity[4],
        "fecha_corte": identity[5],
        "archivada": identity[6] is not None,
        "frase_requerida": f"ELIMINAR {str(reconciliation_id).upper()} {identity[4][:7]}",
        "cantidades": counts,
        "bloqueos": blockers,
        "libera_cuenta_periodo": not blockers,
    }


def _populate_authorized_records(cursor, reconciliation_id):
    cursor.execute(
        """CREATE TEMP TABLE contared_purga_autorizada (
             tabla text NOT NULL, registro_id text NOT NULL,
             conciliacion_id uuid NOT NULL,
             PRIMARY KEY (tabla,registro_id)
           ) ON COMMIT DROP"""
    )
    cursor.execute(
        "GRANT SELECT ON TABLE pg_temp.contared_purga_autorizada TO PUBLIC"
    )
    for table, query in TARGET_RECORDS:
        if table == "cruce_movimientos":
            query = """SELECT concat_ws(':',cm.cruce_id,cm.movimiento_id) AS registro_id
                FROM contared.cruce_movimientos cm
                JOIN contared.cruces x ON x.id=cm.cruce_id
                WHERE x.conciliacion_id=%s"""
        elif table == "ejecucion_propuestas":
            query = """SELECT concat_ws(':',ejecucion_id,propuesta_id) AS registro_id
                FROM contared.ejecucion_propuestas WHERE conciliacion_id=%s"""
        elif table == "deposito_movimientos":
            query = """SELECT concat_ws(':',dm.caso_id,dm.movimiento_id) AS registro_id
                FROM contared.deposito_movimientos dm
                JOIN contared.casos_deposito d ON d.id=dm.caso_id
                WHERE d.conciliacion_origen_id=%s OR d.conciliacion_resolucion_id=%s"""
        else:
            query = query.replace("id::text", "id::text AS registro_id", 1)
        cursor.execute(
            "INSERT INTO pg_temp.contared_purga_autorizada(tabla,registro_id,conciliacion_id) "
            f"SELECT %s,registro_id,%s FROM ({query}) q "
            "ON CONFLICT DO NOTHING",
            [table, reconciliation_id]
            + [reconciliation_id] * query.count("%s"),
        )


def _delete_target_audit(cursor, reconciliation_id):
    cursor.execute(
        "SELECT contared.eliminar_auditoria_purga(%s)",
        [reconciliation_id],
    )


def _lock_and_validate_scope(cursor, request, reconciliation_id, scope):
    cursor.execute(
        """
        SELECT r.id::text,r.cuenta_id::text,cb.empresa_id::text,cb.pais_codigo,
               cb.alias,cb.numero,b.nombre,cb.moneda_codigo,r.periodo::text,
               r.fecha_corte::text,r.archivada_en
        FROM contared.conciliaciones r
        JOIN contared.cuentas_bancarias cb ON cb.id=r.cuenta_id
        JOIN contared.bancos b ON b.id=cb.banco_id
        JOIN contared.empresas e ON e.id=cb.empresa_id AND e.activa IS TRUE
        JOIN contared.usuarios u ON u.id=%s AND u.auth_user_id=%s AND u.activo IS TRUE
        JOIN contared.accesos a ON a.usuario_id=u.id
          AND a.empresa_id=cb.empresa_id AND a.pais_codigo=cb.pais_codigo
          AND a.rol='administrador'
        WHERE r.id=%s AND cb.empresa_id=%s AND cb.pais_codigo=%s
        FOR UPDATE OF r FOR SHARE OF u,a,e
        """,
        [
            scope["user_id"], request.user.auth_user_id, reconciliation_id,
            scope["company_id"], scope["country_code"],
        ],
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "reconciliation_id": row[0],
        "account_id": row[1],
        "company_id": row[2],
        "country_code": row[3],
        "account_alias": row[4],
        "account_number": row[5],
        "bank": row[6],
        "currency": row[7],
        "period": row[8],
        "cutoff": row[9],
        "archived_at": row[10],
    }


def _existing_receipt_in_scope(cursor, request, reconciliation_id, scope):
    cursor.execute(
        """
        SELECT e.id::text
        FROM contared.eliminaciones_conciliacion e
        JOIN contared.empresas company
          ON company.id=e.empresa_id AND company.activa IS TRUE
        JOIN contared.usuarios u
          ON u.id=%s AND u.auth_user_id=%s AND u.activo IS TRUE
        JOIN contared.accesos a
          ON a.usuario_id=u.id AND a.empresa_id=e.empresa_id
          AND a.pais_codigo=e.pais_codigo AND a.rol='administrador'
        WHERE e.conciliacion_id=%s
          AND e.empresa_id=%s AND e.pais_codigo=%s
        """,
        [
            scope["user_id"], request.user.auth_user_id, reconciliation_id,
            scope["company_id"], scope["country_code"],
        ],
    )
    return cursor.fetchone() is not None


def _receipt_scope(request, reconciliation_id):
    session = load_session_access(request.user.auth_user_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    """SELECT e.id::text,e.empresa_id::text,e.pais_codigo
                       FROM contared.eliminaciones_conciliacion e
                       WHERE e.conciliacion_id=%s""",
                    [reconciliation_id],
                )
                row = cursor.fetchone()
    except DatabaseError as exc:
        _raise_deletion_database_error(exc, "receipt_scope_lookup")
    if row is None:
        raise NotFound("La conciliación no existe.")
    access = next(
        (item for item in session["accesos"]
         if item["empresa_id"] == row[1] and item["pais_codigo"] == row[2]),
        None,
    )
    if access is None:
        raise NotFound("La conciliación no existe.")
    scope = {
        "user_id": session["usuario"]["id"],
        "company_id": row[1],
        "country_code": row[2],
        "role": access["rol"],
    }
    _require_administrator(scope)
    return row[0], scope


def _cleanup_result(cursor, reconciliation_id):
    cursor.execute(
        """
        SELECT e.id::text,e.cantidades,e.archivos_total,
               count(s.id) FILTER (WHERE s.eliminado_en IS NOT NULL),
               count(s.id) FILTER (WHERE s.eliminado_en IS NULL)
        FROM contared.eliminaciones_conciliacion e
        LEFT JOIN contared.eliminacion_conciliacion_storage s
          ON s.eliminacion_id=e.id
        WHERE e.conciliacion_id=%s
        GROUP BY e.id
        """,
        [reconciliation_id],
    )
    row = cursor.fetchone()
    if row is None:
        raise NotFound("No existe un comprobante de eliminación para esta conciliación.")
    complete = int(row[4]) == 0
    return {
        "id": str(reconciliation_id),
        "eliminacion_id": row[0],
        "eliminada": True,
        "limpieza_completa": complete,
        "archivos_total": int(row[2]),
        "archivos_eliminados": int(row[3]),
        "archivos_pendientes": int(row[4]),
        "cantidades": row[1],
    }


def _retry_storage_cleanup(reconciliation_id):
    stage = "load_pending_storage"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id::text,object_path
                   FROM contared.eliminacion_conciliacion_storage
                   WHERE eliminacion_id=(SELECT id FROM contared.eliminaciones_conciliacion
                     WHERE conciliacion_id=%s)
                     AND eliminado_en IS NULL
                   ORDER BY id""",
                [reconciliation_id],
            )
            pending = cursor.fetchall()
        for file_id, object_path in pending:
            if not isinstance(object_path, str) or not object_path or ".." in object_path.split("/"):
                raise DatabaseError("La ruta privada pendiente no es válida.")
            succeeded = False
            for _ in range(3):
                try:
                    _storage_request("DELETE", object_path, allow_not_found=True)
                    succeeded = True
                except ImportStorageUnavailable:
                    stage = "record_storage_failure"
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """UPDATE contared.eliminacion_conciliacion_storage
                               SET intentos=intentos+1,ultimo_error='storage_delete_failed'
                               WHERE id=%s AND eliminado_en IS NULL""",
                            [file_id],
                        )
                    logger.warning(
                        "Private file cleanup attempt failed (reconciliation_id=%s).",
                        reconciliation_id,
                    )
                else:
                    stage = "record_storage_success"
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """UPDATE contared.eliminacion_conciliacion_storage
                               SET intentos=intentos+1,eliminado_en=now(),ultimo_error=NULL
                               WHERE id=%s AND eliminado_en IS NULL""",
                            [file_id],
                        )
                    break
            if not succeeded:
                continue
        stage = "mark_storage_cleanup_complete"
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE contared.eliminaciones_conciliacion e
                   SET limpieza_completa=NOT EXISTS (
                     SELECT 1 FROM contared.eliminacion_conciliacion_storage s
                     WHERE s.eliminacion_id=e.id AND s.eliminado_en IS NULL)
                   WHERE e.conciliacion_id=%s""",
                [reconciliation_id],
            )
            return _cleanup_result(cursor, reconciliation_id)
    except DatabaseError as exc:
        _raise_deletion_database_error(exc, stage)


class ReconciliationDeletionView(APIView):
    def get(self, request, reconciliation_id):
        scope = _scope_for_reconciliation(request, reconciliation_id)
        _require_administrator(scope)
        _require_deletion_schema()
        try:
            stage = "preview"
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    preview = _preview(cursor, reconciliation_id, scope)
        except DatabaseError as exc:
            _raise_deletion_database_error(exc, stage)
        return Response(preview)

    def delete(self, request, reconciliation_id):
        _require_deletion_schema()
        data = request.data
        if not isinstance(data, dict):
            raise ValidationError({"detail": "La solicitud debe ser un objeto JSON."})
        if data.get("reintentar_storage") is True:
            if set(data) != {"reintentar_storage"}:
                raise ValidationError({"detail": "La solicitud de reintento contiene campos no permitidos."})
            _receipt_scope(request, reconciliation_id)
            return Response(_retry_storage_cleanup(reconciliation_id))
        if set(data) - {"motivo", "frase_confirmacion", "confirmar_irreversible"}:
            raise ValidationError({"detail": "La solicitud contiene campos no permitidos."})
        try:
            _receipt_scope(request, reconciliation_id)
        except NotFound:
            pass
        else:
            return Response(_retry_storage_cleanup(reconciliation_id))
        reason = data.get("motivo")
        phrase = data.get("frase_confirmacion")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValidationError({"motivo": "Indica un motivo de hasta 1,000 caracteres."})
        if data.get("confirmar_irreversible") is not True:
            raise ValidationError({
                "confirmar_irreversible": "Confirma explícitamente que la eliminación es irreversible."
            })

        try:
            scope = _scope_for_reconciliation(request, reconciliation_id)
        except NotFound:
            _receipt_scope(request, reconciliation_id)
            return Response(_retry_storage_cleanup(reconciliation_id))
        _require_administrator(scope)
        stage = "lock_reconciliation"
        try:
            result = None
            with transaction.atomic():
                with connection.cursor() as cursor:
                    stage = "lock_reconciliation"
                    selected = _lock_and_validate_scope(
                        cursor, request, reconciliation_id, scope,
                    )
                    if selected is None:
                        if not _existing_receipt_in_scope(
                            cursor, request, reconciliation_id, scope,
                        ):
                            raise PermissionDenied(
                                "El acceso de administrador cambió o la conciliación ya no está disponible.",
                                code="scope_not_authorized",
                            )
                    else:
                        expected_phrase = (
                            f"ELIMINAR {str(reconciliation_id).upper()} {selected['period'][:7]}"
                        )
                        if not isinstance(phrase, str) or phrase.strip() != expected_phrase:
                            raise ValidationError({
                                "frase_confirmacion": "Escribe exactamente la frase mostrada en la vista previa."
                            })
                        stage = "revalidate_dependencies"
                        preview = _preview(cursor, reconciliation_id, scope)
                        if preview["bloqueos"]:
                            details = "; ".join(
                                f"{item['tipo']} ("
                                f"{'período ' + item['periodo'] + '; ' if item.get('periodo') else ''}"
                                f"{item['cantidad']} registro(s)"
                                + (
                                    ": "
                                    + ", ".join(
                                        f"{reference['tipo_registro']}"
                                        + (
                                            f" en conciliación {reference['conciliacion_id']}"
                                            if reference.get("conciliacion_id") else ""
                                        )
                                        + (
                                            f" período {reference['periodo']}"
                                            if reference.get("periodo") else ""
                                        )
                                        + (
                                            " (propiedad indeterminada)"
                                            if reference.get("indeterminada") else ""
                                        )
                                        for reference in item.get("referencias", [])
                                    )
                                    if item.get("referencias") else ""
                                )
                                + ")"
                                for item in preview["bloqueos"]
                            )
                            raise ReconciliationConflict(
                                "No se puede eliminar sin afectar otras referencias: " + details
                            )
                        stage = "collect_storage_paths"
                        paths = _storage_paths(cursor, reconciliation_id)
                        purge_id = uuid4()
                        stage = "set_maintenance_scope"
                        cursor.execute(
                            "SELECT set_config('contared.actor_id',%s,true)",
                            [scope["user_id"]],
                        )
                        cursor.execute(
                            "SELECT set_config('contared.purga_conciliacion_id',%s,true)",
                            [str(reconciliation_id)],
                        )
                        stage = "authorize_purge_rows"
                        _populate_authorized_records(cursor, reconciliation_id)
                        stage = "write_deletion_receipt"
                        cursor.execute(
                            """INSERT INTO contared.eliminaciones_conciliacion
                               (id,conciliacion_id,empresa_id,pais_codigo,actor_id,motivo,
                                cantidades,archivos_total,limpieza_completa)
                               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                            [
                                purge_id, reconciliation_id, scope["company_id"],
                                scope["country_code"], scope["user_id"], reason.strip(),
                                json.dumps(preview["cantidades"]), len(paths), not paths,
                            ],
                        )
                        if paths:
                            stage = "queue_storage_cleanup"
                            cursor.execute(
                                """INSERT INTO contared.eliminacion_conciliacion_storage
                                   (eliminacion_id,object_path)
                                   SELECT %s,path FROM unnest(%s::text[]) AS paths(path)""",
                                [purge_id, paths],
                            )
                        for table in DELETE_ORDER:
                            stage = f"delete:{table}"
                            cursor.execute(DELETE_SQL[table])
                        stage = "delete_target_audit"
                        _delete_target_audit(cursor, reconciliation_id)
                        stage = "commit_transaction"
            result = _retry_storage_cleanup(reconciliation_id)
        except (DatabaseError, ImportStorageUnavailable) as exc:
            if isinstance(exc, ImportStorageUnavailable):
                logger.error(
                    "Permanent reconciliation deletion storage failure "
                    "(stage=storage_cleanup, error_type=%s).",
                    type(exc).__name__,
                )
                raise
            _raise_deletion_database_error(exc, stage)
        return Response(result)


class ReconciliationDeletionPendingView(APIView):
    def get(self, request):
        scope = scope_from_request(request)
        _require_administrator(scope)
        _require_deletion_schema()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """
                        SELECT e.conciliacion_id::text,e.id::text,e.motivo,
                               e.creada_en::text,count(s.id) FILTER (WHERE s.eliminado_en IS NULL)
                        FROM contared.eliminaciones_conciliacion e
                        JOIN contared.eliminacion_conciliacion_storage s
                          ON s.eliminacion_id=e.id AND s.eliminado_en IS NULL
                        WHERE e.empresa_id=%s AND e.pais_codigo=%s
                        GROUP BY e.id
                        ORDER BY e.creada_en DESC LIMIT 100
                        """,
                        [scope["company_id"], scope["country_code"]],
                    )
                    rows = cursor.fetchall()
        except DatabaseError as exc:
            _raise_deletion_database_error(exc, "pending_cleanup_lookup")
        return Response({
            "results": [
                {
                    "conciliacion_id": row[0],
                    "eliminacion_id": row[1],
                    "motivo": row[2],
                    "eliminada_en": row[3],
                    "archivos_pendientes": int(row[4]),
                }
                for row in rows
            ]
        })
