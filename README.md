# ContaRed

ContaRed es el punto de partida para una aplicación privada de conciliación bancaria de una empresa que opera en El Salvador y Guatemala.

## Estado de esta entrega

El inicio de sesión se realiza con Supabase Auth. El backend verifica el token y resuelve `auth_user_id` contra `contared.usuarios`; solo devuelve un perfil activo y sus combinaciones autorizadas de empresa, país y rol desde `contared.accesos`. La consulta ignora empresas inactivas y solo incluye SV/GT. La respuesta se consulta dentro de una transacción de solo lectura.

La migración inicial está en `supabase/migrations/202610270001_inicial.sql`. Ya fue ejecutada en Supabase antes de esta entrega; **no la ejecutes de nuevo**. Django no tiene modelos de dominio ni migraciones para `contared`.

El usuario puede elegir un país y empresa de su propia lista de accesos. Los módulos de Bancos y cuentas bancarias y la primera etapa de Conciliaciones están habilitados para la combinación activa. Esta etapa permite crear borradores, importar Excel y consultar movimientos; no confirma cruces, resuelve pendientes ni habilita cierre definitivo.

## Módulo de Bancos y cuentas

En la navegación, entra a **Cuentas** después de seleccionar empresa y país. El módulo muestra datos reales del esquema `contared`, con paginación del servidor, búsqueda y filtro por estado. Un administrador puede crear y editar bancos, crear cuentas, cambiar alias y activar/desactivar registros. Los roles auxiliar, revisor y consulta tienen lectura.

Recorrido para registrar el primer banco y la primera cuenta:

1. Inicia sesión con el usuario vinculado a Supabase Auth y seleccionado en `contared.usuarios`.
2. Selecciona la empresa y el país a los que tienes acceso.
3. En **Cuentas**, abre la pestaña **Bancos** y registra el nombre del banco. No se permite duplicarlo dentro del mismo país.
4. Abre **Cuentas bancarias** y registra el banco activo, la moneda existente, el número completo como texto y un alias.
5. Conserva el número exacto, incluyendo ceros iniciales. La edición posterior solo permite cambiar alias y estado.
6. Para desactivar el banco, usa su botón correspondiente. Las cuentas históricas permanecen consultables; no se podrá activar una cuenta mientras el banco permanezca inactivo.

## Módulo de Conciliaciones

Después de seleccionar la empresa y país autorizados, entra a **Conciliaciones** y elige una cuenta bancaria activa. Crea el período indicando el mes y la fecha de corte. Puedes dejar los saldos iniciales o finales de banco y libros vacíos; vacío significa que no se capturó un saldo, no cero. Si ya hay una conciliación para la cuenta y el mes, abre la existente.

En el detalle:

1. En **Resumen**, revisa los saldos capturados y las cargas realizadas. Los saldos manuales se identifican como tales y los totales de movimientos se calculan solo con datos importados.
2. Para cada origen, selecciona **Importar movimientos**, elige banco o libros y carga un archivo `.xlsx` de hasta 10 MiB.
3. Si guardaste un perfil para ese banco y origen, puedes reutilizarlo. Selecciona la hoja, la fila de encabezados y mapea fecha, referencia, descripción y monto. Para importes separados, asigna explícitamente las columnas de salidas y entradas; la vista previa muestra los signos que se guardarán.
4. Revisa las filas, los errores, filas vacías, encabezados repetidos y totales. Corrige las filas no válidas y vuelve a cargar el archivo. Las fechas fuera del período o posteriores al corte requieren excluirse explícitamente o corregirse; no se ajustan automáticamente.
5. Confirma la carga solo cuando la vista previa no tenga errores pendientes. Puedes guardar el mapeo como un perfil versionado. Los archivos temporales de vista previa vencen a las dos horas; al confirmar, la aplicación conserva el original en el almacenamiento privado y los movimientos importados son inmutables.
6. Consulta cada conjunto en **Movimientos del banco** o **Movimientos de libros**. Las tablas permiten paginar y filtrar por fecha, referencia y descripción; abre el origen de una fila para consultar sus datos originales.

Administrador y auxiliar pueden crear conciliaciones, editar saldos de borradores e importar archivos. Revisor y consulta tienen acceso de solo lectura. Los mismos permisos y el ámbito de empresa/país se verifican en el backend. La igualdad de totales no declara una conciliación como conciliada: en esta etapa no se habilitan cruces, resolución de pendientes ni cierre.

## Requisitos

- Python 3.10 o posterior.
- Node.js 20 o posterior y npm.
- Un proyecto Supabase existente.
- La migración SQL inicial ya aplicada, conservada en `supabase/migrations/202610270001_inicial.sql`.

## Frontend

```powershell
Set-Location frontend
Copy-Item .env.example .env
npm install
npm run dev
```

Configura en `frontend/.env`:

- `VITE_SUPABASE_URL`: URL pública del proyecto Supabase.
- `VITE_SUPABASE_PUBLISHABLE_KEY`: clave publicable/anon para Supabase Auth; nunca una clave `service_role`.
- `VITE_API_BASE_URL`: URL del backend Django.

La aplicación ofrece inicio de sesión con correo y contraseña. No incluye registro público. Deshabilita también el registro en la configuración de Supabase Auth. Una sesión válida no concede por sí misma acceso a empresa o país.

El logo se importa directamente desde `backend/img/logo.jpg`; el archivo original no se modifica.

## Backend

```powershell
Set-Location backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Completa `backend/.env` con:

- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` y `CORS_ALLOWED_ORIGINS`.
- `SUPABASE_URL`, `SUPABASE_ISSUER`, `SUPABASE_JWKS_URL` y `SUPABASE_PUBLISHABLE_KEY`.
- `SUPABASE_SERVICE_ROLE_KEY` solo en el backend para subir y limpiar originales en el bucket privado `contared-documentos`. Esta credencial privilegiada no debe estar en el frontend ni en logs. Sin configurarla, las vistas previas funcionan, pero no se pueden confirmar cargas.
- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` y `POSTGRES_SSLMODE`.

Las credenciales de PostgreSQL permanecen solo en el backend. La conexión configura `search_path=contared`; Django no tiene modelos de dominio ni migraciones para ese esquema. No ejecutes `manage.py migrate` contra Supabase. `/api/v1/status/` comprueba la conexión con un `SELECT 1`. `/api/v1/session/` usa una transacción PostgreSQL `READ ONLY` y consultas calificadas al esquema existente; no escribe ni cambia permisos.

La API devuelve solamente `usuario`, `accesos` y `paises` autorizados. Cada acceso asocia empresa, código/nombre de país y rol. Los fallos diferenciados incluyen perfil no vinculado (`profile_not_found`), perfil inactivo (`profile_inactive`), falta de accesos (`no_access`), proveedor de identidad no disponible y base no disponible. El selector de frontend no otorga permisos; la autorización se resuelve en servidor.

Para iniciar el backend en desarrollo:

```powershell
python manage.py runserver 127.0.0.1:8000 --noreload
```

El comando `runserver` de ContaRed omite la comprobación de migraciones de Django para no inspeccionar ni modificar tablas fuera del esquema existente. No uses `manage.py migrate` en este proyecto.

El backend verifica tokens Supabase con firma, vencimiento, emisor configurado, `aud=authenticated` y un `sub` UUID. Para ES256 y RS256 verifica localmente con las claves públicas de JWKS. Para HS256 consulta `/auth/v1/user` del proyecto con la clave publicable, confirma la identidad y no almacena ni comparte el secreto de firma; esta verificación agrega una llamada a Auth por petición.

## Pruebas

Backend, sin base de datos configurada ni conexión a Supabase:

```powershell
Set-Location backend
python manage.py test --settings=config.test_settings contared_api
```

El perfil de pruebas utiliza exclusivamente SQLite en memoria y mocks de consultas; no toma credenciales de PostgreSQL y nunca se conecta a producción. Incluye pruebas sintéticas para banco/libros, signos, referencias con ceros, fechas ambiguas/fuera de corte, fórmulas sin valor calculado, importes decimales, exclusiones, asociación de temporales al usuario y lectura de la hoja/encabezado seleccionados. Las pruebas de sesión ejercitan perfil ausente/inactivo, sin acceso, respuestas SV/GT por empresa y rol, filtros de empresas activas, consulta vinculada al UUID autenticado y error de base de datos. No insertan datos ficticios en Supabase.

Para comprobar la conexión real de solo lectura, con los `.env` configurados, inicia el backend y visita `http://localhost:8000/api/v1/status/`. Esto solo ejecuta `SELECT 1`. `/api/v1/session/` requiere un token de usuario obtenido al iniciar sesión.

El motor independiente de propuestas usa igualdad exacta, no modifica movimientos y limita las agrupaciones a tres partidas por combinación. Las propuestas requieren confirmación explícita en cualquier futura interfaz conectada.

## Esquema y habilitación del primer administrador

La migración documenta el esquema privado y sus tablas. Las consultas de sesión usan los campos declarados en `usuarios`, `accesos`, `empresas` y `paises`. No se crearon modelos Django ni se ejecutó SQL de migración.

El administrador ya debe existir en Supabase Auth y estar vinculado explícitamente a `usuarios.auth_user_id`, con `usuarios.activo = true` y accesos explícitos en `accesos`. El backend nunca crea perfiles ni concede permisos al iniciar sesión.

## Pendientes

- Corregir `POSTGRES_HOST` en `backend/.env`: durante la verificación su nombre no resolvió por DNS, por lo que `/api/v1/status/` devolvió `database_unavailable`. El nombre del host no se imprimió.
- Repetir la verificación de solo lectura y validar `/api/v1/session/` con el usuario real después de corregir la conexión.
- Verificar contra una base PostgreSQL aislada la integración de escritura, auditoría y almacenamiento después de configurar las credenciales de almacenamiento.
- Conectar las propuestas del motor con los cruces y pendientes existentes, manteniendo sus restricciones diferidas y trazabilidad.
- Implementar reporte Excel de cuatro hojas como borrador; no habilitar cierre hasta validar su fórmula.
- Evaluar y entregar, si son necesarias, migraciones SQL **adicionales** como archivos pendientes, sin ejecutarlas.

## Fuentes de configuración

- [Supabase: JSON Web Tokens](https://supabase.com/docs/guides/auth/jwts)
- [Supabase: JWT Signing Keys](https://supabase.com/docs/guides/auth/signing-keys)
