BEGIN;

CREATE TABLE IF NOT EXISTS contared.catalogo_cuentas_bancarias (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  numero text NOT NULL CHECK (btrim(numero) <> ''),
  banco_nombre text NOT NULL CHECK (btrim(banco_nombre) <> ''),
  alias text NOT NULL CHECK (btrim(alias) <> ''),
  moneda_codigo text REFERENCES contared.monedas(codigo),
  UNIQUE (pais_codigo,numero)
);

CREATE TABLE contared.catalogo_cuenta_empresa (
  catalogo_id uuid NOT NULL REFERENCES contared.catalogo_cuentas_bancarias(id) ON DELETE CASCADE,
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id) ON DELETE CASCADE,
  asociada_en timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (catalogo_id,empresa_id)
);

ALTER TABLE contared.catalogo_cuentas_bancarias ENABLE ROW LEVEL SECURITY;
ALTER TABLE contared.catalogo_cuenta_empresa ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE contared.catalogo_cuentas_bancarias,
  contared.catalogo_cuenta_empresa FROM PUBLIC,anon,authenticated;
GRANT SELECT ON TABLE contared.catalogo_cuentas_bancarias TO authenticated;
GRANT SELECT ON TABLE contared.catalogo_cuenta_empresa TO authenticated;

INSERT INTO contared.catalogo_cuentas_bancarias
  (pais_codigo,numero,banco_nombre,alias,moneda_codigo)
VALUES
  ('GT','0040040592','Banco Industrial','BIQ','GTQ'),
  ('GT','0040040584','Banco Industrial','BI$3','GTQ'),
  ('GT','090003566','Banco GYT Continental','GYT',NULL),
  ('GT','901020461','Banco de América Central','BAC',NULL),
  ('GT','3445256003','Banco de Desarrollo Rural','BANRURAL',NULL)
ON CONFLICT (pais_codigo,numero) DO UPDATE
SET banco_nombre=EXCLUDED.banco_nombre,
    alias=EXCLUDED.alias,
    moneda_codigo=EXCLUDED.moneda_codigo;

-- Vincula únicamente cuentas ya existentes cuyo número coincide exactamente.
-- No crea ni modifica cuentas adicionales ni duplica cuentas por alias.
INSERT INTO contared.catalogo_cuenta_empresa (catalogo_id,empresa_id)
SELECT DISTINCT c.id, cb.empresa_id
FROM contared.catalogo_cuentas_bancarias c
JOIN contared.cuentas_bancarias cb
  ON cb.pais_codigo=c.pais_codigo AND btrim(cb.numero)=btrim(c.numero)
JOIN contared.bancos b ON b.id=cb.banco_id AND b.pais_codigo=cb.pais_codigo
WHERE c.pais_codigo='GT'
  AND regexp_replace(lower(b.nombre), '[^a-z0-9]', '', 'g')
      = regexp_replace(lower(c.banco_nombre), '[^a-z0-9]', '', 'g')
ON CONFLICT DO NOTHING;

COMMENT ON TABLE contared.catalogo_cuentas_bancarias IS
  'Catálogo autorizado de cuentas bancarias. Los bancos de El Salvador no deben usarlo.';
COMMENT ON COLUMN contared.catalogo_cuentas_bancarias.numero IS
  'Número bancario conservado como texto, incluyendo ceros iniciales.';

INSERT INTO contared.migraciones(version)
VALUES ('202611040001_catalogo_cuentas_guatemala')
ON CONFLICT (version) DO NOTHING;

COMMIT;
