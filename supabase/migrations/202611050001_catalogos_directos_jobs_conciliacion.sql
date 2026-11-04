BEGIN;

-- Sincronización acotada a país/empresa: reutiliza 0040040592 y corrige solo su alias.
UPDATE contared.cuentas_bancarias c
SET alias='BIQ'
WHERE c.pais_codigo='GT'
  AND btrim(c.numero)=btrim('0040040592');

-- Reutiliza la tabla de catálogo creada por 202611040001 cuando exista.
-- GT no se duplica aquí: esta migración solo corrige datos pendientes y agrega SV.
DO $$
BEGIN
  IF to_regclass('contared.catalogo_cuentas_bancarias') IS NOT NULL THEN
    UPDATE contared.catalogo_cuentas_bancarias
    SET moneda_codigo=NULL
    WHERE pais_codigo='GT'
      AND numero IN ('090003566','901020461','3445256003');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS contared.catalogo_cuentas_sv (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pais_codigo text NOT NULL DEFAULT 'SV' CHECK (pais_codigo='SV')
    REFERENCES contared.paises(codigo),
  banco_nombre text NOT NULL CHECK (btrim(banco_nombre) <> ''),
  numero_completo text,
  terminacion text NOT NULL CHECK (terminacion ~ '^[0-9]{4}$'),
  alias text NOT NULL CHECK (btrim(alias) <> ''),
  moneda_codigo text REFERENCES contared.monedas(codigo),
  confirmado boolean NOT NULL DEFAULT false,
  UNIQUE (pais_codigo,banco_nombre,terminacion,alias)
);

INSERT INTO contared.catalogo_cuentas_sv
  (pais_codigo,banco_nombre,terminacion,alias,moneda_codigo,confirmado)
VALUES
  ('SV','Banco Agrícola','9596','AGRICOLA','USD',true),
  ('SV','Banco Cuscatlán','2509','CUSCA 2509','USD',true),
  ('SV','Banco Cuscatlán','4978','CUSCA 4978','USD',true),
  ('SV','BAC','0386','BAC 386','USD',true),
  ('SV','BAC','6715','BAC 6715','USD',true),
  ('SV','BAC','0327','BAC 327','USD',true),
  ('SV','Promerica','0049','PROM 49','USD',true),
  ('SV','Davivienda','8202','DAV 68202','USD',true),
  ('SV','Davivienda','1962','DAV 962','USD',true)
ON CONFLICT (pais_codigo,banco_nombre,terminacion,alias) DO UPDATE
SET moneda_codigo=EXCLUDED.moneda_codigo,
    confirmado=EXCLUDED.confirmado;

CREATE TABLE IF NOT EXISTS contared.trabajos_conciliacion_directa (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  usuario_creador_id uuid NOT NULL REFERENCES contared.usuarios(id),
  conciliacion_id uuid REFERENCES contared.conciliaciones(id),
  estado text NOT NULL DEFAULT 'recibiendo'
    CHECK (estado IN ('recibiendo','analizando','requiere_aclaraciones','preparado','aprobado','fallido')),
  cuenta_sugerida_id uuid,
  periodo_sugerido date,
  progreso jsonb NOT NULL DEFAULT '{}'::jsonb,
  resultado jsonb NOT NULL DEFAULT '{}'::jsonb,
  creado_en timestamptz NOT NULL DEFAULT now(),
  actualizado_en timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contared.archivos_trabajo_conciliacion_directa (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trabajo_id uuid NOT NULL REFERENCES contared.trabajos_conciliacion_directa(id) ON DELETE CASCADE,
  nombre_archivo text NOT NULL CHECK (btrim(nombre_archivo) <> ''),
  extension text NOT NULL CHECK (extension IN ('xlsx','xls')),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  tamano_bytes bigint NOT NULL CHECK (tamano_bytes >= 0),
  object_path text NOT NULL,
  estado text NOT NULL CHECK (estado IN ('recibido','analizando','extraido','requiere_revision','fallido')),
  reconocimiento jsonb NOT NULL DEFAULT '{}'::jsonb,
  errores jsonb NOT NULL DEFAULT '[]'::jsonb,
  creado_en timestamptz NOT NULL DEFAULT now(),
  actualizado_en timestamptz NOT NULL DEFAULT now(),
  UNIQUE (trabajo_id,sha256)
);

CREATE TABLE IF NOT EXISTS contared.etapas_trabajo_conciliacion_directa (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trabajo_id uuid NOT NULL REFERENCES contared.trabajos_conciliacion_directa(id) ON DELETE CASCADE,
  etapa text NOT NULL,
  estado text NOT NULL CHECK (estado IN ('pendiente','en_curso','completo','fallido','requiere_aclaracion')),
  detalle text,
  iniciado_en timestamptz NOT NULL DEFAULT now(),
  terminado_en timestamptz,
  UNIQUE (trabajo_id,etapa)
);

ALTER TABLE contared.catalogo_cuentas_sv ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.trabajos_conciliacion_directa ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.archivos_trabajo_conciliacion_directa ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.etapas_trabajo_conciliacion_directa ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE contared.catalogo_cuentas_sv,
  contared.trabajos_conciliacion_directa,
  contared.archivos_trabajo_conciliacion_directa,
  contared.etapas_trabajo_conciliacion_directa
  FROM PUBLIC,anon,authenticated;
GRANT SELECT ON TABLE contared.catalogo_cuentas_sv TO authenticated;
GRANT SELECT,INSERT,UPDATE ON TABLE contared.trabajos_conciliacion_directa,
  contared.archivos_trabajo_conciliacion_directa,
  contared.etapas_trabajo_conciliacion_directa TO service_role;

INSERT INTO contared.migraciones(version)
VALUES ('202611050001_catalogos_directos_jobs_conciliacion')
ON CONFLICT (version) DO NOTHING;

COMMIT;
