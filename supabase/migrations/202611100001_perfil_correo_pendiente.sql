-- Conserva el correo vigente y el nuevo correo mientras Supabase Auth espera confirmacion.
ALTER TABLE contared.usuarios
  ADD COLUMN correo_pendiente text;

CREATE UNIQUE INDEX usuarios_correo_pendiente_normalizado
  ON contared.usuarios (lower(correo_pendiente))
  WHERE correo_pendiente IS NOT NULL;
