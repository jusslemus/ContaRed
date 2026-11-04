-- Archive conciliations without deleting or resolving their related records.
BEGIN;

ALTER TABLE contared.conciliaciones
  ADD COLUMN archivada_en timestamptz,
  ADD COLUMN archivada_por uuid REFERENCES contared.usuarios(id),
  ADD COLUMN motivo_archivo text,
  ADD CONSTRAINT conciliaciones_archivo_coherente CHECK (
    (archivada_en IS NULL AND archivada_por IS NULL AND motivo_archivo IS NULL)
    OR (archivada_en IS NOT NULL AND archivada_por IS NOT NULL
      AND motivo_archivo IS NOT NULL AND btrim(motivo_archivo) <> '')
  );
CREATE INDEX conciliaciones_archivadas
  ON contared.conciliaciones(cuenta_id,periodo DESC)
  WHERE archivada_en IS NOT NULL;

CREATE TABLE contared.eventos_archivo_conciliacion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conciliacion_id uuid NOT NULL REFERENCES contared.conciliaciones(id),
  accion text NOT NULL CHECK (accion IN ('archivada','restaurada')),
  actor_id uuid NOT NULL REFERENCES contared.usuarios(id),
  ocurrido_en timestamptz NOT NULL DEFAULT now(),
  motivo text NOT NULL CHECK (btrim(motivo) <> ''),
  UNIQUE (id,conciliacion_id)
);
CREATE INDEX eventos_archivo_conciliacion_historial
  ON contared.eventos_archivo_conciliacion(conciliacion_id,ocurrido_en,id);

CREATE FUNCTION contared.validar_conciliacion_archivada() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
DECLARE
  v_old jsonb;
  v_new jsonb;
  v_payload jsonb;
  v_ids uuid[] := ARRAY[]::uuid[];
  v_value text;
  v_id uuid;
  v_archived_at timestamptz;
BEGIN
  IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
  IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;

  FOR v_payload IN
    SELECT value FROM jsonb_array_elements(jsonb_build_array(v_old,v_new))
  LOOP
    FOREACH v_value IN ARRAY ARRAY[
      v_payload->>'conciliacion_id',
      v_payload->>'conciliacion_origen_id',
      v_payload->>'conciliacion_resolucion_id'
    ]
    LOOP
      IF v_value IS NOT NULL THEN
        v_ids := array_append(v_ids,v_value::uuid);
      END IF;
    END LOOP;

    IF TG_TABLE_NAME='movimientos' AND v_payload->>'carga_id' IS NOT NULL THEN
      v_ids := array_append(v_ids,(
        SELECT c.conciliacion_id FROM contared.cargas c
        WHERE c.id=(v_payload->>'carga_id')::uuid
      ));
    ELSIF TG_TABLE_NAME='cruce_movimientos' AND v_payload->>'cruce_id' IS NOT NULL THEN
      v_ids := array_append(v_ids,(
        SELECT x.conciliacion_id FROM contared.cruces x
        WHERE x.id=(v_payload->>'cruce_id')::uuid
      ));
    ELSIF TG_TABLE_NAME='cheque_eventos' AND v_payload->>'cheque_id' IS NOT NULL THEN
      v_ids := array_append(v_ids,(
        SELECT ch.conciliacion_origen_id FROM contared.cheques ch
        WHERE ch.id=(v_payload->>'cheque_id')::uuid
      ));
      v_ids := array_append(v_ids,(
        SELECT ch.conciliacion_resolucion_id FROM contared.cheques ch
        WHERE ch.id=(v_payload->>'cheque_id')::uuid
      ));
    ELSIF TG_TABLE_NAME='deposito_movimientos' AND v_payload->>'caso_id' IS NOT NULL THEN
      v_ids := array_append(v_ids,(
        SELECT d.conciliacion_origen_id FROM contared.casos_deposito d
        WHERE d.id=(v_payload->>'caso_id')::uuid
      ));
      v_ids := array_append(v_ids,(
        SELECT d.conciliacion_resolucion_id FROM contared.casos_deposito d
        WHERE d.id=(v_payload->>'caso_id')::uuid
      ));
    ELSIF TG_TABLE_NAME='deposito_eventos' AND v_payload->>'caso_id' IS NOT NULL THEN
      v_ids := array_append(v_ids,(
        SELECT d.conciliacion_origen_id FROM contared.casos_deposito d
        WHERE d.id=(v_payload->>'caso_id')::uuid
      ));
      v_ids := array_append(v_ids,(
        SELECT d.conciliacion_resolucion_id FROM contared.casos_deposito d
        WHERE d.id=(v_payload->>'caso_id')::uuid
      ));
    END IF;
  END LOOP;

  FOR v_id IN
    SELECT DISTINCT item FROM unnest(v_ids) AS ids(item)
    WHERE item IS NOT NULL ORDER BY item
  LOOP
    SELECT archivada_en INTO v_archived_at
      FROM contared.conciliaciones WHERE id=v_id FOR UPDATE;
    IF v_archived_at IS NOT NULL THEN
      RAISE EXCEPTION 'No se permiten cambios en conciliaciones archivadas'
        USING ERRCODE='55000',CONSTRAINT='conciliacion_archivada';
    END IF;
  END LOOP;

  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION contared.proteger_conciliacion_archivada() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
BEGIN
  IF TG_OP='DELETE' AND OLD.archivada_en IS NOT NULL THEN
    RAISE EXCEPTION 'No se puede eliminar una conciliación archivada'
      USING ERRCODE='55000',CONSTRAINT='conciliacion_archivada';
  END IF;
  IF TG_OP='UPDATE' AND OLD.archivada_en IS NOT NULL
     AND (to_jsonb(NEW)-'archivada_en'-'archivada_por'-'motivo_archivo')
         IS DISTINCT FROM
         (to_jsonb(OLD)-'archivada_en'-'archivada_por'-'motivo_archivo') THEN
    RAISE EXCEPTION 'No se permiten cambios en conciliaciones archivadas'
      USING ERRCODE='55000',CONSTRAINT='conciliacion_archivada';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER proteger_conciliacion_archivada
  BEFORE UPDATE OR DELETE ON contared.conciliaciones
  FOR EACH ROW EXECUTE FUNCTION contared.proteger_conciliacion_archivada();

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'cargas','movimientos','cruces','cruce_movimientos','pendientes','ajustes',
    'reportes','ejecuciones_comparacion','propuestas_comparacion',
    'ejecucion_propuestas','cheques','cheque_eventos','casos_deposito',
    'deposito_movimientos','deposito_eventos'
  ]
  LOOP
    IF to_regclass('contared.'||table_name) IS NOT NULL THEN
      EXECUTE format(
        'CREATE TRIGGER proteger_dependencia_conciliacion_archivada
         BEFORE INSERT OR UPDATE OR DELETE ON contared.%I
         FOR EACH ROW EXECUTE FUNCTION contared.validar_conciliacion_archivada()',
        table_name
      );
    END IF;
  END LOOP;
END $$;

CREATE FUNCTION contared.impedir_mutacion_evento_archivo() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'El historial de archivo de conciliaciones es inmutable';
END $$;
CREATE TRIGGER evento_archivo_conciliacion_inmutable
  BEFORE UPDATE OR DELETE ON contared.eventos_archivo_conciliacion
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_mutacion_evento_archivo();

ALTER TABLE contared.eventos_archivo_conciliacion ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.eventos_archivo_conciliacion FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON TABLE contared.eventos_archivo_conciliacion TO service_role;
CREATE TRIGGER auditoria_cambios AFTER INSERT
  ON contared.eventos_archivo_conciliacion
  FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria();

INSERT INTO contared.migraciones(version)
VALUES ('202610300003_archivo_conciliaciones');
COMMIT;
