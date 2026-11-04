-- Registra cheques importados sin fabricar movimientos ni conciliaciones.
-- Requiere las tablas operativas de cheques y el esquema de auditoría existente.
BEGIN;

DO $$
BEGIN
  IF to_regclass('contared.cheques') IS NULL
     OR to_regclass('contared.cheque_eventos') IS NULL
     OR to_regclass('contared.cuentas_bancarias') IS NULL
     OR to_regclass('contared.migraciones') IS NULL THEN
    RAISE EXCEPTION
      'Faltan las migraciones anteriores de cheques; aplíquelas antes de 202611010001_cheques_independientes.sql.';
  END IF;
END $$;

ALTER TABLE contared.cheques
  ALTER COLUMN movimiento_emision_id DROP NOT NULL,
  ALTER COLUMN conciliacion_origen_id DROP NOT NULL,
  ADD COLUMN descripcion text,
  ADD COLUMN referencia text,
  ADD COLUMN periodo_origen date,
  ADD COLUMN clave_fuente text;

UPDATE contared.cheques ch
SET descripcion=m.descripcion,
    referencia=m.referencia
FROM contared.movimientos m
WHERE m.id=ch.movimiento_emision_id;

UPDATE contared.cheques ch
SET periodo_origen=r.periodo
FROM contared.conciliaciones r
WHERE r.id=ch.conciliacion_origen_id;

UPDATE contared.cheques
SET periodo_origen=date_trunc('month',fecha_emision)::date
WHERE periodo_origen IS NULL;

ALTER TABLE contared.cheques
  ALTER COLUMN periodo_origen SET NOT NULL,
  ADD CONSTRAINT cheques_descripcion_requerida
    CHECK (
      movimiento_emision_id IS NOT NULL
      OR (descripcion IS NOT NULL AND btrim(descripcion) <> '')
    ),
  ADD CONSTRAINT cheques_independientes_requieren_periodo
    CHECK (
      conciliacion_origen_id IS NOT NULL
      OR (movimiento_emision_id IS NULL AND periodo_origen IS NOT NULL)
    );

CREATE UNIQUE INDEX cheques_clave_fuente_unica
  ON contared.cheques(cuenta_id,clave_fuente)
  WHERE clave_fuente IS NOT NULL;
CREATE INDEX cheques_registro_independiente
  ON contared.cheques(cuenta_id,periodo_origen DESC,fecha_emision DESC)
  WHERE movimiento_emision_id IS NULL;

CREATE TABLE contared.cheque_importaciones (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL REFERENCES contared.cuentas_bancarias(id),
  tipo_documento text NOT NULL
    CHECK (tipo_documento IN ('registro_completo','pendientes_al_corte')),
  fecha_corte date NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  nombre_archivo text NOT NULL CHECK (btrim(nombre_archivo) <> ''),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  object_path text NOT NULL CHECK (btrim(object_path) <> ''),
  perfil jsonb NOT NULL CHECK (jsonb_typeof(perfil)='object'),
  registros_total integer NOT NULL CHECK (registros_total >= 0),
  creado_por uuid NOT NULL REFERENCES contared.usuarios(id),
  creado_en timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cuenta_id,tipo_documento,version),
  UNIQUE (cuenta_id,tipo_documento,sha256)
);
CREATE INDEX cheque_importaciones_historial
  ON contared.cheque_importaciones(cuenta_id,creado_en DESC,version DESC);

CREATE TABLE contared.cheque_importacion_detalles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  importacion_id uuid NOT NULL
    REFERENCES contared.cheque_importaciones(id),
  cheque_id uuid NOT NULL,
  fila_origen integer NOT NULL CHECK (fila_origen > 0),
  numero_cheque text NOT NULL,
  fecha_emision date NOT NULL,
  beneficiario text,
  descripcion text NOT NULL,
  referencia text,
  monto numeric(20,4) NOT NULL CHECK (monto > 0),
  estado text NOT NULL
    CHECK (estado IN ('retenido','en_circulacion')),
  datos_originales jsonb NOT NULL CHECK (jsonb_typeof(datos_originales)='object'),
  creado_en timestamptz NOT NULL DEFAULT now(),
  UNIQUE (importacion_id,fila_origen)
);
CREATE INDEX cheque_importacion_detalles_cheque
  ON contared.cheque_importacion_detalles(cheque_id,importacion_id);
COMMENT ON COLUMN contared.cheque_importacion_detalles.cheque_id IS
  'Identificador operativo al importar; se conserva como referencia histórica aunque se elimine una conciliación vinculada.';

CREATE FUNCTION contared.impedir_mutacion_importacion_cheques() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'El historial de importación de cheques es inmutable';
END $$;
CREATE TRIGGER cheque_importacion_inmutable
  BEFORE UPDATE OR DELETE ON contared.cheque_importaciones
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_mutacion_importacion_cheques();
CREATE TRIGGER cheque_importacion_detalle_inmutable
  BEFORE UPDATE OR DELETE ON contared.cheque_importacion_detalles
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_mutacion_importacion_cheques();

ALTER TABLE contared.cheque_importaciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.cheque_importacion_detalles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.cheque_importaciones,
  contared.cheque_importacion_detalles FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE contared.cheque_importaciones,
  contared.cheque_importacion_detalles TO service_role;
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE
  ON contared.cheque_importaciones
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE
  ON contared.cheque_importacion_detalles
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();

COMMENT ON COLUMN contared.cheques.movimiento_emision_id IS
  'NULL mientras el cheque importado no esté vinculado a una emisión contable en Libros.';
COMMENT ON COLUMN contared.cheques.conciliacion_origen_id IS
  'NULL para registros independientes cargados desde un documento de cheques.';
COMMENT ON COLUMN contared.cheques.periodo_origen IS
  'Período del documento que originó el registro; independiente de una conciliación.';

INSERT INTO contared.migraciones(version)
VALUES ('202611010001_cheques_independientes');
COMMIT;
