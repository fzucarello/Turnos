# OSEP Scheduler Admin

Panel operativo privado para consultar las últimas ejecuciones del bot OSEP sin publicar el diagnóstico en GitHub Actions.

- URL: `https://osep-admin.franchezuca.workers.dev`
- Worker: `osep-admin`
- Fuente de datos: D1 `osep-scheduler-ledger`, binding `LEDGER`
- Acceso: Cloudflare Access; sólo miembros de la cuenta Cloudflare autorizada.
- Zona horaria mostrada: `America/Argentina/Mendoza`
- El panel es de sólo lectura y no dispara workflows ni modifica reservas.

El Worker productivo `osep-scheduler` sigue siendo el scheduler. Este panel es un Worker separado para reducir el riesgo de afectar la automatización existente.
