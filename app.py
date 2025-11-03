import os, sys, re, pathlib, logging, datetime as dt, traceback, asyncio, threading
from dotenv import load_dotenv
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================== ENV / CONFIG ==================
load_dotenv()

PORTAL_URL = os.getenv("PORTAL_URL", "https://www.osep.mendoza.gov.ar/webapp_pri")
OSEP_USER  = os.getenv("OSEP_USER")
OSEP_PASS  = os.getenv("OSEP_PASS")

# Objetivos originales (algunos quedan para futuro)
OBJ_FECHA_TXT = os.getenv("OBJ_FECHA_TXT")
OBJ_HORA_TXT  = os.getenv("OBJ_HORA_TXT")
OBJ_SERVICIO  = os.getenv("OBJ_SERVICIO")
OBJ_MEDICO    = os.getenv("OBJ_MEDICO", "false")  # si "false" => no filtra por profesional
OBJ_SEDE_TXT  = os.getenv("OBJ_SEDE_TXT")
OBJ_ZONA   = os.getenv("OBJ_ZONA")
OBJ_DEPTO  = os.getenv("OBJ_DEPTO")  # agregado; si querés otro, definilo en .env
OBJ_FECHA_FLEXIBLE = os.getenv("OBJ_FECHA_FLEXIBLE", "true").lower() == "true"
OBJ_DIA_FLEXIBLE   = os.getenv("OBJ_DIA_FLEXIBLE", "true").lower() == "true"
OBJ_HORA_FLEXIBLE  = os.getenv("OBJ_HORA_FLEXIBLE", "true").lower() == "true"


HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
TOUT     = int(os.getenv("TIMEOUT_MS", "20000"))

DRY_RUN            = os.getenv("DRY_RUN", "false").lower() == "true"
STOP_AFTER_LOGIN   = os.getenv("STOP_AFTER_LOGIN", "false").lower() == "true"
# STOP_AFTER_IFRAME ignorado en este flujo nuevo
# STOP_AFTER_IFRAME  = os.getenv("STOP_AFTER_IFRAME", "false").lower() == "true"


# ================== EVIDENCIAS (DESACTIVADO PARA EJECUCIÓN SIMPLE) ==================
EVID = None  # No se crearán carpetas ni archivos de evidencia

# =============================================================================
# BASE_DIR  = pathlib.Path(r"C:\Users\Francesco Zucarello\OneDrive - Estudio Trípoli\Personal\Scripts\Turnos OSEP")
# EVID_ROOT = BASE_DIR
# RUN_ID    = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
# EVID      = EVID_ROOT / f"evidencia_{RUN_ID}"
# EVID.mkdir(parents=True, exist_ok=True)
# =============================================================================

# ================== LOGGING (solo a archivo + consola) ==================
log = logging.getLogger("osep")
log.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

log.info("Logging iniciado (solo consola, sin archivo).")

# =============================================================================
# log_file = EVID / "log.txt"
# file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
# file_handler.setFormatter(formatter)
# log.addHandler(file_handler)
# 
# log.info(f"Logging iniciado. Archivo: {log_file}")
# =============================================================================

# ================== UTILS ==================
def must_env(var: str):
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"Falta variable de entorno {var}")
    return val

async def short_sleep(seconds: float = 1.0):
    await asyncio.sleep(seconds)

async def wait_blocker_gone(page, timeout_ms: int = 15000):
    """
    Intenta detectar overlays típicos (blockUI / overlays / toasts) y esperar a que desaparezcan.
    """
    end = dt.datetime.now() + dt.timedelta(milliseconds=timeout_ms)
    sel = ".blockUI, .blockOverlay, .blockMsg, .ui-blocker, .blocker, .jq-toast-wrap, .toast, .loading, .modal-backdrop.show"
    while dt.datetime.now() < end:
        try:
            cnt = await page.locator(sel).count()
            if cnt == 0:
                # Damos un toque de gracia y nos vamos
                await asyncio.sleep(0.2)
                return
        except Exception:
            # Si falla el locator, igual seguimos intentando hasta timeout
            pass
        await asyncio.sleep(0.2)
    log.debug("wait_blocker_gone: timeout esperando que desaparezca el overlay.")

# ================== FLOWS ==================
async def login(page):
    log.info("1) Navegando al portal…")
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=TOUT)

    log.info("2) Login (usuario/contraseña)…")
    user_sel = 'input[name="usuario"], input#usuario, input[placeholder*="Usuario" i]'
    pass_sel = 'input[name="password"], input#password, input[placeholder*="Contraseña" i], input[type="password"]'
    await page.locator(user_sel).first.wait_for(timeout=TOUT)
    await page.fill(user_sel, must_env("OSEP_USER"))
    await page.fill(pass_sel, must_env("OSEP_PASS"))

    btn = page.get_by_role("button", name=re.compile(r"(ingresar|entrar|acceder|login)", re.I)).first
    if not await btn.is_visible():
        btn = page.locator('button:has-text("Ingresar"), input[type="submit"], a:has-text("Ingresar")').first
    await btn.click(timeout=TOUT)

    await page.wait_for_load_state("networkidle", timeout=TOUT)
    log.info("Login: OK (si seguís viendo la pantalla de login, hay que ajustar selectores).")

async def flujo_turnos_nuevo(page):
    """
    Luego del login:
      - Navegar a listarCompleto
      - Click en pestaña 'Nuevo'
      - Seleccionar Servicio (servimod), Zona (id_zona), Depto (id_dpto)
      - Si OBJ_MEDICO == 'false' => click Buscar
        Else: escribir profesional, Enter, esperar overlay, luego click Buscar
      - Esperar y leer tabla #tblResultadoProfesionales, volcar filas al log
      - Mantener la ventana abierta para inspección manual
    """
    # 3) Navegación directa
    listar_url = PORTAL_URL.rstrip("/") + "/action/applicationAfi/turnos/turno/listar/listarCompleto"
    log.info(f"3) Navegando directo a listarCompleto: {listar_url}")
    await page.goto(listar_url, wait_until="domcontentloaded", timeout=TOUT)

    # 4) Click en pestaña "Nuevo"
    log.info("4) Click en pestaña 'Nuevo'…")
    # Varios selectores por las dudas:
    nuevo_link = page.locator('a.nav-link[href="#divTNue"] >> text=Nuevo').first
    if not await nuevo_link.is_visible():
        nuevo_link = page.locator('a.nav-link[href="#divTNue"]').first
    await nuevo_link.click(timeout=TOUT)
    await short_sleep(1)

    # 5) Seleccionar Servicio
    log.info(f"5) Seleccionando servicio: {OBJ_SERVICIO!r}")
    serv_sel = page.locator("select#servimod")
    await serv_sel.wait_for(timeout=TOUT)
    # Intentar por label (visible text)
    try:
        await serv_sel.select_option(label=OBJ_SERVICIO.strip())
    except Exception:
        # Plan B: igualar por mayúsculas usando evaluate
        await page.evaluate(
            """(svc_text) => {
                const sel = document.querySelector('#servimod');
                if (!sel) return;
                const t = (svc_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_SERVICIO
        )
    await short_sleep(1)

    # 6) Seleccionar Zona
    log.info(f"6) Seleccionando zona: {OBJ_ZONA!r}")
    zona_sel = page.locator("select#id_zona")
    await zona_sel.wait_for(timeout=TOUT)
    try:
        await zona_sel.select_option(label=OBJ_ZONA.strip())
    except Exception:
        await page.evaluate(
            """(zona_text) => {
                const sel = document.querySelector('#id_zona');
                if (!sel) return;
                const t = (zona_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_ZONA
        )

    # IMPORTANTE: el cambio de zona dispara cargar departamentos
    # Damos un pequeño tiempo para que se complete esa carga
    await short_sleep(0.8)

    # 7) Seleccionar Departamento
    log.info(f"7) Seleccionando departamento: {OBJ_DEPTO!r}")
    dpto_sel = page.locator("select#id_dpto")
    await dpto_sel.wait_for(timeout=TOUT)
    try:
        await dpto_sel.select_option(label=OBJ_DEPTO.strip())
    except Exception:
        await page.evaluate(
            """(dep_text) => {
                const sel = document.querySelector('#id_dpto');
                if (!sel) return;
                const t = (dep_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_DEPTO
        )
    await short_sleep(1)

    # 8) Lógica del profesional (OBJ_MEDICO)
    buscar_btn = page.locator('input#buscar.button.buscar').first
    prof_input = page.locator('input#profesionalBusquedaComodin_turn')

    obj_medico_txt = (OBJ_MEDICO or "").strip()
    if obj_medico_txt.lower() == "false" or obj_medico_txt == "":
        log.info("8) OBJ_MEDICO = false => no se filtra por profesional; se clickea 'Buscar'.")
        try:
            await buscar_btn.click(timeout=TOUT)
        except Exception as e:
            log.warning(f"No pude hacer click en Buscar de forma directa ({e}); intento alternativo…")
            await page.locator("input#buscar").first.click()
    else:
        log.info(f"8) Filtrando por profesional: {obj_medico_txt!r}")
        await prof_input.wait_for(timeout=TOUT)
        # limpiar + tipear
        await prof_input.fill("")
        await prof_input.fill(obj_medico_txt)
        # Enter para disparar buscador/loader
        await prof_input.press("Enter")
        # Esperar overlay/toaster cargue y se vaya
        await wait_blocker_gone(page, timeout_ms=20000)
        # Y recién ahí Buscar
        await buscar_btn.click(timeout=TOUT)

    # 9) Esperar tabla de resultados y volcarla al log
    log.info("9) Esperando tabla de resultados…")
    tabla = page.locator("#tblResultadoProfesionales")
    await tabla.wait_for(timeout=TOUT)

    # Scraping de filas
    filas = await page.evaluate("""
        () => {
            const tbl = document.querySelector('#tblResultadoProfesionales');
            const out = [];
            if (!tbl) return out;
            const rows = tbl.querySelectorAll('tbody tr');
            for (const tr of rows) {
                const tds = tr.querySelectorAll('td');
                if (tds.length < 6) continue;
                const toText = (el) => (el ? el.innerText.trim().replace(/\\s+\\n/g, "\\n").replace(/\\s+/g,' ').trim() : "");
                out.push({
                    profesional: toText(tds[0]),
                    domicilio:   toText(tds[1]),
                    servicio:    toText(tds[2]),
                    horario:     toText(tds[3]),
                    disp:        toText(tds[4]),
                    agenda:      toText(tds[5]),
                    rowIndex:    Array.from(tr.parentNode.children).indexOf(tr)
                });
            }
            return out;
        }
    """)

    if not filas:
        log.warning("No se encontraron filas en la tabla (0 resultados).")
        return

    log.info(f"Resultados detectados: {len(filas)} fila(s).")

    # ---------------- FILTROS ----------------
    prof_filtro = (os.getenv("OBJ_PROFESIONAL") or "").strip().lower()
    dom_filtro  = (os.getenv("OBJ_DOMICILIO") or "").strip().lower()
    hor_filtro  = (os.getenv("OBJ_HORARIO_TURNO") or "").strip().lower()
    dias_filtro = [d.strip().upper() for d in (os.getenv("OBJ_DIAS_VALIDOS") or "").split(",") if d.strip()]
    fecha_filtro = (os.getenv("OBJ_FECHA_DISP") or "").strip()

    def cumple(f):
        # Profesional
        if prof_filtro and prof_filtro != "false" and prof_filtro not in f["profesional"].lower():
            return False
        # Domicilio
        if dom_filtro and dom_filtro != "false" and dom_filtro not in f["domicilio"].lower():
            return False
        # Horario
        if hor_filtro and hor_filtro != "false" and hor_filtro not in f["horario"].lower():
            return False
        # Días válidos
        if dias_filtro and not any(d in f["horario"].upper() for d in dias_filtro):
            return False
        # Fecha DISP
        if fecha_filtro and fecha_filtro != "false" and f["disp"] != fecha_filtro:
            return False
        # Si disp es --- descartamos (sin disponibilidad)
        if f["disp"].strip() == "---":
            return False
        return True

    candidatas = [f for f in filas if cumple(f)]

    # Si no hay coincidencias exactas, elegimos la más próxima por fecha
    from datetime import datetime
    
    if not candidatas:
        hoy = datetime.now()
        fechas_validas = []
        for f in filas:
            try:
                if f["disp"].strip() == "---":
                    continue
                fecha = datetime.strptime(f["disp"], "%d-%m-%Y")
                if fecha >= hoy:
                    fechas_validas.append((fecha, f))
            except Exception:
                continue
    
        if fecha_filtro and not OBJ_FECHA_FLEXIBLE:
            log.warning("No se encontró la fecha exacta y la flexibilidad está desactivada.")
            return
    
        if fechas_validas:
            fechas_validas.sort(key=lambda x: x[0])
            candidatas = [fechas_validas[0][1]]
            log.info(f"Usando la más próxima: {candidatas[0]['disp']}")
        else:
            log.warning("No hay fechas disponibles próximas.")
            return


    objetivo = candidatas[0]
    log.info(f"Seleccionada: {objetivo}")

    # Click en el ícono "Ver Agenda"
    fila_index = objetivo["rowIndex"]
    log.info(f"Haciendo click en 'Ver Agenda' (fila {fila_index})…")
    agenda_icon = page.locator(f"#tblResultadoProfesionales tbody tr:nth-of-type({fila_index + 1}) img#img_agenda_prof")
    await agenda_icon.first.click(timeout=TOUT)

    # Esperar a que desaparezca el overlay/toaster
    await wait_blocker_gone(page, timeout_ms=20000)

    # 10) Leer iframe de la agenda
    log.info("10) Esperando iframe de Agenda…")
    iframe = None
    for _ in range(20):
        for f in page.frames:
            if "pickMostrarAgenda_iframe" in (f.name or ""):
                iframe = f
                break
        if iframe:
            break
        await asyncio.sleep(0.5)
    if not iframe:
        log.error("No encontré el iframe de agenda.")
        return

    # Extraer información de la tabla dentro del iframe
    # 11) Leer tabla de horarios disponibles (espera robusta)
    log.info("11) Esperando que cargue la agenda dentro del iframe…")
    try:
        await iframe.wait_for_selector("div.horario_disponible, table.tabla_dias_horarios", timeout=10000)
    except Exception:
        log.warning("No se detectó ninguna celda con horario_disponible antes de timeout.")
    
    log.info("11) Leyendo tabla de horarios disponibles (estructura real)…")
    horarios = await iframe.evaluate("""
        () => {
            const cab = document.querySelectorAll('table.tabla_dias_horarios th.cabecera_dia, table.tabla_dias_horarios th.cabecera_hoy');
            const dias_labels = Array.from(cab).map(th => th.innerText.trim().replace(/\\s+/g,' '));
            const data = {};
            for (const lbl of dias_labels) data[lbl] = [];
    
            const filas = document.querySelectorAll('table.tabla_dias_horarios tbody tr');
            filas.forEach(tr => {
                const celdas = tr.querySelectorAll('td');
                celdas.forEach((td, idx) => {
                    const divs = td.querySelectorAll('div.horario_disponible');
                    divs.forEach(div => {
                        const txt = div.textContent.trim();
                        if (txt) {
                            const dia = dias_labels[idx] || `Columna ${idx}`;
                            data[dia] = data[dia] || [];
                            data[dia].push(txt);
                        }
                    });
                });
            });
            return data;
        }
    """)


    if not horarios:
        log.warning("No se detectaron horarios disponibles en la agenda.")
    else:
        for dia, horas in horarios.items():
            if horas:
                log.info(f"{dia}: {', '.join(horas)}")
            else:
                log.info(f"{dia}: sin horarios disponibles")

    



# 12) Seleccionar turno según franja horaria configurada
    from datetime import datetime

    log.info("12) Seleccionando turno dentro de franja horaria configurada…")

    hora_min = os.getenv("OBJ_HORA_MIN", "00:00").strip()
    hora_max = os.getenv("OBJ_HORA_MAX", "23:59").strip()
    # Si alguno viene "false", asignar valores amplios por defecto
    if hora_min.lower() == "false" or not hora_min:
        hora_min = "00:00"
    if hora_max.lower() == "false" or not hora_max:
        hora_max = "23:59"
    prioridad = (os.getenv("OBJ_HORA_PRIORIDAD", "EARLIEST") or "EARLIEST").upper()

    def hora_a_minutos(h):
        try:
            hh, mm = map(int, h.split(":"))
            return hh * 60 + mm
        except Exception:
            return None

    hmin = hora_a_minutos(hora_min)
    hmax = hora_a_minutos(hora_max)

    # Buscar el primer día con horarios disponibles
    dia_valido, horas_validas = None, []
    for dia, horas in horarios.items():
        if horas:
            filtradas = []
            for h in horas:
                hm = hora_a_minutos(h)
                if hm and hmin <= hm <= hmax:
                    filtradas.append(h)
            if filtradas:
                dia_valido = dia
                horas_validas = filtradas
                break

    if not dia_valido:
        if OBJ_HORA_FLEXIBLE:
            # No hay horario dentro del rango, tomar el primero disponible
            for dia, horas in horarios.items():
                if horas:
                    dia_valido = dia
                    hora_elegida = sorted(horas)[0]
                    log.info(f"No se encontró horario en rango, usando primero disponible: {dia_valido} / {hora_elegida}")
                    break
            else:
                log.warning("No se encontró ningún horario disponible en ningún día.")
                return
        else:
            log.warning(f"No se encontró ningún horario entre {hora_min} y {hora_max}, y flexibilidad está desactivada.")
            return
    else:
        if prioridad == "LATEST":
            hora_elegida = sorted(horas_validas)[-1]
        else:
            hora_elegida = sorted(horas_validas)[0]
        log.info(f"Día elegido: {dia_valido} / Hora elegida: {hora_elegida}")


        # Click en el div correspondiente
        log.info("Haciendo click en el horario disponible…")
        # Buscar el div con esa hora exacta
        await iframe.click(f"div.horario_disponible:text('{hora_elegida}')")
        await short_sleep(0.5)


        # 13) Esperar cuadro de confirmación
        log.info("13) Esperando cuadro de confirmación de turno…")
        try:

            # Esperar unos instantes extra por si se está animando
            await short_sleep(5.0)
        
            log.info("Cuadro de confirmación detectado. Simulando Tab + Enter para aceptar…")
        
            # Aseguramos foco en la página principal
            await page.bring_to_front()
        
            # Tecleamos Tab (para enfocar el botón Aceptar) y luego Enter
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.3)
            
            if DRY_RUN:
                log.info("(DRY_RUN activo) No se presiona Enter; flujo detenido antes de confirmar turno.")
                return
            else:
                await page.keyboard.press("Enter")
                log.info("Teclas Tab + Enter enviadas correctamente. Esperando que se cierre el cuadro…")        
                # Esperamos a que desaparezca el cuadro
                await page.wait_for_selector("#pickCustomTwoButtons", state="detached", timeout=10000)
                log.info("Cuadro de confirmación cerrado correctamente.")
        
        except Exception as e:
            log.error(f"No se pudo aceptar el cuadro de confirmación: {e}")
            return




            # 14) Esperar cuadro final de reserva
            log.info("14) Esperando cuadro final con datos del turno…")
            try:
                await page.wait_for_selector("div.pick_print table", timeout=20000)
                turno_info = await page.evaluate("""
                    () => {
                        const tbl = document.querySelector("div.pick_print table");
                        if (!tbl) return {};
                        const txt = tbl.innerText.trim().split("\\n");
                        return txt;
                    }
                """)
                log.info("==== TURNO CONFIRMADO ====")
                for line in turno_info:
                    log.info(line)
                log.info("===========================")
            except Exception as e:
                log.error(f"No se pudo leer la confirmación del turno: {e}")


    log.info("10) Flujo completado. La ventana quedará ABIERTA para revisión manual.")

# ================== MAIN ==================
async def amain() -> int:
    log.info("==== INICIO OSEP TURNOS (FLUJO NUEVO) ====")
    if not OSEP_USER or not OSEP_PASS:
        log.error("Definí OSEP_USER y OSEP_PASS en .env")
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.bring_to_front()
        try:
            await page.evaluate("window.moveTo(0,0); window.resizeTo(screen.availWidth, screen.availHeight);")
        except Exception:
            pass



        try:
            await login(page)

            if STOP_AFTER_LOGIN:
                log.info("STOP_AFTER_LOGIN activo: fin de prueba.")
                return 0

            await flujo_turnos_nuevo(page)
            return 0

        except Exception as e:
            log.error("Error en ejecución:\n" + "".join(traceback.format_exception(e)))
            return 3

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

# ================== Footer compatible Spyder/Terminal (Windows) ==================
if __name__ == "__main__":
    def _runner():
        if sys.platform.startswith("win"):
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            rc = loop.run_until_complete(amain())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        os._exit(rc)

    try:
        asyncio.get_running_loop()
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
    except RuntimeError:
        sys.exit(asyncio.run(amain()))


# ================== ENV / CONFIG ==================
load_dotenv()

PORTAL_URL = os.getenv("PORTAL_URL", "https://www.osep.mendoza.gov.ar/webapp_pri")
OSEP_USER  = os.getenv("OSEP_USER")
OSEP_PASS  = os.getenv("OSEP_PASS")

# Objetivos originales (algunos quedan para futuro)
OBJ_FECHA_TXT = os.getenv("OBJ_FECHA_TXT")
OBJ_HORA_TXT  = os.getenv("OBJ_HORA_TXT")
OBJ_SERVICIO  = os.getenv("OBJ_SERVICIO")
OBJ_MEDICO    = os.getenv("OBJ_MEDICO", "false")  # si "false" => no filtra por profesional
OBJ_SEDE_TXT  = os.getenv("OBJ_SEDE_TXT")
OBJ_ZONA   = os.getenv("OBJ_ZONA")
OBJ_DEPTO  = os.getenv("OBJ_DEPTO")  # agregado; si querés otro, definilo en .env
OBJ_FECHA_FLEXIBLE = os.getenv("OBJ_FECHA_FLEXIBLE", "true").lower() == "true"
OBJ_DIA_FLEXIBLE   = os.getenv("OBJ_DIA_FLEXIBLE", "true").lower() == "true"
OBJ_HORA_FLEXIBLE  = os.getenv("OBJ_HORA_FLEXIBLE", "true").lower() == "true"


HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
TOUT     = int(os.getenv("TIMEOUT_MS", "20000"))

DRY_RUN            = os.getenv("DRY_RUN", "false").lower() == "true"
STOP_AFTER_LOGIN   = os.getenv("STOP_AFTER_LOGIN", "false").lower() == "true"
# STOP_AFTER_IFRAME ignorado en este flujo nuevo
# STOP_AFTER_IFRAME  = os.getenv("STOP_AFTER_IFRAME", "false").lower() == "true"


# ================== EVIDENCIAS (DESACTIVADO PARA EJECUCIÓN SIMPLE) ==================
EVID = None  # No se crearán carpetas ni archivos de evidencia

# =============================================================================
# BASE_DIR  = pathlib.Path(r"C:\Users\Francesco Zucarello\OneDrive - Estudio Trípoli\Personal\Scripts\Turnos OSEP")
# EVID_ROOT = BASE_DIR
# RUN_ID    = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
# EVID      = EVID_ROOT / f"evidencia_{RUN_ID}"
# EVID.mkdir(parents=True, exist_ok=True)
# =============================================================================

# ================== LOGGING (solo a archivo + consola) ==================
log = logging.getLogger("osep")
log.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

log.info("Logging iniciado (solo consola, sin archivo).")

# =============================================================================
# log_file = EVID / "log.txt"
# file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
# file_handler.setFormatter(formatter)
# log.addHandler(file_handler)
# 
# log.info(f"Logging iniciado. Archivo: {log_file}")
# =============================================================================

# ================== UTILS ==================
def must_env(var: str):
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"Falta variable de entorno {var}")
    return val

async def short_sleep(seconds: float = 1.0):
    await asyncio.sleep(seconds)

async def wait_blocker_gone(page, timeout_ms: int = 15000):
    """
    Intenta detectar overlays típicos (blockUI / overlays / toasts) y esperar a que desaparezcan.
    """
    end = dt.datetime.now() + dt.timedelta(milliseconds=timeout_ms)
    sel = ".blockUI, .blockOverlay, .blockMsg, .ui-blocker, .blocker, .jq-toast-wrap, .toast, .loading, .modal-backdrop.show"
    while dt.datetime.now() < end:
        try:
            cnt = await page.locator(sel).count()
            if cnt == 0:
                # Damos un toque de gracia y nos vamos
                await asyncio.sleep(0.2)
                return
        except Exception:
            # Si falla el locator, igual seguimos intentando hasta timeout
            pass
        await asyncio.sleep(0.2)
    log.debug("wait_blocker_gone: timeout esperando que desaparezca el overlay.")

# ================== FLOWS ==================
async def login(page):
    log.info("1) Navegando al portal…")
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=TOUT)

    log.info("2) Login (usuario/contraseña)…")
    user_sel = 'input[name="usuario"], input#usuario, input[placeholder*="Usuario" i]'
    pass_sel = 'input[name="password"], input#password, input[placeholder*="Contraseña" i], input[type="password"]'
    await page.locator(user_sel).first.wait_for(timeout=TOUT)
    await page.fill(user_sel, must_env("OSEP_USER"))
    await page.fill(pass_sel, must_env("OSEP_PASS"))

    btn = page.get_by_role("button", name=re.compile(r"(ingresar|entrar|acceder|login)", re.I)).first
    if not await btn.is_visible():
        btn = page.locator('button:has-text("Ingresar"), input[type="submit"], a:has-text("Ingresar")').first
    await btn.click(timeout=TOUT)

    await page.wait_for_load_state("networkidle", timeout=TOUT)
    log.info("Login: OK (si seguís viendo la pantalla de login, hay que ajustar selectores).")

async def flujo_turnos_nuevo(page):
    """
    Luego del login:
      - Navegar a listarCompleto
      - Click en pestaña 'Nuevo'
      - Seleccionar Servicio (servimod), Zona (id_zona), Depto (id_dpto)
      - Si OBJ_MEDICO == 'false' => click Buscar
        Else: escribir profesional, Enter, esperar overlay, luego click Buscar
      - Esperar y leer tabla #tblResultadoProfesionales, volcar filas al log
      - Mantener la ventana abierta para inspección manual
    """
    # 3) Navegación directa
    listar_url = PORTAL_URL.rstrip("/") + "/action/applicationAfi/turnos/turno/listar/listarCompleto"
    log.info(f"3) Navegando directo a listarCompleto: {listar_url}")
    await page.goto(listar_url, wait_until="domcontentloaded", timeout=TOUT)

    # 4) Click en pestaña "Nuevo"
    log.info("4) Click en pestaña 'Nuevo'…")
    # Varios selectores por las dudas:
    nuevo_link = page.locator('a.nav-link[href="#divTNue"] >> text=Nuevo').first
    if not await nuevo_link.is_visible():
        nuevo_link = page.locator('a.nav-link[href="#divTNue"]').first
    await nuevo_link.click(timeout=TOUT)
    await short_sleep(1)

    # 5) Seleccionar Servicio
    log.info(f"5) Seleccionando servicio: {OBJ_SERVICIO!r}")
    serv_sel = page.locator("select#servimod")
    await serv_sel.wait_for(timeout=TOUT)
    # Intentar por label (visible text)
    try:
        await serv_sel.select_option(label=OBJ_SERVICIO.strip())
    except Exception:
        # Plan B: igualar por mayúsculas usando evaluate
        await page.evaluate(
            """(svc_text) => {
                const sel = document.querySelector('#servimod');
                if (!sel) return;
                const t = (svc_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_SERVICIO
        )
    await short_sleep(1)

    # 6) Seleccionar Zona
    log.info(f"6) Seleccionando zona: {OBJ_ZONA!r}")
    zona_sel = page.locator("select#id_zona")
    await zona_sel.wait_for(timeout=TOUT)
    try:
        await zona_sel.select_option(label=OBJ_ZONA.strip())
    except Exception:
        await page.evaluate(
            """(zona_text) => {
                const sel = document.querySelector('#id_zona');
                if (!sel) return;
                const t = (zona_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_ZONA
        )

    # IMPORTANTE: el cambio de zona dispara cargar departamentos
    # Damos un pequeño tiempo para que se complete esa carga
    await short_sleep(0.8)

    # 7) Seleccionar Departamento
    log.info(f"7) Seleccionando departamento: {OBJ_DEPTO!r}")
    dpto_sel = page.locator("select#id_dpto")
    await dpto_sel.wait_for(timeout=TOUT)
    try:
        await dpto_sel.select_option(label=OBJ_DEPTO.strip())
    except Exception:
        await page.evaluate(
            """(dep_text) => {
                const sel = document.querySelector('#id_dpto');
                if (!sel) return;
                const t = (dep_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_DEPTO
        )
    await short_sleep(1)

    # 8) Lógica del profesional (OBJ_MEDICO)
    buscar_btn = page.locator('input#buscar.button.buscar').first
    prof_input = page.locator('input#profesionalBusquedaComodin_turn')

    obj_medico_txt = (OBJ_MEDICO or "").strip()
    if obj_medico_txt.lower() == "false" or obj_medico_txt == "":
        log.info("8) OBJ_MEDICO = false => no se filtra por profesional; se clickea 'Buscar'.")
        try:
            await buscar_btn.click(timeout=TOUT)
        except Exception as e:
            log.warning(f"No pude hacer click en Buscar de forma directa ({e}); intento alternativo…")
            await page.locator("input#buscar").first.click()
    else:
        log.info(f"8) Filtrando por profesional: {obj_medico_txt!r}")
        await prof_input.wait_for(timeout=TOUT)
        # limpiar + tipear
        await prof_input.fill("")
        await prof_input.fill(obj_medico_txt)
        # Enter para disparar buscador/loader
        await prof_input.press("Enter")
        # Esperar overlay/toaster cargue y se vaya
        await wait_blocker_gone(page, timeout_ms=20000)
        # Y recién ahí Buscar
        await buscar_btn.click(timeout=TOUT)

    # 9) Esperar tabla de resultados y volcarla al log
    log.info("9) Esperando tabla de resultados…")
    tabla = page.locator("#tblResultadoProfesionales")
    await tabla.wait_for(timeout=TOUT)

    # Scraping de filas
    filas = await page.evaluate("""
        () => {
            const tbl = document.querySelector('#tblResultadoProfesionales');
            const out = [];
            if (!tbl) return out;
            const rows = tbl.querySelectorAll('tbody tr');
            for (const tr of rows) {
                const tds = tr.querySelectorAll('td');
                if (tds.length < 6) continue;
                const toText = (el) => (el ? el.innerText.trim().replace(/\\s+\\n/g, "\\n").replace(/\\s+/g,' ').trim() : "");
                out.push({
                    profesional: toText(tds[0]),
                    domicilio:   toText(tds[1]),
                    servicio:    toText(tds[2]),
                    horario:     toText(tds[3]),
                    disp:        toText(tds[4]),
                    agenda:      toText(tds[5]),
                    rowIndex:    Array.from(tr.parentNode.children).indexOf(tr)
                });
            }
            return out;
        }
    """)

    if not filas:
        log.warning("No se encontraron filas en la tabla (0 resultados).")
        return

    log.info(f"Resultados detectados: {len(filas)} fila(s).")

    # ---------------- FILTROS ----------------
    prof_filtro = (os.getenv("OBJ_PROFESIONAL") or "").strip().lower()
    dom_filtro  = (os.getenv("OBJ_DOMICILIO") or "").strip().lower()
    hor_filtro  = (os.getenv("OBJ_HORARIO_TURNO") or "").strip().lower()
    dias_filtro = [d.strip().upper() for d in (os.getenv("OBJ_DIAS_VALIDOS") or "").split(",") if d.strip()]
    fecha_filtro = (os.getenv("OBJ_FECHA_DISP") or "").strip()

    def cumple(f):
        # Profesional
        if prof_filtro and prof_filtro != "false" and prof_filtro not in f["profesional"].lower():
            return False
        # Domicilio
        if dom_filtro and dom_filtro != "false" and dom_filtro not in f["domicilio"].lower():
            return False
        # Horario
        if hor_filtro and hor_filtro != "false" and hor_filtro not in f["horario"].lower():
            return False
        # Días válidos
        if dias_filtro and not any(d in f["horario"].upper() for d in dias_filtro):
            return False
        # Fecha DISP
        if fecha_filtro and fecha_filtro != "false" and f["disp"] != fecha_filtro:
            return False
        # Si disp es --- descartamos (sin disponibilidad)
        if f["disp"].strip() == "---":
            return False
        return True

    candidatas = [f for f in filas if cumple(f)]

    # Si no hay coincidencias exactas, elegimos la más próxima por fecha
    from datetime import datetime
    
    if not candidatas:
        hoy = datetime.now()
        fechas_validas = []
        for f in filas:
            try:
                if f["disp"].strip() == "---":
                    continue
                fecha = datetime.strptime(f["disp"], "%d-%m-%Y")
                if fecha >= hoy:
                    fechas_validas.append((fecha, f))
            except Exception:
                continue
    
        if fecha_filtro and not OBJ_FECHA_FLEXIBLE:
            log.warning("No se encontró la fecha exacta y la flexibilidad está desactivada.")
            return
    
        if fechas_validas:
            fechas_validas.sort(key=lambda x: x[0])
            candidatas = [fechas_validas[0][1]]
            log.info(f"Usando la más próxima: {candidatas[0]['disp']}")
        else:
            log.warning("No hay fechas disponibles próximas.")
            return


    objetivo = candidatas[0]
    log.info(f"Seleccionada: {objetivo}")

    # Click en el ícono "Ver Agenda"
    fila_index = objetivo["rowIndex"]
    log.info(f"Haciendo click en 'Ver Agenda' (fila {fila_index})…")
    agenda_icon = page.locator(f"#tblResultadoProfesionales tbody tr:nth-of-type({fila_index + 1}) img#img_agenda_prof")
    await agenda_icon.first.click(timeout=TOUT)

    # Esperar a que desaparezca el overlay/toaster
    await wait_blocker_gone(page, timeout_ms=20000)

    # 10) Leer iframe de la agenda
    log.info("10) Esperando iframe de Agenda…")
    iframe = None
    for _ in range(20):
        for f in page.frames:
            if "pickMostrarAgenda_iframe" in (f.name or ""):
                iframe = f
                break
        if iframe:
            break
        await asyncio.sleep(0.5)
    if not iframe:
        log.error("No encontré el iframe de agenda.")
        return

    # Extraer información de la tabla dentro del iframe
    # 11) Leer tabla de horarios disponibles (espera robusta)
    log.info("11) Esperando que cargue la agenda dentro del iframe…")
    try:
        await iframe.wait_for_selector("div.horario_disponible, table.tabla_dias_horarios", timeout=10000)
    except Exception:
        log.warning("No se detectó ninguna celda con horario_disponible antes de timeout.")
    
    log.info("11) Leyendo tabla de horarios disponibles (estructura real)…")
    horarios = await iframe.evaluate("""
        () => {
            const cab = document.querySelectorAll('table.tabla_dias_horarios th.cabecera_dia, table.tabla_dias_horarios th.cabecera_hoy');
            const dias_labels = Array.from(cab).map(th => th.innerText.trim().replace(/\\s+/g,' '));
            const data = {};
            for (const lbl of dias_labels) data[lbl] = [];
    
            const filas = document.querySelectorAll('table.tabla_dias_horarios tbody tr');
            filas.forEach(tr => {
                const celdas = tr.querySelectorAll('td');
                celdas.forEach((td, idx) => {
                    const divs = td.querySelectorAll('div.horario_disponible');
                    divs.forEach(div => {
                        const txt = div.textContent.trim();
                        if (txt) {
                            const dia = dias_labels[idx] || `Columna ${idx}`;
                            data[dia] = data[dia] || [];
                            data[dia].push(txt);
                        }
                    });
                });
            });
            return data;
        }
    """)


    if not horarios:
        log.warning("No se detectaron horarios disponibles en la agenda.")
    else:
        for dia, horas in horarios.items():
            if horas:
                log.info(f"{dia}: {', '.join(horas)}")
            else:
                log.info(f"{dia}: sin horarios disponibles")

    



# 12) Seleccionar turno según franja horaria configurada
    from datetime import datetime

    log.info("12) Seleccionando turno dentro de franja horaria configurada…")

    hora_min = os.getenv("OBJ_HORA_MIN", "00:00").strip()
    hora_max = os.getenv("OBJ_HORA_MAX", "23:59").strip()
    # Si alguno viene "false", asignar valores amplios por defecto
    if hora_min.lower() == "false" or not hora_min:
        hora_min = "00:00"
    if hora_max.lower() == "false" or not hora_max:
        hora_max = "23:59"
    prioridad = (os.getenv("OBJ_HORA_PRIORIDAD", "EARLIEST") or "EARLIEST").upper()

    def hora_a_minutos(h):
        try:
            hh, mm = map(int, h.split(":"))
            return hh * 60 + mm
        except Exception:
            return None

    hmin = hora_a_minutos(hora_min)
    hmax = hora_a_minutos(hora_max)

    # Buscar el primer día con horarios disponibles
    dia_valido, horas_validas = None, []
    for dia, horas in horarios.items():
        if horas:
            filtradas = []
            for h in horas:
                hm = hora_a_minutos(h)
                if hm and hmin <= hm <= hmax:
                    filtradas.append(h)
            if filtradas:
                dia_valido = dia
                horas_validas = filtradas
                break

    if not dia_valido:
        if OBJ_HORA_FLEXIBLE:
            # No hay horario dentro del rango, tomar el primero disponible
            for dia, horas in horarios.items():
                if horas:
                    dia_valido = dia
                    hora_elegida = sorted(horas)[0]
                    log.info(f"No se encontró horario en rango, usando primero disponible: {dia_valido} / {hora_elegida}")
                    break
            else:
                log.warning("No se encontró ningún horario disponible en ningún día.")
                return
        else:
            log.warning(f"No se encontró ningún horario entre {hora_min} y {hora_max}, y flexibilidad está desactivada.")
            return
    else:
        if prioridad == "LATEST":
            hora_elegida = sorted(horas_validas)[-1]
        else:
            hora_elegida = sorted(horas_validas)[0]
        log.info(f"Día elegido: {dia_valido} / Hora elegida: {hora_elegida}")


        # Click en el div correspondiente
        log.info("Haciendo click en el horario disponible…")
        # Buscar el div con esa hora exacta
        await iframe.click(f"div.horario_disponible:text('{hora_elegida}')")
        await short_sleep(0.5)


        # 13) Esperar cuadro de confirmación
        log.info("13) Esperando cuadro de confirmación de turno…")
        try:

            # Esperar unos instantes extra por si se está animando
            await short_sleep(5.0)
        
            log.info("Cuadro de confirmación detectado. Simulando Tab + Enter para aceptar…")
        
            # Aseguramos foco en la página principal
            await page.bring_to_front()
        
            # Tecleamos Tab (para enfocar el botón Aceptar) y luego Enter
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.3)
            
            if DRY_RUN:
                log.info("(DRY_RUN activo) No se presiona Enter; flujo detenido antes de confirmar turno.")
                return
            else:
                await page.keyboard.press("Enter")
                log.info("Teclas Tab + Enter enviadas correctamente. Esperando que se cierre el cuadro…")        
                # Esperamos a que desaparezca el cuadro
                await page.wait_for_selector("#pickCustomTwoButtons", state="detached", timeout=10000)
                log.info("Cuadro de confirmación cerrado correctamente.")
        
        except Exception as e:
            log.error(f"No se pudo aceptar el cuadro de confirmación: {e}")
            return




            # 14) Esperar cuadro final de reserva
            log.info("14) Esperando cuadro final con datos del turno…")
            try:
                await page.wait_for_selector("div.pick_print table", timeout=20000)
                turno_info = await page.evaluate("""
                    () => {
                        const tbl = document.querySelector("div.pick_print table");
                        if (!tbl) return {};
                        const txt = tbl.innerText.trim().split("\\n");
                        return txt;
                    }
                """)
                log.info("==== TURNO CONFIRMADO ====")
                for line in turno_info:
                    log.info(line)
                log.info("===========================")
            except Exception as e:
                log.error(f"No se pudo leer la confirmación del turno: {e}")


    log.info("10) Flujo completado. La ventana quedará ABIERTA para revisión manual.")

# ================== MAIN ==================
async def amain() -> int:
    log.info("==== INICIO OSEP TURNOS (FLUJO NUEVO) ====")
    if not OSEP_USER or not OSEP_PASS:
        log.error("Definí OSEP_USER y OSEP_PASS en .env")
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.bring_to_front()
        try:
            await page.evaluate("window.moveTo(0,0); window.resizeTo(screen.availWidth, screen.availHeight);")
        except Exception:
            pass



        try:
            await login(page)

            if STOP_AFTER_LOGIN:
                log.info("STOP_AFTER_LOGIN activo: fin de prueba.")
                return 0

            await flujo_turnos_nuevo(page)
            return 0

        except Exception as e:
            log.error("Error en ejecución:\n" + "".join(traceback.format_exception(e)))
            return 3

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

# ================== Footer compatible Spyder/Terminal (Windows) ==================
if __name__ == "__main__":
    def _runner():
        if sys.platform.startswith("win"):
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            rc = loop.run_until_complete(amain())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        os._exit(rc)

    try:
        asyncio.get_running_loop()
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
    except RuntimeError:
        sys.exit(asyncio.run(amain()))


# ================== ENV / CONFIG ==================
load_dotenv()

PORTAL_URL = os.getenv("PORTAL_URL", "https://www.osep.mendoza.gov.ar/webapp_pri")
OSEP_USER  = os.getenv("OSEP_USER")
OSEP_PASS  = os.getenv("OSEP_PASS")

# Objetivos originales (algunos quedan para futuro)
OBJ_FECHA_TXT = os.getenv("OBJ_FECHA_TXT")
OBJ_HORA_TXT  = os.getenv("OBJ_HORA_TXT")
OBJ_SERVICIO  = os.getenv("OBJ_SERVICIO")
OBJ_MEDICO    = os.getenv("OBJ_MEDICO", "false")  # si "false" => no filtra por profesional
OBJ_SEDE_TXT  = os.getenv("OBJ_SEDE_TXT")
OBJ_ZONA   = os.getenv("OBJ_ZONA")
OBJ_DEPTO  = os.getenv("OBJ_DEPTO")  # agregado; si querés otro, definilo en .env
OBJ_FECHA_FLEXIBLE = os.getenv("OBJ_FECHA_FLEXIBLE", "true").lower() == "true"
OBJ_DIA_FLEXIBLE   = os.getenv("OBJ_DIA_FLEXIBLE", "true").lower() == "true"
OBJ_HORA_FLEXIBLE  = os.getenv("OBJ_HORA_FLEXIBLE", "true").lower() == "true"


HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
TOUT     = int(os.getenv("TIMEOUT_MS", "20000"))

DRY_RUN            = os.getenv("DRY_RUN", "false").lower() == "true"
STOP_AFTER_LOGIN   = os.getenv("STOP_AFTER_LOGIN", "false").lower() == "true"
# STOP_AFTER_IFRAME ignorado en este flujo nuevo
# STOP_AFTER_IFRAME  = os.getenv("STOP_AFTER_IFRAME", "false").lower() == "true"


# ================== EVIDENCIAS (DESACTIVADO PARA EJECUCIÓN SIMPLE) ==================
EVID = None  # No se crearán carpetas ni archivos de evidencia

# =============================================================================
# BASE_DIR  = pathlib.Path(r"C:\Users\Francesco Zucarello\OneDrive - Estudio Trípoli\Personal\Scripts\Turnos OSEP")
# EVID_ROOT = BASE_DIR
# RUN_ID    = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
# EVID      = EVID_ROOT / f"evidencia_{RUN_ID}"
# EVID.mkdir(parents=True, exist_ok=True)
# =============================================================================

# ================== LOGGING (solo a archivo + consola) ==================
log = logging.getLogger("osep")
log.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

log.info("Logging iniciado (solo consola, sin archivo).")

# =============================================================================
# log_file = EVID / "log.txt"
# file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
# file_handler.setFormatter(formatter)
# log.addHandler(file_handler)
# 
# log.info(f"Logging iniciado. Archivo: {log_file}")
# =============================================================================

# ================== UTILS ==================
def must_env(var: str):
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"Falta variable de entorno {var}")
    return val

async def short_sleep(seconds: float = 1.0):
    await asyncio.sleep(seconds)

async def wait_blocker_gone(page, timeout_ms: int = 15000):
    """
    Intenta detectar overlays típicos (blockUI / overlays / toasts) y esperar a que desaparezcan.
    """
    end = dt.datetime.now() + dt.timedelta(milliseconds=timeout_ms)
    sel = ".blockUI, .blockOverlay, .blockMsg, .ui-blocker, .blocker, .jq-toast-wrap, .toast, .loading, .modal-backdrop.show"
    while dt.datetime.now() < end:
        try:
            cnt = await page.locator(sel).count()
            if cnt == 0:
                # Damos un toque de gracia y nos vamos
                await asyncio.sleep(0.2)
                return
        except Exception:
            # Si falla el locator, igual seguimos intentando hasta timeout
            pass
        await asyncio.sleep(0.2)
    log.debug("wait_blocker_gone: timeout esperando que desaparezca el overlay.")

# ================== FLOWS ==================
async def login(page):
    log.info("1) Navegando al portal…")
    await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=TOUT)

    log.info("2) Login (usuario/contraseña)…")
    user_sel = 'input[name="usuario"], input#usuario, input[placeholder*="Usuario" i]'
    pass_sel = 'input[name="password"], input#password, input[placeholder*="Contraseña" i], input[type="password"]'
    await page.locator(user_sel).first.wait_for(timeout=TOUT)
    await page.fill(user_sel, must_env("OSEP_USER"))
    await page.fill(pass_sel, must_env("OSEP_PASS"))

    btn = page.get_by_role("button", name=re.compile(r"(ingresar|entrar|acceder|login)", re.I)).first
    if not await btn.is_visible():
        btn = page.locator('button:has-text("Ingresar"), input[type="submit"], a:has-text("Ingresar")').first
    await btn.click(timeout=TOUT)

    await page.wait_for_load_state("networkidle", timeout=TOUT)
    log.info("Login: OK (si seguís viendo la pantalla de login, hay que ajustar selectores).")

async def flujo_turnos_nuevo(page):
    """
    Luego del login:
      - Navegar a listarCompleto
      - Click en pestaña 'Nuevo'
      - Seleccionar Servicio (servimod), Zona (id_zona), Depto (id_dpto)
      - Si OBJ_MEDICO == 'false' => click Buscar
        Else: escribir profesional, Enter, esperar overlay, luego click Buscar
      - Esperar y leer tabla #tblResultadoProfesionales, volcar filas al log
      - Mantener la ventana abierta para inspección manual
    """
    # 3) Navegación directa
    listar_url = PORTAL_URL.rstrip("/") + "/action/applicationAfi/turnos/turno/listar/listarCompleto"
    log.info(f"3) Navegando directo a listarCompleto: {listar_url}")
    await page.goto(listar_url, wait_until="domcontentloaded", timeout=TOUT)

    # 4) Click en pestaña "Nuevo"
    log.info("4) Click en pestaña 'Nuevo'…")
    # Varios selectores por las dudas:
    nuevo_link = page.locator('a.nav-link[href="#divTNue"] >> text=Nuevo').first
    if not await nuevo_link.is_visible():
        nuevo_link = page.locator('a.nav-link[href="#divTNue"]').first
    await nuevo_link.click(timeout=TOUT)
    await short_sleep(1)

    # 5) Seleccionar Servicio
    log.info(f"5) Seleccionando servicio: {OBJ_SERVICIO!r}")
    serv_sel = page.locator("select#servimod")
    await serv_sel.wait_for(timeout=TOUT)
    # Intentar por label (visible text)
    try:
        await serv_sel.select_option(label=OBJ_SERVICIO.strip())
    except Exception:
        # Plan B: igualar por mayúsculas usando evaluate
        await page.evaluate(
            """(svc_text) => {
                const sel = document.querySelector('#servimod');
                if (!sel) return;
                const t = (svc_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_SERVICIO
        )
    await short_sleep(1)

    # 6) Seleccionar Zona
    log.info(f"6) Seleccionando zona: {OBJ_ZONA!r}")
    zona_sel = page.locator("select#id_zona")
    await zona_sel.wait_for(timeout=TOUT)
    try:
        await zona_sel.select_option(label=OBJ_ZONA.strip())
    except Exception:
        await page.evaluate(
            """(zona_text) => {
                const sel = document.querySelector('#id_zona');
                if (!sel) return;
                const t = (zona_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_ZONA
        )

    # IMPORTANTE: el cambio de zona dispara cargar departamentos
    # Damos un pequeño tiempo para que se complete esa carga
    await short_sleep(0.8)

    # 7) Seleccionar Departamento
    log.info(f"7) Seleccionando departamento: {OBJ_DEPTO!r}")
    dpto_sel = page.locator("select#id_dpto")
    await dpto_sel.wait_for(timeout=TOUT)
    try:
        await dpto_sel.select_option(label=OBJ_DEPTO.strip())
    except Exception:
        await page.evaluate(
            """(dep_text) => {
                const sel = document.querySelector('#id_dpto');
                if (!sel) return;
                const t = (dep_text||'').trim().toUpperCase();
                for (const opt of sel.options) {
                    if ((opt.textContent||'').trim().toUpperCase() === t) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            OBJ_DEPTO
        )
    await short_sleep(1)

    # 8) Lógica del profesional (OBJ_MEDICO)
    buscar_btn = page.locator('input#buscar.button.buscar').first
    prof_input = page.locator('input#profesionalBusquedaComodin_turn')

    obj_medico_txt = (OBJ_MEDICO or "").strip()
    if obj_medico_txt.lower() == "false" or obj_medico_txt == "":
        log.info("8) OBJ_MEDICO = false => no se filtra por profesional; se clickea 'Buscar'.")
        try:
            await buscar_btn.click(timeout=TOUT)
        except Exception as e:
            log.warning(f"No pude hacer click en Buscar de forma directa ({e}); intento alternativo…")
            await page.locator("input#buscar").first.click()
    else:
        log.info(f"8) Filtrando por profesional: {obj_medico_txt!r}")
        await prof_input.wait_for(timeout=TOUT)
        # limpiar + tipear
        await prof_input.fill("")
        await prof_input.fill(obj_medico_txt)
        # Enter para disparar buscador/loader
        await prof_input.press("Enter")
        # Esperar overlay/toaster cargue y se vaya
        await wait_blocker_gone(page, timeout_ms=20000)
        # Y recién ahí Buscar
        await buscar_btn.click(timeout=TOUT)

    # 9) Esperar tabla de resultados y volcarla al log
    log.info("9) Esperando tabla de resultados…")
    tabla = page.locator("#tblResultadoProfesionales")
    await tabla.wait_for(timeout=TOUT)

    # Scraping de filas
    filas = await page.evaluate("""
        () => {
            const tbl = document.querySelector('#tblResultadoProfesionales');
            const out = [];
            if (!tbl) return out;
            const rows = tbl.querySelectorAll('tbody tr');
            for (const tr of rows) {
                const tds = tr.querySelectorAll('td');
                if (tds.length < 6) continue;
                const toText = (el) => (el ? el.innerText.trim().replace(/\\s+\\n/g, "\\n").replace(/\\s+/g,' ').trim() : "");
                out.push({
                    profesional: toText(tds[0]),
                    domicilio:   toText(tds[1]),
                    servicio:    toText(tds[2]),
                    horario:     toText(tds[3]),
                    disp:        toText(tds[4]),
                    agenda:      toText(tds[5]),
                    rowIndex:    Array.from(tr.parentNode.children).indexOf(tr)
                });
            }
            return out;
        }
    """)

    if not filas:
        log.warning("No se encontraron filas en la tabla (0 resultados).")
        return

    log.info(f"Resultados detectados: {len(filas)} fila(s).")

    # ---------------- FILTROS ----------------
    prof_filtro = (os.getenv("OBJ_PROFESIONAL") or "").strip().lower()
    dom_filtro  = (os.getenv("OBJ_DOMICILIO") or "").strip().lower()
    hor_filtro  = (os.getenv("OBJ_HORARIO_TURNO") or "").strip().lower()
    dias_filtro = [d.strip().upper() for d in (os.getenv("OBJ_DIAS_VALIDOS") or "").split(",") if d.strip()]
    fecha_filtro = (os.getenv("OBJ_FECHA_DISP") or "").strip()

    def cumple(f):
        # Profesional
        if prof_filtro and prof_filtro != "false" and prof_filtro not in f["profesional"].lower():
            return False
        # Domicilio
        if dom_filtro and dom_filtro != "false" and dom_filtro not in f["domicilio"].lower():
            return False
        # Horario
        if hor_filtro and hor_filtro != "false" and hor_filtro not in f["horario"].lower():
            return False
        # Días válidos
        if dias_filtro and not any(d in f["horario"].upper() for d in dias_filtro):
            return False
        # Fecha DISP
        if fecha_filtro and fecha_filtro != "false" and f["disp"] != fecha_filtro:
            return False
        # Si disp es --- descartamos (sin disponibilidad)
        if f["disp"].strip() == "---":
            return False
        return True

    candidatas = [f for f in filas if cumple(f)]

    # Si no hay coincidencias exactas, elegimos la más próxima por fecha
    from datetime import datetime
    
    if not candidatas:
        hoy = datetime.now()
        fechas_validas = []
        for f in filas:
            try:
                if f["disp"].strip() == "---":
                    continue
                fecha = datetime.strptime(f["disp"], "%d-%m-%Y")
                if fecha >= hoy:
                    fechas_validas.append((fecha, f))
            except Exception:
                continue
    
        if fecha_filtro and not OBJ_FECHA_FLEXIBLE:
            log.warning("No se encontró la fecha exacta y la flexibilidad está desactivada.")
            return
    
        if fechas_validas:
            fechas_validas.sort(key=lambda x: x[0])
            candidatas = [fechas_validas[0][1]]
            log.info(f"Usando la más próxima: {candidatas[0]['disp']}")
        else:
            log.warning("No hay fechas disponibles próximas.")
            return


    objetivo = candidatas[0]
    log.info(f"Seleccionada: {objetivo}")

    # Click en el ícono "Ver Agenda"
    fila_index = objetivo["rowIndex"]
    log.info(f"Haciendo click en 'Ver Agenda' (fila {fila_index})…")
    agenda_icon = page.locator(f"#tblResultadoProfesionales tbody tr:nth-of-type({fila_index + 1}) img#img_agenda_prof")
    await agenda_icon.first.click(timeout=TOUT)

    # Esperar a que desaparezca el overlay/toaster
    await wait_blocker_gone(page, timeout_ms=20000)

    # 10) Leer iframe de la agenda
    log.info("10) Esperando iframe de Agenda…")
    iframe = None
    for _ in range(20):
        for f in page.frames:
            if "pickMostrarAgenda_iframe" in (f.name or ""):
                iframe = f
                break
        if iframe:
            break
        await asyncio.sleep(0.5)
    if not iframe:
        log.error("No encontré el iframe de agenda.")
        return

    # Extraer información de la tabla dentro del iframe
    # 11) Leer tabla de horarios disponibles (espera robusta)
    log.info("11) Esperando que cargue la agenda dentro del iframe…")
    try:
        await iframe.wait_for_selector("div.horario_disponible, table.tabla_dias_horarios", timeout=10000)
    except Exception:
        log.warning("No se detectó ninguna celda con horario_disponible antes de timeout.")
    
    log.info("11) Leyendo tabla de horarios disponibles (estructura real)…")
    horarios = await iframe.evaluate("""
        () => {
            const cab = document.querySelectorAll('table.tabla_dias_horarios th.cabecera_dia, table.tabla_dias_horarios th.cabecera_hoy');
            const dias_labels = Array.from(cab).map(th => th.innerText.trim().replace(/\\s+/g,' '));
            const data = {};
            for (const lbl of dias_labels) data[lbl] = [];
    
            const filas = document.querySelectorAll('table.tabla_dias_horarios tbody tr');
            filas.forEach(tr => {
                const celdas = tr.querySelectorAll('td');
                celdas.forEach((td, idx) => {
                    const divs = td.querySelectorAll('div.horario_disponible');
                    divs.forEach(div => {
                        const txt = div.textContent.trim();
                        if (txt) {
                            const dia = dias_labels[idx] || `Columna ${idx}`;
                            data[dia] = data[dia] || [];
                            data[dia].push(txt);
                        }
                    });
                });
            });
            return data;
        }
    """)


    if not horarios:
        log.warning("No se detectaron horarios disponibles en la agenda.")
    else:
        for dia, horas in horarios.items():
            if horas:
                log.info(f"{dia}: {', '.join(horas)}")
            else:
                log.info(f"{dia}: sin horarios disponibles")

    



# 12) Seleccionar turno según franja horaria configurada
    from datetime import datetime

    log.info("12) Seleccionando turno dentro de franja horaria configurada…")

    hora_min = os.getenv("OBJ_HORA_MIN", "00:00").strip()
    hora_max = os.getenv("OBJ_HORA_MAX", "23:59").strip()
    # Si alguno viene "false", asignar valores amplios por defecto
    if hora_min.lower() == "false" or not hora_min:
        hora_min = "00:00"
    if hora_max.lower() == "false" or not hora_max:
        hora_max = "23:59"
    prioridad = (os.getenv("OBJ_HORA_PRIORIDAD", "EARLIEST") or "EARLIEST").upper()

    def hora_a_minutos(h):
        try:
            hh, mm = map(int, h.split(":"))
            return hh * 60 + mm
        except Exception:
            return None

    hmin = hora_a_minutos(hora_min)
    hmax = hora_a_minutos(hora_max)

    # Buscar el primer día con horarios disponibles
    dia_valido, horas_validas = None, []
    for dia, horas in horarios.items():
        if horas:
            filtradas = []
            for h in horas:
                hm = hora_a_minutos(h)
                if hm and hmin <= hm <= hmax:
                    filtradas.append(h)
            if filtradas:
                dia_valido = dia
                horas_validas = filtradas
                break

    if not dia_valido:
        if OBJ_HORA_FLEXIBLE:
            # No hay horario dentro del rango, tomar el primero disponible
            for dia, horas in horarios.items():
                if horas:
                    dia_valido = dia
                    hora_elegida = sorted(horas)[0]
                    log.info(f"No se encontró horario en rango, usando primero disponible: {dia_valido} / {hora_elegida}")
                    break
            else:
                log.warning("No se encontró ningún horario disponible en ningún día.")
                return
        else:
            log.warning(f"No se encontró ningún horario entre {hora_min} y {hora_max}, y flexibilidad está desactivada.")
            return
    else:
        if prioridad == "LATEST":
            hora_elegida = sorted(horas_validas)[-1]
        else:
            hora_elegida = sorted(horas_validas)[0]
        log.info(f"Día elegido: {dia_valido} / Hora elegida: {hora_elegida}")


        # Click en el div correspondiente
        log.info("Haciendo click en el horario disponible…")
        # Buscar el div con esa hora exacta
        await iframe.click(f"div.horario_disponible:text('{hora_elegida}')")
        await short_sleep(0.5)


        # 13) Esperar cuadro de confirmación
        log.info("13) Esperando cuadro de confirmación de turno…")
        try:

            # Esperar unos instantes extra por si se está animando
            await short_sleep(5.0)
        
            log.info("Cuadro de confirmación detectado. Simulando Tab + Enter para aceptar…")
        
            # Aseguramos foco en la página principal
            await page.bring_to_front()
        
            # Tecleamos Tab (para enfocar el botón Aceptar) y luego Enter
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.3)
            
            if DRY_RUN:
                log.info("(DRY_RUN activo) No se presiona Enter; flujo detenido antes de confirmar turno.")
                return
            else:
                await page.keyboard.press("Enter")
                log.info("Teclas Tab + Enter enviadas correctamente. Esperando que se cierre el cuadro…")        
                # Esperamos a que desaparezca el cuadro
                await page.wait_for_selector("#pickCustomTwoButtons", state="detached", timeout=10000)
                log.info("Cuadro de confirmación cerrado correctamente.")
        
        except Exception as e:
            log.error(f"No se pudo aceptar el cuadro de confirmación: {e}")
            return




            # 14) Esperar cuadro final de reserva
            log.info("14) Esperando cuadro final con datos del turno…")
            try:
                await page.wait_for_selector("div.pick_print table", timeout=20000)
                turno_info = await page.evaluate("""
                    () => {
                        const tbl = document.querySelector("div.pick_print table");
                        if (!tbl) return {};
                        const txt = tbl.innerText.trim().split("\\n");
                        return txt;
                    }
                """)
                log.info("==== TURNO CONFIRMADO ====")
                for line in turno_info:
                    log.info(line)
                log.info("===========================")
            except Exception as e:
                log.error(f"No se pudo leer la confirmación del turno: {e}")


    log.info("10) Flujo completado. La ventana quedará ABIERTA para revisión manual.")

# ================== MAIN ==================
async def amain() -> int:
    log.info("==== INICIO OSEP TURNOS (FLUJO NUEVO) ====")
    if not OSEP_USER or not OSEP_PASS:
        log.error("Definí OSEP_USER y OSEP_PASS en .env")
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.bring_to_front()
        try:
            await page.evaluate("window.moveTo(0,0); window.resizeTo(screen.availWidth, screen.availHeight);")
        except Exception:
            pass



        try:
            await login(page)

            if STOP_AFTER_LOGIN:
                log.info("STOP_AFTER_LOGIN activo: fin de prueba.")
                return 0

            await flujo_turnos_nuevo(page)
            return 0

        except Exception as e:
            log.error("Error en ejecución:\n" + "".join(traceback.format_exception(e)))
            return 3

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

# ================== Footer compatible Spyder/Terminal (Windows) ==================
if __name__ == "__main__":
    def _runner():
        if sys.platform.startswith("win"):
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            rc = loop.run_until_complete(amain())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        os._exit(rc)

    try:
        asyncio.get_running_loop()
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
    except RuntimeError:
        sys.exit(asyncio.run(amain()))