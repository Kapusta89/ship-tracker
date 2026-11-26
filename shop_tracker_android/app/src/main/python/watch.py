"""Сторож за конкретным судном: свои правила поверх общего фильтра алертов.

Общий фильтр (`filter_origins` → `filter_destinations`) ищет танкеры по
маршруту «наш порт → наш порт». Этого мало, когда нужно следить за
конкретным теплоходом: он может быть сухогрузом, идти из чужого порта или
вообще не иметь нашего порта в назначении. Поэтому у судна есть своё
правило (`watch_rules` в конфиге):

  * сообщить, когда судно войдёт в N км от нашего порта (любого или
    выбранного) — с запасом по гистерезису, чтобы на границе радиуса не
    было двух сообщений подряд;
  * сообщить, когда сменится Destination (в том числе когда его снимут);
  * сообщить, когда судно пропадёт из AIS дольше N часов (и снова — только
    после того, как связь вернулась и пропала заново).

Состояние (были ли уже внутри радиуса, что было в Destination, о какой
потере связи уже сообщили) хранится в data/watch_state.json: после
перезапуска окна тот же случай не присылается заново.

Модуль ничего не знает про Telegram и базу — только правило, судно и
состояние. Отправку делает core.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import geo

BASE_DIR = (Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
DATA_DIR = BASE_DIR / "data"
STATE_PATH = DATA_DIR / "watch_state.json"

# Значения по умолчанию для полей правила: их подставляет окно, когда
# пользователь только включает галочку.
DEFAULT_ENTER_KM = 50.0
DEFAULT_SILENT_HOURS = 6.0
# Пределы — от опечатки: «войти в 50000 км» или «пропасть на 1000 ч»
# иначе превратились бы в правило, которое срабатывает всегда или никогда.
MAX_ENTER_KM = 2000.0
MAX_SILENT_HOURS = 240.0
# Гистерезис радиуса: выходим из «внутри» только на 15 % дальше порога,
# иначе судно, стоящее ровно в 50 км, давало бы сообщение за сообщением.
HYSTERESIS = 1.15

KINDS = {
    "enter": "вход в радиус",
    "destination": "смена назначения",
    "silent": "потеря AIS",
}


def _number(value):
    try:
        result = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return result if result == result else None      # NaN


def _text(value, limit=60) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean_rule(raw, ports=None):
    """Проверенное правило из того, что пришло из конфига или из окна.

    Возвращает None, если правило пустое (ни одного условия): такой записи
    в списке быть не должно, иначе она зря проверяется на каждом сообщении.
    """
    if not isinstance(raw, dict):
        return None
    mmsi = "".join(ch for ch in str(raw.get("mmsi") or "") if ch.isdigit())
    if not (6 <= len(mmsi) <= 9):
        return None
    enter_km = _number(raw.get("enter_km")) or 0.0
    enter_km = max(0.0, min(MAX_ENTER_KM, enter_km))
    silent_hours = _number(raw.get("silent_hours")) or 0.0
    silent_hours = max(0.0, min(MAX_SILENT_HOURS, silent_hours))
    port = _text(raw.get("port"), 40).upper()
    if port:
        known = {str((item or {}).get("name") or "").upper() for item in ports or []}
        if known and port not in known:
            port = ""                     # порт убрали из настроек — следим за всеми
    watched_destination = bool(raw.get("destination"))
    if not (enter_km > 0 or silent_hours > 0 or watched_destination):
        return None
    return {
        "mmsi": mmsi,
        "name": _text(raw.get("name")),
        "port": port,
        "enter_km": round(enter_km, 1),
        "destination": watched_destination,
        "silent_hours": round(silent_hours, 1),
    }


def load_rules(config) -> list:
    """Все правила сторожа из конфига, без пустых и битых."""
    ports = (config or {}).get("ports") or []
    result = []
    seen = set()
    for raw in (config or {}).get("watch_rules") or []:
        rule = clean_rule(raw, ports)
        if not rule or rule["mmsi"] in seen:
            continue
        seen.add(rule["mmsi"])
        result.append(rule)
    return result


def rules_mmsis(rules) -> set:
    return {rule["mmsi"] for rule in rules or [] if rule.get("mmsi")}


def rule_for(rules, mmsi):
    target = str(mmsi or "").strip()
    if not target:
        return None
    for rule in rules or []:
        if rule.get("mmsi") == target:
            return rule
    return None


def rule_text(rule) -> str:
    """Правило одной строкой — для карточки и журнала."""
    rule = rule or {}
    parts = []
    if rule.get("enter_km"):
        port = rule.get("port") or "любого порта"
        parts.append(f"вход в {rule['enter_km']:g} км от {port}")
    if rule.get("destination"):
        parts.append("смена Destination")
    if rule.get("silent_hours"):
        parts.append(f"потеря AIS {rule['silent_hours']:g} ч")
    return ", ".join(parts) or "условий нет"


def load_state(path=None) -> dict:
    path = Path(path) if path else STATE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state, path=None) -> None:
    path = Path(path) if path else STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _age_hours(value, now=None):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value)[:19].replace("T", " "))
    except (TypeError, ValueError):
        return None
    now = now or datetime.now()
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    return max(0.0, (now - moment).total_seconds() / 3600.0)


# Родительный падеж для сообщения: «в 50 км от Измаила», а не «от Измаил».
# Ключи и русские подписи, и латинские имена: в настройках порты бывают
# записаны и так, и так (config.json хранит имя «IZMAIL»).
PORT_GENITIVE = {
    "ИЗМАИЛ": "Измаила", "IZMAIL": "Измаила",
    "ВИЛКОВО": "Вилково", "VYLKOVE": "Вилково",
    "ЗАТОКА": "Затоки", "ZATOKA": "Затоки",
    "ЧЕРНОМОРСК": "Черноморска", "CHORNOMORSK": "Черноморска",
    "ОДЕССА": "Одессы", "ODESSA": "Одессы",
    "ЮЖНЫЙ": "Южного", "YUZHNY": "Южного",
    "ОЧАКОВ": "Очакова", "OCHAKIV": "Очакова",
}


def _port_label(port, genitive=False) -> str:
    label = str((port or {}).get("label") or (port or {}).get("name") or "порт")
    if not genitive:
        return label
    for key in (label.upper(), str((port or {}).get("name") or "").upper()):
        if key in PORT_GENITIVE:
            return PORT_GENITIVE[key]
    return f"порта {label}"


def _targets(rule, ports) -> list:
    name = str((rule or {}).get("port") or "").upper()
    chosen = [port for port in ports or []
              if not name or str(port.get("name") or "").upper() == name]
    return chosen or list(ports or [])


def _nearest(lat, lon, ports):
    best = None
    for port in ports or []:
        try:
            km = geo.haversine_km(lat, lon, float(port["lat"]), float(port["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or km < best[1]:
            best = (port, km)
    return best


def check_position(rule, vessel, ports, state, now=None) -> list:
    """Проверяет правило по очередной позиции: [(вид, текст), …].

    `state` — общий словарь состояния; функция сама заводит и обновляет
    запись судна, но сохранять файл должен вызывающий (он решает, когда).
    """
    rule = rule or {}
    vessel = vessel or {}
    mmsi = str(rule.get("mmsi") or "")
    if not mmsi:
        return []
    entry = state.setdefault(mmsi, {})
    entry["checked"] = datetime.now().isoformat(timespec="seconds")
    alerts = []

    lat, lon = _number(vessel.get("lat")), _number(vessel.get("lon"))
    enter_km = _number(rule.get("enter_km")) or 0.0
    if enter_km > 0 and lat is not None and lon is not None:
        nearest = _nearest(lat, lon, _targets(rule, ports))
        if nearest is not None:
            port, km = nearest
            key = str(port.get("name") or "").upper() or "PORT"
            inside = entry.setdefault("inside", {})
            was_inside = bool(inside.get(key))
            if not was_inside and km <= enter_km:
                inside[key] = True
                alerts.append(("enter",
                               f"вошло в {enter_km:g} км от {_port_label(port, True)} "
                               f"(сейчас {km:.0f} км)"))
            elif was_inside and km > enter_km * HYSTERESIS:
                inside[key] = False

    if rule.get("destination"):
        current = _text(vessel.get("destination")).upper()
        previous = entry.get("destination")
        # первое наблюдение ничего не сообщает: это точка отсчёта, а не смена
        if previous is None:
            entry["destination"] = current
        elif current and current != previous:
            entry["destination"] = current
            if previous:
                alerts.append(("destination", f"сменило назначение: {previous} → {current}"))
            else:
                alerts.append(("destination", f"появилось назначение: {current}"))
        elif not current and previous:
            entry["destination"] = ""
            alerts.append(("destination", f"сняло назначение (было {previous})"))
    return alerts


def check_silence(rule, vessel, state, now=None) -> list:
    """Проверяет «пропасть дольше N часов»: одно сообщение на случай."""
    rule = rule or {}
    hours = _number(rule.get("silent_hours")) or 0.0
    mmsi = str(rule.get("mmsi") or "")
    if hours <= 0 or not mmsi or not vessel:
        return []
    age = _age_hours((vessel or {}).get("position_at") or (vessel or {}).get("updated_at"),
                     now)
    if age is None:
        return []
    entry = state.setdefault(mmsi, {})
    if age >= hours:
        if entry.get("silent"):
            return []
        entry["silent"] = True
        lat, lon = _number(vessel.get("lat")), _number(vessel.get("lon"))
        where = f"{lat:.2f}, {lon:.2f}" if lat is not None and lon is not None else "неизвестна"
        when = str(vessel.get("position_at") or "")[:16].replace("T", " ")
        text = f"нет сигнала AIS {age:.0f} ч (порог {hours:g} ч)"
        if when:
            text += f"; последняя позиция {where} от {when}"
        return [("silent", text)]
    # связь вернулась: следующий провал снова будет новостью
    if entry.get("silent") and age < hours / 2:
        entry["silent"] = False
    return []
