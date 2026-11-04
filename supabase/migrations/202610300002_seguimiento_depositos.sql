-- Seguimiento operativo de depósitos y sus movimientos relacionados.
-- No modifica movimientos fuente ni genera asientos de ajuste.
BEGIN;

CREATE TABLE contared.casos_deposito (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_origen_id uuid NOT NULL,
  clasificacion text NOT NULL CHECK (clasificacion IN (
    'por_investigar','deposito_transito','deposito_mal_operado'
  )),
  estado text NOT NULL DEFAULT 'abierto' CHECK (estado IN ('abierto','resuelto')),
  origen_correcto text CHECK (origen_correcto IN ('banco','libros')),
  observacion text NOT NULL DEFAULT '',
  responsable_id uuid REFERENCES contared.usuarios(id),
  soporte_ruta text,
  conciliacion_resolucion_id uuid,
  fecha_efectiva date,
  motivo_resolucion text,
  creado_por uuid NOT NULL REFERENCES contared.usuarios(id),
  creado_en timestamptz NOT NULL DEFAULT now(),
  actualizado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_origen_id,cuenta_id)
    REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (conciliacion_resolucion_id,cuenta_id)
    REFERENCES contared.conciliaciones(id,cuenta_id),
  CHECK ((estado='abierto' AND conciliacion_resolucion_id IS NULL
      AND fecha_efectiva IS NULL AND motivo_resolucion IS NULL)
    OR (estado='resuelto' AND conciliacion_resolucion_id IS NOT NULL
      AND fecha_efectiva IS NOT NULL AND motivo_resolucion IS NOT NULL
      AND btrim(motivo_resolucion)<>'')),
  CHECK (origen_correcto IS NULL OR
    (clasificacion='deposito_mal_operado' AND soporte_ruta IS NOT NULL)),
  UNIQUE (id,cuenta_id)
);
CREATE INDEX casos_deposito_abiertos
  ON contared.casos_deposito(cuenta_id,clasificacion,creado_en)
  WHERE estado='abierto';
CREATE INDEX casos_deposito_origen
  ON contared.casos_deposito(conciliacion_origen_id,estado);

CREATE TABLE contared.deposito_movimientos (
  caso_id uuid NOT NULL,
  cuenta_id uuid NOT NULL,
  movimiento_id uuid NOT NULL UNIQUE,
  rol text NOT NULL CHECK (rol IN ('banco','libros','resolucion')),
  PRIMARY KEY (caso_id,movimiento_id),
  FOREIGN KEY (caso_id,cuenta_id)
    REFERENCES contared.casos_deposito(id,cuenta_id),
  FOREIGN KEY (movimiento_id,cuenta_id)
    REFERENCES contared.movimientos(id,cuenta_id)
);
CREATE INDEX deposito_movimientos_por_caso
  ON contared.deposito_movimientos(caso_id,rol);

CREATE FUNCTION contared.validar_movimiento_deposito() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
DECLARE v_lado text;
BEGIN
  SELECT lado INTO v_lado FROM contared.movimientos
    WHERE id=NEW.movimiento_id AND cuenta_id=NEW.cuenta_id;
  IF v_lado IS NULL OR
     (NEW.rol IN ('banco','libros') AND NEW.rol<>v_lado) OR
     (NEW.rol='resolucion' AND v_lado<>'banco') THEN
    RAISE EXCEPTION 'El rol del movimiento no corresponde a su lado';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER validar_movimiento_deposito
  BEFORE INSERT OR UPDATE ON contared.deposito_movimientos
  FOR EACH ROW EXECUTE FUNCTION contared.validar_movimiento_deposito();

CREATE TABLE contared.deposito_eventos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caso_id uuid NOT NULL REFERENCES contared.casos_deposito(id),
  tipo text NOT NULL CHECK (tipo IN ('clasificado','actualizado','resuelto')),
  clasificacion_anterior text,
  clasificacion_nueva text NOT NULL CHECK (clasificacion_nueva IN (
    'por_investigar','deposito_transito','deposito_mal_operado'
  )),
  origen_correcto text CHECK (origen_correcto IN ('banco','libros')),
  estado_anterior text,
  estado_nuevo text NOT NULL CHECK (estado_nuevo IN ('abierto','resuelto')),
  fecha_efectiva date,
  registrado_por uuid NOT NULL REFERENCES contared.usuarios(id),
  registrado_en timestamptz NOT NULL DEFAULT now(),
  observacion text NOT NULL DEFAULT '',
  motivo text,
  soporte_ruta text,
  conciliacion_id uuid REFERENCES contared.conciliaciones(id),
  UNIQUE (id,caso_id)
);
CREATE INDEX deposito_eventos_historial
  ON contared.deposito_eventos(caso_id,registrado_en DESC);

CREATE FUNCTION contared.impedir_mutacion_evento_deposito() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'El historial de depósitos es inmutable';
END $$;
CREATE TRIGGER deposito_evento_inmutable
  BEFORE UPDATE OR DELETE ON contared.deposito_eventos
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_mutacion_evento_deposito();

ALTER TABLE contared.casos_deposito ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.deposito_movimientos ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.deposito_eventos ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.casos_deposito,contared.deposito_movimientos,
  contared.deposito_eventos FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE contared.casos_deposito,
  contared.deposito_movimientos,contared.deposito_eventos TO service_role;
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE
  ON contared.casos_deposito FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE
  ON contared.deposito_movimientos FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();
CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE
  ON contared.deposito_eventos FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();

DO $$
DECLARE table_name text;
BEGIN
  IF to_regprocedure('contared.validar_conciliacion_archivada()') IS NOT NULL THEN
    FOREACH table_name IN ARRAY ARRAY[
      'casos_deposito','deposito_movimientos','deposito_eventos'
    ]
    LOOP
      EXECUTE format(
        'CREATE TRIGGER proteger_dependencia_conciliacion_archivada
         BEFORE INSERT OR UPDATE OR DELETE ON contared.%I
         FOR EACH ROW EXECUTE FUNCTION contared.validar_conciliacion_archivada()',
        table_name
      );
    END LOOP;
  END IF;
END $$;

INSERT INTO contared.migraciones(version)
VALUES ('202610300002_seguimiento_depositos');
COMMIT;
