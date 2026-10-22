import { BOT_KEYS, botConfig, fingerprint, isBusinessSlot, isCatalogWindow, isComparisonWindow, schedule, text } from "./config.ts";
import type { BotConfig } from "./config.ts";
import { credentials } from "./credentials.ts";
const ACTIVE = ["queued", "in_progress", "waiting", "pending", "requested"];

function log(event: string, fields: Record<string, unknown> = {}) {
  console.log(JSON.stringify({ timestamp: new Date().toISOString(), event, ...fields }));
}

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

// Reintentar lecturas es seguro: nunca producen una reserva ni crean un Run.
async function read(url: string, headers: HeadersInit): Promise<Response> {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await fetch(url, { headers, signal: AbortSignal.timeout(10000), redirect: "manual" });
      if (response.ok) return response;
      log("github_read_response", { path: new URL(url).pathname, attempt: attempt + 1, http_status: response.status });
      const status = response.status;
      await response.body?.cancel();
      if (status !== 429 && status < 500) throw new Error(`github_read_http_${status}`);
      if (attempt === 2) throw new Error(`github_read_http_${status}`);
    } catch (error) {
      log("github_read_error", { path: new URL(url).pathname, attempt: attempt + 1,
        error_type: error instanceof Error && ["Error", "TypeError", "TimeoutError", "AbortError"].includes(error.name) ? error.name : "unknown",
        code: error instanceof Error && /^github_read_http_\d{3}$/.test(error.message) ? error.message : "read_exception" });
      // Los errores de permisos no mejoran repitiendo la llamada.
      if (error instanceof Error && /github_read_http_[34]/.test(error.message) && !error.message.endsWith("429")) throw error;
      if (attempt === 2) throw new Error(error instanceof Error && /^github_read_http_\d{3}$/.test(error.message) ? error.message : "github_read_unavailable");
    }
    await sleep(250 * (attempt + 1));
  }
  throw new Error("github_read_unavailable");
}

export async function dispatch(controller: Pick<ScheduledController, "cron" | "scheduledTime">, env: Env) {
  const { cron, scheduledTime } = controller;
  const workerMode = String(env.MODE);
  const stored = await env.LEDGER.prepare(
    "SELECT config_json,schedule_json,paused FROM control_config WHERE id=1"
  ).first<{config_json:string;schedule_json:string;paused:number}>();
  const catalogPlan = schedule(stored ? {
    SCHEDULE_START: JSON.parse(stored.schedule_json).start,
    SCHEDULE_END: JSON.parse(stored.schedule_json).end,
    INTERVAL_MINUTES: String(JSON.parse(stored.schedule_json).interval),
  } : env);
  // Prioridad: búsquedas manuales del usuario > búsquedas programadas > catálogo.
  const lastCatalog=workerMode==="production" ? await env.LEDGER.prepare(
    "SELECT MAX(updated_at) AS last FROM manual_queue WHERE kind='catalog_master' AND state='accepted'"
  ).first<{last:string|null}>() : null;
  // Evitar consultas masivas continuas al portal externo.
  const allowCatalog=isCatalogWindow(scheduledTime,catalogPlan) &&
    (!lastCatalog?.last || Date.now()-Date.parse(lastCatalog.last)>=5*60000);
  const allowComparison=isComparisonWindow(scheduledTime,catalogPlan);
  const manual = workerMode === "production"
    ? await env.LEDGER.prepare(
        "SELECT id,dry_run,kind,config_json FROM manual_queue WHERE state='pending' AND (kind NOT IN ('catalog','catalog_master','catalog_compare') OR (kind='catalog_compare' AND ?=1) OR (kind IN ('catalog','catalog_master') AND ?=1)) ORDER BY CASE WHEN kind='appointment' THEN 0 WHEN kind='catalog_compare' THEN 1 ELSE 2 END,created_at LIMIT 1"
      ).bind(allowComparison?1:0,allowCatalog?1:0).first<{id:string;dry_run:number;kind:string;config_json:string}>()
    : null;
  const mode = manual ? "manual" : workerMode;
  const context = { cron, scheduled_at: new Date(scheduledTime).toISOString(), mode };
  log("cron_received", context);
  if (mode === "paused" || (!manual && stored?.paused === 1)) { log("paused", context); return; }
  if (!["validate", "production", "manual"].includes(mode)) throw new Error("invalid_mode");
  const plan = catalogPlan;
  // El cron técnico sólo despierta cada minuto; los horarios efectivos vienen de Cloudflare.
  // Validate permite una prueba inmediata, incluso fuera de la franja, sin reservar.
  if (cron !== "* * * * *" || (mode === "production" && !isBusinessSlot(scheduledTime, plan)) ||
      Date.now() - scheduledTime > 5 * 60000 || scheduledTime - Date.now() > 60000) {
    log("slot_ignored", context); return;
  }
  if (!env.GITHUB_TOKEN) throw new Error("missing_GITHUB_TOKEN");
  if (mode === "validate" && !/^[a-zA-Z0-9_-]{1,80}$/.test(text(env, "VALIDATION_ID"))) throw new Error("config_invalid_VALIDATION_ID");
  let config: BotConfig;
  try {
    const legacy = Object.fromEntries(BOT_KEYS.map(k => [k, env[k as keyof Env]]));
    legacy.OBJ_DOMICILIO_FLEXIBLE ??= "false";
    legacy.OBJ_PROFESIONAL_FLEXIBLE ??= "false";
    const source = manual ? JSON.parse(manual.config_json)
      : stored ? JSON.parse(stored.config_json) : legacy;
    source.OBJ_FECHA_DESDE ??= "";
    source.OBJ_EXCLUSIONES_SEMANA ??= "";
    source.OBJ_HORA_OBJETIVO ??= "";
    source.OBJ_HORARIOS_SEMANA ??= "";
    source.OBJ_CATALOGO = (manual?.kind === "catalog" || manual?.kind === "catalog_master" || manual?.kind === "catalog_compare") ? "true" : "false";
    if(manual?.kind==="catalog_compare"){
      source.OBJ_ZONA="*"; source.OBJ_DEPTO="__CATALOG_COMPARE__";
    }
    if (manual) source.DRY_RUN = manual.kind==="catalog_compare" || manual.dry_run === 1 ? "true" : "false";
    config = botConfig(source, mode === "validate");
  }
  catch (error) {
    const code = error instanceof Error && /^config_invalid_/.test(error.message) ? error.message : "config_invalid";
    log("config_error", { ...context, code }); throw new Error(code);
  }
  const configJson = JSON.stringify(config);
  const configSha256 = await fingerprint(configJson);
  // El identificador público es aleatorio; no permite adivinar filtros por su hash.
  const requestId = crypto.randomUUID();

  // Un INSERT atómico gana; los demás intentos del mismo slot no llaman a GitHub.
  // En validación todos los ticks usan una sola clave, por lo que la prueba ocurre una vez.
  const slot = manual ? (manual.kind==="catalog_master" ? `manual:${manual.id}:${Math.floor(scheduledTime/60000)}` : `manual:${manual.id}`) : mode === "validate" ? `validation:${env.VALIDATION_ID}` : `cron:${Math.floor(scheduledTime / 60000)}`;
  const result = await env.LEDGER.prepare(
    "INSERT INTO dispatches(slot,scheduled_at,cron,mode,state,updated_at,config_sha256,request_id,config_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(slot) DO NOTHING",
  ).bind(slot, context.scheduled_at, cron, mode, "claimed", new Date().toISOString(), configSha256, requestId, configJson).run();
  if (result.meta.changes !== 1) { log("duplicate_skipped", { ...context, slot }); return; }
  if (manual) await env.LEDGER.prepare(
    "UPDATE manual_queue SET state='dispatching',updated_at=? WHERE id=? AND state='pending'"
  ).bind(new Date().toISOString(),manual.id).run();

  let postStarted = false;
  async function record(state: string, status: number | null = null, runId: number | null = null) {
    await env.LEDGER.prepare("UPDATE dispatches SET state=?,http_status=?,run_id=?,updated_at=? WHERE slot=?")
      .bind(state, status, runId, new Date().toISOString(), slot).run();
    if (manual) await env.LEDGER.prepare(
      "UPDATE manual_queue SET state=?,updated_at=?,run_id=?,error_code=? WHERE id=?"
    ).bind(
      state === "busy_skipped" ? (manual.kind==="catalog_master"?"pending":"busy")
      : state === "accepted" ? "accepted"
      : state === "rejected" ? "rejected"
      : state === "uncertain" ? "uncertain" : "dispatching",
      new Date().toISOString(),runId,state === "rejected" ? String(status) : null,manual.id
    ).run();
    log(state, { ...context, slot, http_status: status, run_id: runId });
  }
  const owner = encodeURIComponent(env.GITHUB_OWNER);
  const repo = encodeURIComponent(env.GITHUB_REPO);
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW);
  const base = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}`;
  const headers = {
    "Accept": "application/vnd.github+json", "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "User-Agent": "osep-cloudflare-scheduler", "X-GitHub-Api-Version": "2026-03-10",
  };
  try {
    // No habilitar producción mientras siga declarado el cron anterior en main.
    // El repositorio es público: esta lectura no recibe el token.
    if (mode === "production" || mode === "manual") {
      const response = await read(`https://raw.githubusercontent.com/${owner}/${repo}/${encodeURIComponent(env.GITHUB_REF)}/.github/workflows/${workflow}`, { "Cache-Control": "no-cache" });
      const config = await response.text(); // Archivo conocido y pequeño del repositorio configurado.
      if (/^\s+schedule\s*:/m.test(config)) throw new Error("github_schedule_still_present");
    }
    const checks = await Promise.all(ACTIVE.map(async status => {
      const response = await read(`${base}/runs?status=${status}&per_page=1`, headers);
      const data: unknown = await response.json();
      if (!data || typeof data !== "object" || !("workflow_runs" in data) ||
          !Array.isArray(data.workflow_runs)) throw new Error("invalid_runs_response");
      return data.workflow_runs.length > 0;
    }));
    if (checks.some(Boolean)) { await record("busy_skipped"); return; }

    await record("sending"); // Queda evidencia antes de enviar una operación no idempotente.
    postStarted = true;
    const dryRun = config.DRY_RUN === "true";
    log("github_dispatch_attempt", { ...context, slot, dry_run: dryRun, config_sha256: configSha256,
      interval_minutes: plan.interval });
    const response = await fetch(`${base}/dispatches`, {
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(20000),
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ ref: env.GITHUB_REF, inputs: { dry_run: dryRun ? "true" : "false", request_id: requestId } }),
    });
    // La API actual devuelve 200 con ID; se admite también el antiguo 204.
    if (response.status === 200 || response.status === 204) {
      let runId: number | null = null;
      if (response.status === 200) {
        try {
          const data: unknown = await response.json();
          runId = data && typeof data === "object" && "workflow_run_id" in data &&
            typeof data.workflow_run_id === "number" ? data.workflow_run_id : null;
        } catch { /* HTTP aceptado, aunque el cuerpo no pueda leerse. No reenviar. */ }
      }
      await record("accepted", response.status, runId);
    } else {
      const status = response.status;
      await response.body?.cancel();
      await record(status >= 500 ? "uncertain" : "rejected", status);
      throw new Error(`github_dispatch_http_${status}`);
    }
    // Los criterios sólo se entregan durante 20 minutos. Detalles privados: 30 días.
    await env.LEDGER.prepare("UPDATE dispatches SET config_json=NULL WHERE scheduled_at < ? AND config_json IS NOT NULL")
      .bind(new Date(Date.now() - 20 * 60000).toISOString()).run();
    await env.LEDGER.prepare("UPDATE dispatches SET bot_log=NULL WHERE scheduled_at < ? AND bot_log IS NOT NULL")
      .bind(new Date(Date.now() - 30 * 86400000).toISOString()).run();
    await env.LEDGER.prepare("DELETE FROM dispatches WHERE scheduled_at < ?")
      .bind(new Date(Date.now() - 90 * 86400000).toISOString()).run();
  } catch (error) {
    // No copiar cuerpos HTTP, cabeceras ni excepciones externas a los logs.
    // No se repite el POST: un timeout/5xx podría ocultar un Run ya creado.
    const code = error instanceof Error && /^(github_|invalid_runs_response)/.test(error.message)
      ? error.message : "integration_error";
    log("dispatch_error", { ...context, slot, code, post_started: postStarted });
    // Conservar los estados accepted/rejected si ya hubo respuesta; claimed/sending
    // permiten identificar las interrupciones de proceso incluso si D1 falla.
    await env.LEDGER.prepare("UPDATE dispatches SET state=?,updated_at=? WHERE slot=? AND state IN ('claimed','sending')")
      .bind(postStarted ? "uncertain" : "preflight_failed", new Date().toISOString(), slot).run();
    if (manual) await env.LEDGER.prepare(
      "UPDATE manual_queue SET state=?,updated_at=?,error_code=? WHERE id=?"
    ).bind(postStarted ? "uncertain" : "error",new Date().toISOString(),code,manual.id).run();
    throw new Error(code);
  }
}

export default {
  async scheduled(controller, env) { controller.noRetry(); await dispatch(controller, env); },
  // El endpoint sólo entrega secretos a un Run autenticado; nunca dispara ni reserva.
  async fetch(request, env) { return credentials(request, env); },
} satisfies ExportedHandler<Env>;

