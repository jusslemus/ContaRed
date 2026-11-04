BEGIN;

-- Guatemala: moneda confirmada por cuenta, sin inferirla por el alias.
DO $$
DECLARE
  conflicto record;
BEGIN
  FOR conflicto IN
    SELECT c.id, c.numero, c.moneda_codigo AS moneda_operativa,
           CASE btrim(c.numero)
             WHEN '0040040592' THEN 'GTQ'
             WHEN '0040040584' THEN 'GTQ'
             WHEN '090003566' THEN 'GTQ'
             WHEN '901020461' THEN 'GTQ'
             WHEN '3445256003' THEN 'GTQ'
             ELSE c.moneda_codigo
           END AS moneda_confirmada
    FROM contared.cuentas_bancarias c
    WHERE c.pais_codigo = 'GT'
      AND btrim(c.numero) IN (
        '0040040592','0040040584','090003566','901020461','3445256003'
      )
      AND c.moneda_codigo IS DISTINCT FROM 'GTQ'
  LOOP
    RAISE NOTICE 'Conflicto previo GT; cuenta=%, operativa=%, confirmada=%',
      conflicto.numero, conflicto.moneda_operativa, conflicto.moneda_confirmada;
  END LOOP;
END $$;

UPDATE contared.cuentas_bancarias c
SET moneda_codigo = 'GTQ',
    alias = CASE btrim(c.numero)
      WHEN '0040040592' THEN 'BIQ'
      WHEN '0040040584' THEN 'BI$3'
      WHEN '090003566' THEN 'GYT'
      WHEN '901020461' THEN 'BAC'
      WHEN '3445256003' THEN 'BANRURAL'
      ELSE c.alias
    END
WHERE c.pais_codigo = 'GT'
  AND btrim(c.numero) IN (
    '0040040592','0040040584','090003566','901020461','3445256003'
  );

INSERT INTO contared.catalogo_cuentas_bancarias
  (pais_codigo, numero, banco_nombre, alias, moneda_codigo)
VALUES
  ('GT','0040040592','Banco Industrial','BIQ','GTQ'),
  ('GT','0040040584','Banco Industrial','BI$3','GTQ'),
  ('GT','090003566','Banco GYT Continental','GYT','GTQ'),
  ('GT','901020461','Banco de América Central','BAC','GTQ'),
  ('GT','3445256003','Banco de Desarrollo Rural','BANRURAL','GTQ')
ON CONFLICT (pais_codigo, numero) DO UPDATE
SET banco_nombre = EXCLUDED.banco_nombre,
    alias = EXCLUDED.alias,
    moneda_codigo = EXCLUDED.moneda_codigo;

-- El Salvador: los valores son terminaciones, no números completos.
DO $$
DECLARE
  conflicto record;
BEGIN
  IF to_regclass('contared.catalogo_cuentas_sv') IS NOT NULL THEN
    FOR conflicto IN
      SELECT c.id, c.numero, c.moneda_codigo AS moneda_operativa, s.alias
      FROM contared.cuentas_bancarias c
      JOIN contared.catalogo_cuentas_sv s
        ON s.pais_codigo = c.pais_codigo
       AND btrim(c.numero) = btrim(s.terminacion)
      WHERE c.pais_codigo = 'SV'
        AND c.moneda_codigo IS DISTINCT FROM 'USD'
    LOOP
      RAISE NOTICE 'Conflicto previo SV; terminacion=%, operativa=%, confirmada=USD',
        conflicto.numero, conflicto.moneda_operativa;
    END LOOP;

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
    SET moneda_codigo = EXCLUDED.moneda_codigo,
        confirmado = true;

    -- Solo actualiza cuentas operativas cuyo número coincide exactamente con una terminación catalogada.
    UPDATE contared.cuentas_bancarias c
    SET moneda_codigo = 'USD',
        alias = s.alias
    FROM contared.catalogo_cuentas_sv s
    WHERE c.pais_codigo = 'SV'
      AND s.pais_codigo = 'SV'
      AND btrim(c.numero) = btrim(s.terminacion);
  END IF;
END $$;

-- Identifica vinculaciones contradictorias sin reinterpretar ni modificar importes históricos.
DO $$
DECLARE
  conflicto record;
BEGIN
  FOR conflicto IN
    SELECT c.id, c.numero, c.moneda_codigo AS moneda_operativa,
           k.moneda_codigo AS moneda_catalogo, c.alias, k.alias AS alias_catalogo
    FROM contared.cuentas_bancarias c
    JOIN contared.catalogo_cuentas_bancarias k
      ON k.pais_codigo = c.pais_codigo
     AND btrim(k.numero) = btrim(c.numero)
    WHERE c.pais_codigo = 'GT'
      AND c.moneda_codigo IS DISTINCT FROM k.moneda_codigo
  LOOP
    RAISE NOTICE 'Conflicto de moneda GT; cuenta=%, operativa=%, catalogo=%',
      conflicto.numero, conflicto.moneda_operativa, conflicto.moneda_catalogo;
  END LOOP;
END $$;

INSERT INTO contared.migraciones(version)
VALUES ('202611070001_monedas_catalogo_confirmadas_gt_sv')
ON CONFLICT (version) DO NOTHING;

COMMIT;
