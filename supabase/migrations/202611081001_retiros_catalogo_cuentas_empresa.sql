BEGIN;

CREATE TABLE IF NOT EXISTS contared.catalogo_cuentas_retiradas (
  catalogo_id uuid NOT NULL,
  empresa_id uuid NOT NULL REFERENCES contared.empresas(id),
  pais_codigo text NOT NULL REFERENCES contared.paises(codigo),
  retirada_en timestamptz NOT NULL DEFAULT now(),
  retirada_por uuid NOT NULL REFERENCES contared.usuarios(id),
  PRIMARY KEY (catalogo_id, empresa_id, pais_codigo)
);

CREATE INDEX IF NOT EXISTS idx_catalogo_cuentas_retiradas_empresa_pais
  ON contared.catalogo_cuentas_retiradas(empresa_id, pais_codigo);

ALTER TABLE contared.catalogo_cuentas_retiradas ENABLE ROW LEVEL SECURITY;
GRANT SELECT,INSERT,UPDATE ON TABLE contared.catalogo_cuentas_retiradas TO authenticated;

COMMIT;
