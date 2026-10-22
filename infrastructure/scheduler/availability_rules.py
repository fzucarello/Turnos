"""Reglas estrictas y reutilizables de disponibilidad del turno.

Las exclusiones y la fecha mínima nunca se flexibilizan. Este módulo no
usa Playwright, red ni archivos y se puede probar con unittest.
"""
from datetime import date
import json
import re


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Horario inválido")
    hh, mm = map(int, value.split(":"))
    return hh * 60 + mm


def minimum_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Fecha mínima inválida")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Fecha mínima inválida") from exc


def weekly_rules(week_json, exclusions_json, global_start, global_end):
    """Retorna siete reglas (None=día no disponible o tupla de horas y exclusiones)."""
    lo, hi = minute(global_start), minute(global_end)
    if lo > hi:
        raise ValueError("Rango horario general inválido")

    if week_json:
        try:
            raw_week = json.loads(week_json)
        except (ValueError, TypeError) as exc:
            raise ValueError("Semana inválida") from exc
        if not isinstance(raw_week, list) or len(raw_week) != 7:
            raise ValueError("Semana inválida")
    else:
        raw_week = [[global_start, global_end] for _ in range(7)]

    if exclusions_json:
        try:
            raw_exclusions = json.loads(exclusions_json)
        except (ValueError, TypeError) as exc:
            raise ValueError("Exclusiones inválidas") from exc
        if not isinstance(raw_exclusions, list) or len(raw_exclusions) != 7:
            raise ValueError("Exclusiones inválidas")
    else:
        raw_exclusions = [[] for _ in range(7)]

    rules = []
    for weekday, window in enumerate(raw_week):
        if window is None:
            if raw_exclusions[weekday]:
                raise ValueError("No se admiten exclusiones en un día desactivado")
            rules.append(None)
            continue
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("Horario semanal inválido")
        start, end = minute(window[0]), minute(window[1])
        if start > end:
            raise ValueError("Horario semanal invertido")
        exclusions = raw_exclusions[weekday]
        if not isinstance(exclusions, list) or len(exclusions) > 8:
            raise ValueError("Demasiadas exclusiones")
        intervals = []
        for interval in exclusions:
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError("Exclusión inválida")
            a, b = minute(interval[0]), minute(interval[1])
            if a >= b or a < start or b > end:
                raise ValueError("La exclusión debe estar dentro del horario del día")
            intervals.append((a, b))
        intervals.sort()
        if any(a < previous_b for (_, previous_b), (a, _) in zip(intervals, intervals[1:])):
            raise ValueError("Las exclusiones se superponen")
        rules.append((start, end, intervals))
    return rules


def can_attend(weekday, when_minute, rules):
    """weekday: 0 lunes a 6 domingo. El extremo final excluido sí es elegible."""
    if not isinstance(weekday, int) or weekday < 0 or weekday > 6:
        return False
    selected = rules[weekday]
    if selected is None or when_minute is None:
        return False
    start, end, blocks = selected
    if not start <= when_minute <= end:
        return False
    return not any(a <= when_minute < b for a, b in blocks)
