import asyncio
import json
import pathlib
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0,str(pathlib.Path(__file__).parents[1]))
from catalog_http import CatalogError, SessionHTTP, SEARCH_PATH, DEPARTMENT_PATH
from catalog_scan import scan, browser_departments, browser_search
from test_catalog_http import html

PAGE = """
<form name="hiddenPrevious"><input type="button" id="buscar" style="display:none"></form>
<form id="datosFiltroNuevoTurno" name="datosFiltroNuevoTurno">
<select id="servimod" name="servimod"><option value="s">SERVICIO</option></select>
<select id="id_zona" name="id_zona" onchange="departments()">
<option value="">Seleccionar zona</option><option value="z1">ZONA UNO</option><option value="z2">ZONA DOS</option></select>
<select id="id_dpto" name="id_dpto"></select>
<input name="id_tmovafi" value="PRIVATE_FAKE">
<input id="buscar" type="button" value="Buscar" onclick="search()">
</form><div id="lista_profesionales">OLD_TABLE</div>
<script>
async function departments(){
 const r=await fetch('DEPARTMENT_PATH?id_zona='+document.querySelector('#id_zona').value);
 document.querySelector('#id_dpto').innerHTML=await r.text();
}
async function search(){
 const p=new URLSearchParams(new FormData(document.querySelector('#datosFiltroNuevoTurno')));
 p.set('buscar','Buscar');
 const r=await fetch('SEARCH_PATH',{method:'POST',body:p});
 if(r.headers.get('content-type')==='text/html')
   document.querySelector('#lista_profesionales').innerHTML=await r.text();
}
</script>
""".replace("SEARCH_PATH",SEARCH_PATH).replace("DEPARTMENT_PATH",DEPARTMENT_PATH).replace(
 "OLD_TABLE",html([["9 ANTERIOR","CENTRO ANTERIOR","S","H","D","A"]]))

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p = await async_playwright().start()
        self.browser=await self.p.chromium.launch(channel="chrome",headless=True)
        self.page=await self.browser.new_page()
        self.searches=[]
        self.javascript=False
        self.reverse_rows=False
        async def route(request_route):
            request=request_route.request
            path=urlsplit(request.url).path
            if path=="/":
                body=PAGE
                if self.reverse_rows:
                    body=body.replace("document.querySelector('#lista_profesionales').innerHTML=await r.text();", "{ const dom=new DOMParser().parseFromString(await r.text(),'text/html'); const tb=dom.querySelector('tbody'); Array.from(tb.children).reverse().forEach(tr=>tb.appendChild(tr)); document.querySelector('#lista_profesionales').innerHTML=dom.body.innerHTML; }")
                return await request_route.fulfill(content_type="text/html",body=body)
            if path==DEPARTMENT_PATH:
                await asyncio.sleep(0.03)
                return await request_route.fulfill(content_type="text/html",body='<option value="">Seleccionar</option><option value="d1">UNO</option><option value="d2">DOS</option>')
            if path==SEARCH_PATH:
                fields=parse_qs(request.post_data)
                self.searches.append(fields)
                await asyncio.sleep(0.03)
                if self.javascript:
                    return await request_route.fulfill(content_type="text/javascript",body="alert('error');")
                rows=[["1 PROFESIONAL "+fields["id_dpto"][0],"CENTRO "+fields["id_zona"][0],"S","H","D","A"]]
                if self.reverse_rows:
                    rows.append(['2 SEGUNDO PROFESIONAL','OTRO CENTRO','S','H','D','A'])
                body=html(rows)
                return await request_route.fulfill(content_type="text/html",body=body)
            raise AssertionError("Unexpected endpoint")
        await self.page.route("https://portal.example/**",route)
        await self.page.goto("https://portal.example/")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    def transport(self,mismatch=False):
        async def read(client,url,method="GET",fields=None):
            client.calls+=1
            await asyncio.sleep(0.01)
            if urlsplit(url).path==DEPARTMENT_PATH:
                return '<option value="d1">UNO</option><option value="d2">DOS</option>'
            return html([["1 PROFESIONAL "+fields["id_dpto"],"CENTRO "+("DIFERENTE" if mismatch else fields["id_zona"]),"S","H","D","A"]])
        return read

    async def test_complete_scan_uses_fresh_response_then_http(self):
        with patch.object(SessionHTTP,"read",self.transport()):
            result,metrics=await scan(self.page,"SERVICIO","*","*",3000)
        self.assertEqual(len(result["entries"]),4)
        self.assertEqual(metrics["browser_searches"],2)
        self.assertEqual(metrics["http_searches"],4)
        self.assertEqual(metrics["comparisons"],2)
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["captureVersion"],2)
        self.assertNotIn("ANTERIOR",json.dumps(result))

    async def test_mismatch_falls_back_to_browser_for_every_combination(self):
        with patch.object(SessionHTTP,"read",self.transport(mismatch=True)):
            result,metrics=await scan(self.page,"SERVICIO","*","*",3000)
        self.assertEqual(metrics["fallbacks"],1)
        self.assertEqual(metrics["browser_searches"],4)
        self.assertEqual(len(result["entries"]),4)
        self.assertNotIn("DIFERENTE",json.dumps(result))

    async def test_javascript_with_old_table_never_produces_catalog(self):
        self.javascript=True
        with self.assertRaisesRegex(CatalogError,"unexpected_content_type"):
            await scan(self.page,"SERVICIO","*","*",3000)
        self.assertIn("ANTERIOR",await self.page.locator("#tblResultadoProfesionales").inner_text())

    async def test_rendered_record_mismatch_is_rejected(self):
        await self.page.add_style_tag(content='td:nth-child(2) { text-transform: uppercase }')
        with self.assertRaisesRegex(CatalogError,'browser_result_mismatch'):
            await scan(self.page,'SERVICIO','*','*',3000)

    async def test_client_side_sorting_preserves_equivalence(self):
        self.reverse_rows=True
        await self.page.reload()
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(len(records),2)

    async def test_native_search_action_emits_the_same_validated_request(self):
        await self.page.evaluate("""() => {
          window.buscarProfesionales=search;
          document.querySelector('#datosFiltroNuevoTurno #buscar').onclick=()=>{throw Error('Wrong action')};
        }""")
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(len(records),1)
        self.assertEqual(len(self.searches),1)

    async def test_selected_controls_are_checked_independently_of_legacy_named_items(self):
        await self.page.evaluate("""() => {
          const elements=document.querySelector('#datosFiltroNuevoTurno').elements;
          const original=elements.namedItem.bind(elements);
          elements.namedItem=name=>['servimod','id_zona'].includes(name)?null:original(name);
        }""")
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(len(records),1)

    async def test_comparison_mode_covers_two_zones_without_partial_publication(self):
        with patch.object(SessionHTTP,"read",self.transport()):
            result,metrics=await scan(self.page,"SERVICIO","*","*",3000,True)
        self.assertIsNone(result)
        self.assertTrue(metrics["equivalent"])
        self.assertEqual(metrics["comparisons"],2)
        self.assertEqual(len(set(f["id_zona"][0] for f in self.searches)),2)

