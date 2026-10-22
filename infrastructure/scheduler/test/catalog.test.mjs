import test from 'node:test';
import assert from 'node:assert/strict';
import { deflateSync } from 'node:zlib';
import { parseMaster } from '../src/credentials.ts';

const valid=()=>({scope:'master',captureVersion:2,service:'SERVICIO FICTICIO',
 services:['SERVICIO FICTICIO'],zones:['ZONA'],departmentsByZone:{ZONA:['UNO','DOS']},
 entries:[['ZONA','UNO',[['PROFESIONAL','CENTRO']]],['ZONA','DOS',[]]],errors:[]});
const packed=d=>'OSEP_CATALOG_PACK='+deflateSync(Buffer.from(JSON.stringify(d))).toString('base64');

test('catálogo completo acepta vacío verificado y conserva todas las relaciones',async()=>{
 assert.deepEqual((await parseMaster(packed(valid()))).entries,valid().entries);
});

for(const [name,change] of [
 ['método antiguo',d=>delete d.captureVersion],
 ['error parcial',d=>d.errors.push(['ZONA','DOS'])],
 ['departamento omitido',d=>d.entries.pop()],
 ['duplicado',d=>d.entries[1]=d.entries[0]],
 ['zona inexistente',d=>d.entries[1][0]='OTRA'],
 ['departamento ajeno',d=>d.entries[1][1]='OTRO'],
 ['zona no recorrida',d=>d.zones.push('OTRA')],
 ['zona vacía',d=>d.zones=[]],
 ['límite de filas',d=>d.entries[0][2]=Array.from({length:501},()=>['P','C'])],
 ['servicio fuera del catálogo',d=>d.service='OTRO']
]){
 test('no publica '+name,async()=>{
   const d=valid();change(d);assert.equal(await parseMaster(packed(d)),null);
 });
}
test('paquete inválido, demasiado grande o incompleto no se publica',async()=>{
 for(const log of ['', 'OSEP_CATALOG_PACK=not-deflate','OSEP_CATALOG_PACK='+'A'.repeat(26001)])
   assert.equal(await parseMaster(log),null);
});

