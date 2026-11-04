-- Persist comparison executions, review proposals and dismissals without
-- changing source movements or forcing proposal states into contared.cruces.
BEGIN;

CREATE INDEX cargas_conciliacion_comparacion
  ON contared.cargas(conciliacion_id,estado,origen);

CREATE TABLE contared.ejecuciones_comparacion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_id uuid NOT NULL,
  moneda_codigo text NOT NULL REFERENCES contared.monedas(codigo),
  periodo date NOT NULL,
  fecha_corte date NOT NULL,
  huella_datos text NOT NULL CHECK (huella_datos ~ '^[a-f0-9]{64}$'),
  creada_por uuid NOT NULL REFERENCES contared.usuarios(id),
  creada_en timestamptz NOT NULL DEFAULT now(),
  ultima_ejecucion_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_id,cuenta_id)
    REFERENCES contared.conciliaciones(id,cuenta_id),
  UNIQUE (conciliacion_id,huella_datos),
  UNIQUE (id,conciliacion_id,cuenta_id)
);

CREATE TABLE contared.propuestas_comparacion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ejecucion_origen_id uuid NOT NULL,
  conciliacion_id uuid NOT NULL,
  cuenta_id uuid NOT NULL,
  regla_codigo text NOT NULL CHECK (regla_codigo IN (
    'reference_amount','amount_date','reference_date_group',
    'reference_amount_mismatch','ambiguous','unmatched'
  )),
  clave_propuesta text NOT NULL CHECK (clave_propuesta ~ '^[a-f0-9]{64}$'),
  banco_ids jsonb NOT NULL CHECK (jsonb_typeof(banco_ids) = 'array'),
  libros_ids jsonb NOT NULL CHECK (jsonb_typeof(libros_ids) = 'array'),
  explicacion text NOT NULL CHECK (btrim(explicacion) <> ''),
  estado text NOT NULL DEFAULT 'propuesta'
    CHECK (estado IN ('propuesta','descartada','confirmada')),
  cruce_id uuid UNIQUE,
  resuelta_por uuid REFERENCES contared.usuarios(id),
  resuelta_en timestamptz,
  motivo_descarte text,
  creada_por uuid NOT NULL REFERENCES contared.usuarios(id),
  creada_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (ejecucion_origen_id,conciliacion_id,cuenta_id)
    REFERENCES contared.ejecuciones_comparacion(id,conciliacion_id,cuenta_id),
  FOREIGN KEY (conciliacion_id,cuenta_id)
    REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (cruce_id,cuenta_id)
    REFERENCES contared.cruces(id,cuenta_id),
  UNIQUE (conciliacion_id,clave_propuesta),
  UNIQUE (id,conciliacion_id,cuenta_id),
  CHECK (
    (jsonb_array_length(banco_ids) > 0 OR jsonb_array_length(libros_ids) > 0)
    AND (
    (estado = 'propuesta' AND cruce_id IS NULL AND resuelta_por IS NULL
      AND resuelta_en IS NULL AND motivo_descarte IS NULL)
    OR (estado = 'descartada' AND cruce_id IS NULL AND resuelta_por IS NOT NULL
      AND resuelta_en IS NOT NULL)
    OR (estado = 'confirmada' AND cruce_id IS NOT NULL
      AND resuelta_por IS NOT NULL AND resuelta_en IS NOT NULL
      AND motivo_descarte IS NULL)
    )
  )
);
CREATE INDEX propuestas_comparacion_revision
  ON contared.propuestas_comparacion(conciliacion_id,estado,creada_en);
CREATE INDEX ejecuciones_comparacion_recientes
  ON contared.ejecuciones_comparacion
    (conciliacion_id,cuenta_id,ultima_ejecucion_en DESC);

CREATE TABLE contared.ejecucion_propuestas (
  ejecucion_id uuid NOT NULL,
  propuesta_id uuid NOT NULL,
  conciliacion_id uuid NOT NULL,
  cuenta_id uuid NOT NULL,
  creada_en timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ejecucion_id,propuesta_id),
  FOREIGN KEY (ejecucion_id,conciliacion_id,cuenta_id)
    REFERENCES contared.ejecuciones_comparacion(id,conciliacion_id,cuenta_id),
  FOREIGN KEY (propuesta_id,conciliacion_id,cuenta_id)
    REFERENCES contared.propuestas_comparacion(id,conciliacion_id,cuenta_id)
);

ALTER TABLE contared.ejecuciones_comparacion ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.propuestas_comparacion ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.ejecucion_propuestas ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.ejecuciones_comparacion FROM PUBLIC,anon,authenticated;
REVOKE ALL ON TABLE contared.propuestas_comparacion FROM PUBLIC,anon,authenticated;
REVOKE ALL ON TABLE contared.ejecucion_propuestas FROM PUBLIC,anon,authenticated;

CREATE TRIGGER auditoria_cambios
  AFTER INSERT OR UPDATE OR DELETE ON contared.ejecuciones_comparacion
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();
CREATE TRIGGER auditoria_cambios
  AFTER INSERT OR UPDATE OR DELETE ON contared.propuestas_comparacion
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();
CREATE TRIGGER auditoria_cambios
  AFTER INSERT OR UPDATE OR DELETE ON contared.ejecucion_propuestas
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();

UPDATE contared.monedas SET nombre='Dólares estadounidenses' WHERE codigo='USD';
UPDATE contared.monedas SET nombre='Quetzales guatemaltecos' WHERE codigo='GTQ';

INSERT INTO contared.migraciones(version)
VALUES ('202610290001_comparacion_movimientos');

COMMIT;
