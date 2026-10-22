"""Descubre catálogos visibles en OSEP sin abrir agendas ni reservar turnos.

Recorre el mismo login y selectores ya utilizados por app.py.
Solo obtiene opciones para la especialidad, zona y departamento seleccionados.
"""
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.async_api import async_playwright
from app import login, PORTAL_URL, TOUT, HEADLESS

logging.getLogger("osep").setLevel(logging.WARNING)

async def options(page, selector):
    return await page.locator(selector).evaluate("""
      node => Array.from(node.options || [])
        .filter(o => o.value && !o.disabled && (o.textContent || '').trim())
        .map(o => (o.textContent || '').replace(/\\s+/g,' ').trim())
    """)

async def pick(page, selector, value):
    node = page.locator(selector)
    await node.wait_for(timeout=TOUT)
    try:
        await node.select_option(label=value.strip(),timeout=TOUT)
    except Exception:
        # Nunca seleccionar una opción alternativa por suposición.
        raise ValueError("La opción actual no figura en OSEP: " + selector)
    await page.wait_for_timeout(700)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        try:
            page = await browser.new_page()
            await login(page)
            url = PORTAL_URL.rstrip("/") + "/action/applicationAfi/turnos/turno/listar/listarCompleto"
            await page.goto(url,wait_until="domcontentloaded",timeout=TOUT)
            link = page.locator('a.nav-link[href="#divTNue"]').first
            await link.click(timeout=TOUT)
            await page.wait_for_timeout(800)
            await page.locator("#servimod").wait_for(timeout=TOUT)
            services = await options(page, "#servimod")
            selected_service = os.environ["OBJ_SERVICIO"].strip()
            await pick(page, "#servimod", selected_service)
            zones = await options(page, "#id_zona")
            selected_zone = os.environ["OBJ_ZONA"].strip()
            await pick(page, "#id_zona", selected_zone)
            departments = await options(page, "#id_dpto")
            selected_department = os.environ["OBJ_DEPTO"].strip()
            await pick(page, "#id_dpto", selected_department)
            centers = []
            professionals = []
            try:
                await page.locator("input#buscar").first.click(timeout=TOUT)
                await page.locator("#tblResultadoProfesionales").wait_for(timeout=TOUT)
                await page.wait_for_timeout(700)
                records = await page.locator("#tblResultadoProfesionales tbody tr").evaluate_all("""
                    rows => rows.map(row => {
                      const td = Array.from(row.querySelectorAll('td'));
                      return {
                        doctor: (td[0]?.innerText || '').trim(),
                        center: (td[1]?.innerText || '').trim()
                      };
                    })
                """)
                for record in records:
                    domicilio = re.sub(r"\\s+", " ", str(record.get("center", "")).strip())
                    profesional = re.sub(r"^\\d+\\s+", "", str(record.get("doctor", "")).strip())
                    if not domicilio:
                        continue
                    if "OSEP HUNUC HUAR" in domicilio.upper():
                        nombre = "OSEP HUNUC HUAR"
                    else:
                        nombre = domicilio
                        if profesional and nombre.upper().startswith(profesional.upper()):
                            nombre = nombre[len(profesional):].strip()
                        nombre = re.split(r"\\b(?:PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA) SECCION\\b|\\(\\d{4}\\)|\\bTEL\\.?", nombre, flags=re.I)[0].strip()
                    if nombre:
                        centers.append(nombre[:120])
                centers=sorted(set(centers))
                professionals=sorted(set(re.sub(r"^\\d+\\s+", "", str(r.get("doctor","")).strip()) for r in records if r.get("doctor")))
            except Exception:
                pass  # Los desplegables ya son útiles; los resultados pueden no estar disponibles.
            result = {
              "services":services[:250],"zones":zones[:100],"departments":departments[:150],
              "centers":centers[:250],"professionals":professionals[:250],
              "selectedService":selected_service,"selectedZone":selected_zone,
              "selectedDepartment":selected_department
            }
            print("OSEP_CATALOG_JSON="+json.dumps(result,ensure_ascii=False,separators=(",",":")),flush=True)
            return 0
        finally:
            await browser.close()

if __name__=="__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print("No fue posible actualizar las opciones desde el portal ("+type(exc).__name__+").",flush=True)
        sys.exit(3)
