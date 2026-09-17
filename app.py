import os
import sys
import re
import logging
import datetime as dt
import traceback
import asyncio
import threading

from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright


# ============================================================
# CÓDIGOS DE SALIDA
# ============================================================
#
# GitHub Actions considera:
#
#   0       = ejecución exitosa
#   != 0    = ejecución con error
#
# Para este bot vamos a usar:
#
#   0  = TURNO CONSEGUIDO Y CONFIRMADO
#   10 = búsqueda correcta pero SIN TURNO
#   2  = configuración incompleta
#   3  = error técnico / resultado indeterminado
#   11 = DRY_RUN
#
# De esta manera, un Run verde significa específicamente
# que OSEP mostró la confirmación final.
# ============================================================

RC_TURNO_CONFIRMADO = 0
RC_CONFIG = 2
RC_ERROR_TECNICO = 3
RC_SIN_TURNO = 10
RC_DRY_RUN = 11


# ============================================================
# ENV / CONFIG
# ============================================================

load_dotenv()

PORTAL_URL = os.getenv(
    "PORTAL_URL",
    "https://www.osep.mendoza.gov.ar/webapp_pri"
)

OSEP_USER = os.getenv("OSEP_USER")
OSEP_PASS = os.getenv("OSEP_PASS")

OBJ_SERVICIO = os.getenv("OBJ_SERVICIO")
OBJ_MEDICO = os.getenv("OBJ_MEDICO", "false")
OBJ_ZONA = os.getenv("OBJ_ZONA")
OBJ_DEPTO = os.getenv("OBJ_DEPTO")

OBJ_FECHA_FLEXIBLE = (
    os.getenv("OBJ_FECHA_FLEXIBLE", "true").lower() == "true"
)

OBJ_DIA_FLEXIBLE = (
    os.getenv("OBJ_DIA_FLEXIBLE", "true").lower() == "true"
)

OBJ_HORA_FLEXIBLE = (
    os.getenv("OBJ_HORA_FLEXIBLE", "true").lower() == "true"
)

HEADLESS = (
    os.getenv("HEADLESS", "true").lower() != "false"
)

TOUT = int(
    os.getenv("TIMEOUT_MS", "20000")
)

DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower() == "true"
)

STOP_AFTER_LOGIN = (
    os.getenv("STOP_AFTER_LOGIN", "false").lower() == "true"
)


# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("osep")
log.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(message)s"
)

if not log.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

log.info("Logging iniciado (solo consola, sin archivo).")


# ============================================================
# GITHUB ACTIONS
# ============================================================

def gh_annotation(level: str, title: str, message: str):
    """
    Genera una annotation visible dentro de GitHub Actions.

    level:
        notice
        warning
        error
    """

    safe_title = str(title).replace("\n", " ")

    safe_message = (
        str(message)
        .replace("\r", "")
        .replace("\n", "%0A")
    )

    print(
        f"::{level} title={safe_title}::{safe_message}",
        flush=True
    )


def gh_summary(title: str, lines=None):
    """
    Escribe un resumen visible en la pantalla principal del Run.

    Fuera de GitHub Actions simplemente no hace nada.
    """

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")

    if not summary_path:
        return

    lines = lines or []

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(f"## {title}\n\n")

            for line in lines:
                f.write(f"{line}\n")

            f.write("\n")

    except Exception as e:
        log.debug(
            f"No se pudo escribir GITHUB_STEP_SUMMARY: {e}"
        )


def terminar_sin_turno(motivo: str) -> int:
    """
    Se usa exclusivamente cuando la consulta funcionó
    pero no se encontró disponibilidad que cumpla los criterios.
    """

    log.warning(motivo)

    gh_annotation(
        "warning",
        "SIN TURNO DISPONIBLE",
        motivo
    )

    gh_summary(
        "🟡 SIN TURNO DISPONIBLE",
        [
            "La consulta a OSEP se ejecutó correctamente, "
            "pero no se encontró un turno que cumpla "
            "los criterios configurados.",
            "",
            f"**Motivo:** {motivo}",
        ]
    )

    return RC_SIN_TURNO


def terminar_error(
    motivo: str,
    indeterminado: bool = False
) -> int:

    if indeterminado:
        titulo = "RESULTADO INDETERMINADO"
        resumen = "🔴 RESULTADO INDETERMINADO"

    else:
        titulo = "ERROR TÉCNICO"
        resumen = "🔴 ERROR TÉCNICO"

    log.error(motivo)

    gh_annotation(
        "error",
        titulo,
        motivo
    )

    gh_summary(
        resumen,
        [
            motivo,
            "",
            "Este resultado NO se considera "
            "'sin turno disponible'.",
            "Revisar el log del Run.",
        ]
    )

    return RC_ERROR_TECNICO


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def must_env(var: str):

    val = os.getenv(var)

    if not val:
        raise RuntimeError(
            f"Falta variable de entorno {var}"
        )

    return val


async def short_sleep(
    seconds: float = 1.0
):
    await asyncio.sleep(seconds)


async def wait_blocker_gone(
    page,
    timeout_ms: int = 15000
):
    """
    Espera overlays típicos del portal OSEP.
    """

    end = (
        dt.datetime.now()
        + dt.timedelta(milliseconds=timeout_ms)
    )

    sel = (
        ".blockUI, "
        ".blockOverlay, "
        ".blockMsg, "
        ".ui-blocker, "
        ".blocker, "
        ".jq-toast-wrap, "
        ".toast, "
        ".loading, "
        ".modal-backdrop.show"
    )

    while dt.datetime.now() < end:

        try:
            cnt = await page.locator(sel).count()

            if cnt == 0:
                await asyncio.sleep(0.2)
                return

        except Exception:
            pass

        await asyncio.sleep(0.2)

    log.debug(
        "wait_blocker_gone: timeout esperando overlay."
    )


def normalizar(txt: str) -> str:

    return (
        txt or ""
    ).strip().upper()


def parse_fecha_disp(txt: str):

    try:
        return datetime.strptime(
            (txt or "").strip(),
            "%d-%m-%Y"
        )

    except Exception:
        return None


def hora_a_minutos(h: str):

    try:
        hh, mm = map(
            int,
            h.strip().split(":")
        )

        return hh * 60 + mm

    except Exception:
        return None


# ============================================================
# DÍAS DE LA SEMANA
# ============================================================

DAY_ALIASES = {

    "LUNES": (
        "LUN",
        "LU",
    ),

    "MARTES": (
        "MAR",
        "MA",
    ),

    "MIERCOLES": (
        "MIE",
        "MIÉ",
        "MI",
    ),

    "MIÉRCOLES": (
        "MIE",
        "MIÉ",
        "MI",
    ),

    "JUEVES": (
        "JUE",
        "JU",
    ),

    "VIERNES": (
        "VIE",
        "VI",
    ),

    "SABADO": (
        "SAB",
        "SÁB",
        "SA",
    ),

    "SÁBADO": (
        "SAB",
        "SÁB",
        "SA",
    ),

    "DOMINGO": (
        "DOM",
        "DO",
    ),
}


def dias_configurados():
    """
    Ejemplo YAML:

        OBJ_DIAS_VALIDOS: "LUNES,MIERCOLES,VIERNES"

    Si la variable no existe, acepta cualquier día.
    """

    raw = os.getenv(
        "OBJ_DIAS_VALIDOS",
        ""
    )

    return [
        normalizar(x)
        for x in raw.split(",")
        if x.strip()
    ]


def texto_contiene_dia_valido(
    texto: str,
    dias_validos
) -> bool:
    """
    Permite reconocer tanto:

        TARDE MA MI JU

    como:

        Jue 17-09 14:00 a 18:00
    """

    if not dias_validos:
        return True

    t = normalizar(texto)

    tokens = set(
        re.findall(
            r"[A-ZÁÉÍÓÚÜÑ]+",
            t
        )
    )

    for dia in dias_validos:

        aliases = DAY_ALIASES.get(
            dia,
            (dia,)
        )

        if dia in tokens:
            return True

        if any(
            alias in tokens
            for alias in aliases
        ):
            return True

    return False


# ============================================================
# LOGIN
# ============================================================

async def login(page):

    log.info(
        "1) Navegando al portal…"
    )

    await page.goto(
        PORTAL_URL,
        wait_until="domcontentloaded",
        timeout=TOUT
    )

    log.info(
        "2) Login (usuario/contraseña)…"
    )

    user_sel = (
        'input[name="usuario"], '
        'input#usuario, '
        'input[placeholder*="Usuario" i]'
    )

    pass_sel = (
        'input[name="password"], '
        'input#password, '
        'input[placeholder*="Contraseña" i], '
        'input[type="password"]'
    )

    await page.locator(
        user_sel
    ).first.wait_for(
        timeout=TOUT
    )

    await page.fill(
        user_sel,
        must_env("OSEP_USER")
    )

    await page.fill(
        pass_sel,
        must_env("OSEP_PASS")
    )

    btn = page.get_by_role(
        "button",
        name=re.compile(
            r"(ingresar|entrar|acceder|login)",
            re.I
        )
    ).first

    if not await btn.is_visible():

        btn = page.locator(
            'button:has-text("Ingresar"), '
            'input[type="submit"], '
            'a:has-text("Ingresar")'
        ).first

    await btn.click(
        timeout=TOUT
    )

    await page.wait_for_load_state(
        "networkidle",
        timeout=TOUT
    )

    log.info(
        "Login: OK "
        "(si seguís viendo login, revisar selectores)."
    )


# ============================================================
# FLUJO PRINCIPAL
# ============================================================

async def flujo_turnos_nuevo(page) -> int:

    # ========================================================
    # 3) NAVEGAR A TURNOS
    # ========================================================

    listar_url = (
        PORTAL_URL.rstrip("/")
        + "/action/applicationAfi/turnos/"
          "turno/listar/listarCompleto"
    )

    log.info(
        f"3) Navegando directo a listarCompleto: "
        f"{listar_url}"
    )

    await page.goto(
        listar_url,
        wait_until="domcontentloaded",
        timeout=TOUT
    )


    # ========================================================
    # 4) PESTAÑA NUEVO
    # ========================================================

    log.info(
        "4) Click en pestaña 'Nuevo'…"
    )

    nuevo_link = page.locator(
        'a.nav-link[href="#divTNue"] >> text=Nuevo'
    ).first

    if not await nuevo_link.is_visible():

        nuevo_link = page.locator(
            'a.nav-link[href="#divTNue"]'
        ).first

    await nuevo_link.click(
        timeout=TOUT
    )

    await short_sleep(1)


    # ========================================================
    # 5) SERVICIO
    # ========================================================

    log.info(
        f"5) Seleccionando servicio: "
        f"{OBJ_SERVICIO!r}"
    )

    serv_sel = page.locator(
        "select#servimod"
    )

    await serv_sel.wait_for(
        timeout=TOUT
    )

    try:

        await serv_sel.select_option(
            label=OBJ_SERVICIO.strip()
        )

    except Exception:

        await page.evaluate(
            """
            (svc_text) => {

                const sel =
                    document.querySelector('#servimod');

                if (!sel) return;

                const t =
                    (svc_text || '')
                    .trim()
                    .toUpperCase();

                for (const opt of sel.options) {

                    if (
                        (opt.textContent || '')
                        .trim()
                        .toUpperCase() === t
                    ) {

                        sel.value = opt.value;

                        sel.dispatchEvent(
                            new Event(
                                'change',
                                {bubbles:true}
                            )
                        );

                        break;
                    }
                }
            }
            """,
            OBJ_SERVICIO
        )

    await short_sleep(1)


    # ========================================================
    # 6) ZONA
    # ========================================================

    log.info(
        f"6) Seleccionando zona: "
        f"{OBJ_ZONA!r}"
    )

    zona_sel = page.locator(
        "select#id_zona"
    )

    await zona_sel.wait_for(
        timeout=TOUT
    )

    try:

        await zona_sel.select_option(
            label=OBJ_ZONA.strip()
        )

    except Exception:

        await page.evaluate(
            """
            (zona_text) => {

                const sel =
                    document.querySelector('#id_zona');

                if (!sel) return;

                const t =
                    (zona_text || '')
                    .trim()
                    .toUpperCase();

                for (const opt of sel.options) {

                    if (
                        (opt.textContent || '')
                        .trim()
                        .toUpperCase() === t
                    ) {

                        sel.value = opt.value;

                        sel.dispatchEvent(
                            new Event(
                                'change',
                                {bubbles:true}
                            )
                        );

                        break;
                    }
                }
            }
            """,
            OBJ_ZONA
        )

    # Al cambiar zona OSEP carga los departamentos.
    await short_sleep(0.8)


    # ========================================================
    # 7) DEPARTAMENTO
    # ========================================================

    log.info(
        f"7) Seleccionando departamento: "
        f"{OBJ_DEPTO!r}"
    )

    dpto_sel = page.locator(
        "select#id_dpto"
    )

    await dpto_sel.wait_for(
        timeout=TOUT
    )

    try:

        await dpto_sel.select_option(
            label=OBJ_DEPTO.strip()
        )

    except Exception:

        await page.evaluate(
            """
            (dep_text) => {

                const sel =
                    document.querySelector('#id_dpto');

                if (!sel) return;

                const t =
                    (dep_text || '')
                    .trim()
                    .toUpperCase();

                for (const opt of sel.options) {

                    if (
                        (opt.textContent || '')
                        .trim()
                        .toUpperCase() === t
                    ) {

                        sel.value = opt.value;

                        sel.dispatchEvent(
                            new Event(
                                'change',
                                {bubbles:true}
                            )
                        );

                        break;
                    }
                }
            }
            """,
            OBJ_DEPTO
        )

    await short_sleep(1)


    # ========================================================
    # 8) PROFESIONAL
    # ========================================================

    buscar_btn = page.locator(
        'input#buscar.button.buscar'
    ).first

    prof_input = page.locator(
        'input#profesionalBusquedaComodin_turn'
    )

    obj_medico_txt = (
        OBJ_MEDICO or ""
    ).strip()

    if (
        obj_medico_txt.lower() == "false"
        or obj_medico_txt == ""
    ):

        log.info(
            "8) OBJ_MEDICO = false => "
            "no se filtra por profesional."
        )

        try:

            await buscar_btn.click(
                timeout=TOUT
            )

        except Exception as e:

            log.warning(
                "No pude hacer click en Buscar "
                f"de forma directa ({e}); "
                "intento alternativo…"
            )

            await page.locator(
                "input#buscar"
            ).first.click()

    else:

        log.info(
            f"8) Filtrando por profesional: "
            f"{obj_medico_txt!r}"
        )

        await prof_input.wait_for(
            timeout=TOUT
        )

        await prof_input.fill("")

        await prof_input.fill(
            obj_medico_txt
        )

        await prof_input.press(
            "Enter"
        )

        await wait_blocker_gone(
            page,
            timeout_ms=20000
        )

        await buscar_btn.click(
            timeout=TOUT
        )


    # ========================================================
    # 9) TABLA DE RESULTADOS
    # ========================================================

    log.info(
        "9) Esperando tabla de resultados…"
    )

    tabla = page.locator(
        "#tblResultadoProfesionales"
    )

    await tabla.wait_for(
        timeout=TOUT
    )

    filas = await page.evaluate(
        """
        () => {

            const tbl =
                document.querySelector(
                    '#tblResultadoProfesionales'
                );

            const out = [];

            if (!tbl) return out;

            const rows =
                tbl.querySelectorAll('tbody tr');

            for (const tr of rows) {

                const tds =
                    tr.querySelectorAll('td');

                if (tds.length < 6)
                    continue;

                const toText = (el) => (
                    el
                    ? el.innerText
                        .trim()
                        .replace(/\\s+\\n/g, "\\n")
                        .replace(/\\s+/g, ' ')
                        .trim()
                    : ""
                );

                out.push({

                    profesional:
                        toText(tds[0]),

                    domicilio:
                        toText(tds[1]),

                    servicio:
                        toText(tds[2]),

                    horario:
                        toText(tds[3]),

                    disp:
                        toText(tds[4]),

                    agenda:
                        toText(tds[5]),

                    rowIndex:
                        Array.from(
                            tr.parentNode.children
                        ).indexOf(tr)
                });
            }

            return out;
        }
        """
    )

    if not filas:

        return terminar_sin_turno(
            "OSEP no devolvió filas de profesionales "
            "con disponibilidad."
        )

    log.info(
        f"Resultados detectados: "
        f"{len(filas)} fila(s)."
    )


    # ========================================================
    # FILTROS ADICIONALES
    # ========================================================

    prof_filtro = normalizar(
        os.getenv(
            "OBJ_PROFESIONAL",
            ""
        )
    )

    dom_filtro = normalizar(
        os.getenv(
            "OBJ_DOMICILIO",
            ""
        )
    )

    hor_filtro = normalizar(
        os.getenv(
            "OBJ_HORARIO_TURNO",
            ""
        )
    )

    dias_filtro = dias_configurados()

    fecha_filtro = (
        os.getenv(
            "OBJ_FECHA_DISP"
        )
        or ""
    ).strip()


    def cumple_base(
        f,
        aplicar_dias=True
    ):

        # Profesional
        if (
            prof_filtro
            and prof_filtro != "FALSE"
            and prof_filtro
            not in normalizar(
                f["profesional"]
            )
        ):
            return False

        # Domicilio
        if (
            dom_filtro
            and dom_filtro != "FALSE"
            and dom_filtro
            not in normalizar(
                f["domicilio"]
            )
        ):
            return False

        # Horario general
        if (
            hor_filtro
            and hor_filtro != "FALSE"
            and hor_filtro
            not in normalizar(
                f["horario"]
            )
        ):
            return False

        # Días
        if (
            aplicar_dias
            and dias_filtro
        ):

            if not texto_contiene_dia_valido(
                f["horario"],
                dias_filtro
            ):
                return False

        # Sin disponibilidad
        if (
            f["disp"].strip()
            == "---"
        ):
            return False

        return True


    # Primero se respetan los días indicados.
    candidatas_base = [

        f

        for f in filas

        if cumple_base(
            f,
            aplicar_dias=True
        )
    ]


    # Si no hay resultados y el día es flexible,
    # podemos aceptar otro día.
    if (
        not candidatas_base
        and dias_filtro
        and OBJ_DIA_FLEXIBLE
    ):

        log.info(
            "No hubo coincidencias con "
            "OBJ_DIAS_VALIDOS; "
            "OBJ_DIA_FLEXIBLE=true => "
            "se permiten otros días."
        )

        candidatas_base = [

            f

            for f in filas

            if cumple_base(
                f,
                aplicar_dias=False
            )
        ]


    if not candidatas_base:

        if dias_filtro:

            return terminar_sin_turno(
                "No se encontraron resultados "
                "que cumplan los días válidos."
            )

        return terminar_sin_turno(
            "No se encontraron resultados "
            "que cumplan los filtros configurados."
        )


    # ========================================================
    # FILTRO DE FECHA
    # ========================================================

    hoy = datetime.now().date()

    candidatas_con_fecha = []

    for f in candidatas_base:

        fecha = parse_fecha_disp(
            f["disp"]
        )

        if (
            fecha
            and fecha.date() >= hoy
        ):

            candidatas_con_fecha.append(
                (fecha, f)
            )


    if not candidatas_con_fecha:

        return terminar_sin_turno(
            "No hay fechas disponibles "
            "iguales o posteriores a hoy."
        )


    # Si se pidió una fecha exacta.
    if (
        fecha_filtro
        and fecha_filtro.lower()
        != "false"
    ):

        exactas = [

            (fecha, f)

            for fecha, f
            in candidatas_con_fecha

            if (
                f["disp"].strip()
                == fecha_filtro
            )
        ]

        if exactas:

            candidatas_con_fecha = exactas

        elif OBJ_FECHA_FLEXIBLE:

            log.info(
                "No se encontró la fecha exacta "
                f"{fecha_filtro}; "
                "OBJ_FECHA_FLEXIBLE=true => "
                "se usará la próxima disponible."
            )

        else:

            return terminar_sin_turno(
                "No se encontró disponibilidad "
                f"para la fecha {fecha_filtro} "
                "y OBJ_FECHA_FLEXIBLE=false."
            )


    # Elegir la fecha disponible más próxima.
    candidatas_con_fecha.sort(
        key=lambda x: x[0]
    )

    objetivo = (
        candidatas_con_fecha[0][1]
    )

    log.info(
        f"Seleccionada: {objetivo}"
    )


    # ========================================================
    # ABRIR AGENDA
    # ========================================================

    fila_index = objetivo[
        "rowIndex"
    ]

    log.info(
        "Haciendo click en "
        f"'Ver Agenda' (fila {fila_index})…"
    )

    agenda_icon = page.locator(
        "#tblResultadoProfesionales "
        "tbody "
        f"tr:nth-of-type({fila_index + 1}) "
        "img#img_agenda_prof"
    )

    await agenda_icon.first.click(
        timeout=TOUT
    )

    await wait_blocker_gone(
        page,
        timeout_ms=20000
    )


    # ========================================================
    # 10) IFRAME AGENDA
    # ========================================================

    log.info(
        "10) Esperando iframe de Agenda…"
    )

    iframe = None

    for _ in range(20):

        for f in page.frames:

            if (
                "pickMostrarAgenda_iframe"
                in (f.name or "")
            ):

                iframe = f
                break

        if iframe:
            break

        await asyncio.sleep(0.5)


    if not iframe:

        return terminar_error(
            "No se encontró el iframe "
            "de agenda de OSEP."
        )


    # ========================================================
    # 11) HORARIOS
    # ========================================================

    log.info(
        "11) Esperando que cargue "
        "la agenda dentro del iframe…"
    )

    try:

        await iframe.wait_for_selector(
            "div.horario_disponible, "
            "table.tabla_dias_horarios",
            timeout=10000
        )

    except Exception:

        log.warning(
            "No se detectó horario_disponible "
            "antes del timeout; "
            "se intentará leer igualmente."
        )


    log.info(
        "11) Leyendo tabla "
        "de horarios disponibles…"
    )

    horarios = await iframe.evaluate(
        """
        () => {

            const cab =
                document.querySelectorAll(
                    'table.tabla_dias_horarios '
                    + 'th.cabecera_dia, '
                    + 'table.tabla_dias_horarios '
                    + 'th.cabecera_hoy'
                );

            const dias_labels =
                Array.from(cab).map(
                    th =>
                        th.innerText
                        .trim()
                        .replace(/\\s+/g, ' ')
                );

            const data = {};

            for (const lbl of dias_labels) {
                data[lbl] = [];
            }

            const filas =
                document.querySelectorAll(
                    'table.tabla_dias_horarios '
                    + 'tbody tr'
                );

            filas.forEach(tr => {

                const celdas =
                    tr.querySelectorAll('td');

                celdas.forEach(
                    (td, idx) => {

                        const divs =
                            td.querySelectorAll(
                                'div.horario_disponible'
                            );

                        divs.forEach(
                            div => {

                                const txt =
                                    div.textContent
                                    .trim();

                                if (txt) {

                                    const dia =
                                        dias_labels[idx]
                                        || `Columna ${idx}`;

                                    data[dia] =
                                        data[dia]
                                        || [];

                                    data[dia].push(
                                        txt
                                    );
                                }
                            }
                        );
                    }
                );
            });

            return data;
        }
        """
    )


    if not horarios:

        return terminar_sin_turno(
            "La agenda abrió correctamente, "
            "pero no devolvió horarios."
        )


    for dia, horas in horarios.items():

        if horas:

            log.info(
                f"{dia}: "
                f"{', '.join(horas)}"
            )

        else:

            log.info(
                f"{dia}: "
                "sin horarios disponibles"
            )


    # ========================================================
    # 12) SELECCIONAR DÍA Y HORARIO
    # ========================================================

    log.info(
        "12) Seleccionando turno "
        "según criterios configurados…"
    )

    hora_min = os.getenv(
        "OBJ_HORA_MIN",
        "00:00"
    ).strip()

    hora_max = os.getenv(
        "OBJ_HORA_MAX",
        "23:59"
    ).strip()


    if (
        hora_min.lower() == "false"
        or not hora_min
    ):
        hora_min = "00:00"


    if (
        hora_max.lower() == "false"
        or not hora_max
    ):
        hora_max = "23:59"


    hmin = hora_a_minutos(
        hora_min
    )

    hmax = hora_a_minutos(
        hora_max
    )


    if (
        hmin is None
        or hmax is None
    ):

        return terminar_error(
            "Formato inválido en "
            "OBJ_HORA_MIN / OBJ_HORA_MAX: "
            f"{hora_min!r} / {hora_max!r}."
        )


    prioridad = normalizar(
        os.getenv(
            "OBJ_HORA_PRIORIDAD",
            "EARLIEST"
        )
    )


    if prioridad not in {
        "EARLIEST",
        "LATEST"
    }:

        prioridad = "EARLIEST"


    # ========================================================
    # FILTRAR DÍAS REALES DE LA AGENDA
    # ========================================================

    agenda_dias = list(
        horarios.items()
    )


    if dias_filtro:

        agenda_dias_validos = [

            (dia, horas)

            for dia, horas
            in agenda_dias

            if texto_contiene_dia_valido(
                dia,
                dias_filtro
            )
        ]


        if agenda_dias_validos:

            agenda_dias = (
                agenda_dias_validos
            )


        elif OBJ_DIA_FLEXIBLE:

            log.info(
                "No hay horarios en los "
                "días configurados; "
                "OBJ_DIA_FLEXIBLE=true => "
                "se permiten otros días."
            )


        else:

            return terminar_sin_turno(
                "La agenda no tiene disponibilidad "
                "en OBJ_DIAS_VALIDOS y "
                "OBJ_DIA_FLEXIBLE=false."
            )


    # ========================================================
    # BUSCAR HORAS DENTRO DE LA FRANJA
    # ========================================================

    opciones_en_rango = []


    for dia, horas in agenda_dias:

        for h in horas:

            hm = hora_a_minutos(h)

            if (
                hm is not None
                and hmin <= hm <= hmax
            ):

                opciones_en_rango.append(
                    (dia, h, hm)
                )


    if opciones_en_rango:

        # Mantener el primer día disponible.
        primer_dia = (
            opciones_en_rango[0][0]
        )

        opciones_primer_dia = [

            x

            for x
            in opciones_en_rango

            if x[0] == primer_dia
        ]


        if prioridad == "LATEST":

            elegido = max(
                opciones_primer_dia,
                key=lambda x: x[2]
            )

        else:

            elegido = min(
                opciones_primer_dia,
                key=lambda x: x[2]
            )


        dia_valido, hora_elegida, _ = elegido


    else:

        # No encontramos horario en la franja.

        if not OBJ_HORA_FLEXIBLE:

            return terminar_sin_turno(
                "No se encontró ningún horario "
                f"entre {hora_min} y {hora_max} "
                "y OBJ_HORA_FLEXIBLE=false."
            )


        # Si es flexible, aceptar cualquier hora.
        opciones_flexibles = []


        for dia, horas in agenda_dias:

            for h in horas:

                hm = hora_a_minutos(h)

                if hm is not None:

                    opciones_flexibles.append(
                        (dia, h, hm)
                    )


        if not opciones_flexibles:

            return terminar_sin_turno(
                "No se encontró ningún horario "
                "disponible en la agenda."
            )


        primer_dia = (
            opciones_flexibles[0][0]
        )

        opciones_primer_dia = [

            x

            for x
            in opciones_flexibles

            if x[0] == primer_dia
        ]


        if prioridad == "LATEST":

            elegido = max(
                opciones_primer_dia,
                key=lambda x: x[2]
            )

        else:

            elegido = min(
                opciones_primer_dia,
                key=lambda x: x[2]
            )


        dia_valido, hora_elegida, _ = elegido


        log.info(
            "No hubo horario dentro del rango; "
            "OBJ_HORA_FLEXIBLE=true => "
            f"se usará {dia_valido} / "
            f"{hora_elegida}."
        )


    log.info(
        f"Día elegido: {dia_valido} / "
        f"Hora elegida: {hora_elegida}"
    )


    # ========================================================
    # CLICK EN HORARIO
    # ========================================================

    log.info(
        "Haciendo click en "
        "el horario disponible…"
    )

    # Conservamos exactamente la misma técnica
    # que utilizaba el script original.
    await iframe.click(
        "div.horario_disponible:"
        f"text('{hora_elegida}')"
    )

    await short_sleep(0.5)


    # ========================================================
    # 13) CUADRO DE CONFIRMACIÓN
    # ========================================================

    log.info(
        "13) Esperando cuadro "
        "de confirmación de turno…"
    )


    try:

        # Este ID ya estaba presente
        # en el código original.
        await page.wait_for_selector(
            "#pickCustomTwoButtons",
            state="visible",
            timeout=10000
        )


        log.info(
            "Cuadro de confirmación detectado. "
            "Simulando Tab + Enter para aceptar…"
        )


        await page.bring_to_front()


        # Conservamos el mismo mecanismo
        # que actualmente te funciona.
        await page.keyboard.press(
            "Tab"
        )

        await asyncio.sleep(0.3)


        # ====================================================
        # DRY RUN
        # ====================================================

        if DRY_RUN:

            mensaje = (
                "DRY_RUN activo: "
                "se encontró un turno y "
                "se abrió el cuadro de confirmación, "
                "pero NO se presionó Enter."
            )

            log.warning(
                mensaje
            )

            gh_annotation(
                "warning",
                "DRY RUN",
                mensaje
            )

            gh_summary(
                "🧪 DRY RUN",
                [
                    f"**Profesional:** "
                    f"{objetivo['profesional']}",

                    f"**Disponibilidad:** "
                    f"{objetivo['disp']}",

                    f"**Día:** "
                    f"{dia_valido}",

                    f"**Hora:** "
                    f"{hora_elegida}",

                    "",
                    "No se confirmó el turno "
                    "porque DRY_RUN=true.",
                ]
            )

            return RC_DRY_RUN


        # ====================================================
        # CONFIRMAR
        # ====================================================

        await page.keyboard.press(
            "Enter"
        )


        log.info(
            "Teclas Tab + Enter enviadas "
            "correctamente. "
            "Esperando que se cierre "
            "el cuadro…"
        )


        await page.wait_for_selector(
            "#pickCustomTwoButtons",
            state="detached",
            timeout=10000
        )


        log.info(
            "Cuadro de confirmación "
            "cerrado correctamente."
        )


    except Exception as e:

        return terminar_error(
            "No se pudo completar el cuadro "
            "de confirmación del turno: "
            f"{type(e).__name__}: {e}"
        )


    # ========================================================
    # 14) VERIFICACIÓN FINAL
    # ========================================================
    #
    # MUY IMPORTANTE:
    #
    # Este selector NO es nuevo.
    #
    # Ya existía en tu app.py:
    #
    #     div.pick_print table
    #
    # El problema era que el código original
    # lo tenía después de un `return` dentro
    # del bloque except, por lo que nunca
    # llegaba a ejecutarse.
    #
    # Un Run solamente será VERDE si:
    #
    # 1. se encontró turno;
    # 2. se seleccionó;
    # 3. se envió Enter;
    # 4. se cerró el cuadro;
    # 5. OSEP mostró esta tabla final;
    # 6. la tabla contiene texto.
    #
    # Si 1-4 ocurren pero 5-6 no pueden
    # comprobarse, NO asumimos que falló.
    # Marcamos RESULTADO INDETERMINADO.
    # ========================================================

    log.info(
        "14) Verificando confirmación "
        "final del turno en OSEP…"
    )


    try:

        confirmacion = page.locator(
            "div.pick_print table"
        )


        await confirmacion.wait_for(
            state="visible",
            timeout=20000
        )


        texto_confirmacion = (
            await confirmacion.inner_text()
        ).strip()


        if not texto_confirmacion:

            return terminar_error(
                "OSEP mostró el contenedor final "
                "de confirmación, pero la tabla "
                "estaba vacía. "
                "No se puede asegurar el resultado.",
                indeterminado=True
            )


        turno_info = [

            linea.strip()

            for linea
            in texto_confirmacion.splitlines()

            if linea.strip()
        ]


        # ====================================================
        # TURNO CONFIRMADO
        # ====================================================

        log.info(
            "==================================="
        )

        log.info(
            "✅ TURNO CONSEGUIDO Y CONFIRMADO"
        )

        log.info(
            "==================================="
        )


        for line in turno_info:

            log.info(
                line
            )


        log.info(
            "==================================="
        )


        gh_annotation(
            "notice",
            "TURNO CONSEGUIDO",
            "OSEP mostró la confirmación "
            "final del turno."
        )


        gh_summary(
            "✅ TURNO CONSEGUIDO Y CONFIRMADO",
            [
                f"**Profesional:** "
                f"{objetivo['profesional']}",

                f"**Servicio:** "
                f"{objetivo['servicio']}",

                f"**Disponibilidad informada:** "
                f"{objetivo['disp']}",

                f"**Día seleccionado:** "
                f"{dia_valido}",

                f"**Hora seleccionada:** "
                f"{hora_elegida}",

                "",

                "**Confirmación devuelta por OSEP:**",

                *[
                    f"- {line}"
                    for line in turno_info
                ],
            ]
        )


        return RC_TURNO_CONFIRMADO


    except Exception as e:

        # ====================================================
        # CASO IMPORTANTE
        # ====================================================
        #
        # Acá ya enviamos Enter.
        #
        # Por eso NO afirmamos:
        #
        #     "el turno no se consiguió"
        #
        # porque podría haberse registrado
        # y simplemente haber cambiado el HTML.
        #
        # Lo marcamos como INDETERMINADO.
        # ====================================================

        return terminar_error(
            "Se envió la confirmación del turno "
            "y el cuadro se cerró, pero no apareció "
            "`div.pick_print table` dentro del "
            "tiempo esperado. "
            "El bot NO puede afirmar si OSEP "
            "reservó o no el turno. "
            "Detalle técnico: "
            f"{type(e).__name__}: {e}",
            indeterminado=True
        )


# ============================================================
# MAIN
# ============================================================

async def amain() -> int:

    log.info(
        "==== INICIO OSEP TURNOS "
        "(FLUJO NUEVO) ===="
    )


    # ========================================================
    # VALIDAR CONFIGURACIÓN MÍNIMA
    # ========================================================

    faltantes = [

        nombre

        for nombre, valor
        in {

            "OSEP_USER":
                OSEP_USER,

            "OSEP_PASS":
                OSEP_PASS,

            "OBJ_SERVICIO":
                OBJ_SERVICIO,

            "OBJ_ZONA":
                OBJ_ZONA,

            "OBJ_DEPTO":
                OBJ_DEPTO,

        }.items()

        if not valor
    ]


    if faltantes:

        mensaje = (
            "Faltan variables obligatorias: "
            + ", ".join(faltantes)
        )


        log.error(
            mensaje
        )


        gh_annotation(
            "error",
            "CONFIGURACIÓN INCOMPLETA",
            mensaje
        )


        gh_summary(
            "🔴 CONFIGURACIÓN INCOMPLETA",
            [
                mensaje
            ]
        )


        return RC_CONFIG


    # ========================================================
    # PLAYWRIGHT
    # ========================================================

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox"
            ]
        )

        context = (
            await browser.new_context()
        )

        page = (
            await context.new_page()
        )


        try:

            await page.bring_to_front()


            try:

                await page.evaluate(
                    "window.moveTo(0,0); "
                    "window.resizeTo("
                    "screen.availWidth,"
                    "screen.availHeight"
                    ");"
                )

            except Exception:
                pass


            # =================================================
            # LOGIN
            # =================================================

            await login(page)


            if STOP_AFTER_LOGIN:

                mensaje = (
                    "STOP_AFTER_LOGIN activo: "
                    "login completado y prueba "
                    "detenida antes de buscar turnos."
                )

                log.warning(
                    mensaje
                )

                gh_annotation(
                    "warning",
                    "PRUEBA DETENIDA",
                    mensaje
                )

                gh_summary(
                    "🧪 PRUEBA DETENIDA",
                    [
                        mensaje
                    ]
                )

                return RC_DRY_RUN


            # =================================================
            # MUY IMPORTANTE
            # =================================================
            #
            # En el código anterior se hacía:
            #
            #     await flujo_turnos_nuevo(page)
            #     return 0
            #
            # Por eso TODO aparecía verde.
            #
            # Ahora devolvemos exactamente
            # el resultado del flujo.
            # =================================================

            return await flujo_turnos_nuevo(
                page
            )


        except Exception as e:

            detalle = "".join(
                traceback.format_exception(e)
            )


            log.error(
                "Error en ejecución:\n"
                + detalle
            )


            gh_annotation(
                "error",
                "ERROR TÉCNICO",
                f"{type(e).__name__}: {e}"
            )


            gh_summary(
                "🔴 ERROR TÉCNICO",
                [
                    f"`{type(e).__name__}: {e}`",
                    "",
                    "Revisar el log completo "
                    "del step **Run bot**.",
                ]
            )


            return RC_ERROR_TECNICO


        finally:

            try:
                await context.close()

            except Exception:
                pass


            try:
                await browser.close()

            except Exception:
                pass


# ============================================================
# EJECUCIÓN
# ============================================================
#
# Compatible con:
#
# - GitHub Actions
# - terminal
# - Windows
# - entornos donde ya exista un event loop
#   como algunas configuraciones de Spyder/IPython
# ============================================================

def _run_amain_sync() -> int:

    if sys.platform.startswith(
        "win"
    ):

        try:

            asyncio.set_event_loop_policy(
                asyncio.WindowsProactorEventLoopPolicy()
            )

        except Exception:
            pass


    return asyncio.run(
        amain()
    )


if __name__ == "__main__":

    try:

        asyncio.get_running_loop()


    except RuntimeError:

        # Caso normal:
        # terminal / GitHub Actions
        sys.exit(
            _run_amain_sync()
        )


    else:

        # Fallback para Spyder/IPython
        # cuando ya existe un event loop.

        result = {
            "rc": RC_ERROR_TECNICO
        }


        def _runner():

            try:

                result["rc"] = (
                    _run_amain_sync()
                )

            except Exception as e:

                log.error(
                    "Error ejecutando amain() "
                    f"en thread: {e}"
                )

                result["rc"] = (
                    RC_ERROR_TECNICO
                )


        t = threading.Thread(
            target=_runner,
            daemon=True
        )

        t.start()
        t.join()


        raise SystemExit(
            result["rc"]
        )
