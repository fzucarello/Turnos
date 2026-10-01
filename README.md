# Turnos OSEP

Repositorio público de código. La configuración de búsqueda, las credenciales y
el detalle de resultados se administran exclusivamente en Cloudflare.

[Configurar el bot en Cloudflare](https://dash.cloudflare.com/bf1a13a86a0c4508e22f959c8454e963/workers/services/view/osep-scheduler/production/settings).
[Guía de uso y consulta privada de resultados](infrastructure/scheduler/README.md).

MODE=paused pausa nuevas búsquedas. MODE=production las habilita. Frecuencia:
INTERVAL_MINUTES. Franja: SCHEDULE_START y SCHEDULE_END, en hora de Mendoza.
MODE=validate y un VALIDATION_ID nuevo hacen una prueba sin reservar.
No publicar valores reales de esos parámetros en GitHub.

GitHub Actions recibe sólo un identificador aleatorio y una opción de prueba.
Cloudflare entrega la configuración y credenciales al Run autorizado por OIDC.
El bot las utiliza en memoria. Los mensajes del portal se capturan y su detalle
se envía a Cloudflare D1 por una conexión autenticada. Los registros públicos
muestran únicamente mensajes generales y datos técnicos de la ejecución.

[Estado general de Actions](https://github.com/fzucarello/Turnos/actions/workflows/osep.yml).

Las horas de ejecución y el estado técnico de Actions siguen siendo públicos.
Los administradores de GitHub/Cloudflare y el proveedor del runner forman parte
del circuito de confianza. Esta protección no elimina copias externas anteriores.
