import asyncio
import json
import pathlib
import sys
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0,str(pathlib.Path(__file__).parents[1]))
from catalog_http import CatalogError, SessionHTTP, SEARCH_PATH, DEPARTMENT_PATH
from catalog_scan import scan, browser_departments, browser_search, open_new_turn_tab
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
        self.no_results_notice=False
        self.reverse_rows=False
        self.paginate_rows=False
        self.reuse_table=False
        self.keep_stale=False
        self.latin1=False
        async def route(request_route):
            request=request_route.request
            path=urlsplit(request.url).path
            if path=="/":
                body=PAGE
                if self.latin1:
                    body+='''<script>window.search=()=>{
                      const x=new XMLHttpRequest();x.open('POST','SEARCH_PATH');
                      x.setRequestHeader('Content-Type','application/x-www-form-urlencoded;charset=UTF-8');
                      x.onload=()=>document.querySelector('#lista_profesionales').innerHTML=x.responseText;
                      const p=new URLSearchParams(new FormData(document.querySelector('#datosFiltroNuevoTurno')));
                      p.set('buscar','Buscar');x.send(p.toString());
                    };</script>'''.replace('SEARCH_PATH',SEARCH_PATH)
                if self.paginate_rows:
                    body='<style>.ocultar{display:none}</style>'+body.replace("document.querySelector('#lista_profesionales').innerHTML=await r.text();", "{document.querySelector('#lista_profesionales').innerHTML=await r.text();Array.from(document.querySelectorAll('#tblResultadoProfesionales tbody tr')).slice(20).forEach(r=>r.style.display='none');}")
                if self.reuse_table:
                    body=body.replace("document.querySelector('#lista_profesionales').innerHTML=await r.text();", "document.querySelector('#tblResultadoProfesionales').innerHTML=new DOMParser().parseFromString(await r.text(),'text/html').querySelector('table').innerHTML;")
                if self.keep_stale:
                    body=body.replace("document.querySelector('#lista_profesionales').innerHTML=await r.text();", "await r.text();")
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
                if self.no_results_notice:
                    return await request_route.fulfill(content_type="text/javascript",
                        body="mostrarInfo('No se encontraron agendas en esta especialidad');")
                rows=[["1 PROFESIONAL "+fields["id_dpto"][0],"CENTRO "+fields["id_zona"][0],"S","H","D","A"]]
                if self.reverse_rows:
                    rows.append(['2 SEGUNDO PROFESIONAL','OTRO CENTRO','S','H','D','A'])
                if self.paginate_rows:
                    rows=[[str(i+1)+' PROFESIONAL '+str(i),'CENTRO<br><span class="ocultar">TEXTO OCULTO</span><br>DOMICILIO','S','H','D','A'] for i in range(21)]
                body=html(rows)
                if self.latin1:
                    body=html([['1 PROFESIONAL FICTICIO PEÑA','CENTRO FICTICIO','S','H','D','A']])
                    return await request_route.fulfill(content_type='text/html; charset=ISO-8859-1',body=body.encode('iso-8859-1'))
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

    async def test_tab_navigation_retries_once_after_transient_timeout(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        mock_page=MagicMock()
        mock_page.goto=AsyncMock()
        mock_page.wait_for_timeout=AsyncMock()
        link=MagicMock()
        link.wait_for=AsyncMock(side_effect=[PlaywrightTimeoutError('temporary'),None])
        link.click=AsyncMock()
        link_collection=MagicMock()
        link_collection.first=link
        select=MagicMock()
        select.wait_for=AsyncMock()
        select.is_visible=AsyncMock(return_value=False)
        password=MagicMock()
        password.is_visible=AsyncMock(return_value=False)
        mock_page.locator.side_effect=lambda sel: (
            link_collection if '#divTNue' in sel else
            select if sel=='#servimod' else password)
        await open_new_turn_tab(mock_page,'https://portal.example/webapp_pri',1000)
        self.assertEqual(mock_page.goto.await_count,2)
        self.assertEqual(link.click.await_count,1)
        self.assertEqual(mock_page.wait_for_timeout.await_count,1)

    async def test_tab_navigation_fails_closed_if_login_reappears(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        mock_page=MagicMock()
        mock_page.goto=AsyncMock()
        link=MagicMock()
        link.wait_for=AsyncMock(side_effect=PlaywrightTimeoutError('temporary'))
        link_collection=MagicMock()
        link_collection.first=link
        select=MagicMock()
        select.is_visible=AsyncMock(return_value=False)
        password=MagicMock()
        password.is_visible=AsyncMock(return_value=True)
        mock_page.locator.side_effect=lambda sel: (
            link_collection if '#divTNue' in sel else
            select if sel=='#servimod' else password)
        with self.assertRaisesRegex(CatalogError,'session_expired_during_catalog'):
            await open_new_turn_tab(mock_page,'https://portal.example/webapp_pri',1000)
        self.assertEqual(mock_page.goto.await_count,1)

    async def test_new_tab_timeout_diagnostic_has_only_dom_counts(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        mock_page=MagicMock()
        mock_page.goto=AsyncMock()
        mock_page.wait_for_timeout=AsyncMock()
        mock_link=MagicMock()
        mock_link.wait_for=AsyncMock(side_effect=PlaywrightTimeoutError('temporary'))
        mock_link_collection=MagicMock()
        mock_link_collection.first=mock_link
        mock_link_collection.count=AsyncMock(return_value=1)
        service=MagicMock()
        service.count=AsyncMock(return_value=1)
        service.is_visible=AsyncMock(return_value=False)
        form=MagicMock()
        form.count=AsyncMock(return_value=1)
        password=MagicMock()
        password.is_visible=AsyncMock(return_value=False)
        mock_page.locator.side_effect=lambda selector: {
            'a[href="#divTNue"], [data-bs-target="#divTNue"], [data-target="#divTNue"]':mock_link_collection,
            '#servimod':service,
            'form[name="datosFiltroNuevoTurno"]':form,
            'input[type="password"]':password
        }[selector]
        with self.assertRaisesRegex(CatalogError,'new_turn_tab_timeout') as ctx:
            await open_new_turn_tab(mock_page,'https://portal.example/webapp_pri',1000)
        self.assertEqual(ctx.exception.diagnostic,
            {'attempts':3,'stage':'tab_visible','tab_links':1,
             'service_controls':1,'forms':1})
        self.assertEqual(mock_page.goto.await_count,3)

    async def test_tab_form_visible_needs_no_tab_click(self):
        mock_page=MagicMock()
        mock_page.goto=AsyncMock()
        service=MagicMock()
        service.is_visible=AsyncMock(return_value=True)
        mock_page.locator.side_effect=lambda sel: service if sel=="#servimod" else MagicMock()
        await open_new_turn_tab(mock_page,"https://portal.example/webapp_pri",1000)
        mock_page.goto.assert_awaited_once()
        service.wait_for.assert_not_called()

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

    async def test_no_results_js_does_not_read_stale_professionals(self):
        self.no_results_notice=True
        with patch.object(SessionHTTP,"read",self.transport()):
            result,metrics=await scan(self.page,"SERVICIO","*","*",3000)
        self.assertEqual(len(result["entries"]),4)
        self.assertTrue(all(not e[2] for e in result["entries"]))
        self.assertEqual(metrics["browser_searches"],4)
        self.assertNotIn("ANTERIOR",str(result))

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

    async def test_pager_footer_and_action_changes_preserve_catalog_identity(self):
        await self.page.evaluate("""() => {
          const target=document.querySelector('#lista_profesionales');
          new MutationObserver(()=>{
            const table=target.querySelector('table');
            if(!table||table.dataset.changed)return;
            table.dataset.changed='true';
            table.querySelector('thead tr:last-child').remove();
            const footer=table.insertRow();const c=footer.insertCell();c.colSpan=6;c.textContent='Mostrando otra página';
            table.querySelector('tbody tr td:last-child').textContent='Botón de agenda actualizado';
          }).observe(target,{childList:true,subtree:true});
        }""")
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(len(records),1)

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

    async def test_later_pager_rows_render_with_hidden_cell_text_still_hidden(self):
        self.paginate_rows=True
        await self.page.reload()
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(len(records),21)
        self.assertTrue(all(center=='CENTRO' for _,center in records))
        self.assertNotIn('TEXTO OCULTO',str(records))

    async def test_latin1_response_matches_browser_rendered_accents(self):
        self.latin1=True
        await self.page.reload()
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(records,[('PROFESIONAL FICTICIO PEÑA','CENTRO FICTICIO')])

    async def test_portal_can_refresh_data_inside_the_same_table_node(self):
        self.reuse_table=True
        await self.page.reload()
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        self.assertEqual(len(await browser_search(self.page,client,'s','z1','d1',3000)),1)

    async def test_fresh_http_response_without_table_refresh_is_rejected(self):
        self.keep_stale=True
        await self.page.reload()
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        with self.assertRaisesRegex(CatalogError,'browser_table_not_refreshed'):
            await browser_search(self.page,client,'s','z1','d1',3000)

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

    async def test_filter_reset_before_search_is_restored_without_new_change_events(self):
        client=SessionHTTP(self.page)
        await browser_departments(self.page,client,'z1',3000)
        await self.page.evaluate("""() => {
          const service=document.querySelector('#servimod');
          service.add(new Option('Seleccionar servicio',''));
          document.querySelector('#id_dpto').addEventListener('change',()=>{
            service.value='';document.querySelector('#id_zona').value='';
          });
        }""")
        records=await browser_search(self.page,client,'s','z1','d1',3000)
        self.assertEqual(len(records),1)
        self.assertEqual(self.searches[0]['servimod'],['s'])
        self.assertEqual(self.searches[0]['id_zona'],['z1'])

    async def test_comparison_mode_covers_two_zones_without_partial_publication(self):
        with patch.object(SessionHTTP,"read",self.transport()):
            result,metrics=await scan(self.page,"SERVICIO","*","*",3000,True)
        self.assertIsNone(result)
        self.assertTrue(metrics["equivalent"])
        self.assertEqual(metrics["comparisons"],2)
        self.assertEqual(len(set(f["id_zona"][0] for f in self.searches)),2)


