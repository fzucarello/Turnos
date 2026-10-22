# OSEP · Panel privado de control

URL protegida por Cloudflare Access: https://osep-admin.franchezuca.workers.dev/

## Estado del despliegue (22/10/2026)

Código, migración D1 y pruebas de CI: terminados. Merge realizado en main.
**Pendiente de publicar:** la versión nueva de osep-admin y osep-scheduler. El panel visible de Cloudflare continúa siendo el de sólo lectura hasta realizar ese despliegue.

La API de Cloudflare rechazó conectar GitHub automáticamente: *This project is disconnected from your Git account*. Se requiere completar la autorización OAuth GitHub → Cloudflare Workers Builds desde el navegador.

Configuración de los dos Workers en Cloudflare → Workers & Pages → nombre del Worker → Settings → Builds → Connect:

- osep-admin: repositorio fzucarello/Turnos, branch main, root directory infrastructure/admin; build command npm run build; deploy command npm run deploy.
- osep-scheduler: mismo repo y branch, root directory infrastructure/scheduler; build command npm run check && npm test; deploy command npm run deploy. Desplegar el scheduler sólo cuando el panel esté listo y verificar luego los bindings existentes (GITHUB_TOKEN, OSEP_USER, OSEP_PASS y LEDGER). El Wrangler del scheduler tiene keep_vars=true.

Para el panel establecer en Cloudflare sus bindings de texto (nunca secretos públicos): TEAM_DOMAIN = https://franchezuca-osep.cloudflareaccess.com; POLICY_AUD = 4b3a4ab0786b5a848a8d15e56fae523cbaecf513faf40b5cf20d474d47831bde; ADMIN_EMAIL = franchezuca@gmail.com. La app Access preexistente se mantiene activa. Después de habilitar Builds, publicar y probar antes de iniciar reservas manuales.

## Componentes

- osep-admin: aplicación web responsive y API, con validación criptográfica del JWT Cloudflare Access, audiencia y correo autorizado.
- D1 osep-scheduler-ledger: configuración versionada en control_config, búsquedas manuales en manual_queue y resultados históricos en dispatches.
- osep-scheduler: cron de Cloudflare cada minuto; lee configuración D1 y dispara workflow_dispatch en GitHub.
- GitHub Actions: ejecuta el bot Python/Playwright, con credenciales y logs privados enviados a Cloudflare.
- app.py: mantiene la reserva y añade fallback opcional de centro y/o profesional; el filtro de centro se aplica sobre el texto completo de la columna de domicilio de atención.

## Uso

1. Búsqueda: seleccioná especialidad, zona, departamento, centro y profesional y guardá.
2. Cada centro/profesional puede ser obligatorio, preferido o libre.
3. Prueba sin reservar: crea un pedido manual seguro en cola que el scheduler inicia en el siguiente ciclo de un minuto; no confirma turnos.
4. Buscar y reservar: exige escribir RESERVAR. Puede confirmar un turno real.
5. Programación: ventana horaria Mendoza, frecuencia, pausa y reanudación de ejecuciones programadas. Las manuales pueden ejecutarse aun con programación pausada.
6. Ejecuciones: estado y log privado, vinculados a Runs de GitHub.

## Reglas y limitaciones

- La especialidad siempre es obligatoria, para impedir reservar una consulta distinta.
- Zona y departamento corresponden a selectores obligatorios de OSEP.
- El fallback preferido de centro o profesional se aplica cuando no aparecen filas con fechas válidas bajo la preferencia; si el único candidato falla más tarde dentro de la agenda, la búsqueda puede necesitar un segundo intento.
- Las listas de especialidad/zona/departamento son sugerencias editables; todavía no representan el catálogo vivo completo de OSEP.
- Los secretos OSEP_USER, OSEP_PASS y GITHUB_TOKEN siguen encriptados como Cloudflare Secrets del Worker osep-scheduler. Para rotarlos usá Workers & Pages → osep-scheduler → Settings → Variables and Secrets. Nunca se copian a D1 ni se exponen en la UI.
- El panel está protegido con Cloudflare Access y valida el JWT firmado además de restringir al correo ADMIN_EMAIL almacenado como binding del Worker. El login Google se puede configurar al final mediante un IdP OAuth de Google y una política de acceso restringida.
- La API usa validación estricta, control de revisión concurrente y protección de origen/CSRF.
- La cola manual no ejecuta Python en Cloudflare: inicia el mismo GitHub Action mediante un request_id autorizado en D1.
- GitHub puede enviar emails por exit code 1 cuando se obtiene turno, como estrategia operativa preexistente.

## Despliegue y mantenimiento

- Migración: infrastructure/scheduler/migrations/0003_control_panel.sql
- Panel: node infrastructure/admin/build.mjs, desplegar Worker con infrastructure/admin/wrangler.jsonc.
- Scheduler: npm ci, npm run check, npm test, npm run build, npm run deploy en infrastructure/scheduler.
- La CI de la rama prueba Python, TypeScript, tests del scheduler, compilación de ambos Workers y rechazo de acceso anónimo sin llamar a OSEP.
- Para detener nuevas ejecuciones, Programación → Pausar; en emergencia MODE=paused en el Worker scheduler. Esto no cancela jobs existentes.
- Para rollback, volver al deployment anterior de osep-scheduler mediante Cloudflare. La estructura antigua de D1 y los secretos no se borran.
