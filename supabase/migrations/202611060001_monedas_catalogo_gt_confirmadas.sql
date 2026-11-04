BEGIN;

UPDATE contared.cuentas_bancarias
SET alias = 'BIQ',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND btrim(numero) = '0040040592';

UPDATE contared.cuentas_bancarias
SET alias = 'BI$3',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND btrim(numero) = '0040040584';

UPDATE contared.catalogo_cuentas_bancarias
SET banco_nombre = 'Banco Industrial',
    alias = 'BIQ',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND numero = '0040040592';

UPDATE contared.catalogo_cuentas_bancarias
SET banco_nombre = 'Banco Industrial',
    alias = 'BI$3',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND numero = '0040040584';

UPDATE contared.catalogo_cuentas_bancarias
SET alias = 'GYT',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND numero = '090003566';

UPDATE contared.catalogo_cuentas_bancarias
SET alias = 'BAC',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND numero = '901020461';

UPDATE contared.catalogo_cuentas_bancarias
SET alias = 'BANRURAL',
    moneda_codigo = 'GTQ'
WHERE pais_codigo = 'GT'
  AND numero = '3445256003';

INSERT INTO contared.catalogo_cuentas_bancarias
  (pais_codigo, numero, banco_nombre, alias, moneda_codigo)
VALUES
  ('GT', '0040040592', 'Banco Industrial', 'BIQ', 'GTQ'),
  ('GT', '0040040584', 'Banco Industrial', 'BI$3', 'GTQ'),
  ('GT', '090003566', 'Banco GYT Continental', 'GYT', 'GTQ'),
  ('GT', '901020461', 'Banco de América Central', 'BAC', 'GTQ'),
  ('GT', '3445256003', 'Banco de Desarrollo Rural', 'BANRURAL', 'GTQ')
ON CONFLICT (pais_codigo, numero) DO UPDATE
SET banco_nombre = EXCLUDED.banco_nombre,
    alias = EXCLUDED.alias,
    moneda_codigo = EXCLUDED.moneda_codigo;

INSERT INTO contared.migraciones(version)
VALUES ('202611060001_monedas_catalogo_gt_confirmadas')
ON CONFLICT (version) DO NOTHING;

COMMIT;
