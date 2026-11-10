ALTER TABLE contared.usuarios
  ADD COLUMN cambio_clave_obligatorio boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN contared.usuarios.cambio_clave_obligatorio IS
  'Indica que la persona debe cambiar su contraseña en Supabase Auth antes de usar ContaRed.';
