import { createRemoteJWKSet, jwtVerify } from "jose";
import type { JWTVerifyGetKey } from "jose";
import { botConfig, fingerprint } from "./config.ts";

// Política de confianza fija: los criterios operativos no pueden ampliar el acceso.
export const CREDENTIALS_URL = "https://osep-scheduler.franchezuca.workers.dev/credentials";
export const RESULT_URL = "https://osep-scheduler.franchezuca.workers.dev/result";
export const ISSUER = "https://token.actions.githubusercontent.com";
const REPOSITORY = "fzucarello/Turnos";
const WORKFLOW = `${REPOSITORY}/.github/workflows/osep.yml@refs/heads/main`;
const SUBJECTS = ["repo:fzucarello/Turnos:ref:refs/heads/main",
  "repo:fzucarello@241853003/Turnos@1089108669:ref:refs/heads/main"];
// Sólo se cachean claves PÚBLICAS de GitHub; nunca tokens ni credenciales.
const githubKeys = createRemoteJWKSet(new URL(`${ISSUER}/.well-known/jwks`), {
  timeoutDuration: 5000, cooldownDuration: 30000, cacheMaxAge: 600000,
});

function reply(status: number, data: unknown) {
  return Response.json(data, { status, headers: {
    "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
  } });
}

async function smallBody(request: Request, limit: number): Promise<string> {
  if (!request.body) throw new Error("missing_body");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) { await reader.cancel(); throw new Error("body_too_large"); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes);
}

export async function credentials(request: Request, env: Env, keys: JWTVerifyGetKey = githubKeys): Promise<Response> {
  const url = new URL(request.url);
  const resultRequest = url.href === RESULT_URL;
  if (url.href !== CREDENTIALS_URL && !resultRequest) return reply(404, { error: "not_found" });
  if (request.method !== "POST") return reply(405, { error: "method_not_allowed" });
  const authorization = request.headers.get("Authorization") ?? "";
  if (!/^Bearer [A-Za-z0-9._-]{1,8192}$/.test(authorization)) return reply(401, { error: "unauthorized" });
  try {
    const { payload } = await jwtVerify(authorization.slice(7), keys, {
      algorithms: ["RS256"], issuer: ISSUER, audience: CREDENTIALS_URL,
      requiredClaims: ["exp", "iat", "nbf", "sub", "run_id", "run_attempt"],
      maxTokenAge: "5 minutes", clockTolerance: 5,
    });
    if (payload.repository !== REPOSITORY || payload.repository_id !== "1089108669" ||
        payload.repository_owner_id !== "241853003" || payload.ref !== "refs/heads/main" ||
        payload.workflow_ref !== WORKFLOW || payload.event_name !== "workflow_dispatch" ||
        payload.runner_environment !== "github-hosted" || payload.run_attempt !== "1" ||
        !SUBJECTS.includes(String(payload.sub)) || typeof payload.run_id !== "string" ||
        !/^[1-9][0-9]{0,15}$/.test(payload.run_id) || !Number.isSafeInteger(Number(payload.run_id))) {
      console.log(JSON.stringify({ event: "credentials_denied", code: "identity_policy" }));
      return reply(403, { error: "forbidden" });
    }
    if (request.headers.get("Content-Type")?.split(";")[0].trim() !== "application/json") {
      return reply(400, { error: "invalid_request" });
    }
    let body: unknown;
    try { body = JSON.parse(await smallBody(request, resultRequest ? 262144 : 1024)); }
    catch { return reply(400, { error: "invalid_request" }); }
    if (!body || typeof body !== "object" || Array.isArray(body) ||
        Object.keys(body).sort().join(",") !== (resultRequest ? "exit_code,log,log_truncated,request_id,status" : "request_id") ||
        !("request_id" in body) || typeof body.request_id !== "string" ||
        !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(body.request_id)) {
      return reply(400, { error: "invalid_request" });
    }
    // También se exige un dispatch real y reciente, aceptado por GitHub y conciliado en D1.
    const accepted = await env.LEDGER.prepare(
      "SELECT config_json,config_sha256,bot_completed_at FROM dispatches WHERE request_id=? AND state='accepted' AND run_id=? AND scheduled_at>=?",
    ).bind(body.request_id, Number(payload.run_id),
      new Date(Date.now() - 20 * 60000).toISOString()).first<{ config_json: string | null; config_sha256: string; bot_completed_at: string | null }>();
    if (!accepted) {
      console.log(JSON.stringify({ event: "credentials_denied", code: "dispatch_not_authorized" }));
      return reply(403, { error: "dispatch_not_authorized" });
    }
    if (accepted.bot_completed_at) return reply(409, { error: "already_completed" });
    if (resultRequest) {
      if (!("exit_code" in body) || !Number.isInteger(body.exit_code) || ![0,1,3,5,6].includes(Number(body.exit_code)) ||
          !("status" in body) || typeof body.status !== "string" ||
          !["sin_turno","prueba_finalizada","turno_informado","error_bot","error_inicio","timeout"].includes(body.status) ||
          !("log" in body) || typeof body.log !== "string" || new TextEncoder().encode(body.log).length > 32768 ||
          !("log_truncated" in body) || typeof body.log_truncated !== "boolean") return reply(400, { error: "invalid_request" });
      // Defensa adicional: nunca conservar credenciales en el diagnóstico privado.
      let privateLog = body.log;
      for (const value of [env.OSEP_USER, env.OSEP_PASS]) if (value) privateLog = privateLog.split(value).join("[REDACTED]");
      const saved = await env.LEDGER.prepare(
        "UPDATE dispatches SET bot_status=?,bot_exit_code=?,bot_log=?,bot_log_truncated=?,bot_completed_at=?,config_json=NULL WHERE request_id=? AND run_id=? AND bot_completed_at IS NULL",
      ).bind(body.status, body.exit_code, privateLog, body.log_truncated ? 1 : 0, new Date().toISOString(), body.request_id, Number(payload.run_id)).run();
      if (saved.meta.changes !== 1) return reply(409, { error: "already_completed" });
      console.log(JSON.stringify({ event: "private_result_saved", run_id: Number(payload.run_id) }));
      return reply(200, { ok: true });
    }
    if (!accepted.config_json || await fingerprint(accepted.config_json) !== accepted.config_sha256) {
      return reply(503, { error: "configuration_unavailable" });
    }
    botConfig(JSON.parse(accepted.config_json));
    if (![env.OSEP_USER, env.OSEP_PASS].every(v => typeof v === "string" && v.length > 0 && v.length <= 4096 && !v.includes("\0"))) {
      return reply(503, { error: "credentials_unavailable" });
    }
    // Sin cuerpos, cabeceras, tokens ni secretos en logs o D1.
    console.log(JSON.stringify({ event: "private_runtime_authorized", run_id: Number(payload.run_id) }));
    return reply(200, { credentials: { OSEP_USER: env.OSEP_USER, OSEP_PASS: env.OSEP_PASS }, config_json: accepted.config_json,
      config_sha256: accepted.config_sha256 });
  } catch (error) {
    // Excepciones de firma, JWKS o D1 se cierran sin entregar datos ni registrar su contenido.
    const allowed = ["ERR_JWT_CLAIM_VALIDATION_FAILED", "ERR_JWT_EXPIRED", "ERR_JWS_SIGNATURE_VERIFICATION_FAILED",
      "ERR_JWS_INVALID", "ERR_JWK_INVALID", "ERR_JWKS_NO_MATCHING_KEY", "ERR_JWKS_TIMEOUT", "ERR_JOSE_GENERIC"];
    const code = error && typeof error === "object" && "code" in error && typeof error.code === "string" && allowed.includes(error.code)
      ? error.code : "authorization_unavailable";
    console.log(JSON.stringify({ event: "credentials_denied", code }));
    return reply(401, { error: "authorization_unavailable" });
  }
}
