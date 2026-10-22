import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { deflateSync } from 'node:zlib';
import { generateKeyPair, SignJWT } from 'jose';
import { credentials,CREDENTIALS_URL,RESULT_URL,ISSUER } from '../src/credentials.ts';

const {privateKey,publicKey}=await generateKeyPair('RS256');
const id='a1234567-1234-4234-8234-123456789abc';
const master=()=>({scope:'master',captureVersion:2,service:'SERVICIO',
 services:['SERVICIO'],zones:['ZONA'],departmentsByZone:{ZONA:['DEPTO']},
 entries:[['ZONA','DEPTO',[['PROFESIONAL NUEVO','CENTRO NUEVO']]]],errors:[]});
async function setup(fail=false){
 const db=new DatabaseSync(':memory:');
 for(const name of ['0001_dispatches.sql','0002_config_fingerprint.sql','0003_private_runtime.sql','0003_control_panel.sql','0004_catalog_vault.sql'])
   db.exec(readFileSync(new URL('../migrations/'+name,import.meta.url),'utf8'));
 db.prepare("INSERT INTO manual_queue(id,created_at,updated_at,submitted_by,dry_run,state,kind,config_json) VALUES(?,?,?,'test',1,'accepted','catalog_master','{}')")
   .run(id,new Date().toISOString(),new Date().toISOString());
 db.prepare("INSERT INTO dispatches(slot,scheduled_at,cron,mode,state,updated_at,request_id,run_id,config_json) VALUES(?,?,'cron','manual','accepted',?,?,1234,?)")
   .run('manual:'+id,new Date().toISOString(),new Date().toISOString(),id,JSON.stringify({OBJ_CATALOGO:'true',OBJ_ZONA:'*',OBJ_SERVICIO:'SERVICIO'}));
 db.exec("INSERT INTO osep_catalog_relations VALUES('SERVICIO','ZONA','DEPTO','[[\"ANTERIOR\",\"CENTRO ANTERIOR\"]]',1,'complete','anterior')");
 const wrap=(sql,params=[])=>({
   bind(...values){return wrap(sql,values);},
   async first(){return db.prepare(sql).get(...params)??null;},
   async run(){const r=db.prepare(sql).run(...params);return {meta:{changes:Number(r.changes)}};},
   sql,params
 });
 const env={OSEP_USER:'fake-user',OSEP_PASS:'fake-password',LEDGER:{
  prepare:wrap,async batch(statements){
   db.exec('BEGIN');
   try{
    const out=[];
    for(const s of statements){
      if(fail&&s.sql.startsWith('INSERT INTO osep_catalog_relations'))throw Error('simulated D1 failure');
      out.push(await s.run());
    }
    db.exec('COMMIT');return out;
   }catch(e){db.exec('ROLLBACK');throw e;}
  }
 }};
 const now=Math.floor(Date.now()/1000);
 const token=await new SignJWT({repository:'fzucarello/Turnos',repository_id:'1089108669',
  repository_owner_id:'241853003',ref:'refs/heads/main',
  workflow_ref:'fzucarello/Turnos/.github/workflows/osep.yml@refs/heads/main',
  event_name:'workflow_dispatch',runner_environment:'github-hosted',run_id:'1234',run_attempt:'1',
  iat:now,nbf:now-1,exp:now+300})
  .setProtectedHeader({alg:'RS256'}).setIssuer(ISSUER).setAudience(CREDENTIALS_URL)
  .setSubject('repo:fzucarello/Turnos:ref:refs/heads/main').sign(privateKey);
 async function send(data){
  return credentials(new Request(RESULT_URL,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
   body:JSON.stringify({request_id:id,exit_code:0,status:'prueba_finalizada',
     log:'OSEP_CATALOG_PACK='+deflateSync(Buffer.from(JSON.stringify(data))).toString('base64'),
     log_truncated:false})}),env,async()=>publicKey);
 }
 return {db,send};
}

test('guardado atómico confirma resultados y caché juntos, y rechaza repetición',async()=>{
 const {db,send}=await setup();
 assert.equal((await send(master())).status,200);
 assert.match(db.prepare('SELECT records_json FROM osep_catalog_relations').get().records_json,/NUEVO/);
 assert.equal(db.prepare('SELECT catalog_status FROM manual_queue').get().catalog_status,'completed');
 assert.equal((await send(master())).status,409);
});

test('fallo de almacenamiento revierte resultado final y conserva caché anterior',async()=>{
 const {db,send}=await setup(true);
 assert.notEqual((await send(master())).status,200);
 assert.match(db.prepare('SELECT records_json FROM osep_catalog_relations').get().records_json,/ANTERIOR/);
 assert.equal(db.prepare('SELECT bot_completed_at FROM dispatches').get().bot_completed_at,null);
 assert.equal(db.prepare('SELECT catalog_status FROM manual_queue').get().catalog_status,null);
});

test('error parcial o servicio distinto no cambia caché ni declara completado',async()=>{
 for(const mutate of [d=>d.errors.push(['ZONA','DEPTO']),d=>d.service='OTRO']){
  const {db,send}=await setup();const d=master();mutate(d);
  assert.equal((await send(d)).status,200);
  assert.match(db.prepare('SELECT records_json FROM osep_catalog_relations').get().records_json,/ANTERIOR/);
  assert.equal(db.prepare('SELECT catalog_status FROM manual_queue').get().catalog_status,'error');
  assert.equal(db.prepare('SELECT bot_exit_code FROM dispatches').get().bot_exit_code,3);
 }
});

