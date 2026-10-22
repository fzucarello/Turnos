"""Diagnóstico de tráfico del módulo privado de turnos OSEP.

No abre agendas ni confirma reservas. Registra exclusivamente metadatos
sanitizados de solicitudes/respuestas XHR y fetch, sin cuerpos, cookies, JWT,
parámetros, usuarios, nombres ni datos de pacientes.
"""
import asyncio
from collections import defaultdict
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.async_api import async_playwright
from app import login, PORTAL_URL, TOUT, HEADLESS

logging.getLogger("osep").setLevel(logging.ERROR)


def safe_path(url):
    parsed = urlsplit(url)
    path = parsed.path
    # Ocultar valores que podrían identificar pacientes, sesiones o entidades.
    path = re.sub(r"/[0-9]{4,}(?=/|$)", "/:id", path)
    path = re.sub(r"/[a-f0-9-]{20,}(?=/|$)", "/:token", path, flags=re.I)
    path = re.sub(r"/[^/]{100,}(?=/|$)", "/:segment", path)
    return path[:190]  # nunca mostrar query string, fragmentos ni credenciales


def safe_body_shape(raw, mime):
    if not raw or len(raw) > 25000:
        return []
    try:
        if "json" in mime or raw.lstrip().startswith("{"):
            value = json.loads(raw)
            if isinstance(value, dict):
                return sorted(str(k)[:55] for k in value.keys())[:25]
            return ["<json-list>"] if isinstance(value, list) else ["<json-scalar>"]
        return sorted(set(str(k)[:55] for k, _ in parse_qsl(raw, keep_blank_values=True)))[:25]
    except Exception:
        return ["<unparsed>"]


def response_shape(value):
    if isinstance(value, dict):
        keys = sorted(str(k)[:55] for k in value.keys())[:25]
        arrays = {str(k)[:55]: len(v) for k, v in value.items() if isinstance(v, list)}
        return {"type": "object", "keys": keys, "arrays": arrays}
    if isinstance(value, list):
        return {
            "type": "array", "count": len(value),
            "item_keys": sorted(str(k)[:55] for k in value[0])[:25]
                if value and isinstance(value[0], dict) else [],
        }
    return {"type": type(value).__name__}


async def option_labels(page, selector):
    return await page.locator(selector).evaluate("""
       e => [...(e.options||[])].filter(o => !o.disabled && o.value &&
         (o.textContent||'').trim() && !/^(seleccione|seleccionar)/i.test((o.textContent||'').trim()))
         .map(o => (o.textContent||'').trim().replace(/\\s+/g,' '))
    """)


async def select(page, selector, label):
    await page.locator(selector).select_option(label=label, timeout=TOUT)
    await page.wait_for_timeout(1000)


async def main():
    result = {"scope": "authenticated_turnos_only", "flow": [], "network": [],
              "notes": [], "total": 0}
    phase = ["open_turnos"]
    events = []
    tasks = []
    portal = urlsplit(PORTAL_URL).hostname or ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        try:
            page = await browser.new_page()
            await login(page)  # No se registra el proceso de autenticación.
            async def capture(response):
                request = response.request
                if request.resource_type not in {"xhr", "fetch", "document"}:
                    return
                parsed = urlsplit(response.url)
                if parsed.hostname != portal or not parsed.path.startswith("/webapp_pri/"):
                    return
                mime = (response.headers.get("content-type") or "").split(";")[0].lower()
                record = {
                    "step": phase[0],
                    "method": request.method,
                    "path": safe_path(response.url),
                    "resource": request.resource_type,
                    "status": response.status,
                    "content_type": mime[:50],
                }
                body = request.post_data or ""
                fields = safe_body_shape(body, request.headers.get("content-type", "").lower())
                if fields:
                    record["request_fields"] = fields
                try:
                    data = await response.body()
                    record["bytes"] = len(data)
                    if "json" in mime and len(data) <= 1000000:
                        try:
                            record["response"] = response_shape(json.loads(data))
                        except Exception:
                            record["response"] = {"type": "invalid_json"}
                    elif "html" in mime:
                        record["response"] = {
                            "type": "html",
                            "table_rows_approx": len(re.findall(rb"<tr\b", data, flags=re.I)),
                            "select_options_approx": len(re.findall(rb"<option\b", data, flags=re.I)),
                        }
                except Exception:
                    record["response"] = {"type": "unavailable"}
                events.append(record)

            page.on("response", lambda resp: tasks.append(asyncio.create_task(capture(resp))))
            url = PORTAL_URL.rstrip("/") + "/action/applicationAfi/turnos/turno/listar/listarCompleto"
            await page.goto(url, wait_until="domcontentloaded", timeout=TOUT)
            await page.locator('a.nav-link[href="#divTNue"]').first.click(timeout=TOUT)
            await page.locator("#servimod").wait_for(timeout=TOUT)
            await page.wait_for_timeout(900)
            services = await option_labels(page, "#servimod")
            result["flow"].append({"step": "open_turnos", "specialties": len(services)})

            chosen = os.getenv("OBJ_SERVICIO", "").strip()
            if chosen not in services:
                chosen = next((x for x in services if "GINECOLOG" in x.upper()),services[0])
            phase[0] = "change_specialty_1"
            await select(page, "#servimod", chosen)
            zones = await option_labels(page, "#id_zona")
            result["flow"].append({"step": phase[0], "zones": len(zones)})

            requested_zone = os.getenv("OBJ_ZONA","").strip()
            zone = requested_zone if requested_zone in zones else zones[0]
            phase[0] = "change_zone_1"
            await select(page, "#id_zona", zone)
            depts = await option_labels(page, "#id_dpto")
            result["flow"].append({"step": phase[0], "departments": len(depts)})

            first = os.getenv("OBJ_DEPTO","").strip()
            if first not in depts:
                first = depts[0]
            phase[0] = "change_department_1"
            await select(page, "#id_dpto", first)
            phase[0] = "search_professionals_1"
            await page.locator("input#buscar").first.click(timeout=TOUT)
            await page.wait_for_timeout(1300)
            count = await page.locator("#tblResultadoProfesionales tbody tr").count()
            result["flow"].append({"step": phase[0], "visible_rows": count})

            if len(depts) > 1:
                other = next(x for x in depts if x != first)
                phase[0] = "change_department_2"
                await select(page, "#id_dpto", other)
                phase[0] = "search_professionals_2"
                await page.locator("input#buscar").first.click(timeout=TOUT)
                await page.wait_for_timeout(1300)
                result["flow"].append({"step": phase[0],
                                       "visible_rows": await page.locator("#tblResultadoProfesionales tbody tr").count()})

            if len(services) > 1:
                second = next((x for x in services if "DERMATOLOG" in x.upper() and x != chosen),
                              next(x for x in services if x != chosen))
                phase[0] = "change_specialty_2"
                await select(page, "#servimod", second)
                zones2 = await option_labels(page, "#id_zona")
                result["flow"].append({"step": phase[0], "zones": len(zones2)})
                if zones2:
                    phase[0] = "change_zone_2"
                    await select(page, "#id_zona", zones2[0])
                    depts2 = await option_labels(page, "#id_dpto")
                    result["flow"].append({"step": phase[0], "departments": len(depts2)})
                    if depts2:
                        phase[0] = "change_department_3"
                        await select(page, "#id_dpto", depts2[0])
                        phase[0] = "search_professionals_3"
                        await page.locator("input#buscar").first.click(timeout=TOUT)
                        await page.wait_for_timeout(1300)
                        result["flow"].append({"step": phase[0],
                                               "visible_rows": await page.locator("#tblResultadoProfesionales tbody tr").count()})
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await browser.close()

    # Publicar únicamente metadatos técnicos: evitar datos de pacientes y de login.
    result["network"] = events[:90]
    result["total"] = len(events)
    print("OSEP_TRAFFIC_SUMMARY="+json.dumps(result, ensure_ascii=False,separators=(",",":")),flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print("OSEP_TRAFFIC_ERROR="+type(exc).__name__,flush=True)
        sys.exit(3)
