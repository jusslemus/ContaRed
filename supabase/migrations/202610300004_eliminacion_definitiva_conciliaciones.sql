-- Permanent, reconciliation-scoped deletion. Apply only after migrations
-- 202610270001 through 202610300003; this is never run by Django or the browser.
BEGIN;

DO $$
DECLARE required_table text;
BEGIN
  FOREACH required_table IN ARRAY ARRAY[
    'conciliaciones','cargas','movimientos','cruces','cruce_movimientos',
    'pendientes','ajustes','reportes','auditoria','ejecuciones_comparacion',
    'propuestas_comparacion','ejecucion_propuestas','cheques','cheque_eventos',
    'casos_deposito','deposito_movimientos','deposito_eventos',
    'eventos_archivo_conciliacion'
  ]
  LOOP
    IF to_regclass('contared.'||required_table) IS NULL THEN
      RAISE EXCEPTION
        'Falta contared.%; aplique primero las migraciones anteriores de ContaRed.',
        required_table;
    END IF;
  END LOOP;
END $$;

CREATE TABLE contared.eliminaciones_conciliacion (
  id uuid PRIMARY KEY,
  conciliacion_id uuid NOT NULL UNIQUE,
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  actor_id uuid NOT NULL REFERENCES contared.usuarios(id),
  creada_en timestamptz NOT NULL DEFAULT now(),
  motivo text NOT NULL CHECK (btrim(motivo) <> ''),
  cantidades jsonb NOT NULL CHECK (jsonb_typeof(cantidades)='object'),
  archivos_total integer NOT NULL CHECK (archivos_total >= 0),
  limpieza_completa boolean NOT NULL DEFAULT false
);
CREATE INDEX eliminaciones_conciliacion_ambito
  ON contared.eliminaciones_conciliacion(empresa_id,pais_codigo,creada_en DESC);

CREATE TABLE contared.eliminacion_conciliacion_storage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  eliminacion_id uuid NOT NULL
    REFERENCES contared.eliminaciones_conciliacion(id) ON DELETE CASCADE,
  object_path text NOT NULL CHECK (btrim(object_path) <> ''),
  intentos integer NOT NULL DEFAULT 0 CHECK (intentos >= 0),
  eliminado_en timestamptz,
  ultimo_error text,
  UNIQUE (eliminacion_id,object_path)
);
CREATE INDEX eliminacion_conciliacion_storage_pendiente
  ON contared.eliminacion_conciliacion_storage(eliminacion_id,id)
  WHERE eliminado_en IS NULL;

ALTER TABLE contared.eliminaciones_conciliacion ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.eliminacion_conciliacion_storage ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.eliminaciones_conciliacion,
  contared.eliminacion_conciliacion_storage FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE contared.eliminaciones_conciliacion,
  contared.eliminacion_conciliacion_storage TO service_role;

-- A transaction-local allowlist is built by the authenticated backend only
-- after locking the selected reconciliation and checking every cross-period
-- reference. It authorizes exact rows, never a table-wide maintenance mode.
CREATE FUNCTION contared.clave_purga(p_tabla text,p_fila jsonb)
RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT CASE p_tabla
    WHEN 'cruce_movimientos' THEN concat_ws(':',p_fila->>'cruce_id',p_fila->>'movimiento_id')
    WHEN 'ejecucion_propuestas' THEN concat_ws(':',p_fila->>'ejecucion_id',p_fila->>'propuesta_id')
    WHEN 'deposito_movimientos' THEN concat_ws(':',p_fila->>'caso_id',p_fila->>'movimiento_id')
    ELSE p_fila->>'id'
  END
$$;

CREATE FUNCTION contared.purga_registro_autorizado(p_tabla text,p_registro_id text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,contared AS $$
DECLARE v_target text;
  v_allowed boolean;
BEGIN
  v_target := nullif(current_setting('contared.purga_conciliacion_id',true),'');
  IF v_target IS NULL OR to_regclass('pg_temp.contared_purga_autorizada') IS NULL THEN
    RETURN false;
  END IF;
  EXECUTE
    'SELECT EXISTS (SELECT 1 FROM pg_temp.contared_purga_autorizada
       WHERE tabla=$1 AND registro_id=$2 AND conciliacion_id=$3::uuid)'
    INTO v_allowed USING p_tabla,p_registro_id,v_target;
  RETURN coalesce(v_allowed,false);
END $$;
REVOKE ALL ON FUNCTION contared.clave_purga(text,jsonb)
  FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION contared.purga_registro_autorizado(text,text)
  FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION contared.clave_purga(text,jsonb),
  contared.purga_registro_autorizado(text,text) TO service_role;

CREATE FUNCTION contared.eliminar_auditoria_purga(p_conciliacion_id uuid)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,contared AS $$
DECLARE v_deleted bigint;
BEGIN
  IF nullif(current_setting('contared.purga_conciliacion_id',true),'')
       IS DISTINCT FROM p_conciliacion_id::text
     OR NOT contared.purga_registro_autorizado(
       'conciliaciones',p_conciliacion_id::text) THEN
    RAISE EXCEPTION 'No existe un alcance de auditoría autorizado para la purga'
      USING ERRCODE='55000',CONSTRAINT='purga_conciliacion_alcance';
  END IF;

  DELETE FROM contared.auditoria a
  USING pg_temp.contared_purga_autorizada p
  WHERE a.tabla=p.tabla AND p.conciliacion_id=p_conciliacion_id
    AND (
      a.registro_id=p.registro_id OR
      (a.registro_id IS NULL AND contared.clave_purga(
        a.tabla,coalesce(a.antes,a.despues))=p.registro_id)
    );
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  RETURN v_deleted;
END $$;
REVOKE ALL ON FUNCTION contared.eliminar_auditoria_purga(uuid)
  FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION contared.eliminar_auditoria_purga(uuid) TO service_role;

CREATE OR REPLACE FUNCTION contared.registrar_auditoria() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,contared AS $$
DECLARE v_old jsonb; v_new jsonb; v_actor uuid; v_key text;
BEGIN
  IF TG_OP='DELETE'
     AND nullif(current_setting('contared.purga_conciliacion_id',true),'') IS NOT NULL THEN
    v_key := contared.clave_purga(TG_TABLE_NAME,to_jsonb(OLD));
    IF contared.purga_registro_autorizado(TG_TABLE_NAME,v_key) THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'Fila fuera del alcance autorizado para eliminación'
      USING ERRCODE='55000',CONSTRAINT='purga_conciliacion_alcance';
  END IF;

  v_actor := nullif(current_setting('contared.actor_id',true),'')::uuid;
  IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
  IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;
  INSERT INTO contared.auditoria(tabla,registro_id,operacion,actor_id,usuario_bd,antes,despues)
  VALUES (TG_TABLE_NAME,coalesce(v_new->>'id',v_old->>'id'),TG_OP,v_actor,session_user,v_old,v_new);
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION contared.registrar_auditoria()
  FROM PUBLIC,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION contared.validar_conciliacion_archivada() RETURNS trigger
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

  IF TG_OP='DELETE'
     AND contared.purga_registro_autorizado(
       TG_TABLE_NAME,contared.clave_purga(TG_TABLE_NAME,v_old)) THEN
    RETURN OLD;
  END IF;

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

CREATE OR REPLACE FUNCTION contared.proteger_conciliacion_archivada() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
BEGIN
  IF TG_OP='DELETE' AND contared.purga_registro_autorizado(
       TG_TABLE_NAME,contared.clave_purga(TG_TABLE_NAME,to_jsonb(OLD))) THEN
    RETURN OLD;
  END IF;
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

CREATE OR REPLACE FUNCTION contared.impedir_mutacion_movimiento() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
BEGIN
  IF TG_OP='DELETE' AND contared.purga_registro_autorizado(
       TG_TABLE_NAME,contared.clave_purga(TG_TABLE_NAME,to_jsonb(OLD))) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'Los movimientos fuente son inmutables; registrar correccion con trazabilidad';
END $$;

CREATE OR REPLACE FUNCTION contared.impedir_mutacion_evento_cheque() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
BEGIN
  IF TG_OP='DELETE' AND contared.purga_registro_autorizado(
       TG_TABLE_NAME,contared.clave_purga(TG_TABLE_NAME,to_jsonb(OLD))) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'El historial de cheques es inmutable';
END $$;

CREATE OR REPLACE FUNCTION contared.impedir_mutacion_evento_deposito() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
BEGIN
  IF TG_OP='DELETE' AND contared.purga_registro_autorizado(
       TG_TABLE_NAME,contared.clave_purga(TG_TABLE_NAME,to_jsonb(OLD))) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'El historial de depósitos es inmutable';
END $$;

CREATE OR REPLACE FUNCTION contared.impedir_mutacion_evento_archivo() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
BEGIN
  IF TG_OP='DELETE' AND contared.purga_registro_autorizado(
       TG_TABLE_NAME,contared.clave_purga(TG_TABLE_NAME,to_jsonb(OLD))) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'El historial de archivo de conciliaciones es inmutable';
END $$;

INSERT INTO contared.migraciones(version)
VALUES ('202610300004_eliminacion_definitiva_conciliaciones');
COMMIT;
