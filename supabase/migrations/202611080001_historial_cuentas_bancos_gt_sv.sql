BEGIN;

-- Dependencia base del catálogo GT.
CREATE TABLE IF NOT EXISTS contared.catalogo_cuentas_bancarias (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  numero text NOT NULL CHECK (btrim(numero) <> ''),
  banco_nombre text NOT NULL CHECK (btrim(banco_nombre) <> ''),
  alias text NOT NULL CHECK (btrim(alias) <> ''),
  moneda_codigo text REFERENCES contared.monedas(codigo),
  UNIQUE (pais_codigo,numero)
);

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

-- Configuración catalogada definitiva GT/SV.
INSERT INTO contared.catalogo_cuentas_bancarias
  (pais_codigo, numero, banco_nombre, alias, moneda_codigo)
VALUES
  ('GT','0040040592','Banco Industrial','BIQ','GTQ'),
  ('GT','0040040584','Banco Industrial','BI$3','GTQ'),
  ('GT','090003566','Banco GYT Continental','GYT','GTQ'),
  ('GT','901020461','Banco de América Central','BAC','GTQ'),
  ('GT','3445256003','Banco de Desarrollo Rural','BANRURAL','GTQ')
ON CONFLICT (pais_codigo, numero) DO UPDATE
SET banco_nombre=EXCLUDED.banco_nombre,
    alias=EXCLUDED.alias,
    moneda_codigo=EXCLUDED.moneda_codigo;

INSERT INTO contared.catalogo_cuentas_sv
  (pais_codigo,banco_nombre,numero_completo,terminacion,alias,moneda_codigo,confirmado)
VALUES
  ('SV','Banco Agrícola',NULL,'9596','AGRICOLA','USD',true),
  ('SV','Banco Cuscatlán',NULL,'2509','CUSCA 2509','USD',true),
  ('SV','Banco Cuscatlán',NULL,'4978','CUSCA 4978','USD',true),
  ('SV','BAC',NULL,'0386','BAC 386','USD',true),
  ('SV','BAC',NULL,'6715','BAC 6715','USD',true),
  ('SV','BAC',NULL,'0327','BAC 327','USD',true),
  ('SV','Promerica',NULL,'0049','PROM 49','USD',true),
  ('SV','Davivienda',NULL,'8202','DAV 68202','USD',true),
  ('SV','Davivienda',NULL,'1962','DAV 962','USD',true)
ON CONFLICT (pais_codigo,banco_nombre,terminacion,alias) DO UPDATE
SET moneda_codigo='USD',
    confirmado=true;

CREATE TABLE IF NOT EXISTS contared.trabajos_conciliacion_directa (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  usuario_creador_id uuid NOT NULL REFERENCES contared.usuarios(id),
  conciliacion_id uuid REFERENCES contared.conciliaciones(id),
  estado text NOT NULL DEFAULT 'recibiendo',
  cuenta_sugerida_id uuid,
  periodo_sugerido date,
  progreso jsonb NOT NULL DEFAULT '{}'::jsonb,
  resultado jsonb NOT NULL DEFAULT '{}'::jsonb,
  creado_en timestamptz NOT NULL DEFAULT now(),
  actualizado_en timestamptz NOT NULL DEFAULT now()
);

-- Campos separados para terminaciones SV y vínculo persistente de trabajos.
ALTER TABLE contared.cuentas_bancarias
  ADD COLUMN IF NOT EXISTS numero_terminacion text,
  ADD COLUMN IF NOT EXISTS numero_completo_verificado boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS incorporacion_provisional boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS alias_autorizado text;

ALTER TABLE contared.trabajos_conciliacion_directa
  ADD COLUMN IF NOT EXISTS banco_id uuid,
  ADD COLUMN IF NOT EXISTS cuenta_id uuid,
  ADD COLUMN IF NOT EXISTS moneda_codigo text,
  ADD COLUMN IF NOT EXISTS periodo_sugerido date,
  ADD COLUMN IF NOT EXISTS estado_historial text,
  ADD COLUMN IF NOT EXISTS responsable_nombre text,
  ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS diferenciado_residual text,
  ADD COLUMN IF NOT EXISTS vinculacion_estado text NOT NULL DEFAULT 'pendiente';

ALTER TABLE contared.trabajos_conciliacion_directa
  DROP CONSTRAINT IF EXISTS trabajos_conciliacion_directa_cuenta_id_fkey;
ALTER TABLE contared.trabajos_conciliacion_directa
  ADD CONSTRAINT trabajos_conciliacion_directa_cuenta_id_fkey
  FOREIGN KEY (cuenta_id) REFERENCES contared.cuentas_bancarias(id);

ALTER TABLE contared.trabajos_conciliacion_directa
  DROP CONSTRAINT IF EXISTS trabajos_conciliacion_directa_estado_check;
ALTER TABLE contared.trabajos_conciliacion_directa
  ADD CONSTRAINT trabajos_conciliacion_directa_estado_check
  CHECK (estado IN (
    'recibiendo','analizando','requiere_aclaraciones','preparado','aprobado',
    'fallido','finalizado','finalizado_con_pendientes'
  ));

CREATE INDEX IF NOT EXISTS idx_trabajos_directa_cuenta
  ON contared.trabajos_conciliacion_directa(cuenta_id,periodo_sugerido DESC);
CREATE INDEX IF NOT EXISTS idx_trabajos_directa_banco
  ON contared.trabajos_conciliacion_directa(banco_id,empresa_id,pais_codigo);

-- Incorporación acotada a Red Intelfon. GT evita crear una cuenta si el número ya existe.
DO $$
DECLARE
  empresa record;
  banco record;
  cuenta record;
  gt_numero text;
  gt_banco text;
  gt_alias text;
BEGIN
  FOR empresa IN SELECT id FROM contared.empresas WHERE nombre='Red Intelfon'
  LOOP
    FOR gt_numero, gt_banco, gt_alias IN
      SELECT v.numero,v.banco,v.alias FROM (VALUES
        ('0040040592','Banco Industrial','BIQ'),
        ('0040040584','Banco Industrial','BI$3'),
        ('090003566','Banco GYT Continental','GYT'),
        ('901020461','Banco de América Central','BAC'),
        ('3445256003','Banco de Desarrollo Rural','BANRURAL')
      ) AS v(numero,banco,alias)
    LOOP
      SELECT b.id INTO banco FROM contared.bancos b
      WHERE b.pais_codigo='GT' AND lower(b.nombre)=lower(gt_banco);
      IF banco.id IS NULL THEN
        SELECT b.id INTO banco FROM contared.bancos b
        WHERE b.pais_codigo='GT'
          AND gt_banco='Banco Industrial' AND lower(b.nombre) LIKE 'banco industrial%';
      END IF;
      IF banco.id IS NULL THEN
        INSERT INTO contared.bancos(pais_codigo,nombre,activo)
        VALUES('GT',gt_banco,true)
        RETURNING id INTO banco;
      END IF;
      SELECT c.id INTO cuenta FROM contared.cuentas_bancarias c
      WHERE c.empresa_id=empresa.id AND c.pais_codigo='GT'
        AND btrim(c.numero)=btrim(gt_numero);
      IF cuenta.id IS NULL THEN
        INSERT INTO contared.cuentas_bancarias
          (empresa_id,pais_codigo,banco_id,moneda_codigo,numero,alias,activa,
           alias_autorizado,numero_completo_verificado,incorporacion_provisional)
        VALUES (empresa.id,'GT',banco.id,'GTQ',gt_numero,gt_alias,true,gt_alias,true,false)
        ON CONFLICT (banco_id,numero) DO NOTHING;
      ELSE
        UPDATE contared.cuentas_bancarias c
        SET alias=gt_alias,
            alias_autorizado=gt_alias,
            banco_id=banco.id,
            numero_completo_verificado=true
        WHERE c.id=cuenta.id AND NOT EXISTS (
          SELECT 1 FROM contared.movimientos m
          WHERE m.cuenta_id=c.id LIMIT 1
        );
      END IF;
    END LOOP;
  END LOOP;
END $$;

-- Incorporación provisional SV: número visible = terminación, número completo sin verificar.
DO $$
DECLARE
  empresa record;
  banco record;
  cuenta record;
  sv_banco text;
  sv_terminacion text;
  sv_alias text;
BEGIN
  FOR empresa IN SELECT id FROM contared.empresas WHERE nombre='Red Intelfon'
  LOOP
    FOR sv_banco,sv_terminacion,sv_alias IN
      SELECT v.banco,v.terminacion,v.alias FROM (VALUES
        ('Banco Agrícola','9596','AGRICOLA'),
        ('Banco Cuscatlán','2509','CUSCA 2509'),
        ('Banco Cuscatlán','4978','CUSCA 4978'),
        ('BAC','0386','BAC 386'),
        ('BAC','6715','BAC 6715'),
        ('BAC','0327','BAC 327'),
        ('Promerica','0049','PROM 49'),
        ('Davivienda','8202','DAV 68202'),
        ('Davivienda','1962','DAV 962')
      ) AS v(banco,terminacion,alias)
    LOOP
      SELECT b.id INTO banco FROM contared.bancos b
      WHERE b.pais_codigo='SV' AND lower(b.nombre)=lower(sv_banco);
      IF banco.id IS NULL THEN
        INSERT INTO contared.bancos(pais_codigo,nombre,activo)
        VALUES('SV',sv_banco,true)
        RETURNING id INTO banco;
      END IF;
      SELECT c.id INTO cuenta FROM contared.cuentas_bancarias c
      WHERE c.empresa_id=empresa.id AND c.pais_codigo='SV'
        AND c.banco_id=banco.id
        AND (btrim(c.numero)=btrim(sv_terminacion)
             OR btrim(c.numero_terminacion)=btrim(sv_terminacion));
      IF cuenta.id IS NULL THEN
        INSERT INTO contared.cuentas_bancarias
          (empresa_id,pais_codigo,banco_id,moneda_codigo,numero,alias,activa,
           numero_terminacion,alias_autorizado,numero_completo_verificado,incorporacion_provisional)
        VALUES (empresa.id,'SV',banco.id,'USD',sv_terminacion,sv_alias,true,
                sv_terminacion,sv_alias,false,true)
        ON CONFLICT (banco_id,numero) DO NOTHING;
      ELSE
        UPDATE contared.cuentas_bancarias c
        SET numero_terminacion=sv_terminacion,
            alias=sv_alias,
            alias_autorizado=sv_alias
        WHERE c.id=cuenta.id;
      END IF;
    END LOOP;
  END LOOP;
END $$;

-- Monedas históricas se sincronizan solo si la cuenta no tiene registros financieros.
-- Si existen movimientos, conciliaciones, cargas o cheques, no se cambia la moneda.
DO $$
DECLARE
  c record;
  cuenta record;
  banco record;
  sv record;
BEGIN
  FOR c IN SELECT c.id,c.empresa_id,c.pais_codigo,c.numero,c.moneda_codigo
           FROM contared.cuentas_bancarias c
           JOIN contared.empresas e ON e.id=c.empresa_id
           WHERE e.nombre='Red Intelfon'
             AND c.pais_codigo IN ('GT','SV')
             AND ((c.pais_codigo='GT' AND c.numero IN (
                    '0040040592','0040040584','090003566','901020461','3445256003'))
               OR c.pais_codigo='SV')
  LOOP
    IF EXISTS (
      SELECT 1 FROM contared.movimientos m WHERE m.cuenta_id=c.id
      UNION ALL SELECT 1 FROM contared.conciliaciones r WHERE r.cuenta_id=c.id
      UNION ALL SELECT 1 FROM contared.cargas ca WHERE ca.cuenta_id=c.id
    ) THEN
      RAISE NOTICE 'Conflicto de moneda histórico: cuenta=%, moneda_operativa=%, pais=%',
        c.numero,c.moneda_codigo,c.pais_codigo;
    ELSE
      UPDATE contared.cuentas_bancarias
      SET moneda_codigo=CASE WHEN c.pais_codigo='GT' THEN 'GTQ' ELSE 'USD' END,
          incorporacion_provisional=false
      WHERE id=c.id;
    END IF;
  END LOOP;
END $$;

INSERT INTO contared.migraciones(version)
VALUES ('202611080001_historial_cuentas_bancos_gt_sv')
ON CONFLICT (version) DO NOTHING;

COMMIT;
