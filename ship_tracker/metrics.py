"""Расчёты по каждому судну — математика на тех данных, что реально есть.

Считается только то, что можно перепроверить по нашим наблюдениям, и каждая
строка прямо говорит, из чего она получена. Если данных не хватает (одна
координата, нет ширины корпуса, нет площади ватерлинии) — расчёт не делается,
и это пишется словами: отсутствующие размеры не восстанавливаются по MMSI,
типу AIS или скорости.

Что считается:

* расстояние между соседними наблюдениями по формуле гаверсинусов
  (R = 6371 км) и перевод в морские мили (1 мор. миля = 1.852 км);
* скорость на каждом интервале v = d / Δt и сравнение с переданной SOG;
* время хода T = D / V (на одном участке) и Σ Dᵢ / Vᵢ, если участков несколько;
* минимум, среднее и максимум скорости, отдельно периоды стоянки;
* разница курсов по круговой шкале (359° → 1° — это 2°, а не 358°);
* отношения корпуса L/B, L/T, B/T — геометрия, не вместимость;
* изменение осадки ΔT = T₂ − T₁, средняя осадка и дифферент, если даны
  осадки носом и кормой;
* длительность стоянки и её отношение к обычной: K = Tтек / Tобыч;
* продолжительность пропусков AIS, самый длинный и их доля от наблюдения;
* ETA = D / V с оговоркой, что задержки не учтены, и свежесть данных
  Δt = tпроверки − tпоследнего AIS.

Формулы расстояния — по прямой между точками, а не по судоходному маршруту,
поэтому фактический путь судна всегда длиннее расчётного.
"""

from datetime import datetime

import analysis
import geo

KM_PER_NM = 1.852
# Ниже этой скорости интервал считается стоянкой (узлы).
STOP_SPEED_KN = 0.5
# Пропуск AIS короче этого не считаем заметным (минуты).
GAP_MIN_MINUTES = 180
# Плотность морской воды по умолчанию, т/м³ (только для оценки массы).
SEA_WATER_DENSITY = 1.025


def nm(km) -> float:
    """Морские мили из километров: 1 мор. миля = 1.852 км."""
    return float(km) / KM_PER_NM


def num(value):
    """Число из значения любого вида; None — если числа нет."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt(value, digits=1) -> str:
    """Число для подписи: «6.2» / «—», без ложной точности."""
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _count(value, one, few, many) -> str:
    """Счёт с правильным падежом: «1 интервал», «2 интервала», «5 интервалов»."""
    number = abs(int(value))
    if number % 10 == 1 and number % 100 != 11:
        return f"{number} {one}"
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return f"{number} {few}"
    return f"{number} {many}"


def _moment(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def course_delta(first, second):
    """Разница курсов по круговой шкале в градусах 0…180.

    Переход 359° → 1° — это 2°, а не 358°: шкала курса замкнута на 360°.
    """
    one, two = num(first), num(second)
    if one is None or two is None:
        return None
    difference = abs((two - one) % 360.0)
    return min(difference, 360.0 - difference)


def distance_km(first, second) -> float:
    """Расстояние по прямой между двумя парами координат (гаверсинусы)."""
    return geo.haversine_km(first[0], first[1], second[0], second[1])


def travel_hours(distance_km, sog) -> float:
    """Время хода T = D / V; скорость в узлах переводится в км/ч."""
    speed = num(sog)
    if speed is None or speed <= 0 or distance_km is None:
        return None
    return float(distance_km) / (speed * KM_PER_NM)


def legs(rows, stop_kn=STOP_SPEED_KN) -> list:
    """Интервалы между соседними наблюдениями: расстояние, время, скорость, курс.

    Скорость на интервале — средняя за интервал (v = d / Δt), а не мгновенная.
    """
    points = []
    for row in rows or []:
        at = _moment(row.get("ts") if isinstance(row, dict) else row)
        lat = num(row.get("lat")) if isinstance(row, dict) else None
        lon = num(row.get("lon")) if isinstance(row, dict) else None
        if at is None or lat is None or lon is None:
            continue
        sog = num(row.get("sog"))
        cog = num(row.get("cog"))
        # неизвестные значения AIS: скорость −1, курс −1/361 — это не данные
        points.append({"at": at, "lat": lat, "lon": lon,
                       "sog": sog if sog is not None and sog >= 0 else None,
                       "cog": cog if cog is not None and 0 <= cog <= 360 else None})
    points.sort(key=lambda point: point["at"])

    result = []
    for before, after in zip(points, points[1:]):
        hours = (after["at"] - before["at"]).total_seconds() / 3600.0
        if hours <= 0:
            continue
        km = distance_km((before["lat"], before["lon"]), (after["lat"], after["lon"]))
        if km != km or km == float("inf"):  # NaN / бесконечность от битых координат
            continue
        speed = nm(km) / hours
        result.append({
            "from": before["at"], "to": after["at"], "hours": hours,
            "km": km, "nm": nm(km), "speed_kn": speed,
            "course_delta": course_delta(before["cog"], after["cog"]),
            "stopped": speed < stop_kn,
            "sog_from": before["sog"], "sog_to": after["sog"],
        })
    return result


def motion(rows) -> dict:
    """Движение по ряду наблюдений: путь, время, скорость, стоянки, курс."""
    moments = set()
    rows_with_time = 0
    for row in rows or []:
        at = _moment(row.get("ts") if isinstance(row, dict) else row)
        if at is not None:
            moments.add(at)
            rows_with_time += 1
    result = {"observations": rows_with_time, "moments": len(moments),
              "intervals": 0, "km": None, "nm": None,
              "hours": None, "speed_min": None, "speed_mean": None, "speed_max": None,
              "straight_km": None, "straight_nm": None, "course_delta": None,
              "stop_count": 0, "stop_hours": 0.0, "sog_mean": None, "sog_last": None,
              "moving_hours": 0.0}
    if len(moments) < 2:
        return result

    items = legs(rows)
    if not items:
        return result
    speeds = [item["speed_kn"] for item in items]
    turns = [item["course_delta"] for item in items if item["course_delta"] is not None]
    sog_values = [num(row.get("sog")) for row in rows or [] if isinstance(row, dict)]
    sog_values = [value for value in sog_values if value is not None]
    first_point, last_point = _first_point(rows), _last_point(rows)
    straight = (distance_km(first_point, last_point)
                if None not in (*first_point, *last_point) else None)

    result.update({
        "intervals": len(items),
        "km": sum(item["km"] for item in items),
        "nm": sum(item["nm"] for item in items),
        "hours": sum(item["hours"] for item in items),
        "speed_min": min(speeds),
        "speed_mean": sum(speeds) / len(speeds),
        "speed_max": max(speeds),
        "course_delta": max(turns) if turns else None,
        "stop_count": sum(1 for item in items if item["stopped"]),
        "stop_hours": sum(item["hours"] for item in items if item["stopped"]),
        "sog_mean": (sum(sog_values) / len(sog_values)) if sog_values else None,
        "sog_last": sog_values[-1] if sog_values else None,
        "straight_km": straight,
        "straight_nm": nm(straight),
        "moving_hours": sum(item["hours"] for item in items if not item["stopped"]),
    })
    return result


def _sorted_points(rows) -> list:
    points = []
    for row in rows or []:
        at = _moment(row.get("ts") if isinstance(row, dict) else row)
        lat = num(row.get("lat")) if isinstance(row, dict) else None
        lon = num(row.get("lon")) if isinstance(row, dict) else None
        if at is None or lat is None or lon is None:
            continue
        points.append((at, lat, lon))
    points.sort(key=lambda point: point[0])
    return points


def _first_point(rows):
    points = _sorted_points(rows)
    return (points[0][1], points[0][2]) if points else (None, None)


def _last_point(rows):
    points = _sorted_points(rows)
    return (points[-1][1], points[-1][2]) if points else (None, None)


def hull(length, beam, draft) -> dict:
    """Отношения корпуса L/B, L/T, B/T — только из известных размеров."""
    length, beam, draft = num(length), num(beam), num(draft)
    result = {"length": length, "beam": beam, "draft": draft,
              "length_beam": None, "length_draft": None, "beam_draft": None}
    if length and beam:
        result["length_beam"] = length / beam
    if length and draft:
        result["length_draft"] = length / draft
    if beam and draft:
        result["beam_draft"] = beam / draft
    return result


def mean_draft(fore, aft):
    """Средняя осадка Tср = (Tнос + Tкорма) / 2 — если даны обе."""
    fore, aft = num(fore), num(aft)
    if fore is None or aft is None:
        return None
    return (fore + aft) / 2.0


def trim(fore, aft):
    """Дифферент по измерениям: ΔT = Tкорма − Tнос (плюс — на корму)."""
    fore, aft = num(fore), num(aft)
    if fore is None or aft is None:
        return None
    return aft - fore


def draft_change(first, second):
    """Изменение осадки ΔT = T₂ − T₁ в метрах."""
    first, second = num(first), num(second)
    if first is None or second is None:
        return None
    return second - first


def displacement_change(delta_draft_m, waterplane_m2, density=SEA_WATER_DENSITY):
    """Оценка изменения водоизмещения ΔM ≈ ρ·A·ΔT (тонны).

    Без площади ватерлинии A и плотности ρ массы не считаются: по одной осадке
    вместимость не восстанавливается.
    """
    delta_draft_m, waterplane_m2 = num(delta_draft_m), num(waterplane_m2)
    if delta_draft_m is None or not waterplane_m2:
        return None
    return float(density) * float(waterplane_m2) * delta_draft_m


def stay_ratio(current_hours, typical_hours):
    """K = Tтек / Tобыч и ΔT = Tтек − Tобыч (в часах)."""
    current, typical = num(current_hours), num(typical_hours)
    if current is None or not typical:
        return {"ratio": None, "delta_hours": None}
    return {"ratio": current / typical, "delta_hours": current - typical}


def gap_stats(rows, window_hours=None, min_minutes=GAP_MIN_MINUTES) -> dict:
    """Пропуски AIS: сколько, самый длинный, суммарно и доля наблюдения."""
    gaps = analysis.dark_periods(rows, min_minutes)
    total = sum(gap["hours"] for gap in gaps)
    window = num(window_hours)
    result = {"count": len(gaps), "total_hours": total, "share_pct": None,
              "longest": gaps[0] if gaps else None, "gaps": gaps}
    if window and window > 0:
        result["share_pct"] = min(100.0, total / window * 100.0)
    return result


def freshness_hours(updated_at, now=None) -> float:
    """Δt обновления = tпроверки − tпоследнего сообщения AIS, в часах."""
    last = _moment(updated_at)
    if last is None:
        return None
    now = now or datetime.now()
    if last > now:
        return 0.0
    return (now - last).total_seconds() / 3600.0


def draft_observation(visits, vessel=None) -> dict:
    """Осадки по визитам и в карточке судна: первая, последняя и ΔT."""
    values = []
    for visit in visits or []:
        for key, when_key in (("draft_in", "entered_at"), ("draft_out", "left_at")):
            value = num(visit.get(key))
            if value is not None:
                values.append((_moment(visit.get(when_key)), value))
    values.sort(key=lambda item: item[0] or datetime.min)
    result = {"first": None, "last": None, "delta": None,
              "first_at": None, "last_at": None,
              "min": num((vessel or {}).get("draft_min")),
              "max": num((vessel or {}).get("draft_max"))}
    if values:
        result["first"], result["first_at"] = values[0][1], values[0][0]
        result["last"], result["last_at"] = values[-1][1], values[-1][0]
        result["delta"] = draft_change(result["first"], result["last"])
    return result


def report(vessel, track_rows, visits=None, stay=None, voyage=None,
           history_hours=168.0, now=None) -> list:
    """Все расчёты по судну — списком готовых строк для карточки."""
    if not vessel:
        return []
    now = now or datetime.now()
    lines = []

    data = motion(track_rows)
    if data["observations"] < 2:
        lines.append("Движение: наблюдений меньше двух — расстояние, скорость и курс "
                     "не рассчитываются (для них нужна временная пара координат).")
    elif data["moments"] < 2:
        lines.append(f"Движение: все сообщения с координатами "
                     f"({data['observations']}) пришли в один и тот же момент "
                     f"времени — временных пар нет, расстояние и скорость "
                     f"не считаются.")
    else:
        hours = data["hours"] or 0.0
        lines.append(
            f"Пройдено по прямой между {data['observations']} наблюдениями: "
            f"{fmt(data['km'])} км ({fmt(data['nm'])} мор. миль) за "
            f"{analysis.span_text(hours)}; средняя скорость за этот путь "
            f"{fmt(data['speed_mean'])} уз. Это прямая между точками, а не "
            f"судоходный маршрут: фактический путь длиннее.")
        if data["intervals"] > 1:
            lines.append(
                f"Скорость по {_count(data['intervals'], 'интервалу', 'интервалам', 'интервалам')}: "
                f"минимум {fmt(data['speed_min'])} уз, средняя {fmt(data['speed_mean'])} уз, "
                f"максимум {fmt(data['speed_max'])} уз.")
        if data["sog_mean"] is not None:
            lines.append(
                f"Сверка с AIS: средняя SOG за наблюдения {fmt(data['sog_mean'])} уз, "
                f"последняя {fmt(data['sog_last'])} уз; расчёт по координатам "
                f"{fmt(data['speed_mean'])} уз — разница "
                f"{fmt(abs((data['speed_mean'] or 0) - (data['sog_last'] or 0)))} уз "
                f"(SOG мгновенная, расчёт — средняя за интервал).")
            if abs((data["speed_mean"] or 0) - (data["sog_last"] or 0)) > 2.0:
                lines.append("  Расхождение большое: SOG могла не обновиться вместе "
                             "с позицией, либо одно из сообщений AIS неточное — "
                             "по этим значениям вывод о движении не делается.")
        if data["stop_count"]:
            lines.append(
                f"Стоянки по повторным наблюдениям: {_count(data['stop_count'], 'интервал', 'интервала', 'интервалов')} "
                f"со скоростью ниже {fmt(STOP_SPEED_KN)} уз, всего "
                f"{analysis.span_text(data['stop_hours'])}; в движении "
                f"{analysis.span_text(data['moving_hours'])}.")
        if data["course_delta"] is not None:
            lines.append(
                f"Курс менялся не более чем на {fmt(data['course_delta'], 0)}° за интервал "
                f"(шкала круговая: 359° → 1° — это 2°). По одному значению курса "
                f"манёвр не определяется.")

    ratios = hull(vessel.get("length_m"), vessel.get("beam_m"), vessel.get("draft"))
    missing = []
    if not ratios["length"]:
        missing.append("длина")
    if not ratios["beam"]:
        missing.append("ширина")
    if not ratios["draft"]:
        missing.append("осадка")
    parts = []
    if ratios["length_beam"]:
        parts.append(f"L/B {fmt(ratios['length_beam'])}")
    if ratios["length_draft"]:
        parts.append(f"L/T {fmt(ratios['length_draft'])}")
    if ratios["beam_draft"]:
        parts.append(f"B/T {fmt(ratios['beam_draft'])}")
    if len(parts) == 3:
        lines.append("Корпус: " + ", ".join(parts) +
                     " — геометрические отношения, а не измерение вместимости "
                     "или класса судна.")
    elif parts:
        lines.append("Корпус: " + ", ".join(parts) + "; остальные отношения не "
                     "считаются: не передано " + ", ".join(missing) + ".")
    else:
        lines.append("Корпус: нет длины и ширины — L/B, L/T, B/T не рассчитываются "
                     "(по MMSI и типу AIS размеры не восстанавливаются).")

    draft = draft_observation(visits, vessel)
    if draft["delta"] is not None:
        lines.append(
            f"Осадка по визитам: {fmt(draft['first'], 2)} → {fmt(draft['last'], 2)} м, "
            f"ΔT {draft['delta']:+.2f} м — "
            + ("больше, значит загрузилось" if draft["delta"] > 0
               else "меньше, значит выгрузилось" if draft["delta"] < -0.05
               else "в пределах точности, нагрузка не изменилась"))
    elif draft["min"] is not None and draft["max"] is not None and draft["max"] > draft["min"]:
        lines.append(
            f"Осадка: от {fmt(draft['min'], 2)} до {fmt(draft['max'], 2)} м по всем "
            f"сообщениям AIS (ΔT {draft['max'] - draft['min']:+.2f} м).")
    elif draft["last"] is not None:
        lines.append(f"Осадка: {fmt(draft['last'], 2)} м — одно значение, изменение "
                     f"нагрузки не рассчитывается.")
    if ratios["draft"]:
        lines.append("Изменение водоизмещения ΔM ≈ ρ·A·ΔT не считается: нет площади "
                     "ватерлинии и плотности воды — по одной осадке масса не определяется.")

    if stay and (stay.get("current_hours") is not None or stay.get("typical_hours")):
        port_label = stay.get("port_label") or stay.get("port") or "порт"
        parts = []
        if stay.get("current_hours") is not None:
            parts.append(f"стоит {analysis.span_text(stay['current_hours'])}")
        if stay.get("typical_hours"):
            source = ("по истории судна" if stay.get("scope") == "судно"
                      else "по истории порта")
            parts.append(f"обычно {analysis.span_text(stay['typical_hours'])} ({source})")
        line = f"Стоянка в {port_label}: " + "; ".join(parts)
        ratio = stay_ratio(stay.get("current_hours"), stay.get("typical_hours"))
        if ratio["ratio"] is not None:
            line += (f". K = {fmt(ratio['ratio'], 2)} от обычной, разница "
                     f"{analysis.span_text(abs(ratio['delta_hours']))} "
                     + ("больше" if ratio["delta_hours"] >= 0 else "меньше"))
            if ratio["ratio"] > 1.5:
                line += ("; вывод о необычности требует числа прошлых визитов "
                         "и способа расчёта среднего")
        lines.append(line + ".")

    gaps = gap_stats(track_rows, history_hours)
    if gaps["count"]:
        longest = gaps["longest"]
        line = (f"Пропуски AIS: {_count(gaps['count'], 'интервал', 'интервала', 'интервала')} от "
                f"{GAP_MIN_MINUTES // 60} ч, суммарно "
                f"{analysis.span_text(gaps['total_hours'])}")
        if gaps["share_pct"] is not None:
            line += f" — {fmt(gaps['share_pct'], 0)} % времени наблюдения"
        line += (f"; самый длинный {analysis.span_text(longest['hours'])} "
                 f"({analysis.moment_text(longest['start'])} → "
                 f"{analysis.moment_text(longest['end'])}). В эти интервалы данные AIS "
                 f"не поступали — возможны пропуски покрытия, приёма или передачи.")
        lines.append(line)

    if voyage:
        port_label = voyage.get("port_label") or voyage.get("port") or "порт"
        if voyage.get("in_port"):
            lines.append(f"Судно уже в порту {port_label}: время прибытия для него "
                         f"не рассчитывается.")
        elif voyage.get("distance_km") is not None:
            sog = vessel.get("sog")
            hours = travel_hours(voyage["distance_km"], sog)
            if hours is not None and hours > 0:
                lines.append(
                    f"До {port_label}: {fmt(voyage['distance_km'])} км "
                    f"({fmt(nm(voyage['distance_km']))} мор. миль) по прямой при "
                    f"{fmt(sog)} уз → T = D/V = {analysis.span_text(hours)}; "
                    f"ETA {voyage['eta']:%d.%m %H:%M}. Задержки, течение и изменение "
                    f"скорости не учтены.")
            else:
                lines.append(f"До {port_label}: {fmt(voyage['distance_km'])} км, но "
                             f"скорость слишком мала — T = D/V не считается.")
    if vessel.get("eta_declared"):
        lines.append("ETA в AIS передаёт капитан: "
                     f"{vessel.get('eta_declared')} — это не расчёт.")

    age = freshness_hours(vessel.get("updated_at"), now)
    if age is not None:
        lines.append(f"Свежесть данных: последнее сообщение AIS "
                     f"{analysis.span_text(age)} назад.")
    return lines


def row_note(vessel) -> str:
    """Короткая строка расчётов для таблицы: только то, что не требует трека."""
    if not vessel:
        return ""
    ratios = hull(vessel.get("length_m"), vessel.get("beam_m"), vessel.get("draft"))
    parts = []
    if ratios["length_beam"]:
        parts.append(f"L/B {fmt(ratios['length_beam'])}")
    if ratios["length_draft"]:
        parts.append(f"L/T {fmt(ratios['length_draft'])}")
    if ratios["beam_draft"]:
        parts.append(f"B/T {fmt(ratios['beam_draft'])}")
    age = freshness_hours(vessel.get("updated_at"))
    if age is not None:
        parts.append(f"связь {analysis.span_text(age)} назад")
    return " · ".join(parts)
