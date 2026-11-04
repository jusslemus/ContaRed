-- Registro operativo e historial de cheques. No altera movimientos ni crea ajustes.
BEGIN;

CREATE TABLE contared.cheques (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_origen_id uuid NOT NULL,
  movimiento_emision_id uuid NOT NULL UNIQUE,
  numero_cheque text NOT NULL CHECK (btrim(numero_cheque) <> ''),
  moneda_codigo text NOT NULL REFERENCES contared.monedas(codigo),
  fecha_emision date NOT NULL,
  beneficiario text,
  monto numeric(20,4) NOT NULL CHECK (monto > 0),
  estado text NOT NULL CHECK (estado IN ('retenido','en_circulacion','cobrado','anulado')),
  fecha_entrega date,
  entregado_por uuid REFERENCES contared.usuarios(id),
  observacion_entrega text,
  movimiento_cobro_id uuid UNIQUE,
  conciliacion_resolucion_id uuid,
  fecha_anulacion date,
  motivo_anulacion text,
  soporte_anulacion_ruta text,
  creado_por uuid NOT NULL REFERENCES contared.usuarios(id),
  creado_en timestamptz NOT NULL DEFAULT now(),
  actualizado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_origen_id,cuenta_id)
    REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (movimiento_emision_id,cuenta_id)
    REFERENCES contared.movimientos(id,cuenta_id),
  FOREIGN KEY (movimiento_cobro_id,cuenta_id)
    REFERENCES contared.movimientos(id,cuenta_id),
  FOREIGN KEY (conciliacion_resolucion_id,cuenta_id)
    REFERENCES contared.conciliaciones(id,cuenta_id),
  CHECK (
    (estado='retenido' AND fecha_entrega IS NULL AND entregado_por IS NULL
      AND movimiento_cobro_id IS NULL AND conciliacion_resolucion_id IS NULL
      AND fecha_anulacion IS NULL AND motivo_anulacion IS NULL
      AND soporte_anulacion_ruta IS NULL)
    OR (estado='en_circulacion' AND fecha_entrega IS NOT NULL AND entregado_por IS NOT NULL
      AND movimiento_cobro_id IS NULL AND conciliacion_resolucion_id IS NULL
      AND fecha_anulacion IS NULL AND motivo_anulacion IS NULL
      AND soporte_anulacion_ruta IS NULL)
    OR (estado='cobrado' AND movimiento_cobro_id IS NOT NULL
      AND conciliacion_resolucion_id IS NOT NULL
      AND fecha_anulacion IS NULL AND motivo_anulacion IS NULL
      AND soporte_anulacion_ruta IS NULL)
    OR (estado='anulado' AND movimiento_cobro_id IS NULL
      AND conciliacion_resolucion_id IS NULL AND fecha_anulacion IS NOT NULL
      AND motivo_anulacion IS NOT NULL AND btrim(motivo_anulacion)<>''
      AND soporte_anulacion_ruta IS NOT NULL)
  )
);
CREATE INDEX cheques_cuenta_estado_fecha
  ON contared.cheques(cuenta_id,estado,fecha_emision DESC);
CREATE INDEX cheques_numero_fecha
  ON contared.cheques(cuenta_id,numero_cheque,fecha_emision);

CREATE TABLE contared.cheque_eventos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cheque_id uuid NOT NULL REFERENCES contared.cheques(id),
  estado_anterior text CHECK (estado_anterior IN ('retenido','en_circulacion','cobrado','anulado')),
  estado_nuevo text NOT NULL CHECK (estado_nuevo IN ('retenido','en_circulacion','cobrado','anulado')),
  fecha_efectiva date NOT NULL,
  registrado_por uuid NOT NULL REFERENCES contared.usuarios(id),
  registrado_en timestamptz NOT NULL DEFAULT now(),
  motivo text,
  observacion text,
  movimiento_banco_id uuid REFERENCES contared.movimientos(id),
  conciliacion_id uuid REFERENCES contared.conciliaciones(id),
  cruce_id uuid REFERENCES contared.cruces(id),
  soporte_ruta text,
  UNIQUE (id,cheque_id)
);
CREATE INDEX cheque_eventos_historial
  ON contared.cheque_eventos(cheque_id,fecha_efectiva DESC,registrado_en DESC);

CREATE FUNCTION contared.impedir_mutacion_evento_cheque() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'El historial de cheques es inmutable';
END $$;
CREATE TRIGGER cheque_evento_inmutable
  BEFORE UPDATE OR DELETE ON contared.cheque_eventos
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_mutacion_evento_cheque();

ALTER TABLE contared.cheques ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.cheque_eventos ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.cheques,contared.cheque_eventos FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE contared.cheques,contared.cheque_eventos TO service_role;
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE ON contared.cheques
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE ON contared.cheque_eventos
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();

-- Reconoce únicamente cruces confirmados 1:1 con número explícito e importes idénticos.
WITH evidencia AS (
  SELECT x.id AS cruce_id,x.cuenta_id,x.conciliacion_id,x.confirmado_por,
         x.confirmado_en,b.id AS banco_id,l.id AS libros_id,l.numero_cheque,
         l.fecha AS fecha_emision,b.fecha AS fecha_cobro,abs(l.monto) AS monto,
         cb.moneda_codigo
  FROM contared.cruces x
  JOIN contared.cruce_movimientos cm_b ON cm_b.cruce_id=x.id
  JOIN contared.movimientos b ON b.id=cm_b.movimiento_id AND b.lado='banco'
  JOIN contared.cruce_movimientos cm_l ON cm_l.cruce_id=x.id
  JOIN contared.movimientos l ON l.id=cm_l.movimiento_id AND l.lado='libros'
  JOIN contared.cuentas_bancarias cb ON cb.id=x.cuenta_id
  WHERE x.estado='confirmado' AND x.tipo='coincidencia'
    AND b.numero_cheque IS NOT NULL AND btrim(b.numero_cheque)<>''
    AND b.numero_cheque=l.numero_cheque
    AND b.monto<0 AND l.monto<0 AND abs(b.monto)=abs(l.monto)
    AND b.fecha>=l.fecha
    AND (SELECT count(*) FROM contared.cruce_movimientos cm
         JOIN contared.movimientos m ON m.id=cm.movimiento_id
         WHERE cm.cruce_id=x.id AND m.lado='banco')=1
    AND (SELECT count(*) FROM contared.cruce_movimientos cm
         JOIN contared.movimientos m ON m.id=cm.movimiento_id
         WHERE cm.cruce_id=x.id AND m.lado='libros')=1
), unicos AS (
  SELECT e.* FROM evidencia e
  WHERE (SELECT count(*) FROM evidencia same_number
         WHERE same_number.cuenta_id=e.cuenta_id
           AND same_number.numero_cheque=e.numero_cheque)=1
    AND (SELECT count(*) FROM contared.movimientos l2
         JOIN contared.cargas c2 ON c2.id=l2.carga_id
         WHERE l2.cuenta_id=e.cuenta_id AND l2.lado='libros'
           AND l2.numero_cheque=e.numero_cheque AND c2.estado='validada')=1
    AND (SELECT count(*) FROM contared.movimientos b2
         JOIN contared.cargas c2 ON c2.id=b2.carga_id
         WHERE b2.cuenta_id=e.cuenta_id AND b2.lado='banco'
           AND b2.numero_cheque=e.numero_cheque AND c2.estado='validada')=1
)
INSERT INTO contared.cheques
  (id,cuenta_id,conciliacion_origen_id,movimiento_emision_id,numero_cheque,
   moneda_codigo,fecha_emision,monto,estado,movimiento_cobro_id,
   conciliacion_resolucion_id,creado_por,creado_en,actualizado_en)
SELECT gen_random_uuid(),cuenta_id,conciliacion_id,libros_id,numero_cheque,
       moneda_codigo,fecha_emision,monto,'cobrado',banco_id,conciliacion_id,
       confirmado_por,confirmado_en,confirmado_en
FROM unicos
ON CONFLICT (movimiento_emision_id) DO NOTHING;

WITH evidencia AS (
  SELECT x.id AS cruce_id,x.cuenta_id,x.conciliacion_id,x.confirmado_por,
         x.confirmado_en,b.id AS banco_id,l.id AS libros_id,l.numero_cheque,
         l.fecha AS fecha_emision,b.fecha AS fecha_cobro,abs(l.monto) AS monto,
         cb.moneda_codigo
  FROM contared.cruces x
  JOIN contared.cruce_movimientos cm_b ON cm_b.cruce_id=x.id
  JOIN contared.movimientos b ON b.id=cm_b.movimiento_id AND b.lado='banco'
  JOIN contared.cruce_movimientos cm_l ON cm_l.cruce_id=x.id
  JOIN contared.movimientos l ON l.id=cm_l.movimiento_id AND l.lado='libros'
  JOIN contared.cuentas_bancarias cb ON cb.id=x.cuenta_id
  WHERE x.estado='confirmado' AND x.tipo='coincidencia'
    AND b.numero_cheque IS NOT NULL AND btrim(b.numero_cheque)<>''
    AND b.numero_cheque=l.numero_cheque
    AND b.monto<0 AND l.monto<0 AND abs(b.monto)=abs(l.monto)
    AND b.fecha>=l.fecha
    AND (SELECT count(*) FROM contared.cruce_movimientos cm
         JOIN contared.movimientos m ON m.id=cm.movimiento_id
         WHERE cm.cruce_id=x.id AND m.lado='banco')=1
    AND (SELECT count(*) FROM contared.cruce_movimientos cm
         JOIN contared.movimientos m ON m.id=cm.movimiento_id
         WHERE cm.cruce_id=x.id AND m.lado='libros')=1
), unicos AS (
  SELECT e.* FROM evidencia e
  WHERE (SELECT count(*) FROM evidencia same_number
         WHERE same_number.cuenta_id=e.cuenta_id
           AND same_number.numero_cheque=e.numero_cheque)=1
    AND (SELECT count(*) FROM contared.movimientos l2
         JOIN contared.cargas c2 ON c2.id=l2.carga_id
         WHERE l2.cuenta_id=e.cuenta_id AND l2.lado='libros'
           AND l2.numero_cheque=e.numero_cheque AND c2.estado='validada')=1
    AND (SELECT count(*) FROM contared.movimientos b2
         JOIN contared.cargas c2 ON c2.id=b2.carga_id
         WHERE b2.cuenta_id=e.cuenta_id AND b2.lado='banco'
           AND b2.numero_cheque=e.numero_cheque AND c2.estado='validada')=1
)
INSERT INTO contared.cheque_eventos
  (cheque_id,estado_anterior,estado_nuevo,fecha_efectiva,registrado_por,
   registrado_en,motivo,movimiento_banco_id,conciliacion_id,cruce_id)
SELECT ch.id,NULL,'cobrado',b.fecha,x.confirmado_por,x.confirmado_en,
       'Cobro reconocido por cruce confirmado 1:1',b.id,x.conciliacion_id,x.id
FROM unicos u
JOIN contared.cheques ch ON ch.movimiento_emision_id=u.libros_id
JOIN contared.cruces x ON x.id=u.cruce_id AND x.estado='confirmado'
JOIN contared.movimientos b ON b.id=u.banco_id
WHERE ch.estado='cobrado'
  AND NOT EXISTS (SELECT 1 FROM contared.cheque_eventos ev WHERE ev.cheque_id=ch.id);

INSERT INTO contared.migraciones(version)
VALUES ('202610300001_cheques');
COMMIT;
