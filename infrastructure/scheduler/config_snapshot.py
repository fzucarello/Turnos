"""Valida una configuración privada en memoria; nunca escribe archivos de Actions."""
import datetime
import hashlib
import json
import os
import re
import sys

BOT_KEYS = {
    "OBJ_SERVICIO", "OBJ_ZONA", "OBJ_DEPTO", "OBJ_MEDICO", "OBJ_PROFESIONAL",
    "OBJ_DOMICILIO", "OBJ_HORARIO_TURNO", "OBJ_DIAS_VALIDOS", "OBJ_FECHA_DISP",
    "OBJ_HORA_MIN", "OBJ_HORA_MAX", "OBJ_HORA_PRIORIDAD", "OBJ_HORA_FLEXIBLE",
    "OBJ_DIA_FLEXIBLE", "OBJ_FECHA_FLEXIBLE", "PORTAL_URL", "HEADLESS",
    "TIMEOUT_MS", "STOP_AFTER_LOGIN", "DRY_RUN",
}
OPTIONAL = {"OBJ_MEDICO", "OBJ_PROFESIONAL", "OBJ_DOMICILIO", "OBJ_HORARIO_TURNO", "OBJ_DIAS_VALIDOS", "OBJ_FECHA_DISP"}
BOOLEANS = {"OBJ_HORA_FLEXIBLE", "OBJ_DIA_FLEXIBLE", "OBJ_FECHA_FLEXIBLE", "HEADLESS", "STOP_AFTER_LOGIN", "DRY_RUN"}


def minute_of_day(value, key):
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError(f"config_invalid_{key}")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(f"config_invalid_{key}")
    return hour * 60 + minute


def validate_snapshot(raw, expected_hash, force_dry_run):
    if not raw or len(raw.encode("utf-8")) > 10000:
        raise ValueError("Falta configuración válida de Cloudflare. Usar MODE=validate allí para probar.")
    actual_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or actual_hash != expected_hash:
        raise ValueError("La huella de configuración no coincide con el envío de Cloudflare.")
    config = json.loads(raw)
    if not isinstance(config, dict) or set(config) != BOT_KEYS:
        raise ValueError("La configuración debe contener únicamente los parámetros autorizados del bot.")
    for key, value in config.items():
        if not isinstance(value, str) or len(value) > 250 or re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError(f"config_invalid_{key}")
        if not value.strip() and key not in OPTIONAL:
            raise ValueError(f"config_invalid_{key}")
    for key in BOOLEANS:
        if config[key] not in {"true", "false"}:
            raise ValueError(f"config_invalid_{key}")
    if config["OBJ_HORA_PRIORIDAD"] not in {"EARLIEST", "LATEST"}:
        raise ValueError("config_invalid_OBJ_HORA_PRIORIDAD")
    min_hour = 0 if config["OBJ_HORA_MIN"] == "false" else minute_of_day(config["OBJ_HORA_MIN"], "OBJ_HORA_MIN")
    max_hour = 1439 if config["OBJ_HORA_MAX"] == "false" else minute_of_day(config["OBJ_HORA_MAX"], "OBJ_HORA_MAX")
    if min_hour > max_hour:
        raise ValueError("config_invalid_OBJ_HORA_RANGE")
    if config["OBJ_FECHA_DISP"] and config["OBJ_FECHA_DISP"] != "false":
        date = config["OBJ_FECHA_DISP"]
        if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", date):
            raise ValueError("config_invalid_OBJ_FECHA_DISP")
        datetime.datetime.strptime(date, "%d-%m-%Y")
    days = {"LUNES", "MARTES", "MIERCOLES", "MIÉRCOLES", "JUEVES", "VIERNES", "SABADO", "SÁBADO", "DOMINGO"}
    if config["OBJ_DIAS_VALIDOS"] and any(d.strip().upper() not in days for d in config["OBJ_DIAS_VALIDOS"].split(",")):
        raise ValueError("config_invalid_OBJ_DIAS_VALIDOS")
    if config["PORTAL_URL"] != "https://www.osep.mendoza.gov.ar/webapp_pri":
        raise ValueError("config_invalid_PORTAL_URL")
    if not re.fullmatch(r"\d+", config["TIMEOUT_MS"]) or not 1000 <= int(config["TIMEOUT_MS"]) <= 120000:
        raise ValueError("config_invalid_TIMEOUT_MS")
    if force_dry_run not in {"true", "false"}:
        raise ValueError("config_invalid_FORCE_DRY_RUN")
    if force_dry_run == "true":
        config["DRY_RUN"] = "true"
    return config


