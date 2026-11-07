"""Карта-картинка для бота: порты и суда с реальными координатами.

Суда без позиции (список AIS-catcher) на карту не наносятся: точки у порта
забивали карту и выглядели как выдуманные. Они остаются в списках — в
приложении, в кнопках портов и в командах бота (/expected, /sources).
"""

import math
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1500, 940
PAD = 80
BG = "#061018"
GRID = "#0f2430"
GRID_TEXT = "#33596b"
PORT = "#35d08a"
PORT_TEXT = "#7fe0b3"
LIVE = "#ff8a65"
TEXT = "#d7e6ee"
DIM = "#5f8ea3"


def _font(size, bold=False):
    names = ["arialbd.ttf", "segoeuib.ttf"] if bold else ["arial.ttf", "segoeui.ttf"]
    names += ["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _points(vessels):
    """Только суда с координатами: (lat, lon, подпись)."""
    points = []
    for vessel in vessels:
        lat, lon = vessel.get("lat"), vessel.get("lon")
        if lat is None or lon is None:
            continue
        try:
            points.append((float(lat), float(lon),
                           str(vessel.get("name") or vessel.get("mmsi") or "?")))
        except (TypeError, ValueError):
            continue
    return points


def _bounds(points, ports):
    lats, lons = [], []
    for lat, lon, _name in points:
        lats.append(lat)
        lons.append(lon)
    for port in ports or []:
        try:
            lats.append(float(port["lat"]))
            lons.append(float(port["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not lats or not lons:
        lats, lons = [45.0], [28.5]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    pad = max(0.1, (lat_max - lat_min) * 0.12, (lon_max - lon_min) * 0.12)
    return lat_min - pad, lat_max + pad, lon_min - pad, lon_max + pad


def render(vessels, ports, path, without_position=0):
    """Рисует карту в PNG и возвращает путь."""
    path = Path(path)
    points = _points(vessels)
    lat_min, lat_max, lon_min, lon_max = _bounds(points, ports)
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2
    scale = min((WIDTH - 2 * PAD) / max(0.1, lon_max - lon_min),
                (HEIGHT - 2 * PAD - 30) / max(0.1, lat_max - lat_min))

    def to_px(lat, lon):
        return WIDTH / 2 + (lon - center_lon) * scale, HEIGHT / 2 - (lat - center_lat) * scale

    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    small = _font(13)
    tiny = _font(12)
    name_font = _font(12)
    label = _font(15, bold=True)
    title_font = _font(22, bold=True)

    # сетка
    step = 0.25
    lat = math.floor(lat_min / step) * step
    while lat <= lat_max:
        y = to_px(lat, center_lon)[1]
        draw.line([(0, y), (WIDTH, y)], fill=GRID)
        draw.text((8, y + 3), f"{lat:.2f}", font=tiny, fill=GRID_TEXT)
        lat = round(lat + step, 4)
    lon = math.floor(lon_min / step) * step
    while lon <= lon_max:
        x = to_px(center_lat, lon)[0]
        draw.line([(x, 0), (x, HEIGHT - 24)], fill=GRID)
        draw.text((x + 4, HEIGHT - 20), f"{lon:.2f}", font=tiny, fill=GRID_TEXT)
        lon = round(lon + step, 4)

    placed = []

    def put_text(x, y, text, font, fill, anchor="lm"):
        """Подпись без наложений: пробуем несколько сдвигов по вертикали."""
        sign = 1 if anchor.startswith("l") else -1
        for shift in (0, -16, 16, -32, 32, -48, 48):
            box = draw.textbbox((x + sign * 13, y + shift), text, font=font, anchor=anchor)
            if not any(box[0] < other[2] and other[0] < box[2]
                       and box[1] < other[3] and other[1] < box[3] for other in placed):
                placed.append(box)
                draw.text((x + sign * 13, y + shift), text, font=font, fill=fill, anchor=anchor)
                return
        box = draw.textbbox((x + sign * 13, y + 48), text, font=font, anchor=anchor)
        placed.append(box)
        draw.text((x + sign * 13, y + 48), text, font=font, fill=fill, anchor=anchor)

    # порты
    for port in ports or []:
        try:
            x, y = to_px(float(port["lat"]), float(port["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        draw.ellipse([x - 6, y - 6, x + 6, y + 6], outline=PORT, width=2)
        draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=PORT)
        port_name = str(port.get("name") or "")
        width = draw.textlength(port_name, font=label)
        placed.append((x - width / 2, y + 9, x + width / 2, y + 26))
        draw.text((x, y + 15), port_name, font=label, fill=PORT_TEXT, anchor="mm")

    # суда с реальной позицией
    for lat, lon, name in points:
        x, y = to_px(lat, lon)
        if not (0 <= x <= WIDTH and 0 <= y <= HEIGHT):
            continue
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=LIVE)
        put_text(x, y, name, name_font, TEXT, "lm")

    now = datetime.now(timezone.utc)
    draw.text((PAD - 44, 20), "Ship Tracker — карта", font=title_font, fill=TEXT)
    draw.text((PAD - 44, 52),
              f"{now:%d.%m.%Y %H:%M} UTC · судов с позицией: {len(points)}",
              font=small, fill=DIM)
    note = "Источник: живые AIS-потоки (AISStream, Open Waters)."
    if without_position:
        note += (f" Суда без позиции ({without_position}) на карту не нанесены — "
                 "их список в /expected.")
    draw.text((PAD - 44, HEIGHT - 50), note, font=tiny, fill=DIM)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path
