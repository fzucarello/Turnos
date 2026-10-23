"""Sincroniza sólo el catálogo del módulo privado, sin abrir agendas."""
import asyncio
import base64
import json
import logging
import os
import sys
import time
import zlib
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from catalog_http import (CatalogError, CatalogNoResults, SessionHTTP, SEARCH_PATH, DEPARTMENT_PATH,
                          checked_response, parse_options, parse_results, records_from_rows, mismatch_diagnostic)

CURRENT_STAGE = "start"

def phase(code):
    global CURRENT_STAGE
    CURRENT_STAGE = code
    if os.getenv('OBJ_DEPTO') == '__CATALOG_COMPARE__':
        print('OSEP_CATALOG_PHASE='+code,flush=True)


async def option_pairs(page, selector):
    return await page.locator(selector).evaluate("""
      node => Array.from(node.options || [])
        .filter(o => o.value && !o.disabled && !/^(seleccione|seleccionar)/i.test((o.textContent||'').trim()))
        .map(o => [o.value,(o.textContent||'').replace(/\\s+/g,' ').trim()])
    """)


def choose(pairs, label):
    matches = [v for v, text in pairs if text == label.strip()]
    if len(matches) != 1:
        raise CatalogError("option_missing_or_ambiguous")
    return matches[0]


async def browser_departments(page, client, zone, timeout):
    phase('department_response')
    async with page.expect_response(
        lambda r: urlsplit(r.url).path == DEPARTMENT_PATH and
        dict(parse_qsl(urlsplit(r.url).query)).get("id_zona") == zone, timeout=timeout
    ) as received:
        await page.locator("#id_zona").select_option(value=zone, timeout=timeout)
    response = await received.value
    pairs = parse_options(await checked_response(response,browser=True))
    client.department_url = response.request.url
    phase('department_dom')
    await page.wait_for_function("""
      expected => JSON.stringify(Array.from(document.querySelector('#id_dpto')?.options||[])
        .filter(o=>o.value&&!o.disabled&&!/^(seleccione|seleccionar)/i.test((o.textContent||'').trim()))
        .map(o=>[o.value,(o.textContent||'').replace(/\\s+/g,' ').trim()])) === JSON.stringify(expected)
    """, arg=pairs, timeout=timeout)
    return pairs


async def browser_search(page, client, service, zone, department, timeout):
    phase('search_controls')
    await page.locator("#id_dpto").select_option(value=department, timeout=timeout)
    expected = {"servimod": service, "id_zona": zone, "id_dpto": department}
    def matches(response):
        request = response.request
        if urlsplit(request.url).path != SEARCH_PATH or request.method != "POST":
            return False
        fields = dict(parse_qsl(request.post_data or "", keep_blank_values=True))
        if os.getenv('OBJ_DEPTO')=='__CATALOG_COMPARE__':
            print('OSEP_CATALOG_REQUEST_MATCH='+json.dumps({k:fields.get(k)==v for k,v in expected.items()},separators=(',',':')),flush=True)
        return all(fields.get(k) == v for k, v in expected.items())
    # Sólo se espera una respuesta iniciada por este clic y con estos filtros.
    phase('search_response')
    async with page.expect_response(matches, timeout=timeout) as received:
        form=page.locator('form[name="datosFiltroNuevoTurno"]')
        meta=await form.evaluate("""(form,expected)=>{
          const before=Object.fromEntries(Object.entries(expected).map(([key,value])=>[key,document.querySelector('#'+key)?.value===value]));
          const allowed=Object.entries(expected).every(([key,value])=>
            Array.from(document.querySelector('#'+key)?.options||[]).some(o=>o.value===value&&!o.disabled));
          if(allowed)for(const [key,value] of Object.entries(expected))document.querySelector('#'+key).value=value;
          return {
          allowed_options:allowed,
          restored_fields:Object.keys(before).filter(key=>!before[key]),
          html_valid:form.checkValidity(),
          controls_valid:Object.keys(expected).every(key=>document.querySelector('#'+key)?.checkValidity()),
          fields:Object.fromEntries(Object.entries(expected).map(([key,value])=>[key,document.querySelector('#'+key)?.value===value])),
          form_fields:Object.fromEntries(Object.entries(expected).map(([key,value])=>[key,form.elements.namedItem(key)?.value===value])),
          associated_fields:Object.fromEntries(Object.keys(expected).map(key=>[key,document.querySelector('#'+key)?.form===form])),
          native_function:typeof window.buscarProfesionales==='function'
        }}""",expected)
        if os.getenv('OBJ_DEPTO')=='__CATALOG_COMPARE__':
            print('OSEP_CATALOG_FORM_CHECK='+json.dumps(meta,separators=(',',':')),flush=True)
        if not meta['allowed_options'] or not meta['html_valid'] or not meta['controls_valid'] or not all(meta['fields'].values()):
            raise CatalogError('search_form_invalid_or_changed')
        await page.evaluate("""() => {
          window.__osepCatalogPending?.observer?.disconnect();
          const state={node:document.querySelector('#tblResultadoProfesionales'),changed:false};
          state.observer=new MutationObserver(changes=>{
            if(document.querySelector('#tblResultadoProfesionales')!==state.node ||
              changes.some(m=>(m.target.nodeType===1?m.target:m.target.parentElement)?.closest('#tblResultadoProfesionales')))
              state.changed=true;
          });
          state.observer.observe(document.body,{subtree:true,childList:true,characterData:true});
          window.__osepCatalogPending=state;
        }""")
        if meta['native_function']:
            phase('search_native_action')
            await page.evaluate('() => window.buscarProfesionales()')
        else:
            phase('search_click')
            await form.locator('input#buscar').click(timeout=timeout)
        phase('search_response')
    response = await received.value
    try:
        html = await checked_response(response,browser=True)
    except CatalogNoResults:
        # La respuesta del POST corresponde a estos filtros y niega explícitamente
        # la existencia de agendas. No leer la tabla anterior del navegador.
        await page.evaluate('() => window.__osepCatalogPending?.observer?.disconnect()')
        client.remember_search(response.request)
        phase('explicit_no_agendas')
        return []
    records = parse_results(html)
    client.remember_search(response.request)
    # Contrastar también con la tabla que acaba de insertar la interfaz.
    phase('search_dom')
    try:
        await page.wait_for_function("""() => {
          const live=document.querySelector('#tblResultadoProfesionales');
          const state=window.__osepCatalogPending;
          return live && state && (live!==state.node || state.changed);
        }""", timeout=timeout)
    except Exception:
        diagnostic=await page.evaluate("""html=>{
          const incoming=new DOMParser().parseFromString(html,'text/html').querySelector('#tblResultadoProfesionales');
          const live=document.querySelector('#tblResultadoProfesionales');
          const shape=t=>({present:!!t,rows:t?.querySelectorAll('tr').length||0,
            columns:Array.from(t?.querySelectorAll('tr')||[]).map(r=>r.querySelectorAll(':scope > td').length)});
          return {incoming:shape(incoming),live:shape(live)};
        }""",html)
        raise CatalogError('browser_table_not_refreshed',diagnostic) from None
    finally:
        await page.evaluate('() => window.__osepCatalogPending?.observer?.disconnect()')
    # Comparar además los pares que efectivamente muestra el paginado del
    # navegador, incluyendo su tratamiento de texto oculto y saltos de línea.
    await page.evaluate("""() => {
      const pager=window.tblResultadoProfesionales;
      if(pager && typeof pager.showall==='function')pager.showall();
      // Algunos paginadores sólo ocultan filas con display:none. Mostrar las
      // filas de datos permite usar innerText real, que respeta texto oculto
      // dentro de las celdas, también en páginas posteriores a la primera.
      const rows=Array.from(document.querySelectorAll('#tblResultadoProfesionales tbody tr'))
        .filter(r=>r.querySelectorAll(':scope > td').length>=6);
      if(rows.length>500)throw new Error('row_limit');
      rows.forEach(r=>r.style.setProperty('display','table-row','important'));
    }""")
    phase('rendered_records')
    visible_rows=await page.locator('#tblResultadoProfesionales tbody tr').evaluate_all("""
      rows => rows.map(r=>Array.from(r.querySelectorAll(':scope > td')))
        .filter(cells=>cells.length>=6).map(cells=>cells.map(c=>c.innerText))
    """)
    rendered=records_from_rows(visible_rows)
    if rendered != records:
        raise CatalogError('browser_result_mismatch',mismatch_diagnostic(records,rendered))
    return records


async def scan(page, service_label, zone_label, department_label, timeout, compare_only=False):
    started = time.monotonic()
    client = SessionHTTP(page, timeout)
    services = await option_pairs(page, "#servimod")
    service = choose(services, service_label)
    await page.locator("#servimod").select_option(value=service, timeout=timeout)
    zones = await option_pairs(page, "#id_zona")
    if not zones or len(zones) > 20 or len(services) > 250:
        raise CatalogError("catalog_option_limit")
    master = zone_label == "*" and department_label == "*"
    selected = zones if master else [(choose(zones, zone_label), zone_label)]
    result = {"scope": "master", "captureVersion": 2, "service": service_label,
              "services": [n for _, n in services], "zones": [n for _, n in zones],
              "departmentsByZone": {}, "entries": [], "errors": []}
    metrics = {"version": 2, "browser_searches": 0, "comparisons": 0,
               "http_searches": 0, "fallbacks": 0, "browser_ms": 0, "http_ms": 0}
    http_enabled = True
    total = 0
    for zi, (zone, zone_name) in enumerate(selected):
        # El primer GET se observa en la interfaz; los siguientes conservan
        # exactamente su ruta y contexto, cambiando sólo la zona elegida.
        if client.department_url is None or not http_enabled or compare_only:
            departments = await browser_departments(page, client, zone, timeout)
        else:
            departments = await client.departments(zone)
        result["departmentsByZone"][zone_name] = [n for _, n in departments]
        wanted = departments if master else [(choose(departments, department_label), department_label)]
        for di, (department, department_name) in enumerate(wanted):
            total += 1
            if total > 80:
                raise CatalogError("combination_limit")
            verify = metrics["comparisons"] < 2 or not http_enabled
            if verify:
                tick = time.monotonic()
                records = await browser_search(page, client, service, zone, department, timeout)
                metrics["browser_ms"] += round((time.monotonic() - tick) * 1000)
                metrics["browser_searches"] += 1
                if http_enabled:
                    phase('http_comparison')
                    tick = time.monotonic()
                    direct = await client.search(service, zone, department)
                    metrics["http_ms"] += round((time.monotonic() - tick) * 1000)
                    metrics["http_searches"] += 1
                    if direct != records:
                        http_enabled = False
                        metrics["fallbacks"] += 1
                    else:
                        metrics["comparisons"] += 1
            else:
                phase('http_search')
                records = await client.search(service, zone, department)
                metrics["http_searches"] += 1
            result["entries"].append([zone_name, department_name, [list(r) for r in records]])
            # Muestra de dos combinaciones, sin publicar catálogo parcial.
            if compare_only and total == 2:
                metrics.update(combinations=total, http_requests=client.calls, retries=client.retries,
                               duration_ms=round((time.monotonic()-started)*1000),
                               equivalent=http_enabled and metrics["comparisons"] == 2)
                return None, metrics
            if compare_only and len(selected) > 1:
                break
            await asyncio.sleep(0.75)
    metrics.update(combinations=total, http_requests=client.calls, retries=client.retries,
                   duration_ms=round((time.monotonic()-started)*1000),
                   equivalent=http_enabled and metrics["comparisons"] >= min(2,total))
    if compare_only:
        raise CatalogError("insufficient_comparison_sample")
    if not master:
        records = result["entries"][0][2]
        return {"captureVersion":2,"services":result["services"],"zones":result["zones"],
                "departments":result["departmentsByZone"][zone_label],
                "centers":sorted(set(r[1] for r in records)),
                "professionals":sorted(set(r[0] for r in records)),
                "departmentsByZone":result["departmentsByZone"],
                "selectedService":service_label,"selectedZone":zone_label,
                "selectedDepartment":department_label}, metrics
    return result, metrics


async def main():
    # Importar el bot sólo para el login existente. Su función de reservas nunca se llama.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from playwright.async_api import async_playwright
    from app import login, PORTAL_URL, TOUT, HEADLESS
    logging.getLogger("osep").setLevel(logging.WARNING)
    compare_only = os.getenv("OBJ_DEPTO") == "__CATALOG_COMPARE__"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS,
                                        channel="chrome" if os.getenv('GITHUB_ACTIONS')=='true' else None)
        try:
            page = await browser.new_page()
            phase('login')
            await login(page)
            phase('open_turnos')
            await page.goto(PORTAL_URL.rstrip("/") + "/action/applicationAfi/turnos/turno/listar/listarCompleto",
                            wait_until="domcontentloaded", timeout=TOUT)
            phase('new_turn_tab')
            await page.locator('a.nav-link[href="#divTNue"]').first.click(timeout=TOUT)
            await page.locator("#servimod").wait_for(timeout=TOUT)
            # Acotar toda exploración a 330s, antes del límite externo de 420s.
            phase('scan')
            async with asyncio.timeout(330):
                result, metrics = await scan(page, os.environ["OBJ_SERVICIO"].strip(),
                                             "*" if compare_only else os.environ["OBJ_ZONA"].strip(),
                                             "*" if compare_only else os.environ["OBJ_DEPTO"].strip(),
                                             min(TOUT, 30000), compare_only)
            print("OSEP_CATALOG_METRICS=" + json.dumps(metrics,separators=(",",":")), flush=True)
            if compare_only:
                return 0 if metrics["equivalent"] else 3
            if result.get("scope") == "master":
                packed = base64.b64encode(zlib.compress(json.dumps(
                    result, ensure_ascii=False,separators=(",",":")).encode("utf-8"),9)).decode("ascii")
                if len(packed) > 26000:
                    raise CatalogError("pack_limit")
                print("OSEP_CATALOG_PACK=" + packed, flush=True)
            else:
                print("OSEP_CATALOG_JSON=" + json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)
            return 0
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        code = str(exc) if isinstance(exc, CatalogError) else type(exc).__name__
        if isinstance(exc,CatalogError) and exc.diagnostic is not None:
            print('OSEP_CATALOG_DIAGNOSTIC='+json.dumps(exc.diagnostic,separators=(',',':')),flush=True)
        print('OSEP_CATALOG_PHASE='+CURRENT_STAGE,flush=True)
        print("OSEP_CATALOG_ERROR=" + code, flush=True)
        sys.exit(3)


