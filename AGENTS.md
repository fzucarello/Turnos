# Contexto funcional y límites

- `app.py` es un bot OSEP probado. Su scraping, selectores, búsqueda y reserva
  son sensibles. No modificarlos sin un fallo concreto y una razón funcional.
- GitHub Actions ejecuta Python + Playwright + Chromium. Cloudflare programa
  y dispara `.github/workflows/osep.yml` con `workflow_dispatch`, y entrega
  credenciales exclusivamente al Run autorizado mediante OIDC y D1.
- Cloudflare es el scheduler; `on.schedule` de GitHub fue retirado tras comprobar
  un dispatch real y el step Run bot completo en DRY_RUN. Consultar
  `infrastructure/scheduler/VERIFICACION.md` y comprobar MODE/cron reales.
- No reintroducir `on.schedule` junto con Cloudflare productivo.
- Conservar `concurrency.group=osep-bot` y `cancel-in-progress=false`.
- La configuración operativa vive sólo en Cloudflare. No agregar valores de búsqueda,
  MODE, frecuencia ni horario a vars de wrangler.jsonc o al workflow GitHub.
  keep_vars=true y los uploads API deben preservar plain_text y secret_text.
  El cron técnico fijo despierta cada minuto; INTERVAL_MINUTES y SCHEDULE_START/END
  determinan los disparos en hora de Mendoza. .dev.vars.example sólo sirve localmente.
- El repositorio es público. Nunca publicar criterios reales, datos del paciente,
  confirmaciones ni trazas del portal en archivos, inputs, logs, Summary o artifacts.
- GitHub recibe sólo request_id aleatorio y dry_run. config_json y config_sha256
  permanecen en Cloudflare D1. config_snapshot.py sólo valida en memoria.
- run_with_credentials.py captura stdout/stderr del bot y elimina canales de
  persistencia de Actions del hijo. El detalle va a POST /result autenticado.
  No ejecutar app.py directamente desde Actions ni reenviar sus mensajes.
- `OSEP_USER`, `OSEP_PASS` y `GITHUB_TOKEN` quedan en Cloudflare como Secret.
  Nunca leer ni imprimir sus valores mediante herramientas o documentos.
  run_with_credentials.py obtiene OSEP_USER/PASS con OIDC y sólo los entrega
  al entorno del proceso hijo. No usar inputs, GITHUB_ENV ni archivos para secretos.
  El endpoint /credentials exige firma GitHub, audiencia exacta, IDs inmutables,
  workflow osep.yml en main, primer intento y Run aceptado reciente en D1.
  No ampliar esta política mediante variables operativas ni omitir su verificación.
  GitHub Secrets antiguos se conservan para reversión, pero el workflow no los usa.
- Para pruebas reales usar el input booleano `dry_run=true`, verificado en el
  código existente: se detiene antes del Enter que confirma la reserva.
- Un timeout/5xx del POST puede ocultar un Run aceptado: nunca reenviar el mismo
  slot sin conciliar los Runs y la tabla D1. No borrar el registro para forzar reintentos.
- Explicar cambios y pruebas en español. No confundir pruebas locales con evidencia productiva.
