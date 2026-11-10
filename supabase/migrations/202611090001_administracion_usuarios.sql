-- Campos para vincular invitaciones por correo con perfiles existentes.
ALTER TABLE contared.usuarios
  ADD COLUMN correo text,
  ADD COLUMN estado_invitacion text NOT NULL DEFAULT 'no_requerida'
    CHECK (estado_invitacion IN ('pendiente','enviada','fallida','no_requerida')),
  ADD COLUMN invitacion_enviada_en timestamptz;

CREATE UNIQUE INDEX usuarios_correo_normalizado
  ON contared.usuarios (lower(correo))
  WHERE correo IS NOT NULL;

-- Desactivar un permiso conserva la fila para historial y reactivación.
ALTER TABLE contared.accesos
  ADD COLUMN activo boolean NOT NULL DEFAULT true;
