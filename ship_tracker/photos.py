"""Фото судна: загрузка с публичной страницы VesselFinder и кэш на диске.

AIS фото не передаёт, поэтому берём его со страницы судна на VesselFinder —
там лежит ссылка вида static.vesselfinder.net/ship-photo/<imo>-<mmsi>-<hash>/1.

Скачанное складываем в data/photos: при повторном выборе судна сайт уже не
дёргаем. Если фото нет, ставим отметку и не ищем его снова несколько дней.
"""

import re
import time
from pathlib import Path

import requests

DETAILS_URL = "https://www.vesselfinder.com/vessels/details/{key}"
PHOTO_PATTERN = re.compile(r"https://static\.vesselfinder\.net/ship-photo/[^\"'\s]+")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Сколько помнить, что у судна нет фото (секунды).
MISSING_TTL = 3 * 24 * 3600
# Меньше этого файл считать битым.
MIN_IMAGE_BYTES = 1000


def photo_file(cache_dir, mmsi) -> Path:
    return Path(cache_dir) / f"{mmsi}.jpg"


def fetch(mmsi, imo=None, cache_dir=None, timeout=20):
    """Возвращает путь к локальному файлу с фото или None.

    Сеть может быть недоступна — тогда просто None, без исключений:
    карточка судна должна открываться и без интернета.
    """
    if not mmsi:
        return None
    cache_dir = Path(cache_dir)
    cached = photo_file(cache_dir, mmsi)
    missing = cache_dir / f"{mmsi}.none"
    try:
        if cached.exists() and cached.stat().st_size >= MIN_IMAGE_BYTES:
            return cached
        if missing.exists() and time.time() - missing.stat().st_mtime < MISSING_TTL:
            return None
    except OSError:
        return None

    # Сначала ищем по IMO, затем по MMSI: VesselFinder понимает оба варианта.
    for key in [value for value in (imo, mmsi) if value]:
        try:
            page = requests.get(DETAILS_URL.format(key=key), headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            return None
        if page.status_code != 200:
            continue
        found = PHOTO_PATTERN.findall(page.content.decode("utf-8", "replace"))
        if not found:
            continue
        try:
            image = requests.get(found[0], headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            return None
        if image.status_code != 200 or len(image.content) < MIN_IMAGE_BYTES:
            continue
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(image.content)
            if missing.exists():
                missing.unlink()
        except OSError:
            return None
        return cached

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{mmsi}.none").write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    return None
