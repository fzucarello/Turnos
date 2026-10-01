# Configuración central de Turnos OSEP

**Toda la configuración operativa se modifica en Cloudflare**, en
[osep-scheduler → Settings](https://dash.cloudflare.com/bf1a13a86a0c4508e22f959c8454e963/workers/services/view/osep-scheduler/production/settings)
→ **Runtime variables and secrets** (enlaces laterales: Variables and secrets).
GitHub ejecuta el bot con los valores recibidos.
No hay que editar el workflow ni copiar cambios al repositorio.

## Cómo cambiar un valor

1. Abrir el enlace anterior e iniciar sesión en Cloudflare.
2. En **Variables and Secrets**, buscar el nombre exacto de la variable.
3. Elegir su opción de edición, conservar tipo **Text** y cambiar su valor.
4. Pulsar **Deploy** para aplicar. **Save version** sólo guarda una versión sin
   activarla. Escribir valores sin comillas.
5. Comprobar que la pantalla muestre el nuevo valor.

Para cambiar varios criterios, primero poner MODE=paused y desplegar.
Editar todos los criterios, comprobarlos y finalmente volver a MODE=production.
Esto evita una búsqueda con una combinación parcial de los cambios.
Una ejecución ya iniciada usa su instantánea anterior hasta terminar; no se cancela.

## Pausa, frecuencia y horario de funcionamiento

| Variable | Qué escribir | Efecto |
| --- | --- | --- |
| MODE | paused | Suspende nuevas búsquedas; no cancela una que ya empezó. |
| MODE | production | Habilita búsquedas en la frecuencia y franja configuradas. |
| INTERVAL_MINUTES | Entero entre 1 y 1440, por ejemplo 10, 15, 20, 30 | Minutos entre búsquedas, contados desde SCHEDULE_START. |
| SCHEDULE_START | 09:00, por ejemplo | Primer horario del día de búsqueda, hora de Mendoza. |
| SCHEDULE_END | 17:00, por ejemplo | Límite inclusive de esa franja, hora de Mendoza. |
| DRY_RUN | true o false | true busca sin confirmar una reserva; false permite reservar. |

Ejemplo ficticio: inicio 09:00, fin 17:00 e intervalo 10 produce 49 horarios.
Con intervalo 20 produce 25 horarios. La franja también admite cruzar medianoche.
El fin es un límite: si no coincide con el intervalo, no se agrega un disparo extra.
Por ejemplo, inicio 09:10, fin 09:30 e intervalo 7 dispara 09:10, 09:17 y 09:24.
Si inicio y fin son iguales, sólo hay un horario diario.
Para abarcar todo el día usar inicio 00:00 y fin 23:59.

**No editar Cron Triggers para cambiar frecuencia.** El único cron técnico es
`* * * * *`: despierta al Worker cada minuto. El Worker sólo consulta GitHub
cuando corresponde buscar. En pausa y fuera de los horarios no llama a GitHub.
No hay conversiones UTC para el usuario. El reloj usa America/Argentina/Mendoza.
Un cron no garantiza la hora exacta en que GitHub asigna un runner.
Si hay un Run activo o pendiente, ese horario se omite sin acumular atrasos.

## Criterios del turno

Todos conservan la lógica del bot existente. El nombre debe ser exacto.
Las horas de esta tabla son **horas del turno deseado**, diferentes de la franja
de funcionamiento del bot. Para quitar un filtro opcional puede usarse false
en los campos que lo admiten; en días dejar el valor vacío.

| Variable | Formato / significado |
| --- | --- |
| OBJ_SERVICIO | Especialidad exacta como aparece en OSEP. Obligatorio. |
| OBJ_ZONA | Zona exacta del portal. Obligatorio. |
| OBJ_DEPTO | Departamento exacto del portal. Obligatorio. |
| OBJ_MEDICO | Profesional a seleccionar en el buscador del portal. false para cualquiera. |
| OBJ_PROFESIONAL | Texto adicional para filtrar la columna profesional de los resultados. Vacío o false para no filtrar. No confundir con OBJ_MEDICO. |
| OBJ_DOMICILIO | Texto que debe aparecer en el domicilio del resultado. Vacío o false para cualquiera. |
| OBJ_HORARIO_TURNO | Texto que debe aparecer en la descripción del horario del resultado. Vacío o false para no filtrar. |
| OBJ_DIAS_VALIDOS | Nombres completos separados por coma, por ejemplo LUNES,MIERCOLES,VIERNES. Vacío para cualquier día. |
| OBJ_FECHA_DISP | Fecha exacta DD-MM-AAAA, por ejemplo 15-10-2026. Vacío o false para próxima fecha disponible. |
| OBJ_HORA_MIN | Hora mínima del turno: 8:00 o 08:00. false equivale a 00:00. |
| OBJ_HORA_MAX | Hora máxima del turno: 17:00. false equivale a 23:59. |
| OBJ_HORA_PRIORIDAD | EARLIEST para la hora más temprana; LATEST para la más tardía, en el primer día disponible. |
| OBJ_HORA_FLEXIBLE | true permite otro horario si no hay en la franja; false exige la franja. |
| OBJ_DIA_FLEXIBLE | true permite otros días si no hay en los indicados; false exige los días indicados. |
| OBJ_FECHA_FLEXIBLE | true permite otra fecha si no hay la exacta; false exige la indicada, cuando se configuró una. |

**Atención a la flexibilidad:** configurar una hora o un día no los convierte
en límites estrictos si su correspondiente variable FLEXIBLE sigue en true.
Para exigir de lunes a viernes entre 08:00 y 12:00, usar esos días y horas,
OBJ_DIA_FLEXIBLE=false y OBJ_HORA_FLEXIBLE=false.
Los textos deben coincidir con el portal. Un formato válido no prueba que una
especialidad, departamento o profesional exista en OSEP.

## Prueba segura desde el mismo lugar

1. Poner MODE=paused y guardar/desplegar.
2. Configurar los criterios que se quieren comprobar.
3. Cambiar VALIDATION_ID por un identificador nuevo, sin espacios, por ejemplo
   prueba-20261001-01. Admitidos: letras, números, guion y guion bajo, hasta 80.
4. Poner MODE=validate y desplegar.
5. En el siguiente minuto se intentará **una única búsqueda sin reservar**, incluso
   fuera de la franja normal. No hace falta cambiar DRY_RUN: validate lo fuerza a true.
6. Abrir [GitHub Actions](https://github.com/fzucarello/Turnos/actions/workflows/osep.yml).
   La ejecución usa un identificador aleatorio. El paso Verify private Cloudflare
   runtime access comprueba configuración y credenciales sin publicar valores.
   Consultar el resultado detallado en Cloudflare D1, según las instrucciones abajo.
7. Al terminar, poner MODE=paused o MODE=production. Para otra prueba usar otro ID.

MODE validate no se repite automáticamente. Si falla o aparece busy_skipped,
investigar antes de usar otro ID. Ante sending/uncertain conciliar primero los Runs:
un timeout puede ocultar un disparo aceptado. No borrar la tabla para forzar reintentos.
No introducir configuración ni datos personales en inputs de GitHub, nombres
VALIDATION_ID o commits. Para probar, usar MODE=validate desde Cloudflare.
Un request_id manual no registrado con el Run aceptado en D1 no obtiene datos.

## Credenciales y variables técnicas

OSEP_USER y OSEP_PASS se guardan como **Secret en Cloudflare**, junto a los
criterios, pero sus valores no son visibles. Para cambiar usuario o contraseña:
pausar MODE, editar el Secret correspondiente, conservar tipo Secret, introducir
el nuevo valor y pulsar Deploy. Probar con MODE=validate y un VALIDATION_ID nuevo;
si el login funciona, volver a production. Nunca convertir credenciales a Text.
No copiar el nuevo valor a GitHub, al código o al chat.

GitHub obtiene ambos secretos al iniciar el paso Run bot por HTTPS, usando una
identidad temporal firmada (OIDC). Cloudflare exige el repositorio y propietario
por sus IDs inmutables, osep.yml en main, evento workflow_dispatch, runner de GitHub,
primer intento y un Run aceptado en D1 con el mismo identificador aleatorio, de hasta 20 minutos.
Tokens: firma RS256, emisor GitHub, audiencia exacta, expiración y antigüedad
máxima de cinco minutos. Otros repositorios, ramas o workflows no tienen acceso.
La respuesta no se cachea. No hay otra clave permanente entre GitHub y Cloudflare.

run_with_credentials.py enmascara los secretos antes de arrancar el bot y sólo
los entrega al entorno del proceso hijo: sin GITHUB_ENV, archivos, inputs ni
outputs. Si falla la autorización, la entrega o el login, el bot no reserva.
La función original de búsqueda/reserva no se modifica. Los secretos antiguos
de GitHub se conservan como respaldo; el workflow vigente no los utiliza.
Siempre se comprueba primero el acceso a credenciales, antes de instalar
Chromium. El paso Run bot las solicita otra vez, sin almacenarlas entre pasos.
El cliente HTTP se identifica como OSEP-GitHub-Actions; la identificación
predeterminada de Python fue rechazada por Cloudflare con 403 antes del Worker.
Los errores sólo muestran etapa/código HTTP o códigos fijos del verificador.
GITHUB_TOKEN sigue como **Secret** en Cloudflare, token fine-grained del
propietario fzucarello, sólo Turnos, Actions Read and write, Metadata read automático.
Nunca convertirlo a Text ni copiarlo a documentos, código, inputs o logs.
Los secretos fueron cargados por el usuario; no se recuperaron los valores de GitHub.

Estas variables son técnicas y habitualmente no se cambian:

| Variable | Valor / propósito |
| --- | --- |
| PORTAL_URL | https://www.osep.mendoza.gov.ar/webapp_pri; otro valor se rechaza. |
| HEADLESS | true para Chromium sin ventana visible en GitHub. |
| TIMEOUT_MS | 25000; espera del navegador, entero de 1000 a 120000 milisegundos. |
| STOP_AFTER_LOGIN | false; true prueba únicamente el acceso al portal. |
| GITHUB_OWNER / GITHUB_REPO | fzucarello / Turnos. |
| GITHUB_WORKFLOW / GITHUB_REF | osep.yml / main. |

## Arquitectura, auditoría y diagnóstico

Cloudflare guarda una instantánea privada y un ID aleatorio en D1, luego dispara
Actions con ese ID. El receptor entrega configuración y credenciales sólo al Run
autorizado. Python valida la instantánea y las entrega al entorno del proceso hijo.
El bot conserva su lógica de búsqueda y reserva.

El proceso hijo no recibe rutas para escribir Summary, GITHUB_ENV, outputs ni
credenciales de Actions. stdout y stderr se capturan en memoria, incluso las
anotaciones y excepciones. Nunca se reenvían mensajes del portal a GitHub.
Se conservan los últimos 32 KiB del diagnóstico, con indicador si se truncó;
se eliminan credenciales antes de enviarlo a Cloudflare. Un fallo de guardado
no publica el detalle como alternativa. Verificar OSEP antes de repetir si hay dudas.

Actions conserva los códigos originales: 0 normal, 1 turno informado por el bot,
3 error. El estado rojo y el correo de GitHub siguen pudiendo indicar un turno
o un error; el detalle se consulta en Cloudflare. No se altera la reserva.

### Consultar resultados privados, paso a paso

1. Entrar en [Cloudflare D1](https://dash.cloudflare.com/bf1a13a86a0c4508e22f959c8454e963/workers/d1).
2. Abrir la base **osep-scheduler-ledger**.
3. Elegir **Console** (Consola).
4. Pegar esta consulta y pulsar **Execute**:

```sql
SELECT scheduled_at, run_id, request_id, bot_status, bot_exit_code,
       bot_log_truncated, bot_completed_at
FROM dispatches ORDER BY scheduled_at DESC LIMIT 20;
```

Para ver el detalle de una ejecución, reemplazar NUMERO por su run_id y ejecutar:

```sql
SELECT bot_status, bot_log, bot_log_truncated, bot_completed_at
FROM dispatches WHERE run_id = NUMERO;
```

bot_status puede ser sin_turno, prueba_finalizada, turno_informado, error_bot,
error_inicio o timeout. turno_informado conserva el significado del código del bot;
si la confirmación no fue verificable, revisar el detalle y el portal OSEP.
Un resultado vacío indica que el Run sigue activo o no pudo enviar su detalle.
accepted sólo acredita aceptación por GitHub, no una reserva.

La consulta de configuración sólo está habilitada durante 20 minutos; se borra
la instantánea al guardar el resultado. Los registros de resultado se conservan
30 días y los metadatos técnicos, 90 días. La depuración ocurre con los nuevos
disparos aceptados: durante pausas extensas no hay borrado automático.
No hay un endpoint público de lectura de resultados.

[Worker](https://dash.cloudflare.com/bf1a13a86a0c4508e22f959c8454e963/workers/services/view/osep-scheduler/production)
→ Observability → Logs: paused, slot_ignored, config_invalid_NOMBRE,
busy_skipped, accepted y private_result_saved ayudan a ubicar un problema.
No publicar el contenido de consultas privadas en issues, commits o capturas.

## Mantenimiento y reversión

wrangler.jsonc contiene sólo infraestructura, cron técnico y keep_vars=true.
**No agregar vars con valores operativos:** un despliegue debe preservar las
variables de Cloudflare. Las actualizaciones por API deben usar keep_bindings
para plain_text y secret_text. La migración SQL 0002 agrega una columna sin
modificar registros anteriores. Registrar las migraciones aplicadas.

El archivo .dev.vars.example es únicamente un ejemplo local para generar tipos
y probar. Su copia .dev.vars está ignorada y no se despliega. No contiene secretos reales.

Con Node 24, desde esta carpeta:

```powershell
npm ci
Copy-Item .dev.vars.example .dev.vars
npm run types
npm run check
npm test
python -m unittest discover -s test -p "test_*.py" -v
npm run build
```

Para publicar con Wrangler autenticado: aplicar migraciones D1 pendientes y
ejecutar npm run deploy. Si se usa API, preservar todas las variables y secretos.
workers.dev está habilitado para POST /credentials y POST /result, que exige OIDC y el registro
D1 del Run. Estos endpoints no disparan búsquedas ni confirman reservas. El resto
de las rutas devuelve 404; GET de ambos endpoints devuelve 405. Previews deshabilitados.
Las URLs no contienen tokens ni secretos. No registrar cabeceras o respuestas.
No reintroducir on.schedule en GitHub.

Para aislar un problema: MODE=paused. Corregir el campo, probar con validate y
volver a production. Mantener D1 y secretos; no activar ambos schedulers.
No restaurar el workflow anterior que publicaba instantáneas o ejecutaba app.py
directamente. Para revertir una falla, mantener MODE=paused y restaurar juntos
una versión de Worker y workflow que conserve la protección de datos privados.
El respaldo histórico local .private-backups está excluido de Git y no se publica.
No borrar D1 ni secretos para forzar reintentos.

Fuentes: [Variables y keep_vars](https://developers.cloudflare.com/workers/wrangler/configuration/#source-of-truth),
[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/),
[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).

## Respaldo histórico privado

Las ejecuciones antiguas retiradas de GitHub tienen metadatos y los logs que seguían disponibles en la tabla privada legacy_runs. Los logs vencidos están identificados como expired. El historial anterior se conserva en el respaldo local .private-backups, excluido de Git. GitHub puede conservar versiones antiguas accesibles mediante enlaces directos; la limpieza del historial no elimina copias externas.
