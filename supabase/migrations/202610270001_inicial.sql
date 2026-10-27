-- ContaRed / migracion inicial / PostgreSQL en Supabase.
-- Ejecutar completa una sola vez en SQL Editor como propietario (postgres).
-- No requiere contrasenas, no elimina tablas existentes, no importa datos reales.
-- Diseno: servidor privado -> esquema contared. Sin acceso directo del navegador.
-- No agregar contared a los esquemas expuestos de la Data API.
BEGIN;

CREATE SCHEMA contared;
REVOKE ALL ON SCHEMA contared FROM PUBLIC, anon, authenticated;

CREATE TABLE contared.migraciones (
  version text PRIMARY KEY,
  aplicada_en timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contared.paises (
  codigo text PRIMARY KEY CHECK (codigo IN ('SV','GT')),
  nombre text NOT NULL
);
CREATE TABLE contared.monedas (
  codigo text PRIMARY KEY CHECK (codigo ~ '^[A-Z]{3}$'),
  nombre text NOT NULL,
  decimales smallint NOT NULL DEFAULT 2 CHECK (decimales BETWEEN 0 AND 4)
);
CREATE TABLE contared.empresas (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre text NOT NULL CHECK (btrim(nombre) <> ''),
  activa boolean NOT NULL DEFAULT true,
  creada_en timestamptz NOT NULL DEFAULT now()
);

-- Vinculo opcional con Supabase Auth. Sin contrasenas en tablas propias.
-- No se crean usuarios ni se asignan permisos automaticamente al registrarse.
CREATE TABLE contared.usuarios (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id uuid UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL,
  nombre text NOT NULL CHECK (btrim(nombre) <> ''),
  activo boolean NOT NULL DEFAULT true,
  creado_en timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE contared.accesos (
  usuario_id uuid NOT NULL REFERENCES contared.usuarios(id),
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  rol text NOT NULL CHECK (rol IN ('administrador','auxiliar','revisor','consulta')),
  PRIMARY KEY (usuario_id, empresa_id, pais_codigo)
);
CREATE TABLE contared.bancos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  nombre text NOT NULL CHECK (btrim(nombre) <> ''),
  activo boolean NOT NULL DEFAULT true,
  UNIQUE (pais_codigo,nombre),
  UNIQUE (id,pais_codigo)
);
CREATE TABLE contared.cuentas_bancarias (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  banco_id uuid NOT NULL,
  moneda_codigo text NOT NULL REFERENCES contared.monedas(codigo),
  numero text NOT NULL CHECK (btrim(numero) <> ''),
  alias text NOT NULL CHECK (btrim(alias) <> ''),
  activa boolean NOT NULL DEFAULT true,
  creada_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (banco_id,pais_codigo) REFERENCES contared.bancos(id,pais_codigo),
  UNIQUE (banco_id,numero),
  UNIQUE (id,empresa_id,pais_codigo)
);

-- Cada version conserva el mapeo de columnas, fechas, signos y codigos TT.
-- Las reglas son configuracion; su ejecucion corresponde al motor de la app.
CREATE TABLE contared.perfiles_importacion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  banco_id uuid,
  origen text NOT NULL CHECK (origen IN ('banco','libros','historico')),
  nombre text NOT NULL,
  version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  configuracion jsonb NOT NULL CHECK (jsonb_typeof(configuracion) = 'object'),
  creado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (banco_id,pais_codigo) REFERENCES contared.bancos(id,pais_codigo),
  UNIQUE (empresa_id,pais_codigo,nombre,version),
  UNIQUE (id,empresa_id,pais_codigo)
);

CREATE TABLE contared.conciliaciones (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL REFERENCES contared.cuentas_bancarias(id),
  periodo date NOT NULL CHECK (extract(day FROM periodo) = 1),
  fecha_corte date NOT NULL,
  estado text NOT NULL DEFAULT 'borrador'
    CHECK (estado IN ('borrador','en_revision')),
  saldo_inicial_banco numeric(20,4),
  saldo_inicial_libros numeric(20,4),
  saldo_final_banco numeric(20,4),
  saldo_final_libros numeric(20,4),
  reglas_version text,
  reglas_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(reglas_snapshot) = 'object'),
  notas text,
  creada_por uuid REFERENCES contared.usuarios(id),
  creada_en timestamptz NOT NULL DEFAULT now(),
  CHECK (fecha_corte >= periodo AND fecha_corte < (periodo + interval '1 month')),
  UNIQUE (cuenta_id,periodo),
  UNIQUE (id,cuenta_id)
);
COMMENT ON TABLE contared.conciliaciones IS
  'Version inicial: no habilita cierre definitivo hasta validar reglas y resumen con contabilidad.';

CREATE TABLE contared.cargas (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  empresa_id uuid NOT NULL,
  pais_codigo text NOT NULL,
  conciliacion_id uuid NOT NULL,
  origen text NOT NULL CHECK (origen IN ('banco','libros','historico')),
  metodo text NOT NULL DEFAULT 'excel' CHECK (metodo IN ('excel','csv','manual')),
  perfil_id uuid,
  nombre_archivo text,
  storage_ruta text,
  sha256 text CHECK (sha256 ~ '^[a-f0-9]{64}$'),
  estado text NOT NULL DEFAULT 'pendiente'
    CHECK (estado IN ('pendiente','validada','rechazada')),
  errores jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(errores) = 'array'),
  creada_por uuid REFERENCES contared.usuarios(id),
  creada_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (cuenta_id,empresa_id,pais_codigo)
    REFERENCES contared.cuentas_bancarias(id,empresa_id,pais_codigo),
  FOREIGN KEY (conciliacion_id,cuenta_id) REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (perfil_id,empresa_id,pais_codigo)
    REFERENCES contared.perfiles_importacion(id,empresa_id,pais_codigo),
  CHECK (metodo = 'manual' OR
    (nombre_archivo IS NOT NULL AND storage_ruta IS NOT NULL AND sha256 IS NOT NULL)),
  UNIQUE (id,cuenta_id)
);
-- Evita importar el mismo archivo dos veces para la misma cuenta y origen.
CREATE UNIQUE INDEX cargas_archivo_unico ON contared.cargas(cuenta_id,origen,sha256)
  WHERE sha256 IS NOT NULL AND estado <> 'rechazada';

CREATE TABLE contared.movimientos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  carga_id uuid NOT NULL,
  lado text NOT NULL CHECK (lado IN ('banco','libros')),
  fecha date NOT NULL,
  fecha_valor date,
  referencia text,
  numero_transaccion text,
  numero_cheque text,
  codigo_tt text,
  descripcion text NOT NULL DEFAULT '',
  monto numeric(20,4) NOT NULL CHECK (monto <> 0),
  saldo_origen numeric(20,4),
  hoja_origen text NOT NULL DEFAULT 'MANUAL',
  fila_origen integer NOT NULL CHECK (fila_origen > 0),
  datos_originales jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(datos_originales) = 'object'),
  creado_por uuid REFERENCES contared.usuarios(id),
  creado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (carga_id,cuenta_id) REFERENCES contared.cargas(id,cuenta_id),
  UNIQUE (carga_id,hoja_origen,fila_origen),
  UNIQUE (id,cuenta_id)
);
COMMENT ON COLUMN contared.movimientos.monto IS
  'Monto normalizado: ingreso positivo, salida negativa. Conservar debe/haber originales en datos_originales.';
CREATE INDEX movimientos_busqueda ON contared.movimientos(cuenta_id,lado,fecha,monto);
CREATE INDEX movimientos_referencia ON contared.movimientos(cuenta_id,referencia);
CREATE INDEX movimientos_cheque ON contared.movimientos(cuenta_id,numero_cheque);

-- Un grupo admite 1:1, 1:N, N:1, anulaciones y reversos.
CREATE TABLE contared.cruces (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_id uuid NOT NULL,
  tipo text NOT NULL CHECK (tipo IN ('coincidencia','anulacion_libros','reverso_banco')),
  estado text NOT NULL DEFAULT 'propuesto' CHECK (estado IN ('propuesto','confirmado')),
  criterio text NOT NULL CHECK (btrim(criterio) <> ''),
  regla_codigo text,
  observacion text,
  creado_por uuid REFERENCES contared.usuarios(id),
  confirmado_por uuid REFERENCES contared.usuarios(id),
  confirmado_en timestamptz,
  creado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_id,cuenta_id) REFERENCES contared.conciliaciones(id,cuenta_id),
  CHECK ((estado = 'confirmado') = (confirmado_por IS NOT NULL AND confirmado_en IS NOT NULL)),
  UNIQUE (id,cuenta_id)
);
CREATE TABLE contared.cruce_movimientos (
  cruce_id uuid NOT NULL,
  cuenta_id uuid NOT NULL,
  movimiento_id uuid NOT NULL UNIQUE,
  PRIMARY KEY (cruce_id,movimiento_id),
  FOREIGN KEY (cruce_id,cuenta_id) REFERENCES contared.cruces(id,cuenta_id),
  FOREIGN KEY (movimiento_id,cuenta_id) REFERENCES contared.movimientos(id,cuenta_id)
);

-- Nunca borrar pendientes resueltos: cambiar estado y documentar resolucion.
CREATE TABLE contared.pendientes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_origen_id uuid NOT NULL,
  movimiento_id uuid NOT NULL UNIQUE,
  tipo text NOT NULL CHECK (tipo IN ('cheque_circulacion','deposito_transito',
    'monto_mal_operado','sin_registro_libros','sin_registro_banco','por_investigar')),
  estado text NOT NULL DEFAULT 'abierto' CHECK (estado IN ('abierto','resuelto','descartado')),
  observacion text NOT NULL DEFAULT '',
  conciliacion_resolucion_id uuid,
  movimiento_resolucion_id uuid,
  motivo_resolucion text,
  resuelto_por uuid REFERENCES contared.usuarios(id),
  resuelto_en timestamptz,
  creado_por uuid REFERENCES contared.usuarios(id),
  creado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_origen_id,cuenta_id) REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (conciliacion_resolucion_id,cuenta_id) REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (movimiento_id,cuenta_id) REFERENCES contared.movimientos(id,cuenta_id),
  FOREIGN KEY (movimiento_resolucion_id,cuenta_id) REFERENCES contared.movimientos(id,cuenta_id),
  CHECK (movimiento_resolucion_id IS NULL OR movimiento_resolucion_id <> movimiento_id),
  CHECK ((estado = 'abierto' AND resuelto_por IS NULL AND resuelto_en IS NULL
      AND motivo_resolucion IS NULL AND conciliacion_resolucion_id IS NULL
      AND movimiento_resolucion_id IS NULL)
    OR (estado <> 'abierto' AND resuelto_por IS NOT NULL AND resuelto_en IS NOT NULL
      AND conciliacion_resolucion_id IS NOT NULL AND motivo_resolucion IS NOT NULL
      AND btrim(motivo_resolucion) <> '')),
  UNIQUE (id,cuenta_id)
);
CREATE INDEX pendientes_abiertos ON contared.pendientes(cuenta_id,tipo) WHERE estado = 'abierto';

CREATE TABLE contared.ajustes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_id uuid NOT NULL,
  pendiente_id uuid,
  lado text NOT NULL CHECK (lado IN ('banco','libros')),
  concepto text NOT NULL CHECK (btrim(concepto) <> ''),
  monto numeric(20,4) NOT NULL CHECK (monto <> 0),
  soporte_ruta text,
  creado_por uuid REFERENCES contared.usuarios(id),
  aprobado_por uuid REFERENCES contared.usuarios(id),
  aprobado_en timestamptz,
  creado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_id,cuenta_id) REFERENCES contared.conciliaciones(id,cuenta_id),
  FOREIGN KEY (pendiente_id,cuenta_id) REFERENCES contared.pendientes(id,cuenta_id),
  CHECK ((aprobado_por IS NULL) = (aprobado_en IS NULL))
);
COMMENT ON TABLE contared.ajustes IS
  'Ajustes explicitos al resumen; no modifican movimientos fuente ni publican asientos contables.';
CREATE TABLE contared.reportes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cuenta_id uuid NOT NULL,
  conciliacion_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  formato text NOT NULL CHECK (formato IN ('xlsx','pdf')),
  storage_ruta text NOT NULL,
  snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
  generado_por uuid REFERENCES contared.usuarios(id),
  generado_en timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conciliacion_id,cuenta_id) REFERENCES contared.conciliaciones(id,cuenta_id),
  UNIQUE (conciliacion_id,version,formato)
);

-- Auditoria automatica. actor_id se establece con SET LOCAL dentro de cada
-- transaccion del servidor, DESPUES de verificar identidad y permisos.
CREATE TABLE contared.auditoria (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tabla text NOT NULL,
  registro_id text,
  operacion text NOT NULL CHECK (operacion IN ('INSERT','UPDATE','DELETE')),
  actor_id uuid REFERENCES contared.usuarios(id),
  usuario_bd text NOT NULL,
  antes jsonb,
  despues jsonb,
  ocurrido_en timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auditoria_registro ON contared.auditoria(tabla,registro_id,ocurrido_en);
CREATE FUNCTION contared.registrar_auditoria() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, contared AS $$
DECLARE v_old jsonb; v_new jsonb; v_actor uuid;
BEGIN
  v_actor := nullif(current_setting('contared.actor_id',true),'')::uuid;
  IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
  IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;
  INSERT INTO contared.auditoria(tabla,registro_id,operacion,actor_id,usuario_bd,antes,despues)
  VALUES (TG_TABLE_NAME,coalesce(v_new->>'id',v_old->>'id'),TG_OP,v_actor,session_user,v_old,v_new);
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION contared.registrar_auditoria() FROM PUBLIC,anon,authenticated;

-- Verificacion diferida: permite insertar un grupo y sus miembros en una misma
-- transaccion, pero no confirmar grupos vacios, desbalanceados o de lados incorrectos.
CREATE FUNCTION contared.validar_cruce() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
DECLARE v_id uuid; v_grupo contared.cruces; v_banco integer; v_libros integer;
  v_total_banco numeric; v_total_libros numeric;
BEGIN
  IF TG_TABLE_NAME = 'cruces' THEN v_id := coalesce(NEW.id,OLD.id);
  ELSE v_id := coalesce(NEW.cruce_id,OLD.cruce_id); END IF;
  SELECT * INTO v_grupo FROM contared.cruces WHERE id=v_id;
  IF NOT FOUND OR v_grupo.estado <> 'confirmado' THEN RETURN NULL; END IF;
  SELECT count(*) FILTER (WHERE m.lado='banco'),count(*) FILTER (WHERE m.lado='libros'),
    coalesce(sum(m.monto) FILTER (WHERE m.lado='banco'),0),
    coalesce(sum(m.monto) FILTER (WHERE m.lado='libros'),0)
  INTO v_banco,v_libros,v_total_banco,v_total_libros
  FROM contared.cruce_movimientos cm JOIN contared.movimientos m ON m.id=cm.movimiento_id
  WHERE cm.cruce_id=v_id;
  IF (v_grupo.tipo='coincidencia' AND (v_banco=0 OR v_libros=0 OR v_total_banco<>v_total_libros))
    OR (v_grupo.tipo='anulacion_libros' AND (v_banco<>0 OR v_libros<2 OR v_total_libros<>0))
    OR (v_grupo.tipo='reverso_banco' AND (v_libros<>0 OR v_banco<2 OR v_total_banco<>0)) THEN
    RAISE EXCEPTION 'El cruce % no cumple los importes o lados requeridos',v_id;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER validar_cruce_grupo AFTER INSERT OR UPDATE ON contared.cruces
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION contared.validar_cruce();
CREATE CONSTRAINT TRIGGER validar_cruce_miembros AFTER INSERT OR UPDATE OR DELETE ON contared.cruce_movimientos
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION contared.validar_cruce();

CREATE FUNCTION contared.impedir_reasignacion_miembro() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'Para reasignar un movimiento, quitar y agregar su relacion en la misma transaccion';
END $$;
CREATE TRIGGER miembro_sin_update BEFORE UPDATE ON contared.cruce_movimientos
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_reasignacion_miembro();

CREATE FUNCTION contared.validar_origen_movimiento() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog,contared AS $$
DECLARE v_origen text;
BEGIN
  SELECT origen INTO v_origen FROM contared.cargas WHERE id=NEW.carga_id;
  IF (v_origen='banco' AND NEW.lado<>'banco')
     OR (v_origen IN ('libros','historico') AND NEW.lado<>'libros') THEN
    RAISE EXCEPTION 'El lado del movimiento no corresponde al origen de la carga';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER movimiento_origen BEFORE INSERT ON contared.movimientos
  FOR EACH ROW EXECUTE FUNCTION contared.validar_origen_movimiento();

-- Los datos importados se conservan; corregir con una carga nueva documentada.
CREATE FUNCTION contared.impedir_mutacion_movimiento() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'Los movimientos fuente son inmutables; registrar correccion con trazabilidad';
END $$;
CREATE TRIGGER movimiento_inmutable BEFORE UPDATE OR DELETE ON contared.movimientos
  FOR EACH ROW EXECUTE FUNCTION contared.impedir_mutacion_movimiento();

INSERT INTO contared.paises VALUES ('SV','El Salvador'),('GT','Guatemala');
INSERT INTO contared.monedas(codigo,nombre) VALUES ('USD','Dolar estadounidense'),('GTQ','Quetzal');
-- La razon social se registra cuando se confirme; no se inventan cuentas ni saldos.

-- Denegacion por defecto, incluso si el esquema se expone accidentalmente.
-- service_role es SOLO del servidor y omite RLS. Sus permisos son explicitos.
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='contared' LOOP
    EXECUTE format('ALTER TABLE contared.%I ENABLE ROW LEVEL SECURITY',t.tablename);
    EXECUTE format('REVOKE ALL ON TABLE contared.%I FROM PUBLIC,anon,authenticated',t.tablename);
    IF t.tablename NOT IN ('auditoria','migraciones') THEN
      EXECUTE format('CREATE TRIGGER auditoria_cambios AFTER INSERT OR UPDATE OR DELETE ON contared.%I FOR EACH ROW EXECUTE FUNCTION contared.registrar_auditoria()',t.tablename);
    END IF;
  END LOOP;
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA contared FROM PUBLIC,anon,authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA contared FROM PUBLIC,anon,authenticated;
GRANT USAGE ON SCHEMA contared TO service_role;
REVOKE ALL ON ALL TABLES IN SCHEMA contared FROM service_role;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA contared TO service_role;
REVOKE INSERT,UPDATE,DELETE ON contared.auditoria,contared.migraciones FROM service_role;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA contared TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA contared TO service_role;
REVOKE EXECUTE ON FUNCTION contared.registrar_auditoria() FROM service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA contared REVOKE ALL ON TABLES FROM PUBLIC,anon,authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA contared REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC,anon,authenticated;

-- Bucket privado para originales y reportes. Sin politicas de acceso de clientes.
INSERT INTO storage.buckets(id,name,public)
VALUES ('contared-documentos','contared-documentos',false);

INSERT INTO contared.migraciones(version) VALUES ('202610270001_inicial');
COMMIT;
