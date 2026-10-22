// OSEP Control: API de administración, sólo detrás de Cloudflare Access.
// La firma JWT se verifica adicionalmente en la aplicación. No acepta secretos de OSEP.
const HTML = "__INLINE_HTML__";
const BOT_KEYS = ["OBJ_SERVICIO","OBJ_ZONA","OBJ_DEPTO","OBJ_MEDICO","OBJ_PROFESIONAL",
"OBJ_DOMICILIO","OBJ_DOMICILIO_FLEXIBLE","OBJ_PROFESIONAL_FLEXIBLE","OBJ_HORARIO_TURNO",
"OBJ_DIAS_VALIDOS","OBJ_FECHA_DISP","OBJ_HORA_MIN","OBJ_HORA_MAX","OBJ_HORA_PRIORIDAD",
"OBJ_HORA_FLEXIBLE","OBJ_DIA_FLEXIBLE","OBJ_FECHA_FLEXIBLE","PORTAL_URL","HEADLESS",
"TIMEOUT_MS","STOP_AFTER_LOGIN","DRY_RUN"];
const OPTIONAL = new Set(["OBJ_MEDICO","OBJ_PROFESIONAL","OBJ_DOMICILIO","OBJ_HORARIO_TURNO","OBJ_DIAS_VALIDOS","OBJ_FECHA_DISP"]);
const BOOL = ["OBJ_DOMICILIO_FLEXIBLE","OBJ_PROFESIONAL_FLEXIBLE","OBJ_HORA_FLEXIBLE",
"OBJ_DIA_FLEXIBLE","OBJ_FECHA_FLEXIBLE","HEADLESS","STOP_AFTER_LOGIN","DRY_RUN"];
const DAYS = new Set(["LUNES","MARTES","MIERCOLES","MIÉRCOLES","JUEVES","VIERNES","SABADO","SÁBADO","DOMINGO"]);
const C = {"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store, private",
"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer"};
function json(body,status=200){return new Response(JSON.stringify(body),{status,headers:C});}
function decode(input){return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(input.replace(/-/g,"+").replace(/_/g,"/")),c=>c.charCodeAt(0))));}
let cachedKeys=null,expires=0;
async function authorized(request,env){
  if(!env.POLICY_AUD || !env.TEAM_DOMAIN || !env.ADMIN_EMAIL)return false;
  const token=request.headers.get("Cf-Access-Jwt-Assertion");
  if(!token || token.length>12000)return false;
  const parts=token.split(".");
  if(parts.length!==3)return false;
  try {
    const header=decode(parts[0]),payload=decode(parts[1]);
    if(header.alg!=="RS256" || !header.kid || payload.iss!==env.TEAM_DOMAIN ||
       !([].concat(payload.aud||[]).includes(env.POLICY_AUD)) ||
       String(payload.email||"").toLowerCase()!==env.ADMIN_EMAIL.toLowerCase() ||
       !Number.isFinite(payload.exp) || payload.exp*1000<=Date.now() ||
       (payload.nbf && payload.nbf*1000>Date.now()+30000))return false;
    if(!cachedKeys || Date.now()>expires){
      const result=await fetch(env.TEAM_DOMAIN+"/cdn-cgi/access/certs",{signal:AbortSignal.timeout(5000)});
      if(!result.ok)return false;
      const body=await result.json();
      cachedKeys=body.keys||[];expires=Date.now()+300000;
    }
    const jwk=cachedKeys.find(k=>k.kid===header.kid);
    if(!jwk)return false;
    const key=await crypto.subtle.importKey("jwk",jwk,{name:"RSASSA-PKCS1-v1_5",hash:"SHA-256"},false,["verify"]);
    const signature=Uint8Array.from(atob(parts[2].replace(/-/g,"+").replace(/_/g,"/")),c=>c.charCodeAt(0));
    return crypto.subtle.verify("RSASSA-PKCS1-v1_5",key,signature,new TextEncoder().encode(parts[0]+"."+parts[1]));
  }catch{return false;}
}
function mins(s){if(!/^\d{1,2}:\d{2}$/.test(s))throw Error("Hora inválida");const [h,m]=s.split(":").map(Number);if(h>23||m>59)throw Error("Hora inválida");return h*60+m;}
function validate(x){
  if(!x || typeof x!=="object" || Array.isArray(x))throw Error("Configuración inválida");
  if(!x.bot || Object.keys(x.bot).sort().join("|")!==[...BOT_KEYS].sort().join("|"))
    throw Error("Parámetros del bot incompletos");
  for(const k of BOT_KEYS){
    const value=x.bot[k];
    if(typeof value!=="string" || value.length>250 || /[\u0000-\u001f\u007f]/.test(value) || (!OPTIONAL.has(k)&&!value.trim()))
      throw Error("Valor inválido: "+k);
  }
  for(const k of BOOL)if(!["true","false"].includes(x.bot[k]))throw Error("Booleano inválido: "+k);
  if(!["EARLIEST","LATEST"].includes(x.bot.OBJ_HORA_PRIORIDAD))throw Error("Prioridad inválida");
  if(x.bot.OBJ_HORA_MIN!=="false"&&mins(x.bot.OBJ_HORA_MIN)>(x.bot.OBJ_HORA_MAX==="false"?1439:mins(x.bot.OBJ_HORA_MAX)))throw Error("Rango de horas inválido");
  if(x.bot.OBJ_FECHA_DISP && x.bot.OBJ_FECHA_DISP!=="false"){
    const m=/^(\d{2})-(\d{2})-(\d{4})$/.exec(x.bot.OBJ_FECHA_DISP);
    if(!m || !Number.isFinite(Date.parse(m[3]+"-"+m[2]+"-"+m[1])) ||
      new Date(Date.UTC(+m[3],+m[2]-1,+m[1])).toISOString().slice(0,10)!==m[3]+"-"+m[2]+"-"+m[1])throw Error("Fecha inválida");
  }
  if(x.bot.OBJ_DIAS_VALIDOS && x.bot.OBJ_DIAS_VALIDOS.split(",").some(d=>!DAYS.has(d.trim().toUpperCase())))throw Error("Días inválidos");
  if(x.bot.PORTAL_URL!=="https://www.osep.mendoza.gov.ar/webapp_pri")throw Error("Portal no autorizado");
  if(!/^\d+$/.test(x.bot.TIMEOUT_MS)||+x.bot.TIMEOUT_MS<1000||+x.bot.TIMEOUT_MS>120000)throw Error("Timeout inválido");
  const plan=x.schedule;
  if(!plan || typeof plan!=="object" || Object.keys(plan).sort().join("|")!=="end|interval|start")throw Error("Programación inválida");
  mins(plan.start); mins(plan.end);
  if(!Number.isInteger(plan.interval)||plan.interval<1||plan.interval>1440)throw Error("Intervalo inválido");
  if(typeof x.paused!=="boolean")throw Error("Pausa inválida");
  return x;
}
async function readBody(request){const t=await request.text();if(t.length>16000)throw Error("Solicitud demasiado grande");return JSON.parse(t);}
async function state(env){
  const [c,rs,mq]=await Promise.all([
    env.LEDGER.prepare("SELECT config_json,schedule_json,paused,revision,updated_at,updated_by FROM control_config WHERE id=1").first(),
    env.LEDGER.prepare("SELECT slot,scheduled_at,state,run_id,bot_status,bot_exit_code,bot_log,bot_completed_at FROM dispatches ORDER BY scheduled_at DESC LIMIT 40").all(),
    env.LEDGER.prepare("SELECT id,created_at,updated_at,dry_run,state,run_id,error_code FROM manual_queue ORDER BY created_at DESC LIMIT 20").all()
  ]);
  return {revision:c?.revision||0,updated_at:c?.updated_at||null,updated_by:c?.updated_by||null,
    config:c?{bot:JSON.parse(c.config_json),schedule:JSON.parse(c.schedule_json),paused:!!c.paused}:null,
    runs:rs.results||[],manual:mq.results||[]};
}
function originOk(request){const u=new URL(request.url);return request.headers.get("Origin")===u.origin && request.headers.get("X-OSEP-CSRF")==="1";}
export default {
 async fetch(request,env){
   const url=new URL(request.url);
   if(url.hostname!=="osep-admin.franchezuca.workers.dev")return new Response("Not found",{status:404});
   if(!(await authorized(request,env)))return new Response("Acceso privado no autorizado",{status:403,headers:{"Cache-Control":"no-store"}});
   if(request.method==="GET"&&url.pathname==="/")
     return new Response(HTML,{headers:{...C,"Content-Type":"text/html; charset=utf-8",
     "Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"}});
   if(request.method==="GET"&&url.pathname==="/api/state"){
     try{return json(await state(env));}catch{return json({error:"No se pudo leer D1"},503);}
   }
   if((request.method==="PUT"&&url.pathname==="/api/config")||
      (request.method==="POST"&&url.pathname==="/api/manual")){
     if(!originOk(request))return json({error:"Origen no autorizado"},403);
     try{
       const incoming=await readBody(request);
       if(request.method==="PUT"){
         const data=validate(incoming.config);
         if(!Number.isInteger(incoming.revision)||incoming.revision<1)return json({error:"Versión inválida"},400);
         const updated=await env.LEDGER.prepare(
           "UPDATE control_config SET config_json=?,schedule_json=?,paused=?,revision=revision+1,updated_at=?,updated_by=? WHERE id=1 AND revision=?"
         ).bind(JSON.stringify(data.bot),JSON.stringify(data.schedule),data.paused?1:0,new Date().toISOString(),env.ADMIN_EMAIL,incoming.revision).run();
         if(updated.meta.changes!==1)return json({error:"La configuración fue modificada. Actualizá la página antes de guardar."},409);
         return json({ok:true});
       }
       if(typeof incoming.dry_run!=="boolean")return json({error:"Tipo de prueba inválido"},400);
       const config=await env.LEDGER.prepare("SELECT config_json FROM control_config WHERE id=1").first();
       if(!config)return json({error:"Todavía no se configuró el panel"},409);
       const bot=JSON.parse(config.config_json);
       validate({bot,schedule:{start:"05:03",end:"01:03",interval:30},paused:false});
       const busy=await env.LEDGER.prepare("SELECT id FROM manual_queue WHERE state IN ('pending','dispatching','busy') LIMIT 1").first();
       if(busy)return json({error:"Ya existe una búsqueda manual pendiente"},409);
       const id=crypto.randomUUID(),now=new Date().toISOString();
       await env.LEDGER.prepare("INSERT INTO manual_queue(id,created_at,updated_at,submitted_by,dry_run,state,config_json) VALUES(?,?,?,?,?,'pending',?)")
         .bind(id,now,now,env.ADMIN_EMAIL,incoming.dry_run?1:0,JSON.stringify(bot)).run();
       return json({ok:true,id,queued:true,detail:"El scheduler la ejecutará en el siguiente ciclo de un minuto."},202);
     }catch(e){return json({error:e instanceof Error?e.message:"Solicitud inválida"},400);}
   }
   return json({error:"No encontrado"},404);
 }
};