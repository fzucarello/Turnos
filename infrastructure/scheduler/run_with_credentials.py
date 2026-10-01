"""Ejecuta el bot con configuración privada en memoria y devuelve su detalle a Cloudflare."""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import urllib.error
from config_snapshot import validate_snapshot

CREDENTIALS_URL = "https://osep-scheduler.franchezuca.workers.dev/credentials"
RESULT_URL = "https://osep-scheduler.franchezuca.workers.dev/result"
LOG_LIMIT = 32768
SAFE_ERRORS = {"unauthorized_environment", "invalid_identity_url", "invalid_identity_response", "invalid_credentials_response", "invalid_request_id", "invalid_private_configuration", "identity_request_failed", "credentials_request_failed", "result_request_failed"}


def stage_request(request, stage, url, token, data=None):
    try:
        return request(url, token, data)
    except urllib.error.HTTPError as error:
        raise ValueError(f"{stage}_http_{int(error.code)}") from None
    except Exception:
        raise ValueError(f"{stage}_request_failed") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, token, data=None):
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "OSEP-GitHub-Actions"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(32769)
        if len(raw) > 32768:
            raise ValueError("response_too_large")
        return json.loads(raw)


def obtain_identity(environ, request=request_json):
    if environ.get("GITHUB_ACTIONS") != "true" or environ.get("GITHUB_REPOSITORY") != "fzucarello/Turnos" or environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("unauthorized_environment")
    if not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}", environ.get("REQUEST_ID", "")):
        raise ValueError("invalid_request_id")
    url = urllib.parse.urlsplit(environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
    if url.scheme != "https" or not (url.hostname or "").endswith(".actions.githubusercontent.com") or url.username or url.password or url.fragment:
        raise ValueError("invalid_identity_url")
    query = [(key, value) for key, value in urllib.parse.parse_qsl(url.query, keep_blank_values=True) if key != "audience"]
    query.append(("audience", CREDENTIALS_URL))
    identity = stage_request(request, "identity", urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query))), environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"])
    token = identity.get("value") if isinstance(identity, dict) else None
    if not isinstance(token, str) or not 1 <= len(token) <= 8192:
        raise ValueError("invalid_identity_response")
    return token


def obtain_credentials(environ, request=request_json):
    token = obtain_identity(environ, request)
    runtime = stage_request(request, "credentials", CREDENTIALS_URL, token, json.dumps({"request_id": environ["REQUEST_ID"]}).encode())
    if not isinstance(runtime, dict) or set(runtime) != {"credentials", "config_json", "config_sha256"}:
        raise ValueError("invalid_credentials_response")
    credentials = runtime["credentials"]
    if not isinstance(credentials, dict) or set(credentials) != {"OSEP_USER", "OSEP_PASS"} or any(not isinstance(v, str) or not 1 <= len(v) <= 4096 or "\0" in v for v in credentials.values()):
        raise ValueError("invalid_credentials_response")
    try:
        config = validate_snapshot(runtime["config_json"], runtime["config_sha256"], environ.get("FORCE_DRY_RUN", "true"))
    except Exception:
        raise ValueError("invalid_private_configuration") from None
    return credentials, config, token


def mask(value):
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::add-mask::" + escaped, flush=True)


def private_child_environment(environ, config, credentials):
    child = dict(environ)
    child.update(config)
    child.update(credentials)
    for key in list(child):
        if key.startswith("ACTIONS_") or key in {"GITHUB_TOKEN", "GITHUB_ENV", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STATE"}:
            child.pop(key)
    return child


def run_private_bot(child_env):
    # Nunca heredar stdout/stderr: capturar también excepciones y comandos de Actions.
    with subprocess.Popen([sys.executable, "-u", "app.py"], env=child_env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=os.name == "posix") as process:
        timed_out = threading.Event()
        def stop():
            timed_out.set()
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        timer = threading.Timer(420, stop)
        timer.start()
        tail = b""
        total = 0
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                total += len(chunk)
                tail = (tail + chunk)[-LOG_LIMIT:]
            code = process.wait()
        finally:
            timer.cancel()
        return (6 if timed_out.is_set() else code), tail.decode("utf-8", errors="replace"), total > LOG_LIMIT


def redact(log, values):
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            for variant in {value, urllib.parse.quote(value, safe=""), json.dumps(value, ensure_ascii=False)[1:-1]}:
                log = log.replace(variant, "[REDACTED]")
    return log


def save_result(environ, code, log, truncated, config, values, request=request_json):
    status = {0: "prueba_finalizada" if config["DRY_RUN"] == "true" else "sin_turno", 1: "turno_informado", 3: "error_bot", 5: "error_inicio", 6: "timeout"}[code]
    token = obtain_identity(environ, request)  # Identidad nueva tras la búsqueda.
    mask(token)
    log = redact(log, [*values, token, environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")])
    encoded = log.encode("utf-8")
    truncated = truncated or len(encoded) > LOG_LIMIT
    log = encoded[-LOG_LIMIT:].decode("utf-8", errors="ignore")
    payload = json.dumps({"request_id": environ["REQUEST_ID"], "exit_code": code, "status": status, "log": log, "log_truncated": truncated}, ensure_ascii=False).encode("utf-8")
    if stage_request(request, "result", RESULT_URL, token, payload) != {"ok": True}:
        raise ValueError("result_request_failed")


def safe_code(error):
    code = str(error) if isinstance(error, ValueError) else "private_runtime_failed"
    return code if code in SAFE_ERRORS or re.fullmatch(r"(?:identity|credentials|result)_http_[1-5][0-9]{2}", code) else "private_runtime_failed"


def main(check_only=False):
    try:
        credentials, config, token = obtain_credentials(os.environ)
    except Exception as error:
        print(f"::error::No se pudo validar el acceso privado de Cloudflare ({safe_code(error)}). El bot no se inició.", flush=True)
        return 4
    for value in [token, *credentials.values()]:
        mask(value)
    if check_only:
        print("Configuración y credenciales privadas verificadas; sin iniciar el bot ni persistir datos.", flush=True)
        return 0
    print("Bot iniciado. El detalle de esta ejecución se conserva únicamente en Cloudflare.", flush=True)
    try:
        code, log, truncated = run_private_bot(private_child_environment(os.environ, config, credentials))
        code = code if code in {0,1,3,6} else 3
    except Exception:
        code, log, truncated = 5, "No se pudo iniciar o supervisar el proceso del bot.", False
    try:
        save_result(os.environ, code, log, truncated, config, [token, *credentials.values()])
    except Exception as error:
        print(f"::error::No se pudo guardar el detalle privado ({safe_code(error)}). Revisar OSEP antes de repetir una reserva.", flush=True)
        return 4
    print("Ejecución finalizada. Consultar el resultado y diagnóstico privados en Cloudflare.", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("## Ejecución OSEP\n\nDetalle privado guardado en Cloudflare. No se publica configuración ni confirmación.\n")
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    sys.exit(main(parser.parse_args().check_only))
