import asyncio
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlencode, parse_qs

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from catalog_http import (CatalogError, SessionHTTP, SEARCH_PATH, checked_response,
                          parse_results, parse_options, request_fields, javascript_diagnostic)

def html(rows=None, total=None):
    rows = rows if rows is not None else [
        ["1 PROFESIONAL FICTICIO", "CENTRO FICTICIO<br>DOMICILIO FICTICIO", "SERVICIO", "HORARIO", "DISP", "AGENDA"]]
    if total is None:
        total = len(rows)
    return ('<table id="tblResultadoProfesionales"><thead><tr><th>Profesional</th>'
            '<th>Domicilio de atención</th><th>Servicio</th><th>Horario</th><th>Disp.</th><th>Agenda</th></tr>'
            '<tr><th>Mostrando 1-1 de '+str(total)+' agendas</th></tr></thead><tbody>' +
            "".join("<tr>"+"".join("<td>"+c+"</td>" for c in r)+"</tr>" for r in rows)+
            "</tbody></table>")

def response(body=None, status=200, mime="text/html", headers=None):
    return types.SimpleNamespace(status=status, headers={"content-type":mime, **(headers or {})},
                                 body=AsyncMock(return_value=(html() if body is None else body).encode()),
                                 dispose=AsyncMock())

def page(responses):
    fetch = AsyncMock(side_effect=responses)
    return types.SimpleNamespace(url="https://portal.example/webapp_pri/turnos",
                                 request=types.SimpleNamespace(fetch=fetch))

class ParserTests(unittest.TestCase):
    def test_javascript_diagnostic_keeps_only_flags_and_known_function_names(self):
        data=javascript_diagnostic(b"alert('No se encontraron agendas PRIVATE_FAKE_SECRET')")
        self.assertTrue(data['mentions_no_results'])
        self.assertEqual(data['recognized_calls'],['alert'])
        self.assertNotIn('PRIVATE_FAKE_SECRET',str(data))

    def test_hidden_duplicate_name_and_block_center(self):
        row=['1 PROFESIONAL FICTICIO','<div>PROFESIONAL FICTICIO</div><div class="ocultar">PROFESIONAL FICTICIO</div><div>CENTRO FICTICIO<br>DOMICILIO</div>','S','H','D','A']
        self.assertEqual(parse_results(html([row])),[('PROFESIONAL FICTICIO','CENTRO FICTICIO')])

    def test_complete_html_duplicates_and_pagination(self):
        row=["1 PROFESIONAL FICTICIO","CENTRO FICTICIO<br>DOMICILIO","S","H","D","A"]
        self.assertEqual(parse_results(html([row,row])),[("PROFESIONAL FICTICIO","CENTRO FICTICIO")])

    def test_no_table_or_login_is_not_empty(self):
        for body in ["", "<form><input type=password></form>", "<script>alert('error')</script>"]:
            with self.assertRaises(CatalogError):
                parse_results(body)

    def test_verified_empty_and_unverified_empty(self):
        self.assertEqual(parse_results(html([])),[])
        with self.assertRaises(CatalogError):
            parse_results('<table id="tblResultadoProfesionales"><th>Profesional Domicilio</th></table>')

    def test_truncation_incomplete_rows_and_label_limit(self):
        for body in [html(total=2), html([["ERROR"]]), html([["X"*141,"CENTRO","S","H","D","A"]]),
                     html([["P","C","S","H","D","A"]]*501)]:
            with self.assertRaises(CatalogError):
                parse_results(body)

    def test_options_exclude_placeholder_and_reject_duplicate_or_limit(self):
        self.assertEqual(parse_options('<select><option value="">Seleccionar</option><option value=1>UNO</option></select>'),[("1","UNO")])
        for body in ["<html>Login</html>", '<option value=1>UNO</option><option value=1>DOS</option>',
                     "".join('<option value="'+str(n)+'">N</option>' for n in range(51))]:
            with self.assertRaises(CatalogError):
                parse_options(body)

    def test_template_keeps_context_and_rejects_scoped_professional(self):
        base={"servimod":"s","id_zona":"z","id_dpto":"d","id_tmovafi":"PRIVATE_FAKE","buscar":"Buscar"}
        self.assertEqual(request_fields(urlencode(base))["id_tmovafi"],"PRIVATE_FAKE")
        for values in [{**base,"id_tmovafi":""},{**base,"id_profe_turn":"FILTER"}]:
            with self.assertRaises(CatalogError):
                request_fields(urlencode(values))
        with self.assertRaises(CatalogError):
            request_fields(urlencode(base)+"&servimod=other")

class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_200_javascript_never_reads_stale_table(self):
        r=response("alert('error')",mime="text/javascript")
        with self.assertRaisesRegex(CatalogError,"unexpected_content_type"):
            await checked_response(r)
        r.body.assert_awaited_once()

    async def test_expired_session_redirect_and_wrong_status(self):
        for r in [response(status=302),response(status=401),response("<form>Login</form>")]:
            with self.assertRaises(CatalogError):
                parse_results(await checked_response(r))

    async def test_sequential_retry_and_retry_after_limit(self):
        a,b=response(status=429,headers={"retry-after":"2"}),response()
        p=page([a,b]); sleep=AsyncMock()
        client=SessionHTTP(p,sleep=sleep)
        self.assertIn("table",await client.read("https://portal.example"+SEARCH_PATH))
        self.assertEqual(client.retries,1)
        a.dispose.assert_awaited_once()
        b.dispose.assert_awaited_once()
        p2=page([response(status=429,headers={"retry-after":"120"})])
        with self.assertRaisesRegex(CatalogError,"rate_limit_stop"):
            await SessionHTTP(p2,sleep=sleep).read("https://portal.example"+SEARCH_PATH)
        self.assertEqual(p2.request.fetch.await_count,1)

    async def test_server_errors_and_timeouts_stop_after_three(self):
        for values in [[response(status=503) for _ in range(3)], [TimeoutError() for _ in range(3)]]:
            p=page(values)
            with self.assertRaises(CatalogError):
                await SessionHTTP(p,sleep=AsyncMock()).read("https://portal.example"+SEARCH_PATH)
            self.assertEqual(p.request.fetch.await_count,3)

    async def test_private_context_and_exact_fields_never_change_patient(self):
        p=page([response()])
        c=SessionHTTP(p,sleep=AsyncMock())
        c.remember_search(types.SimpleNamespace(method="POST",url="https://portal.example"+SEARCH_PATH+"?random=x",
                          post_data=urlencode({"servimod":"s","id_zona":"z","id_dpto":"d",
                                              "id_tmovafi":"PRIVATE_FAKE","buscar":"Buscar"})))
        self.assertEqual(await c.search("s2","z2","d2"),[("PROFESIONAL FICTICIO","CENTRO FICTICIO")])
        args=p.request.fetch.await_args.kwargs
        self.assertEqual(parse_qs(args["data"])["id_tmovafi"],["PRIVATE_FAKE"])
        self.assertEqual(parse_qs(args["data"])["id_dpto"],["d2"])
        self.assertEqual(args["max_redirects"],0)
        self.assertNotIn("ignore_https_errors",args)

    async def test_endpoint_allowlist_prevents_agendas_or_reservation(self):
        p=page([])
        for url in ["http://portal.example"+SEARCH_PATH,"https://other.example"+SEARCH_PATH,
                    "https://portal.example/webapp_pri/reservar"]:
            with self.assertRaisesRegex(CatalogError,"endpoint_not_allowed"):
                await SessionHTTP(p,sleep=AsyncMock()).read(url)
        p.request.fetch.assert_not_called()

    async def test_response_size_and_late_body(self):
        with self.assertRaisesRegex(CatalogError,"body_limit"):
            await checked_response(response(headers={"content-length":"1500001"}))
        r=response()
        async def delayed():
            await asyncio.sleep(0.01)
            return html().encode()
        r.body.side_effect=delayed
        self.assertEqual(parse_results(await checked_response(r)),[("PROFESIONAL FICTICIO","CENTRO FICTICIO")])

