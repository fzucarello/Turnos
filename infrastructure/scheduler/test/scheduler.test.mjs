import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import worker, { dispatch } from '../src/index.ts';
import { botConfig, fingerprint, isBusinessSlot, schedule } from '../src/config.ts';

const config = JSON.parse(readFileSync(new URL('../wrangler.jsonc', import.meta.url)));
const fixture = Object.fromEntries(readFileSync(new URL('../.dev.vars.example', import.meta.url), 'utf8')
  .split(/\r?\n/).map(line => /^([A-Z_]+)="(.*)"$/.exec(line)).filter(Boolean).map(m => [m[1], m[2]]));
const schema = ['0001_dispatches.sql', '0002_config_fingerprint.sql', '0003_private_runtime.sql'].map(name => readFileSync(new URL('../migrations/' + name, import.meta.url), 'utf8')).join('\n');
const originalFetch = globalThis.fetch;
const originalNow = Date.now;
const time = Date.parse('2026-10-01T12:00:00Z');
const controller = { cron: config.triggers.crons[0], scheduledTime: time };
afterEach(() => { globalThis.fetch = originalFetch; Date.now = originalNow; });

function setup(mode = 'validate') {
  Date.now = () => time;
  const db = new DatabaseSync(':memory:');
  db.exec(schema);
  const LEDGER = {
    prepare(sql) {
      return { bind(...params) { return { async run() {
        const result = db.prepare(sql).run(...params);
        return { meta: { changes: Number(result.changes) } };
      } }; } };
    },
  };
  return { db, env: { ...fixture, MODE: mode, GITHUB_TOKEN: 'test-placeholder', LEDGER } };
}

function mockGitHub({ status = 200, busy = false, uncertain = false, oldSchedule = false } = {}) {
  const posts = [];
  globalThis.fetch = async (url, options) => {
    assert.equal(options.redirect, 'manual');
    if (url.includes('raw.githubusercontent.com')) return new Response(oldSchedule ? 'on:\n  schedule:\n' : 'on:\n  workflow_dispatch:\n');
    assert.equal(options.headers.Authorization, 'Bearer test-placeholder');
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      if (uncertain) throw new Error('network lost with possible accepted dispatch');
      return status === 204 ? new Response(null, { status }) : Response.json({ workflow_run_id: 456 }, { status });
    }
    return Response.json({ workflow_runs: busy ? [{ id: 123, status: 'in_progress' }] : [] });
  };
  return posts;
}

test('horario ficticio de prueba: 17 disparos entre 09:00 y 17:00 Mendoza', () => {
  const plan = schedule(fixture);
  for (let minute = 0; minute < 1440; minute++) {
    const timestamp = Date.UTC(2026,9,1) + minute * 60000;
    const utc = new Date(timestamp);
    const planned = utc.getUTCHours() >= 12 && utc.getUTCHours() < 20 && utc.getUTCMinutes() % 30 === 0 || utc.getUTCHours() === 20 && utc.getUTCMinutes() === 0;
    assert.equal(isBusinessSlot(timestamp, plan), planned);
  }
});

test('pausa y configuración incompleta no llaman a GitHub', async () => {
  const { env } = setup('paused');
  globalThis.fetch = () => { throw new Error('Unexpected HTTP'); };
  await dispatch(controller, env);
  env.MODE = 'validate'; env.GITHUB_TOKEN = '';
  await assert.rejects(dispatch(controller, env), /missing_GITHUB_TOKEN/);
});

test('validación envía DRY_RUN true una sola vez incluso con ticks concurrentes', async () => {
  const { db, env } = setup();
  const posts = mockGitHub();
  await Promise.all([dispatch(controller, env), dispatch(controller, env)]);
  Date.now = () => time + 600000;
  await dispatch({ ...controller, scheduledTime: time + 600000 }, env);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].inputs.dry_run, 'true');
  assert.equal(posts[0].ref, 'main');
  assert.deepEqual(db.prepare('SELECT state,http_status,run_id FROM dispatches').get(), { __proto__: null, state: 'accepted', http_status: 200, run_id: 456 });
});

test('un timeout posterior al POST queda incierto y no se reenvía', async () => {
  const { db, env } = setup();
  const posts = mockGitHub({ uncertain: true });
  await assert.rejects(dispatch(controller, env));
  await dispatch(controller, env);
  assert.equal(posts.length, 1);
  assert.equal(db.prepare('SELECT state FROM dispatches').get().state, 'uncertain');
});

for (const status of [401, 403, 422, 429, 500, 503]) {
  test(`HTTP ${status} registrado sin repetir POST`, async () => {
    const { db, env } = setup(); const posts = mockGitHub({ status });
    await assert.rejects(dispatch(controller, env)); await dispatch(controller, env);
    assert.equal(posts.length, 1);
    assert.equal(db.prepare('SELECT http_status FROM dispatches').get().http_status, status);
  });
}

test('un Run activo evita agregar otro a la cola', async () => {
  const { db, env } = setup(); const posts = mockGitHub({ busy: true });
  await dispatch(controller, env); assert.equal(posts.length, 0);
  assert.equal(db.prepare('SELECT state FROM dispatches').get().state, 'busy_skipped');
});

test('producción no arranca mientras exista schedule de GitHub', async () => {
  const { env } = setup('production'); const posts = mockGitHub({ oldSchedule: true });
  await assert.rejects(dispatch(controller, env), /github_schedule_still_present/);
  assert.equal(posts.length, 0);
});

test('producción usa false y admite el HTTP 204 anterior', async () => {
  const { db, env } = setup('production'); const posts = mockGitHub({ status: 204 });
  await dispatch(controller, env);
  assert.equal(posts[0].inputs.dry_run, 'false');
  assert.equal(db.prepare('SELECT state FROM dispatches').get().state, 'accepted');
});

test('slot fuera de horario, cron desconocido o evento atrasado no se ejecuta', async () => {
  const { env } = setup('production'); const posts = mockGitHub();
  await dispatch({ ...controller, scheduledTime: Date.parse('2026-10-01T05:03:00Z') }, env);
  await dispatch({ ...controller, cron: '*/2 * * * *' }, env);
  Date.now = () => time + 6 * 60000;
  await dispatch(controller, env);
  assert.equal(posts.length, 0);
});

test('lecturas transitorias se reintentan y POST permanece único', async () => {
  const { env } = setup(); const posts = mockGitHub(); const normal = globalThis.fetch;
  let failed = false;
  globalThis.fetch = async (url, options) => {
    if (!failed && !options.method) { failed = true; return new Response('', { status: 503 }); }
    return normal(url, options);
  };
  await dispatch(controller, env);
  assert.equal(posts.length, 1);
});

test('validación fuera de franja fuerza prueba segura una sola vez por ID', async () => {
  const { env } = setup(); const posts = mockGitHub();
  const outside = Date.parse('2026-10-01T06:12:00Z'); Date.now = () => outside;
  await dispatch({ ...controller, scheduledTime: outside }, env);
  await dispatch({ ...controller, scheduledTime: outside }, env);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].inputs.dry_run, 'true');
  env.MODE = 'production';
  await dispatch({ ...controller, scheduledTime: outside }, env);
  assert.equal(posts.length, 1);
});

test('intervalo de 20 minutos aplica sin ampliar la franja', () => {
  const plan = schedule({ ...fixture, INTERVAL_MINUTES: '20' });
  let count = 0;
  for (let minute = 0; minute < 1440; minute++) if (isBusinessSlot(Date.UTC(2026,9,1) + minute * 60000, plan)) count++;
  assert.equal(count, 25);
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T12:00Z'), plan), true);
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T12:10Z'), plan), false);
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T20:00Z'), plan), true);
});

test('franja diurna, intervalo de 7 minutos y fin sin alineación no amplían la ventana', () => {
  const plan = schedule({ ...fixture, INTERVAL_MINUTES: '7', SCHEDULE_START: '09:10', SCHEDULE_END: '09:30' });
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T12:10Z'), plan), true);
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T12:24Z'), plan), true);
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T12:30Z'), plan), false);
  assert.equal(isBusinessSlot(Date.parse('2026-10-01T12:31Z'), plan), false);
});

test('dispatch público sólo lleva ID aleatorio y modo seguro; criterios y huella quedan privados', async () => {
  const { db, env } = setup('production'); const posts = mockGitHub();
  await dispatch(controller, env);
  assert.deepEqual(Object.keys(posts[0].inputs).sort(), ['dry_run','request_id']);
  const row = db.prepare('SELECT request_id,config_json,config_sha256 FROM dispatches').get();
  assert.equal(row.request_id, posts[0].inputs.request_id);
  assert.match(row.request_id, /^[a-f0-9-]{36}$/);
  assert.equal(row.config_sha256, await fingerprint(row.config_json));
  assert.equal(JSON.parse(row.config_json).OBJ_MEDICO, fixture.OBJ_MEDICO);
  assert.equal(JSON.stringify(posts).includes(fixture.OBJ_MEDICO), false);
});

test('criterio cambiado se conserva sólo en el siguiente snapshot privado', async () => {
  const { db, env } = setup('production'); const posts = mockGitHub();
  await dispatch(controller, env);
  env.OBJ_MEDICO = 'OTRO PROFESIONAL FICTICIO'; env.INTERVAL_MINUTES = '20';
  Date.now = () => time + 20 * 60000;
  await dispatch({ ...controller, scheduledTime: Date.now() }, env);
  const rows = db.prepare('SELECT config_json FROM dispatches ORDER BY scheduled_at').all();
  assert.equal(JSON.parse(rows[1].config_json).OBJ_MEDICO, env.OBJ_MEDICO);
  assert.notEqual(posts[0].inputs.request_id, posts[1].inputs.request_id);
  assert.equal(JSON.stringify(posts).includes(env.OBJ_MEDICO), false);
});

test('DRY_RUN Cloudflare permanece efectivo sin configuración pública', async () => {
  const { db, env } = setup('production'); env.DRY_RUN = 'true'; const posts = mockGitHub();
  await dispatch(controller, env);
  assert.equal(posts[0].inputs.dry_run, 'true');
  assert.equal(JSON.parse(db.prepare('SELECT config_json FROM dispatches').get().config_json).DRY_RUN, 'true');
});

for (const [key, value] of [['INTERVAL_MINUTES','0'],['INTERVAL_MINUTES','abc'],['SCHEDULE_START','25:03'],['OBJ_HORA_MIN','22:61'],['OBJ_HORA_MAX','07:00'],['OBJ_SERVICIO',''],['OBJ_DIA_FLEXIBLE','yes'],['OBJ_FECHA_DISP','31-02-2026'],['OBJ_DIAS_VALIDOS','LUN'],['OBJ_MEDICO','valor\nOSEP_PASS=x'],['PORTAL_URL','https://otro.example']]) {
  test(`valor inválido ${key} detiene el disparo antes de HTTP y D1`, async () => {
    const { db, env } = setup('production'); env[key] = value;
    globalThis.fetch = () => { throw new Error('Unexpected HTTP'); };
    await assert.rejects(dispatch(controller, env), /config_invalid/);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM dispatches').get().count, 0);
  });
}

test('deploy conserva variables de Cloudflare y no contiene valores operativos', () => {
  assert.equal(config.keep_vars, true);
  assert.equal(config.vars, undefined);
  assert.deepEqual(config.triggers.crons, ['* * * * *']);
});

test('no hay endpoint HTTP para disparar el bot', async () => {
  assert.equal((await worker.fetch(new Request('https://osep-scheduler.franchezuca.workers.dev/'), {})).status, 404);
  assert.equal(config.workers_dev, true);
  assert.equal(config.preview_urls, false);
});
