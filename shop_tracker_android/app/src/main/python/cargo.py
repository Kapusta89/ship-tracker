"""Оценка груза судна по осадке — без внешних справочников.

AIS передаёт осадку (`MaximumStaticDraught`) и габариты корпуса (`Dimension`
A–D: до носа, до кормы, полуширина влево и вправо), но не передаёт ни дедвейт,
ни фактическую загрузку. Поэтому груз считается математически, по закону
Архимеда, с коэффициентом полноты корпуса:

    Δ(d) = ρ · Cb · L · B · d          водоизмещение при осадке d, т
    Δmax = ρ · Cb · L · B · d_полн     водоизмещение при полной осадке, т
    LW   ≈ f · Δmax                    вес порожнего судна, т
    Груз ≈ Δ(d) − LW                   всё, что сверх порожнего, т
    Дедвейт ≈ Δmax − LW

где ρ = 1.02 т/м³ (речная и солоноватая вода), L и B — длина и ширина корпуса,
Cb — коэффициент полноты водоизмещения (зависит от типа судна), f — доля
порожнего судна в полном водоизмещении: чем меньше корпус, тем она больше
(у речных судов на порожний вес приходится до трети водоизмещения).

Полная осадка `d_полн` берётся из наблюдений программы за этим судном
(максимальная осадка, которую оно передавало), а если судно ещё не видели
гружёным — по судам похожей длины из базы.

Точность — порядка 10 %: настоящие дедвейт и порожний вес неизвестны, а на
малой осадке в «нагрузку» попадает ещё и балласт с запасами. Поэтому
результат всегда показывается как «≈», с указанием, откуда взята полная осадка.
"""

WATER_DENSITY = 1.02  # т/м³: речная и солоноватая вода

# Коэффициент полноты водоизмещения по типам AIS (ITU-R M.1371).
BLOCK_COEFFICIENTS = (
    ((80, 89), 0.82),   # танкеры
    ((70, 79), 0.78),   # грузовые
    ((60, 69), 0.66),   # пассажирские
    ((30, 39), 0.60),   # рыболовные, буксиры, спецсудна
    ((50, 59), 0.62),   # лоцманские, буксиры, земснаряды
)
DEFAULT_BLOCK_COEFFICIENT = 0.72


def block_coefficient(type_code):
    """Коэффициент полноты водоизмещения по коду типа AIS."""
    try:
        code = int(type_code)
    except (TypeError, ValueError):
        return DEFAULT_BLOCK_COEFFICIENT
    for (low, high), value in BLOCK_COEFFICIENTS:
        if low <= code <= high:
            return value
    return DEFAULT_BLOCK_COEFFICIENT


def cargo_kind(type_code):
    """Что за груз везёт судно — по коду типа AIS (ITU-R M.1371).

    Название груза AIS не передаёт: известно только назначение судна и —
    для опасных грузов — класс опасности. Поэтому это подпись «по типу судна»,
    а не груз из судовых документов.

    Возвращает (коротко для таблицы, пояснение для карточки) или (None, None).
    """
    try:
        code = int(type_code)
    except (TypeError, ValueError):
        return None, None

    if code == 80:
        return "нефть и нефтепродукты", "танкер (код AIS 80)"
    if 81 <= code <= 84:
        hazard = "ABCD"[code - 81]
        return (f"нефтепродукты, опасный груз кат. {hazard}",
                f"танкер, опасный груз категории {hazard} (код AIS {code})")
    if code == 89:
        return "танкер, груз не указан", "танкер без указания груза (код AIS 89)"
    if code == 70:
        return "генеральные грузы", "сухогруз (код AIS 70)"
    if 71 <= code <= 74:
        hazard = "ABCD"[code - 71]
        return (f"генеральные грузы, опасный груз кат. {hazard}",
                f"сухогруз, опасный груз категории {hazard} (код AIS {code})")
    if code == 79:
        return "сухогруз, груз не указан", "сухогруз без указания груза (код AIS 79)"
    if 60 <= code <= 69:
        return "пассажиры", f"пассажирское судно (код AIS {code})"
    if code in (30, 31):
        return "без груза (промысел)", f"рыболовное судно (код AIS {code})"
    if code in (33,):
        return "без груза (земснаряд)", "земснаряд (код AIS 33)"
    if code in (52, 53):
        return "без груза (буксир)", f"буксир (код AIS {code})"
    return None, None


def lightship_share(length):
    """Доля порожнего судна в полном водоизмещении по длине корпуса."""
    try:
        length = float(length)
    except (TypeError, ValueError):
        return 0.22
    if length < 100:
        return 0.28
    if length < 160:
        return 0.22
    return 0.16


def estimate(length, beam, draft, draft_max=None, reference_max=None, type_code=None):
    """Оценивает груз, дедвейт и загрузку судна.

    `draft_max` — максимальная осадка, которую передавало само судно;
    `reference_max` — максимальная осадка судов похожей длины (запасной вариант).
    Возвращает словарь или None, если данных о корпусе недостаточно.
    """
    try:
        length = float(length)
        beam = float(beam)
        draft = float(draft)
    except (TypeError, ValueError):
        return None
    if not (length > 0 and beam > 0 and draft > 0):
        return None

    def positive(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    own_max = positive(draft_max)
    other_max = positive(reference_max)

    if own_max and own_max - draft > 0.2:
        full_draft = own_max
        full_draft_source = "по максимальной осадке этого судна"
        confidence = "высокая"
    elif other_max and other_max - draft > 0.2:
        full_draft = other_max
        full_draft_source = "по судам похожей длины"
        confidence = "средняя"
    else:
        # полную осадку не видели: считаем текущую предельной, поэтому
        # дедвейт и доля загрузки неизвестны — показываем только тонны
        full_draft = draft
        full_draft_source = "полная осадка не наблюдалась"
        confidence = "низкая"

    coefficient = block_coefficient(type_code)
    share_empty = lightship_share(length)
    full_displacement = WATER_DENSITY * coefficient * length * beam * full_draft
    displacement = WATER_DENSITY * coefficient * length * beam * draft
    lightship = share_empty * full_displacement
    deadweight = max(0.0, full_displacement - lightship)
    cargo = max(0.0, displacement - lightship)
    load_share = (cargo / deadweight) if deadweight > 0 and full_draft > draft else None

    return {
        "length_m": length,
        "beam_m": beam,
        "draft_m": draft,
        "full_draft_m": full_draft,
        "block_coefficient": coefficient,
        "empty_share": share_empty,
        "displacement_t": displacement,
        "deadweight_t": deadweight,
        "lightship_t": lightship,
        "cargo_t": cargo,
        "load_share": load_share,
        "in_ballast": bool(load_share is not None and load_share < 0.25),
        "confidence": confidence,
        "full_draft_source": full_draft_source,
    }
