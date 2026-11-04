ALTER TABLE contared.cuentas_bancarias
  ADD COLUMN codigo_contable text;

ALTER TABLE contared.cuentas_bancarias
  ADD CONSTRAINT cuentas_bancarias_codigo_contable_no_vacio
  CHECK (codigo_contable IS NULL OR btrim(codigo_contable) <> '');

CREATE UNIQUE INDEX cuentas_bancarias_codigo_contable_unico
  ON contared.cuentas_bancarias(empresa_id,pais_codigo,codigo_contable)
  WHERE codigo_contable IS NOT NULL;

COMMENT ON COLUMN contared.cuentas_bancarias.codigo_contable IS
  'Código de cuenta del mayor contable relacionado con esta cuenta bancaria; no es el número de cuenta bancaria.';
