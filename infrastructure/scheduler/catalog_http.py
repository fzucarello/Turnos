"""Lectura acotada del catálogo privado. Nunca abre agendas ni ejecuta reservas."""
import asyncio
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SEARCH_PATH = "/webapp_pri/action/applicationAfi/turnos/agenda/listar/listarCompleto/buscar"
DEPARTMENT_PATH = "/webapp_pri/action/applicationAfi/turnos/agenda/listar/listarCompleto/form/mostrarComboDpto"
MAX_ROWS = 500
MAX_BODY = 1500000


class CatalogError(Exception):
    """Sólo códigos técnicos seguros, sin cuerpos HTTP ni valores de formularios."""

    def __init__(self, code, diagnostic=None):
        super().__init__(code)
        self.diagnostic = diagnostic


class CatalogNoResults(CatalogError):
    """OSEP confirmó explícitamente ausencia de agendas en respuesta AJAX."""


def explicit_empty_notice(body):
    """Sólo aceptar una notificación JS pequeña e inequívoca de cero agendas.

    No interpretar como vacío un error de sesión, autenticación o acceso.
    Jamás se evalúa el JavaScript del servidor ni se conserva su contenido.
    """
    if not 1 <= len(body) <= 2048:
        return False
    try:
        message = body.decode("utf-8")
    except UnicodeError:
        try:
            message = body.decode("latin-1")
        except UnicodeError:
            return False
    if re.search(r"\b(?:error|sesi[oó]n|session|login|logue|usuario|password|contrase[nñ]a|"
                 r"credencial|autenticaci[oó]n|autorizaci[oó]n|permiso|acceso|vencid[ao]|expirad[ao])\b",
                 message, re.I):
        return False
    return bool(
        re.search(r"\bno\b.{0,105}\b(?:encontr\w*|existen?|hay|registr\w*)\b"
                  r".{0,110}\b(?:agendas?|profesionales?|resultados?|registros?)\b", message, re.I | re.S)
        or re.search(r"\bsin\s+(?:agendas?|profesionales?|resultados?|registros?)\b", message, re.I)
    )


def javascript_diagnostic(body):
    text=body.decode('utf-8',errors='replace')
    return {
        'bytes':len(body),
        'mentions_no_results':bool(re.search(r'(?:no\b.{0,100}(?:encontr|exist|hay)|sin\s+).{0,100}(?:agenda|profesional|resultado|registro)',text,re.I|re.S)),
        'mentions_session':bool(re.search(r'(?:sesi.n|session|login|logue)',text,re.I)),
        'verified_empty':explicit_empty_notice(body),
        'recognized_calls':[name for name in ['alert','mostrarMensaje','mostrarMensajeError','showMessage','showError','setFocus'] if re.search(r'\b'+name+r'\s*\(',text)]
    }


def normalized(value):
    return re.sub(r"\s+", " ", value).strip()


def mismatch_diagnostic(records, rendered):
    expected={r[0] for r in records}; actual={r[0] for r in rendered}
    def matching(transform):
        return len({transform(x) for x in expected}&{transform(x) for x in actual})
    tokens={}
    def shape(value):
        output=[]
        for token in re.findall(r'\d+|[^\W\d_]+|[^\w\s]',value):
            if token.isdigit():
                output.append('NUMBER')
            else:
                key=token.casefold()
                if key not in tokens:tokens[key]='T'+str(len(tokens)+1)
                output.append(tokens[key])
        return {'tokens':output,'length':len(value),'leading_digits':len(re.match(r'^\d*',value)[0])}
    return {'response_records':len(records),'rendered_records':len(rendered),
            'intersection':len(set(records)&set(rendered)),
            'response_doctors':len(expected),'rendered_doctors':len(actual),
            'matching_doctors':matching(lambda x:x),
            'matching_casefold':matching(str.casefold),
            'matching_without_spaces':matching(lambda x:re.sub(r'\s+','',x).casefold()),
            'matching_without_ordinal':matching(lambda x:re.sub(r'^\d+\s*','',x).casefold()),
            'response_centers':len({r[1] for r in records}),
            'rendered_centers':len({r[1] for r in rendered}),
            'matching_centers':len({r[1] for r in records}&{r[1] for r in rendered}),
            'response_shapes':[shape(x) for x in sorted(expected-actual)[:5]],
            'rendered_shapes':[shape(x) for x in sorted(actual-expected)[:5]]}


def records_from_rows(rows):
    if len(rows) > MAX_ROWS:
        raise CatalogError("row_limit")
    records = []
    for row in rows:
        if len(row) < 6:
            raise CatalogError("invalid_result_row")
        doctor = normalized(re.sub(r"^\d+\s+", "", row[0].strip()))
        parts = [normalized(p) for p in row[1].splitlines() if normalized(p)]
        if not doctor or not parts:
            raise CatalogError("invalid_result_row")
        if any("OSEP HUNUC HUAR" in p.upper() for p in parts):
            center = "OSEP HUNUC HUAR"
        elif parts[0].upper().startswith(doctor.upper()):
            center = parts[1] if len(parts) > 1 else ""
        else:
            center = parts[0]
        if not center or len(doctor) > 140 or len(center) > 140:
            raise CatalogError("record_limit_or_missing_center")
        records.append((doctor, center))
    return sorted(set(records))


class CatalogHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.rows = []
        self.row = None
        self.cell = None
        self.headers = []
        self.in_header = False
        self.found = False
        self.option = None
        self.options = []
        self.hidden = 0
        self.ignored = []
        self.total_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        void = tag in {'br','img','input','hr','meta','link','wbr','source','area','col','embed','param'}
        if self.ignored:
            if not void:
                self.ignored.append(tag)
            return
        if tag in {'script','style'} or 'ocultar' in a.get('class','').split() or 'hidden' in a or re.search(r'display\s*:\s*none',a.get('style',''),re.I):
            if not void:
                self.ignored.append(tag)
            return
        if tag == "option":
            self.option = [a.get("value", ""), "", "disabled" in a]
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif a.get("id") == "tblResultadoProfesionales":
                self.depth = 1
                self.found = True
        if self.depth != 1:
            return
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "tr":
            self.row = []
        if tag == "th":
            self.in_header = True
        if tag == "td":
            self.cell = ""
        if tag == "br" and self.cell is not None:
            self.cell += "\n"
        if tag in {"div","p","li","section"} and self.cell is not None:
            self.cell += "\n"

    def handle_endtag(self, tag):
        if self.ignored:
            if tag in self.ignored:
                while self.ignored.pop() != tag:
                    pass
            return
        if tag == "option" and self.option is not None:
            value, label, disabled = self.option
            label = normalized(label)
            if value and not disabled and label and not re.match(r"seleccion(?:e|ar)", label, re.I):
                self.options.append((value, label))
            self.option = None
        if tag == "table" and self.depth:
            self.depth -= 1
            return
        if self.depth != 1:
            return
        if tag in {"div","p","li","section"} and self.cell is not None:
            self.cell += "\n"
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "th":
            self.in_header = False
        if tag == "td" and self.cell is not None:
            if self.row is not None:
                self.row.append(self.cell.strip())
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.ignored:
            return
        if self.option is not None:
            self.option[1] += data
        if self.depth != 1 or self.hidden:
            return
        if self.cell is not None:
            # Los saltos de la fuente HTML colapsan a espacios en el navegador.
            # Sólo br y elementos de bloque generan líneas para elegir centro.
            self.cell += re.sub(r"\s+", " ", data)
        if self.in_header:
            self.headers.append(data)
        self.total_text += " " + data


def parse_results(html):
    if len(html.encode("utf-8")) > MAX_BODY:
        raise CatalogError("body_limit")
    parsed = CatalogHTML()
    parsed.feed(html)
    headers = normalized(" ".join(parsed.headers)).lower()
    if not parsed.found or "profesional" not in headers or "domicilio" not in headers:
        raise CatalogError("missing_result_table")
    total = re.search(r"\bde\s+(\d+)\s+agendas\b", normalized(parsed.total_text), re.I)
    data_rows = []
    for row in parsed.rows:
        if len(row) == 1 and re.search(r"(?:sin|no\b.*)\s*(?:registros|resultados|profesionales|agendas)", row[0], re.I):
            continue
        data_rows.append(row)
    if total and int(total[1]) != len(data_rows):
        raise CatalogError("incomplete_result_table")
    if not data_rows and not (total and int(total[1]) == 0 or parsed.rows):
        raise CatalogError("unverified_empty_result")
    return records_from_rows(data_rows)


def parse_options(html):
    parsed = CatalogHTML()
    parsed.feed(html)
    if not parsed.options or len(parsed.options) > 50 or len(set(v for v, _ in parsed.options)) != len(parsed.options):
        raise CatalogError("invalid_department_options")
    return parsed.options


def request_fields(raw):
    fields = parse_qsl(raw, keep_blank_values=True)
    if len(fields) != len(set(k for k, _ in fields)):
        raise CatalogError("duplicate_form_fields")
    values = dict(fields)
    required = {"servimod", "id_zona", "id_dpto", "id_tmovafi", "buscar"}
    if not required.issubset(values) or not values["id_tmovafi"] or chr(36) + "{" in values["id_tmovafi"]:
        raise CatalogError("invalid_search_form")
    for key in ("id_profe_turn", "matrprof_turn", "coditipo_turn", "datosProfesional_turn", "profesionalBusquedaComodin_turn"):
        if values.get(key, "").strip():
            raise CatalogError("filtered_search_form")
    return values


class SessionHTTP:
    def __init__(self, page, timeout=30000, pause=0.75, sleep=asyncio.sleep):
        self.page = page
        self.timeout = min(timeout, 30000)
        self.pause = pause
        self.sleep = sleep
        self.last = 0
        self.calls = 0
        self.retries = 0
        self.search_url = None
        self.department_url = None
        self.template = None

    def remember_search(self, request):
        if urlsplit(request.url).path != SEARCH_PATH or request.method != "POST":
            raise CatalogError("unexpected_search_endpoint")
        self.template = request_fields(request.post_data or "")
        self.search_url = request.url

    async def read(self, url, method="GET", fields=None):
        p = urlsplit(url)
        origin = urlsplit(self.page.url)
        if p.scheme != "https" or p.netloc != origin.netloc or p.path not in {SEARCH_PATH, DEPARTMENT_PATH}:
            raise CatalogError("endpoint_not_allowed")
        for attempt in range(3):
            await self.sleep(max(0, self.pause - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            self.calls += 1
            response = None
            try:
                kwargs = {"timeout": self.timeout, "max_redirects": 0,
                          "headers": {"Referer": self.page.url, "X-Requested-With": "XMLHttpRequest"}}
                if fields is not None:
                    kwargs["data"] = urlencode(fields)
                    kwargs["headers"]["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                response = await self.page.request.fetch(url, method=method, **kwargs)
                if response.status == 429 or 500 <= response.status <= 599:
                    if attempt == 2:
                        raise CatalogError("http_retry_exhausted")
                    retry = response.headers.get("retry-after", "")
                    if retry.isdigit() and int(retry) > 60:
                        raise CatalogError("rate_limit_stop")
                    self.retries += 1
                    await self.sleep(max(int(retry) if retry.isdigit() else 0, 2 ** (attempt + 1)))
                    continue
                return await checked_response(response)
            except CatalogError:
                raise
            except Exception:
                if attempt == 2:
                    raise CatalogError("http_timeout_or_network") from None
                self.retries += 1
                await self.sleep(2 ** (attempt + 1))
            finally:
                if response is not None:
                    await response.dispose()
        raise CatalogError("http_retry_exhausted")

    async def search(self, service, zone, department):
        if self.template is None:
            raise CatalogError("missing_observed_request")
        fields = {**self.template, "servimod": service, "id_zona": zone, "id_dpto": department}
        try:
            return parse_results(await self.read(self.search_url, "POST", fields))
        except CatalogNoResults:
            return []

    async def departments(self, zone):
        if self.department_url is None:
            raise CatalogError("missing_observed_request")
        p = urlsplit(self.department_url)
        query = dict(parse_qsl(p.query, keep_blank_values=True))
        query["id_zona"] = zone
        if "random" in query:
            query["random"] = str(time.time_ns())
        return parse_options(await self.read(urlunsplit(p._replace(query=urlencode(query)))))


async def checked_response(response, browser=False):
    if response.status != 200:
        raise CatalogError("http_" + str(response.status))
    mime = response.headers.get("content-type", "").split(";")[0].lower().strip()
    if mime != "text/html":
        diagnostic=None
        if mime in {'text/javascript','application/javascript'}:
            body=await response.body()
            if explicit_empty_notice(body):
                raise CatalogNoResults("explicit_no_agendas") from None
            if len(body)<=8192:
                diagnostic=javascript_diagnostic(body)
        raise CatalogError("unexpected_content_type",diagnostic)
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        raise CatalogError("body_limit")
    body = await response.body()
    if len(body) > MAX_BODY:
        raise CatalogError("body_limit")
    charset = re.search(r"charset=([\w-]+)", response.headers.get("content-type", ""), re.I)
    # CDP puede recodificar el texto de BrowserResponse a UTF-8. Algunas rutas
    # conservan bytes originales; si no son UTF-8, usar su charset declarado.
    # APIResponse siempre se interpreta con el charset del transporte.
    declared=charset[1] if charset else 'utf-8'
    encodings=['utf-8',declared] if browser else [declared]
    for encoding in dict.fromkeys(encodings):
        try:
            return body.decode(encoding)
        except (UnicodeError,LookupError):
            pass
    raise CatalogError('invalid_response_encoding') from None


