# ContaRed

ContaRed es el punto de partida para una aplicación privada de conciliación bancaria de una empresa que opera en El Salvador y Guatemala.

## Estado de esta entrega

El inicio de sesión se realiza con Supabase Auth. El backend verifica el token y resuelve `auth_user_id` contra `contared.usuarios`; solo devuelve un perfil activo y sus combinaciones autorizadas de empresa, país y rol desde `contared.accesos`. La consulta ignora empresas inactivas y solo incluye SV/GT. La respuesta se consulta dentro de una transacción de solo lectura.

La migración inicial está en `supabase/migrations/202610270001_inicial.sql`. Ya fue ejecutada en Supabase; **no la ejecutes de nuevo**. La migración de comparación `202610290001_comparacion_movimientos.sql` y la migración base de Cheques `202610300001_cheques.sql` fueron aplicadas por el usuario; **no las repitas**. Las nuevas migraciones permanecen pendientes y no se ejecutaron durante este trabajo. Django no tiene modelos de dominio ni migraciones para `contared`.

El usuario puede elegir un país y empresa de su propia lista de accesos. Los módulos de Bancos y cuentas bancarias y Conciliaciones están habilitados para la combinación activa. Conciliaciones permite crear borradores, importar Excel, consultar movimientos, comparar propuestas y revisar cheques ligados a sus movimientos de emisión. No crea ajustes contables ni habilita cierre definitivo.

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

Después de seleccionar la empresa y país autorizados, entra a **Conciliaciones** y elige una cuenta bancaria. Crea el período indicando el mes y la fecha de corte. Puedes dejar los saldos iniciales o finales de banco y libros vacíos; vacío significa que no se capturó un saldo, no cero. Si ya hay una conciliación para la cuenta y el mes, abre la existente. El encabezado compartido conserva empresa, país, cuenta, moneda, período y fecha de corte en sus cuatro pestañas.

En la pantalla principal de **Conciliaciones**, el panel **Cargar documentos** funciona aunque todavía no exista un borrador:

1. Selecciona cuenta y mes/año, arrastra o selecciona varios `.xlsx` o `.xls`; cada documento se inspecciona automáticamente al agregarse. Se muestra una tarjeta por archivo con origen, período, movimientos, saldo final y advertencias.
2. Los documentos reconocidos y consistentes se validan automáticamente y se procesan juntos con **Procesar y conciliar**; no requieren asignar columnas manualmente. Los estados de borrador, importación de Banco, importación de Contabilidad y comparación se muestran por separado; crear o recuperar un borrador no significa que la conciliación esté completa. El resultado informa los archivos procesados, pendientes de revisión y fallidos por separado. Reintentar el mismo documento no duplica una carga ya confirmada.
3. Las excepciones requieren revisión: usa **Revisar configuración** en esa tarjeta para corregir hoja, encabezados, origen o columnas. Para el formato identificado del estado Banco Industrial de septiembre de 2026, las fechas numéricas compatibles con la inversión día/mes se muestran como propuestas por fila. No se modifican ni se excluyen automáticamente: revisa el valor Excel original, la fecha interpretada, la propuesta y su motivo; marca la confirmación y vuelve a validar. El valor y formato originales se conservan en los datos fuente de la importación. Las fechas sin una propuesta inequívoca siguen bloqueando el procesamiento. El backend valida que la extensión coincida con el formato binario; cambiar el nombre no convierte un archivo.
4. Revisa las cargas y consulta movimientos de Banco o Libros. **Ver origen** presenta los datos originales y, si un perfil define un catálogo, la interpretación del tipo de transacción junto con el código recibido.
5. Si los códigos bancarios tienen una interpretación confirmada, configura el catálogo JSON al guardar el perfil de importación asociado al banco. Por ejemplo: `{"CQ":"Cheque cobrado","DE":"Depósito","ND":"Nota de débito"}`. No se asignan significados globales: un código sin configurar se muestra como **Tipo de transacción sin identificar**, junto con el código recibido.
6. Los archivos temporales vencen a las dos horas; al confirmar, la aplicación conserva el original `.xlsx` o `.xls` en almacenamiento privado y los movimientos importados son inmutables. Para inspeccionar un `.xls`, se normaliza una copia temporal; el original se conserva y se verifica por hash.
7. Cuando haya al menos una carga validada de Banco y otra de Libros, usa **Comparar movimientos**. La comparación genera propuestas persistidas; no confirma cruces ni modifica movimientos. Las coincidencias por referencia/monto muestran también las fechas; las propuestas por fecha/monto requieren unicidad; los grupos comparten referencia y fecha y se limitan a tres partidas. Las diferencias de monto, casos ambiguos y movimientos sin contraparte no se pueden confirmar como coincidencias.
8. Revisa Banco y Libros lado a lado, despliega los grupos para ver sus partidas y sumas y abre **Ver origen** en cada movimiento. Administrador y revisor pueden confirmar coincidencias exactas o descartar propuestas; auxiliar puede comparar y consultar; consulta es de solo lectura. Comparar de nuevo los mismos datos reutiliza la ejecución y conserva descartes. Confirmar no recarga la página. Para descartar, escribe un motivo; la propuesta queda registrada con usuario y fecha, pero sus movimientos pueden seguir pendientes. Puedes dejar cualquier propuesta sin resolver para continuar después.
9. **Descargar reporte** genera Excel o PDF con el snapshot persistido en el backend. **Todo el proceso** permite seleccionar uno o más grupos; **Pestaña actual** exporta el grupo abierto. Se incluyen todos los resultados del alcance, no solo los visibles, y el reporte indica que no se aplican filtros de búsqueda ni paginación. Cada archivo incluye conteos, regla, explicación, estado de revisión separado de la clasificación, movimientos y sus montos; los grupos muestran todas sus partidas. Una diferencia descartada conserva su clasificación y aparece tanto en **Diferencias y sin contraparte** como en **Descartadas**. El archivo se guarda en el bucket privado `contared-documentos` y se registra con versión, formato, snapshot y actor en `contared.reportes`. Todos los roles con acceso autorizado pueden descargar; la generación no cambia movimientos ni estados.

La pestaña **Depósitos** permite revisar candidatos y grupos de comparación, documentar casos, registrar diferencias con soporte, consultar historial y resolverlos con evidencia y fecha efectiva. No modifica movimientos ni crea ajustes. Su persistencia requiere la migración adicional indicada abajo; mientras falte, Depósitos muestra un aviso y Pendientes e Inicio continúan mostrando las demás fuentes.

**Pendientes** reúne cheques, depósitos, propuestas y movimientos por investigar desde sus registros originales; aplica filtros en servidor, deduplica las fuentes y separa totales por moneda y naturaleza. **Inicio** muestra conciliaciones recientes y pendientes del ámbito activo, con enlaces a las vistas correspondientes.

La pestaña **Resumen de conciliación** prioriza los saldos finales utilizados (captura manual autorizada; si no existe, detectados del archivo) y la diferencia Banco menos Contabilidad, conservando ambos importes y su procedencia cuando hay conflicto. Las mismas cifras se incluyen en pantalla, PDF y Excel; los saldos iniciales se muestran como “No disponible” si no existe un dato conservado en la conciliación. El saldo ajustado definitivo y el cierre no están disponibles. Sus reportes Excel y PDF son borradores y se almacenan en el bucket privado existente. La detección persistente de saldos finales requiere aplicar `supabase/migrations/202611030002_saldos_detectados_conciliacion.sql`; no requiere reimportar movimientos ni crear cargas.

En la pantalla principal de **Conciliaciones**, **Importar documento separado de cheques** permite seleccionar cuenta, fecha de corte y tipo de documento (registro completo o pendientes al corte), descargar una plantilla propuesta y cargar un Excel independiente. El perfil JSON configurable mapea las columnas; el número, emisión, beneficiario, importe y estado son obligatorios. Por ahora admite estados explícitos **Retenido** o **En circulación**; si el archivo incluye cobrados o anulados se rechaza completo, pues necesitan vínculo con evidencia contable y bancaria. Los registros pendientes se vinculan luego con una emisión de Libros y un cobro bancario coincidentes. No crea cargas ni movimientos contables. Las versiones y filas originales quedan en el historial; reintentar el mismo archivo no duplica registros. El formato real de la institución no se ha proporcionado ni verificado, por lo que se debe adaptar el perfil y validar el documento antes de usarlo operativamente.

En **Cheques**, **Nuevo cheque** ofrece usar una emisión existente de Libros (opción predeterminada), crear una carga y movimiento manual en Libros o registrar el cheque de forma independiente en el auxiliar operativo. El registro independiente no crea cargas ni movimientos contables. Ninguna opción crea una salida de Banco, altera saldos capturados, publica asientos en otro sistema, imprime cheques o ejecuta pagos.

En **Cheques**, los movimientos de Libros con número de cheque explícito aparecen como candidatos hasta que un usuario los clasifique. Al usar un movimiento existente, se conservan sus datos fuente y no se crea otro movimiento; el beneficiario se captura solo si hace falta. Los números repetidos en la cuenta se muestran para revisión, y los duplicados exactos por número, fecha e importe se bloquean. Los ceros iniciales se conservan. Los contadores abarcan todos los períodos de origen de la cuenta hasta la fecha de corte y son independientes de búsqueda y filtro de período.

Para registrar un cheque que todavía no está cargado en Libros, pulsa **Nuevo cheque** y elige una de las opciones manuales. **Registrar manualmente en Libros** crea una carga y movimiento contable inmutables; **Registrar cheque independiente** solo crea el registro del cheque y su historial, sin simular una emisión contable. Ambas requieren número como texto, fecha dentro del período/corte, concepto e importe positivo; confirma Retenido o En circulación (esta última requiere fecha y confirmación de entrega). Revisa los movimientos del mismo número antes de guardar. Una coincidencia exacta de número, fecha e importe se bloquea; posibles coincidencias requieren marcar que se revisaron.

Los estados operativos disponibles son:

- **Retenido:** emitido, pendiente de entrega.
- **En circulación:** entregado, pendiente de cobro.
- **Cobrado:** confirmado contra el banco.
- **Anulado:** cancelado con motivo y soporte.

Retenido o En circulación no se deducen únicamente por la ausencia de una salida bancaria. Administrador y auxiliar clasifican candidatos y registran entregas; administrador y revisor confirman cobros o anulan. Para cobrar se selecciona una salida bancaria validada de la misma cuenta, con número de cheque e importe exactos; un cobro posterior a la emisión es válido. Si el cheque figura Retenido, el cobro comprobado se registra sin inventar una entrega, la pantalla advierte que debe documentarse lo ocurrido y la inconsistencia queda en el historial. Una salida bancaria no se reutiliza para cobrar dos cheques.

Las anulaciones requieren motivo, fecha y soporte privado en `contared-documentos`; son operativas y no revierten asientos. El historial conserva estados anterior/nuevo, fechas efectiva y de registro, usuario, motivo, movimiento y cruce relacionados y soporte descargable bajo autorización. Los cheques pendientes siguen visibles en períodos posteriores. La consulta al corte reconstruye el estado usando las fechas efectivas, por lo que un cobro de octubre no elimina el pendiente al corte de septiembre.

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
- `SUPABASE_SERVICE_ROLE_KEY` solo en el backend para crear identidades, asignar contraseñas mediante la API administrativa oficial de Auth y administrar el bucket privado `contared-documentos`. Esta credencial no debe estar en el frontend ni en logs.
- `SUPABASE_AUTH_REDIRECT_URL`: URL de regreso para el flujo opcional de recuperación de contraseña. No se usa para crear cuentas ni para el cambio obligatorio de contraseña inicial.
- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` y `POSTGRES_SSLMODE`.

Las credenciales de PostgreSQL permanecen solo en el backend. La conexión configura `search_path=contared`; Django no tiene modelos de dominio ni migraciones para ese esquema. No ejecutes `manage.py migrate` contra Supabase. `/api/v1/status/` comprueba la conexión con un `SELECT 1`. `/api/v1/session/` usa una transacción PostgreSQL `READ ONLY` y consultas calificadas al esquema existente; no escribe ni cambia permisos.

La API devuelve solamente `usuario`, `accesos`, `paises` autorizados y el indicador `cambio_clave_obligatorio`. Cada acceso asocia empresa, código/nombre de país y rol. Una cuenta que debe cambiar la contraseña solo puede consultar su sesión y completar ese cambio; el backend bloquea las demás operaciones. El servidor valida la contraseña temporal con Supabase Auth, confirma la actualización mediante Auth Admin y solo después retira el indicador. El selector de frontend no otorga permisos; la autorización se resuelve en servidor.

### Administración de usuarios

La pantalla solo aparece para personas con rol `administrador`. Los accesos nuevos se limitan a empresas y países donde ese administrador tiene un acceso activo con ese rol. La desactivación es por acceso, no por perfil global, y las filas se conservan; el servidor impide retirar al último administrador activo de cada empresa/país y audita cambios con el actor autenticado. Editar nombre y correo requiere que todos los accesos del usuario estén dentro del ámbito administrado; si tiene accesos externos, solo se permite editar los accesos autorizados.

Antes de habilitar esta pantalla, aplica manualmente, en orden, `supabase/migrations/202611090001_administracion_usuarios.sql`, `supabase/migrations/202611100001_perfil_correo_pendiente.sql` y `supabase/migrations/202611100002_cambio_clave_obligatorio.sql` en el SQL Editor de Supabase. La primera agrega correo y estado de invitación a `contared.usuarios` y un estado independiente a `contared.accesos`; la segunda conserva por separado el correo pendiente de confirmación; la tercera agrega el indicador de cambio obligatorio, sin contraseñas. No crean identidades Auth ni cambian contraseñas o permisos existentes. No las ejecutes desde Django ni desde esta aplicación.

**Crear usuario** crea la identidad con contraseña inicial y `email_confirm=true` mediante la API administrativa oficial de Supabase Auth. El primer ingreso requiere una contraseña diferente; ContaRed no guarda contraseñas en sus tablas y no crea filas directamente en `auth.users`. Los correos ya asociados a una identidad reutilizan el perfil y los accesos sin modificar la contraseña; la acción explícita **Asignar contraseña temporal** está limitada a perfiles cuyos accesos están dentro del ámbito del administrador. Un perfil heredado con invitación fallida se reutiliza: se completa su creación si no tiene identidad o se asigna una contraseña temporal si ya la tiene. Los fallos parciales conservan el perfil y evitan duplicar identidades al reintentar. Al editar, se conserva el correo vigente y se registra por separado un cambio pendiente si Supabase Auth requiere confirmación. La recuperación opcional envía un enlace al correo vigente usando Auth.

Los fallos de Auth se registran por operación, estado HTTP, código y mensaje saneado del proveedor; nunca incluyen contraseñas, credenciales, correos, URLs, tokens ni identificadores. La creación directa no depende de correo de invitación.

Los roles que se muestran en **Crear usuario** y **Editar usuario** reflejan la autorización del backend: administrador gestiona dentro de sus ámbitos; auxiliar y administrador pueden analizar y modificar conciliaciones; revisor y consulta son de lectura salvo las acciones de revisión existentes concedidas a revisor; la eliminación definitiva de conciliaciones corresponde solo al administrador.

**Eliminar usuario** primero consulta el alcance autorizado. Retirar accesos solo desactiva los países seleccionados; no elimina perfiles ni registros históricos. El borrado definitivo se permite únicamente si el administrador controla todos los ámbitos, no es su propia cuenta, no quedan referencias por clave foránea y puede verificarse la identidad Auth vinculada o su ausencia. La identidad se elimina mediante la API administrativa oficial de Supabase Auth; si Auth se eliminó pero PostgreSQL no pudo confirmar la transacción, el modal informa el estado y el reintento trata la respuesta Auth `404` como ya completada. No se eliminan registros financieros ni se ejecutan migraciones automáticamente.

Para iniciar el backend en desarrollo:

```powershell
python manage.py runserver 127.0.0.1:8000 --noreload
```

El comando `runserver` de ContaRed omite la comprobación de migraciones de Django para no inspeccionar ni modificar tablas fuera del esquema existente. No uses `manage.py migrate` en este proyecto.

El backend verifica tokens Supabase con firma, vencimiento, emisor configurado, `aud=authenticated` y un `sub` UUID. Para ES256 y RS256 verifica localmente con las claves públicas de JWKS. Para HS256 consulta `/auth/v1/user` del proyecto con la clave publicable, confirma la identidad y no almacena ni comparte el secreto de firma; esta verificación agrega una llamada a Auth por petición.

## Pruebas

Backend, con el entorno virtual del repositorio y sin conexión a Supabase:

```powershell
.\backend\.venv\Scripts\python.exe backend\manage.py test --settings=config.test_settings contared_api
```

El perfil de pruebas usa SQLite en memoria y mocks donde se inspeccionan consultas; no toma credenciales PostgreSQL ni conecta a Supabase. Incluye el caso sintético INTELPRUEBA de 12 movimientos de Banco y 13 de Libros, importes `Decimal`, ocho coincidencias directas (el cheque 000120 incluido una sola vez), grupo de 300 contra 180 + 120, depósito por fecha/monto aunque cambie la referencia, diferencia de 20, comisión solo en Banco y cheque 000121 solo en Libros. También cubre candidatos ambiguos, límites de agrupación, tipos de transacción configurados/desconocidos, importación, autorización de sesión, consulta de cheques por corte, clasificación/reintentos, entrega, cobro exacto, rechazo de montos ambiguos, anulación con soporte e historial.

La administración de usuarios también prueba creación Auth confirmada, reutilización de perfiles y accesos pendientes, reintentos parciales sin duplicar identidades, permisos SV/GT, asignación temporal limitada al ámbito autorizado, bloqueo de operaciones hasta el primer cambio verificado y rechazo de reutilizar la contraseña temporal. Los tests usan únicamente credenciales y respuestas sintéticas; no modifican usuarios de Supabase.

### Verificar INTELPRUEBA desde la interfaz

Usa exclusivamente una instancia Supabase/PostgreSQL local o desechable y el backend apuntando a esa instancia; no uses registros reales. En la instancia de trabajo, las migraciones de comparación y Cheques ya están aplicadas: no las vuelvas a ejecutar. Prepara un usuario de Auth, su perfil y acceso a una empresa de prueba, un banco, una cuenta USD y una conciliación de septiembre de 2026 con corte al 30/09/2026.

Prepara dos archivos `.xlsx` con fechas, referencias guardadas como texto para conservar ceros iniciales, descripción y monto:

- Banco (12 filas): referencias `INT-001` a `INT-007` por 10, 20, 30, 40, 50, 60 y 70 el 01/09; cheque y referencia `000120` por 100 el 01/09; referencia `000003` por 300 el 03/09; depósito `009004` por 220 el 07/09; referencia `000005` por 450 el 05/09; comisión `000006` por 25 el 06/09 (como salida, monto normalizado −25).
- Libros (13 filas): las siete referencias `INT-001` a `INT-007` por los mismos importes del 01/09; cheque `000120` por 100 el 01/09; dos partidas `000003` por 180 y 120 el 03/09; depósito `008004` por 220 el 07/09; referencia `000005` por 430 el 05/09; cheque `000121` por 100 el 09/09.

En la pantalla principal de **Conciliaciones**, selecciona la cuenta y septiembre de 2026 y agrega ambos archivos al panel **Cargar documentos**; se inspeccionan automáticamente. Revisa las dos tarjetas y usa **Procesar y conciliar**; si el reconocimiento no es concluyente, usa **Revisar configuración** solo en esa tarjeta. El cheque 000121 debe quedar como movimiento sin contraparte, no clasificado como retenido o entregado. Guarda el perfil de Banco con el catálogo `{"CQ":"Cheque cobrado","DE":"Depósito","ND":"Nota de débito"}` si quieres verificar las interpretaciones de esos códigos; no se aplican a otros perfiles. Pulsa **Comparar movimientos** y comprueba las ocho coincidencias directas, el grupo `000003`, la posible coincidencia del depósito, la diferencia `000005` por 20, la comisión bancaria y el cheque `000121` sin contraparte. El cheque 000120 debe aparecer una sola vez. Confirma solo una coincidencia igualada, descarta otra propuesta y vuelve a comparar: la confirmación no debe duplicarse y el descarte debe seguir visible en **Descartadas**.

Para verificar la importación independiente de cheques, en esa misma instancia aislada aplica `202611010001_cheques_independientes.sql` solo después de comprobar y aplicar una vez sus prerequisitos. Descarga la plantilla propuesta desde el panel, completa un cheque pendiente con estado explícito, importa el archivo y comprueba que aparece una versión en historial sin que se cree una carga ni un movimiento de Libros. Reintenta el mismo archivo (no debe duplicar versión/cheque), luego sube una versión modificada y verifica el historial y los eventos de estado. Importa una emisión de Libros con número, fecha e importe coincidentes para asociarla, y una salida de Banco coincidente para revisar la confirmación de cobro. El estado operativo solo acepta Retenido o En circulación en esta importación; archivos reales con cobrados/anulados o formatos distintos requieren adaptar y verificar el perfil primero.

En **Cheques**, comprueba que `000120` se presenta como **Cobrado** si su cruce 1:1 cumple la evidencia del backfill de la migración; no debe crearse otro cruce. Busca `000121`, clasifícalo como **Retenido** y registra después una fecha de entrega confirmada para que cambie a **En circulación**. No marques el cheque como entregado solo por no ver una salida bancaria.

Para comprobar pendientes entre meses, selecciona octubre de 2026 en **Cargar documentos**, agrega un archivo de Banco con número de cheque `000121`, fecha posterior a la emisión, salida normalizada `-100` y su referencia original. Reconoce y procesa el documento. En Cheques, elige **Pendientes de períodos anteriores** y confirma el cobro contra esa salida. Comprueba que se registra un cruce en octubre sin duplicar la emisión ni alterar los libros. Vuelve a la conciliación de septiembre y consulta al corte 30/09: el cheque debe conservar el estado que tenía entonces, mientras el historial de octubre muestra el cobro, su usuario, fecha y cruce.

Prueba por separado dos cheques de igual monto con números distintos, números repetidos en la misma cuenta y en cuentas distintas, dos salidas bancarias candidatas, falta de motivo/soporte de anulación y acceso con roles Consulta/Auxiliar/Revisor/Administrador. No uses cuentas ni archivos reales.

Para comprobar reportes, abre **Descargar reporte**, deja seleccionados los cuatro grupos y descarga Excel y PDF. El libro debe tener una hoja **Resumen** y una hoja por grupo; el grupo `000003` debe mostrar las tres partidas entre Banco y Libros, y la referencia `000120` conservar sus ceros iniciales. Comprueba en ambos formatos el estado de la propuesta descartada, su motivo, los conteos y que aparezca también en Diferencias cuando aplique. Repite con **Pestaña actual** en cada pestaña y con selecciones parciales de **Todo el proceso**; el backend exporta el conjunto completo de cada grupo y deja explícito que no aplica filtros de búsqueda o paginación.

Para comprobar la conexión real de solo lectura, con los `.env` configurados, inicia el backend y visita `http://localhost:8000/api/v1/status/`. Esto solo ejecuta `SELECT 1`. `/api/v1/session/` requiere un token de usuario obtenido al iniciar sesión.

El motor usa igualdad decimal exacta, no modifica movimientos, limita los grupos a tres partidas y acota la exploración de candidatos ambiguos. La comparación y la confirmación son operaciones separadas.

### Ejecución y limitaciones de esta entrega

- Backend: `.\backend\.venv\Scripts\python.exe backend\manage.py test --settings=config.test_settings contared_api` ejecutó **178 pruebas, todas aprobadas**, en SQLite en memoria con mocks; no conectó a PostgreSQL ni Supabase.
- Frontend: `npm run build` pasó (TypeScript y Vite).
- Contrato del historial y contadores del frontend: `npm test` ejecutó **3 pruebas, todas aprobadas**.
- No se ejecutó SQL contra Supabase ni se expusieron credenciales.
- No había herramientas PostgreSQL locales detectables en este entorno. Por ello no se verificaron contra PostgreSQL aislado las restricciones de la migración, bloqueos/transacciones simultáneas, reintentos, ni persistencia real de propuestas/descartes/cheques. La verificación visual autenticada confirmó el panel en Conciliaciones aun sin conciliaciones activas; no se subieron archivos ni se procesaron datos porque no se confirmó que la sesión usara una instancia aislada.
- No se incluyeron libros reales de un banco o de un registro de cheques entre los archivos del repositorio. La plantilla de cheques es propuesta y el perfil configurable necesita verificarse con el formato real antes de uso operativo.
- Se generaron reportes con movimientos sintéticos y se abrieron visualmente el PDF y el libro Excel (mediante la vista PDF de impresión de Excel); se comprobaron el ajuste de texto, las tablas entre páginas y los encabezados repetidos. Esto no sustituye la descarga con una sesión autenticada conectada a PostgreSQL aislado.
- La comparación ya está habilitada en la base indicada por el usuario; no vuelvas a aplicar su migración.
- Los reportes usan la estructura ya existente de `contared.reportes`; no requieren una migración nueva. La dependencia `reportlab` genera PDF y `openpyxl` genera Excel. La descarga necesita configuración del backend para el bucket privado.
- El usuario confirmó que Cheques ya tiene aplicada `supabase/migrations/202610300001_cheques.sql`; no la vuelvas a ejecutar. Si el esquema requerido falta realmente, el backend devuelve `cheques_migration_required`; un error de columna en una consulta se registra como fallo interno y no recomienda repetir la migración. Los tests con SQLite/mocks no verifican que el DDL, RLS o los bloqueos se comporten en PostgreSQL.
- Historial devuelve y consume `events` con validación de estructura; una lista vacía se distingue de un error o respuesta inválida. Ejecuta las pruebas frontend con `Set-Location frontend; npm test`.

## Esquema y habilitación del primer administrador

La migración documenta el esquema privado y sus tablas. Las consultas de sesión usan los campos declarados en `usuarios`, `accesos`, `empresas` y `paises`. No se crearon modelos Django ni se ejecutó SQL de migración.

El administrador ya debe existir en Supabase Auth y estar vinculado explícitamente a `usuarios.auth_user_id`, con `usuarios.activo = true` y accesos explícitos en `accesos`. El backend nunca crea perfiles ni concede permisos al iniciar sesión.

## Migración adicional de Cheques (aplicada)

El usuario confirmó la aplicación de `supabase/migrations/202610300001_cheques.sql`, después de las migraciones inicial y de comparación. **No vuelvas a ejecutar** ese archivo ni `supabase/migrations/202610290001_comparacion_movimientos.sql`. No ejecutes `manage.py migrate`.

La migración crea `contared.cheques` asociado al movimiento de emisión en Libros y `contared.cheque_eventos` como historial inmutable, con restricciones, índices, claves foráneas, auditoría, RLS sin acceso directo a clientes y grants solo para `service_role`. Hace un backfill conservador de cheques solo desde cruces confirmados 1:1, número coincidente, importe exacto, fecha de banco posterior a la emisión y número no repetido entre movimientos validados de la cuenta. Las coincidencias no inequívocas quedan para revisión manual. No altera movimientos ni crea ajustes.

## Seguimiento de Depósitos y reportes de conciliación

La migración `supabase/migrations/202610300002_seguimiento_depositos.sql` es adicional y **el estado documentado en esta entrega indica que no se había ejecutado**. Este repositorio no verifica el registro actual de ninguna instancia: antes de usar el seguimiento, consulta `contared.migraciones`; si falta, aplica el archivo completo una sola vez mediante el mecanismo SQL aprobado para esa instancia. No vuelvas a ejecutar las migraciones inicial, de comparación ni de Cheques. No uses `manage.py migrate` ni ejecutes archivos de migración contra Supabase desde este repositorio.

El resumen, Pendientes e Inicio consultan datos persistidos del ámbito autorizado. Los reportes del resumen permanecen como **Borrador**; no habilitan ajustes contables definitivos ni cierre. Una resolución de depósito documenta el seguimiento y su evidencia, pero no cambia movimientos ni afirma que se corrigió un sistema contable externo.

## Saldos detectados, catálogo bancario y ambigüedades

La migración `supabase/migrations/202611030002_saldos_detectados_conciliacion.sql` está pendiente de aplicación. Añade campos separados para los saldos finales detectados y su procedencia; conserva sin sobrescribir los saldos capturados manualmente. Su backfill toma la última fila con saldo acumulado de las cargas validadas existentes, sin volver a insertar movimientos, y omite conciliaciones archivadas. Aplicarla una sola vez, después de verificar `contared.migraciones` y de confirmar que sus prerequisitos están aplicados. No se ejecutó en este desarrollo.

El resumen muestra saldo manual, saldo detectado, valor utilizado y archivo/hoja/fila/método de detección. Si manual y detectado difieren, mantiene el manual y advierte el conflicto. Puede presentar la diferencia de saldos finales sin exigir saldo inicial; la diferencia no se trata como ajuste automático ni como cierre.

Para Banco Industrial de Guatemala, el catálogo se basa en la leyenda de la hoja de estado de cuenta: CQ = Pago de Cheque, NC = Nota de Crédito, ND = Nota de Débito (y DE = Depósito); se limita a ese banco. En filas CQ, No. Doc se conserva como referencia y se proyecta como número de cheque candidato cuando es un identificador válido; 0 y vacío no se usan para enlazar automáticamente. No se infiere que el cheque fue entregado o está en circulación.

Las propuestas ambiguas se presentan como opciones alternativas con su evidencia, no como una suma del conjunto de candidatos ni como diferencia de monto. Solo las agrupaciones no ambiguas muestran totales de grupo. La ambigüedad no se clasifica automáticamente como depósito mal operado.

## Eliminar y archivar conciliaciones

La migración `supabase/migrations/202610300003_archivo_conciliaciones.sql`, la migración `202610300004_eliminacion_definitiva_conciliaciones.sql` y la nueva `202611010001_cheques_independientes.sql` quedan pendientes de aplicación; esta entrega no ejecutó migraciones ni borró datos. La migración `202610300004` requiere primero comparación, Cheques, Depósitos y archivo; `202611010001` requiere la migración base de Cheques `202610300001` y el esquema de archivo usado por el backend. `202611010001` permite conservar cheques independientes con período, añade registro inmutable de importaciones y snapshots por versión, y no enlaza los snapshots con una FK que impida purgar una conciliación. Antes de aplicar cada archivo, verifica su versión en `contared.migraciones` y aplica cada migración completa una sola vez mediante el mecanismo SQL aprobado para esa instancia. No ejecutes migraciones desde pruebas, Django ni desde este repositorio.

En **Conciliaciones**, el filtro **Activas / Archivadas / Todas** consulta los registros del ámbito seleccionado; **Abrir** permite consultar una conciliación archivada en modo de solo lectura. Archivar sigue conservando los datos y no libera la combinación de cuenta y período.

Solo un administrador puede usar **Eliminar definitivamente**. La interfaz consulta primero la vista previa del backend, identifica banco, cuenta, período e ID, muestra cantidades por tipo y los bloqueos entre períodos, y exige motivo, frase exacta y confirmación de irreversibilidad. El backend vuelve a validar el ámbito y el rol, bloquea la conciliación y recalcula referencias en la misma transacción de purga. No clasifica una conciliación como prueba por el nombre ni elimina otros períodos. Las propuestas pendientes, cheques abiertos y depósitos abiertos de la conciliación seleccionada sí forman parte de la limpieza. Si existen cobros, resoluciones, movimientos compartidos u otras referencias desde/hacia otro período, la operación se bloquea.

La purga PostgreSQL es atómica. Conserva empresa, usuarios, permisos, bancos, cuentas y configuración reutilizable; elimina registros dependientes, reportes/snapshots y copias de auditoría ligadas a las filas purgadas. Los snapshots y originales de importaciones independientes de cheques no pertenecen a la conciliación y se conservan; su referencia al cheque operativo es histórica y no bloquea la purga. Deja un comprobante mínimo con ID, actor, fecha, motivo y cantidades. Los objetos privados de la conciliación se borran después del commit PostgreSQL: si Storage no responde, la conciliación ya no existe en la base pero la interfaz informa que la limpieza sigue pendiente. Los administradores pueden reintentar desde la alerta persistente; el sistema no comunica limpieza completa hasta verificar cero archivos pendientes de esa conciliación. El cambio en los triggers de inmutabilidad solo permite borrar las filas exactas incluidas en una lista temporal creada por el backend para esa conciliación; no desactiva triggers o claves foráneas globalmente ni expone una función privilegiada al navegador.

Para desplegar la función, aplica manualmente `202610300004_eliminacion_definitiva_conciliaciones.sql` después de sus prerequisitos y reinicia/despliega el backend y frontend de esta entrega. No pruebes estas acciones con registros financieros reales.

## Pendientes de etapas futuras

- Verificar con PostgreSQL aislado las consultas, restricciones, confirmaciones simultáneas, reintentos, persistencia, acceso privado a soportes y permisos por rol/empresa/país. Las pruebas de esta entrega usan `backend/.venv`, `config.test_settings`, SQLite en memoria y mocks; no verifican el DDL, RLS ni triggers en PostgreSQL.
- Implementar ajustes contables explícitos y cierre definitivo en una etapa futura. La anulación operativa de cheques no registra una reversión contable.
- La detección de duplicados manual/importado bloquea coincidencias exactas de número, fecha e importe para revisión; no hace una fusión automática de registros ni corrige movimientos manuales inmutables.

## Fuentes de configuración

- [Supabase: JSON Web Tokens](https://supabase.com/docs/guides/auth/jwts)
- [Supabase: JWT Signing Keys](https://supabase.com/docs/guides/auth/signing-keys)
