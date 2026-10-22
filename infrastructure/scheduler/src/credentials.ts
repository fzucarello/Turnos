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

// Contraseñas actualizadas desde el panel: AES-256-GCM con clave exclusiva de
// ambos Workers. La base de datos contiene solo texto cifrado y el nonce.
function fromB64(input: string): Uint8Array {
  return Uint8Array.from(atob(input), c => c.charCodeAt(0));
}
async function runtimeCredentials(env: Env): Promise<{OSEP_USER:string;OSEP_PASS:string}> {
  const vault = await env.LEDGER.prepare("SELECT iv,payload FROM osep_vault WHERE id=1")
    .first<{iv:string;payload:string}>();
  if (!vault) return { OSEP_USER: env.OSEP_USER, OSEP_PASS: env.OSEP_PASS };
  const hex = env.OSEP_VAULT_KEY;
  if (typeof hex !== "string" || !/^[a-f0-9]{64}$/i.test(hex)) throw new Error("vault_not_ready");
  const raw = Uint8Array.from(hex.match(/../g)!, b => parseInt(b, 16));
  const key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({
    name:"AES-GCM", iv:fromB64(vault.iv),
    additionalData:new TextEncoder().encode("OSEP-CREDENTIALS-V1")
  }, key, fromB64(vault.payload));
  const result = JSON.parse(new TextDecoder().decode(plain));
  if (!result || typeof result.OSEP_USER!=="string" || typeof result.OSEP_PASS!=="string" ||
      result.OSEP_USER.length<1 || result.OSEP_USER.length>4096 || result.OSEP_PASS.length<1 ||
      result.OSEP_PASS.length>4096) throw new Error("vault_invalid");
  return result;
}

function parseCatalog(log: string): Record<string,unknown> | null {
  const marker = "OSEP_CATALOG_JSON=";
  const line = log.split("\n").find(x=>x.startsWith(marker));
  if (!line) return null;
  try {
    const d = JSON.parse(line.slice(marker.length));
    const names=["services","zones","departments","centers","professionals"];
    if(!d || !names.every(k=>Array.isArray(d[k]) && d[k].length<=250 &&
      d[k].every((v:unknown)=>typeof v==="string"&&v.length<250)))return null;
    for(const k of ["selectedService","selectedZone","selectedDepartment"])
      if(typeof d[k]!=="string"||d[k].length>250)return null;
    return d;
  }catch{return null;}
}

type MasterEntry = [string,string,[string,string][]];
type MasterScan = {service:string;services:string[];zones:string[];departmentsByZone:Record<string,string[]>;entries:MasterEntry[];errors:unknown[]};
async function parseMaster(log:string): Promise<MasterScan|null> {
  const line=log.split("\n").find(x=>x.startsWith("OSEP_CATALOG_PACK="));
  if(!line)return null;
  const encoded=line.slice("OSEP_CATALOG_PACK=".length).trim();
  if(!/^[A-Za-z0-9+/=]{1,26000}$/.test(encoded))return null;
  try {
    const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
    const decoded=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"))).text();
    if(decoded.length>1500000)return null;
    const d=JSON.parse(decoded);
    const short=(v:unknown)=>typeof v==="string"&&v.length>0&&v.length<251;
    const arr=(a:unknown,max:number)=>Array.isArray(a)&&a.length<=max&&a.every(short);
    if(d.scope!=="master"||!short(d.service)||!arr(d.services,250)||!arr(d.zones,25)||
       !Array.isArray(d.entries)||d.entries.length>80||!Array.isArray(d.errors)||
       d.errors.length>80||typeof d.departmentsByZone!=="object"||!d.departmentsByZone)return null;
    if(!Object.entries(d.departmentsByZone).every(([zone,v])=>short(zone)&&arr(v,50)))return null;
    if(!d.entries.every((e:unknown)=>Array.isArray(e)&&e.length===3&&short(e[0])&&short(e[1])&&
      Array.isArray(e[2])&&e[2].length<=500&&e[2].every((row:unknown)=>Array.isArray(row)&&row.length===2&&row.every(short))))return null;
    return d as MasterScan;
  }catch{return null;}
}
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
      "SELECT slot,config_json,config_sha256,bot_completed_at FROM dispatches WHERE request_id=? AND state='accepted' AND run_id=? AND scheduled_at>=?",
    ).bind(body.request_id, Number(payload.run_id),
      new Date(Date.now() - 20 * 60000).toISOString()).first<{ slot:string; config_json: string | null; config_sha256: string; bot_completed_at: string | null }>();
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
      // Nunca se permiten credenciales en los logs, incluso tras una rotación.
      let privateLog = body.log;
      const current = await runtimeCredentials(env);
      for (const value of [env.OSEP_USER, env.OSEP_PASS, current.OSEP_USER, current.OSEP_PASS])
        if (value) privateLog = privateLog.split(value).join("[REDACTED]");
      const isCatalog = JSON.parse(accepted.config_json||"{}").OBJ_CATALOGO==="true";
      const isMaster = isCatalog && JSON.parse(accepted.config_json||"{}").OBJ_ZONA==="*";
      const catalog = isCatalog && !isMaster && body.status==="prueba_finalizada" ? parseCatalog(privateLog) : null;
      const master = isMaster && body.status==="prueba_finalizada" ? await parseMaster(privateLog) : null;
      const saved = await env.LEDGER.prepare(
        "UPDATE dispatches SET bot_status=?,bot_exit_code=?,bot_log=?,bot_log_truncated=?,bot_completed_at=?,config_json=NULL WHERE request_id=? AND run_id=? AND bot_completed_at IS NULL",
      ).bind(body.status, body.exit_code, privateLog, body.log_truncated ? 1 : 0, new Date().toISOString(), body.request_id, Number(payload.run_id)).run();
      if (saved.meta.changes !== 1) return reply(409, { error: "already_completed" });
      if (catalog) await env.LEDGER.prepare(
        "INSERT INTO osep_catalog(id,catalog_json,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET catalog_json=excluded.catalog_json,updated_at=excluded.updated_at"
      ).bind(JSON.stringify(catalog),new Date().toISOString()).run();
      if(master){
        const timestamp=new Date().toISOString();
        const statements=master.entries.map(([zone,department,records])=>env.LEDGER.prepare(
          "INSERT INTO osep_catalog_relations(service,zone,department,records_json,record_count,scan_status,updated_at) VALUES(?,?,?,?,?,'complete',?) ON CONFLICT(service,zone,department) DO UPDATE SET records_json=excluded.records_json,record_count=excluded.record_count,scan_status=excluded.scan_status,updated_at=excluded.updated_at"
        ).bind(master.service,zone,department,JSON.stringify(records),records.length,timestamp));
        if(statements.length)await env.LEDGER.batch(statements);
        const existing=await env.LEDGER.prepare("SELECT catalog_json FROM osep_catalog WHERE id=1").first<{catalog_json:string}>();
        const base=existing?JSON.parse(existing.catalog_json):{};
        base.services=[...new Set([...(base.services||[]),...master.services])].sort();
        base.zones=[...new Set([...(base.zones||[]),...master.zones])].sort();
        base.departmentsByZone={...(base.departmentsByZone||{}),...master.departmentsByZone};
        await env.LEDGER.prepare("INSERT INTO osep_catalog(id,catalog_json,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET catalog_json=excluded.catalog_json,updated_at=excluded.updated_at")
          .bind(JSON.stringify(base),timestamp).run();
      }
      if(typeof accepted.slot==="string"&&accepted.slot.startsWith("manual:")){
        const id=accepted.slot.split(":")[1];
        await env.LEDGER.prepare("UPDATE manual_queue SET state=?,updated_at=?,error_code=? WHERE id=? AND state='accepted'")
          .bind(body.status==="prueba_finalizada"&&(!isMaster||master)?"completed":"error",new Date().toISOString(),
            isMaster&&!master?"catalog_invalid_or_incomplete":null,id).run();
      }
      console.log(JSON.stringify({ event: "private_result_saved", run_id: Number(payload.run_id) }));
      return reply(200, { ok: true });
    }
    if (!accepted.config_json || await fingerprint(accepted.config_json) !== accepted.config_sha256) {
      return reply(503, { error: "configuration_unavailable" });
    }
    botConfig(JSON.parse(accepted.config_json));
    let credentials: {OSEP_USER:string;OSEP_PASS:string};
    try { credentials = await runtimeCredentials(env); }
    catch { return reply(503,{error:"credentials_unavailable"}); }
    if (![credentials.OSEP_USER, credentials.OSEP_PASS].every(v => typeof v === "string" && v.length > 0 && v.length <= 4096 && !v.includes("\0")))
      return reply(503,{error:"credentials_unavailable"});
    // Sin cuerpos, cabeceras, tokens ni secretos en logs o D1.
    console.log(JSON.stringify({ event: "private_runtime_authorized", run_id: Number(payload.run_id) }));
    return reply(200, { credentials, config_json: accepted.config_json,
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
