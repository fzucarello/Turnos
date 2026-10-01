import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPair, SignJWT } from 'jose';
import { credentials, CREDENTIALS_URL, RESULT_URL, ISSUER } from '../src/credentials.ts';

const { privateKey, publicKey } = await generateKeyPair('RS256');
const keys = async () => publicKey;
import { readFileSync } from 'node:fs';
import { botConfig, fingerprint } from '../src/config.ts';
const requestId = 'a1234567-1234-4234-8234-123456789abc';
const fixture = Object.fromEntries(readFileSync(new URL('../.dev.vars.example', import.meta.url), 'utf8').split(/\r?\n/).map(line=>/^([A-Z_]+)="(.*)"$/.exec(line)).filter(Boolean).map(m=>[m[1],m[2]]));
const configJson = JSON.stringify(botConfig(fixture));
const hash = await fingerprint(configJson);
const claims = {
  repository: 'fzucarello/Turnos', repository_id: '1089108669', repository_owner_id: '241853003',
  ref: 'refs/heads/main', workflow_ref: 'fzucarello/Turnos/.github/workflows/osep.yml@refs/heads/main',
  event_name: 'workflow_dispatch', runner_environment: 'github-hosted', run_id: '1234', run_attempt: '1',
};
const subject = 'repo:fzucarello/Turnos:ref:refs/heads/main';
async function token(changes = {}, options = {}) {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ ...claims, iat: now, nbf: now - 1, exp: now + 300, ...changes })
    .setProtectedHeader({ alg: 'RS256' }).setIssuer(options.issuer ?? ISSUER)
    .setAudience(options.audience ?? CREDENTIALS_URL).setSubject(options.subject ?? subject).sign(privateKey);
}
function env({ authorized = true, missing = false, error = false, completed = false, tampered = false } = {}) {
  let queries = 0;
  const value = {
    OSEP_USER: 'test-user-placeholder', OSEP_PASS: missing ? '' : 'test-password-placeholder',
    LEDGER: { prepare(sql) {
      queries++;
      assert.match(sql, /state='accepted'/);
      return { bind(id, runId, oldest) {
        assert.equal(id, requestId); assert.equal(runId, 1234);
        assert.ok(Date.now() - Date.parse(oldest) >= 20 * 60000);
        return { first: async () => { if (error) throw new Error('private-db-detail'); return authorized ? { config_json: tampered ? configJson+" " : configJson, config_sha256:hash, bot_completed_at: completed ? "now" : null } : null; } };
      } };
    } },
  };
  return { value, queries: () => queries };
}
function request(jwt, body = { request_id: requestId }, options = {}) {
  return new Request(options.url ?? CREDENTIALS_URL, {
    method: options.method ?? 'POST',
    headers: { 'Authorization': `Bearer ${jwt}`, 'Content-Type': options.type ?? 'application/json' },
    ...(options.method === 'GET' ? {} : { body: typeof body === 'string' ? body : JSON.stringify(body) }),
  });
}

test('firma válida + identidad autorizada + Run reciente entrega sólo los dos secretos sin caché', async () => {
  const e = env(); const response = await credentials(request(await token()), e.value, keys);
  assert.equal(response.status, 200); assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), null);
  assert.deepEqual(await response.json(), { credentials: { OSEP_USER:e.value.OSEP_USER,OSEP_PASS:e.value.OSEP_PASS }, config_json:configJson,config_sha256:hash });
  assert.equal(e.queries(), 1);
});

test('subject inmutable exige los mismos IDs y el mismo main', async () => {
  const e = env(); const jwt = await token({}, { subject: 'repo:fzucarello@241853003/Turnos@1089108669:ref:refs/heads/main' });
  assert.equal((await credentials(request(jwt), e.value, keys)).status, 200);
});

for (const [key, bad] of Object.entries({ repository: 'attacker/Turnos', repository_id: '999', repository_owner_id: '999',
  ref: 'refs/heads/feature', workflow_ref: 'fzucarello/Turnos/.github/workflows/other.yml@refs/heads/main',
  event_name: 'pull_request', runner_environment: 'self-hosted', run_attempt: '2', run_id: 'not-a-number' })) {
  test(`identidad no autorizada: ${key} no consulta D1 ni entrega datos`, async () => {
    const e = env(); const response = await credentials(request(await token({ [key]: bad })), e.value, keys);
    assert.equal(response.status, 403); assert.equal(e.queries(), 0);
    assert.ok(!(await response.text()).includes('placeholder'));
  });
}

for (const options of [{ audience: 'https://other.example' }, { issuer: 'https://other.example' }, { subject: 'repo:attacker/Turnos:ref:refs/heads/main' }]) {
  test(`audiencia, emisor o subject ajenos se rechazan: ${JSON.stringify(options)}`, async () => {
    const e = env(); assert.notEqual((await credentials(request(await token({}, options)), e.value, keys)).status, 200);
    assert.equal(e.queries(), 0);
  });
}

test('tokens vencidos, futuros, antiguos, manipulados y sin firma válida no entregan datos', async () => {
  const now = Math.floor(Date.now()/1000);
  const values = [await token({ exp: now-30 }), await token({ nbf: now+60 }), await token({ iat: now-400 }), 'a.b.c'];
  const signed = await token(); values.push(signed.slice(0,-10) + 'xxxxxxxxxx');
  for (const jwt of values) {
    const e = env(); assert.equal((await credentials(request(jwt), e.value, keys)).status, 401); assert.equal(e.queries(), 0);
  }
});

test('sin autorización no carga claves remotas ni consulta D1', async () => {
  const e = env(); const r = new Request(CREDENTIALS_URL, { method: 'POST' });
  assert.equal((await credentials(r, e.value, () => { throw Error('unexpected'); })).status, 401);
  assert.equal(e.queries(), 0);
});

test('rutas, método y query string no admitidos no entregan secretos', async () => {
  const e = env(); const jwt = await token();
  for (const url of [CREDENTIALS_URL+'?x=1', CREDENTIALS_URL.replace('/credentials','/'), CREDENTIALS_URL.replace('franchezuca','other')]) {
    assert.equal((await credentials(request(jwt, undefined, { url }), e.value, keys)).status, 404);
  }
  assert.equal((await credentials(request(jwt, undefined, { method: 'GET' }), e.value, keys)).status, 405);
  assert.equal(e.queries(), 0);
});

test('cuerpo grande, JSON inválido, claves adicionales o tipo incorrecto se rechazan antes de D1', async () => {
  const e = env(); const jwt = await token();
  for (const body of ['x'.repeat(1025), '{', { request_id:'invalid' },
    { request_id:requestId, config_sha256:'wrong' }, { request_id:requestId, OSEP_USER:'inject' }]) {
    assert.equal((await credentials(request(jwt, body), e.value, keys)).status, 400);
  }
  assert.equal((await credentials(request(jwt, undefined, {type:'text/plain'}), e.value, keys)).status, 400);
  assert.equal(e.queries(), 0);
});

test('Run no conciliado o antiguo no tiene acceso; fallo de D1 y falta de secrets cierran la entrega', async () => {
  const jwt = await token();
  for (const [options, status] of [[{authorized:false},403],[{error:true},401],[{missing:true},503]]) {
    const response = await credentials(request(jwt), env(options).value, keys);
    assert.equal(response.status, status); assert.ok(!(await response.text()).includes('placeholder'));
  }
});


test('snapshot alterado y Run finalizado no vuelven a entregar datos privados', async () => {
  const jwt = await token();
  assert.equal((await credentials(request(jwt),env({tampered:true}).value,keys)).status,503);
  assert.equal((await credentials(request(jwt),env({completed:true}).value,keys)).status,409);
});

test('guardar resultado privado exige la misma identidad y no lo devuelve ni publica', async () => {
  const e = env(); const values = [];
  const normal = e.value.LEDGER.prepare;
  e.value.LEDGER.prepare = sql => sql.startsWith('UPDATE') ? { bind(...params) { values.push(params); return {run:async()=>({meta:{changes:1}})}; } } : normal(sql);
  const response = await credentials(request(await token(), {request_id:requestId,exit_code:1,status:'turno_informado',log:'paciente ficticio '+e.value.OSEP_PASS,log_truncated:false},{url:RESULT_URL}),e.value,keys);
  assert.equal(response.status,200);
  assert.deepEqual(await response.json(),{ok:true});
  assert.equal(values[0][2].includes(e.value.OSEP_PASS),false);
  assert.equal(values[0][2].includes('paciente ficticio'),true);
});

test('resultado sin JWT o con campos inesperados no se almacena', async () => {
  const e = env();
  assert.equal((await credentials(request('a.b.c',{request_id:requestId},{url:RESULT_URL}),e.value,keys)).status,401);
  assert.equal((await credentials(request(await token(),{request_id:requestId,log:'dato ficticio'},{url:RESULT_URL}),e.value,keys)).status,400);
});
