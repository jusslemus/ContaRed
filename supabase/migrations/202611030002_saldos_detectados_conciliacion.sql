-- Preserve document-detected final balances and their source separately from
-- amounts entered manually on a reconciliation.
BEGIN;

ALTER TABLE contared.conciliaciones
  ADD COLUMN saldo_final_banco_detectado numeric(20,4),
  ADD COLUMN saldo_final_banco_fuente jsonb,
  ADD COLUMN saldo_final_libros_detectado numeric(20,4),
  ADD COLUMN saldo_final_libros_fuente jsonb,
  ADD CONSTRAINT conciliaciones_saldo_banco_detectado_coherente CHECK (
    (saldo_final_banco_detectado IS NULL AND saldo_final_banco_fuente IS NULL)
    OR (saldo_final_banco_detectado IS NOT NULL
      AND jsonb_typeof(saldo_final_banco_fuente)='object')
  ),
  ADD CONSTRAINT conciliaciones_saldo_libros_detectado_coherente CHECK (
    (saldo_final_libros_detectado IS NULL AND saldo_final_libros_fuente IS NULL)
    OR (saldo_final_libros_detectado IS NOT NULL
      AND jsonb_typeof(saldo_final_libros_fuente)='object')
  );

COMMENT ON COLUMN contared.conciliaciones.saldo_final_banco_detectado IS
  'Saldo final obtenido de una carga bancaria; separado del valor manual saldo_final_banco.';
COMMENT ON COLUMN contared.conciliaciones.saldo_final_banco_fuente IS
  'Trazabilidad del saldo bancario detectado: carga, archivo, hoja, fila y método.';
COMMENT ON COLUMN contared.conciliaciones.saldo_final_libros_detectado IS
  'Saldo final obtenido de una carga contable; separado del valor manual saldo_final_libros.';
COMMENT ON COLUMN contared.conciliaciones.saldo_final_libros_fuente IS
  'Trazabilidad del saldo contable detectado: carga, archivo, hoja, fila y método.';

WITH detections AS (
  SELECT r.id,b.detectado AS saldo_banco,b.fuente AS fuente_banco,
         l.detectado AS saldo_libros,l.fuente AS fuente_libros
  FROM contared.conciliaciones r
  LEFT JOIN LATERAL (
    SELECT m.saldo_origen AS detectado,
           jsonb_build_object(
             'carga_id',c.id::text,
             'archivo',c.nombre_archivo,
             'hoja',m.hoja_origen,
             'fila',m.fila_origen,
             'metodo','Última fila de la carga con saldo acumulado no vacío.',
             'campo','saldo_origen',
             'carga_creada_en',c.creada_en
           ) AS fuente
    FROM contared.cargas c
    JOIN contared.movimientos m ON m.carga_id=c.id
    WHERE c.conciliacion_id=r.id AND c.estado='validada'
      AND c.origen='banco' AND m.lado='banco'
      AND m.fecha<=r.fecha_corte AND m.saldo_origen IS NOT NULL
    ORDER BY c.creada_en DESC,c.id DESC,m.fila_origen DESC,m.id DESC
    LIMIT 1
  ) b ON true
  LEFT JOIN LATERAL (
    SELECT m.saldo_origen AS detectado,
           jsonb_build_object(
             'carga_id',c.id::text,
             'archivo',c.nombre_archivo,
             'hoja',m.hoja_origen,
             'fila',m.fila_origen,
             'metodo','Última fila de la carga con saldo acumulado no vacío.',
             'campo','saldo_origen',
             'carga_creada_en',c.creada_en
           ) AS fuente
    FROM contared.cargas c
    JOIN contared.movimientos m ON m.carga_id=c.id
    WHERE c.conciliacion_id=r.id AND c.estado='validada'
      AND c.origen='libros' AND m.lado='libros'
      AND m.fecha<=r.fecha_corte AND m.saldo_origen IS NOT NULL
    ORDER BY c.creada_en DESC,c.id DESC,m.fila_origen DESC,m.id DESC
    LIMIT 1
  ) l ON true
)
UPDATE contared.conciliaciones r
SET saldo_final_banco_detectado=d.saldo_banco,
    saldo_final_banco_fuente=d.fuente_banco,
    saldo_final_libros_detectado=d.saldo_libros,
    saldo_final_libros_fuente=d.fuente_libros
FROM detections d
WHERE d.id=r.id
  AND r.archivada_en IS NULL
  AND (d.saldo_banco IS NOT NULL OR d.saldo_libros IS NOT NULL);

INSERT INTO contared.migraciones(version)
VALUES ('202611030002_saldos_detectados_conciliacion');

COMMIT;
