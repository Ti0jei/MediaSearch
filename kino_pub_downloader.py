"""
Kino.pub downloader logic (поиск + скачивание + статусы) + ОЧЕРЕДЬ.
Работает без GUI, но отдаёт прогресс через callback.
Использует движок загрузки из kino_hls.py и UC-логику из uc_driver.py.
"""

import os
import re
import time
import threading
import queue
from typing import Callable, List, Optional, Tuple

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Наши модули
from kino_hls import download_by_item_url, get_hls_info, start_hls_download
from kino_parser import load_cookies
from uc_driver import (
    _safe_get_driver,
    _check_login,
    _check_login_on,
    login_to_kino,
    DriverPool,
)

KINOPUB_BASE = "https://kino.pub"


# -------------------------------------------------------
# Утилита логирования
# -------------------------------------------------------
def _log(status_cb: Optional[Callable[[str], None]], msg: str):
    print(msg)
    if status_cb:
        try:
            status_cb(msg)
        except Exception:
            pass


# -------------------------------------------------------
# Сериал: сезоны/эпизоды
# -------------------------------------------------------
def parse_series_episodes(
    series_url: str,
    *,
    driver,
    status_cb=None,
    cancel_event=None,
) -> dict:
    """
    Анализирует страницу сериала и возвращает:
      {
        "title": "<название сериала>",
        "seasons": {
           1: [{"episode": 1, "url": "https://..."}, ...],
           2: [...]
        }
      }

    Важно: структура/селекторы на kino.pub могут меняться, поэтому тут несколько fallback-стратегий.
    """
    if driver is None:
        raise RuntimeError("parse_series_episodes() требует активный driver (UC).")

    from urllib.parse import urljoin, urlsplit, urlunsplit, urlencode, parse_qsl

    def _cancelled() -> bool:
        return bool(getattr(cancel_event, "is_set", lambda: False)())

    def _ensure_abs(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        if u.startswith("http"):
            return u
        return urljoin(KINOPUB_BASE + "/", u)

    def _series_episode_url(base: str, season: int, episode: int) -> str:
        base = _ensure_abs(base)
        parts = list(urlsplit(base))
        q = dict(parse_qsl(parts[3], keep_blank_values=True))
        q["season"] = str(season)
        q["episode"] = str(episode)
        parts[3] = urlencode(q)
        parts[4] = ""  # fragment
        return urlunsplit(parts)

    def _ensure_cf_solved() -> bool:
        try:
            from kino_hls import (
                _has_challenge,
                _driver_is_suppressed,
                _wait_challenge_solved,
                _solve_cloudflare_in_visible_browser,
            )
        except Exception:
            return True

        try:
            if not _has_challenge(driver):
                return True
        except Exception:
            return True

        _log(status_cb, "🧩 Обнаружена защита (Cloudflare) — решите в открытом браузере…")

        try:
            if not _driver_is_suppressed(driver):
                _wait_challenge_solved(driver, timeout=90)
                return not _has_challenge(driver)
        except Exception:
            pass

        # suppress-драйвер не показать: пробуем подгрузить cookies/refresh
        try:
            load_cookies(driver)
            driver.refresh()
        except Exception:
            pass

        try:
            if not _has_challenge(driver):
                return True
        except Exception:
            return True

        ok = False
        try:
            ok = _solve_cloudflare_in_visible_browser(series_url, status_cb=status_cb, timeout=180)
        except Exception:
            ok = False
        if not ok:
            return False

        try:
            load_cookies(driver)
            driver.refresh()
        except Exception:
            pass

        try:
            return not _has_challenge(driver)
        except Exception:
            return True

    def _extract_title(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        title = ""

        # 1) стараемся взять именно «русский» заголовок из H1/видимого title
        try:
            h = soup.select_one("h1, .item-title, .page-title, h2, h3")
            if h:
                parts = []
                try:
                    parts = [s for s in list(h.stripped_strings) if s]
                except Exception:
                    parts = []
                if parts:
                    title = str(parts[0]).strip()
                else:
                    title = h.get_text(" ", strip=True)
        except Exception:
            title = ""

        # 2) fallback: og:title
        if not title:
            try:
                meta = soup.select_one("meta[property='og:title']")
                if meta and meta.get("content"):
                    title = str(meta.get("content") or "").strip()
            except Exception:
                title = ""

        title = (title or "").strip()

        # убираем хвосты типа "— Kino.pub"
        try:
            title = re.sub(r"\s*[—-]\s*Kino\.pub.*$", "", title, flags=re.I).strip()
        except Exception:
            pass

        # убираем год в конце, если есть
        try:
            title = re.sub(r"\s+\(\d{4}\)\s*$", "", title).strip()
        except Exception:
            pass

        # если в заголовке есть «RU / EN» — берём левую часть (RU)
        try:
            if "/" in title:
                left = title.split("/", 1)[0].strip()
                if left:
                    title = left
        except Exception:
            pass

        # если есть RU + EN без разделителя (например: "Пацаны The Boys") —
        # убираем англ. хвост, но только если реально есть >=2 латинских слова (чтобы не ломать названия типа "Мистер Robot").
        try:
            has_cyr = bool(re.search(r"[А-Яа-яЁё]", title))
            latin_words = re.findall(r"[A-Za-z]{2,}", title)
            if has_cyr:
                # "Пацаны (The Boys)" -> "Пацаны"
                m = re.match(r"^(.+?)\s*\([^)]*[A-Za-z][^)]*\)\s*$", title)
                if m:
                    left = (m.group(1) or "").strip()
                    if left:
                        title = left

                # "Пацаны — The Boys" -> "Пацаны"
                m = re.match(r"^(.+?)\s*[—-]\s*[A-Za-z].*$", title)
                if m:
                    left = (m.group(1) or "").strip()
                    if left:
                        title = left

            if has_cyr and len(latin_words) >= 2:
                m = re.match(r"^(.+?)\s+[A-Za-z].*$", title)
                if m:
                    title = (m.group(1) or "").strip()
        except Exception:
            pass

        return _normalize_display_name(title) if title else "series"

    def _parse_seasons_from_html(html: str) -> list[int]:
        soup = BeautifulSoup(html, "html.parser")
        nums: list[int] = []
        # типовой блок: "Сезоны:" + span.p-r-sm.p-t-sm
        for el in soup.select("span.p-r-sm.p-t-sm, a.p-r-sm.p-t-sm, button.p-r-sm.p-t-sm"):
            try:
                t = el.get_text(" ", strip=True)
            except Exception:
                t = ""
            t = (t or "").strip()
            if not t.isdigit():
                continue
            try:
                n = int(t)
            except Exception:
                continue
            if 1 <= n <= 99:
                nums.append(n)
        nums = sorted({n for n in nums})
        return nums

    def _find_season_elements() -> dict[int, object]:
        # Selenium элементы, по которым можно кликать
        try:
            from selenium.webdriver.common.by import By
        except Exception:
            return {}
        mapping: dict[int, object] = {}
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, "span.p-r-sm.p-t-sm, a.p-r-sm.p-t-sm, button.p-r-sm.p-t-sm")
        except Exception:
            elems = []
        for el in elems or []:
            try:
                t = str(el.text or "").strip()
            except Exception:
                t = ""
            if not t.isdigit():
                continue
            try:
                n = int(t)
            except Exception:
                continue
            if 1 <= n <= 99 and n not in mapping:
                mapping[n] = el
        return mapping

    def _extract_episodes_from_html(html: str) -> list[tuple[int | None, str | None]]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[tuple[int | None, str | None]] = []

        # основной контейнер эпизодов (по скрину: div.row.m-b)
        cards = soup.select("div.row.m-b .owl-item")
        if not cards:
            # fallback: просто все owl-item на странице
            cards = soup.select(".owl-item")

        for card in cards:
            href = None
            try:
                a = card.select_one("a[href]")
                if a:
                    href = a.get("href")
            except Exception:
                href = None
            if not href:
                try:
                    href = card.get("data-href") or card.get("data-url")
                except Exception:
                    href = None

            if href:
                href = _ensure_abs(str(href))

            ep_num = None
            try:
                text = card.get_text(" ", strip=True)
            except Exception:
                text = ""
            text = (text or "").strip()
            if text:
                m = re.search(r"(?:Эпизод|Episode)\s*(\d{1,3})\b", text, re.I)
                if m:
                    try:
                        ep_num = int(m.group(1))
                    except Exception:
                        ep_num = None

            # фильтруем явно «не эпизоды»: если нет номера и нет ссылки — пропускаем
            if ep_num is None and not href:
                continue

            out.append((ep_num, href))

        # fallback: ссылки в блоке эпизодов
        if not out:
            for a in soup.select("div.row.m-b a[href*='/item/'], div.row.m-b a[href]"):
                try:
                    href = a.get("href")
                except Exception:
                    href = None
                if not href:
                    continue
                href = _ensure_abs(str(href))
                text = ""
                try:
                    text = (a.get_text(" ", strip=True) or "").strip()
                except Exception:
                    text = ""
                ep_num = None
                m = re.search(r"(?:Эпизод|Episode)\s*(\d{1,3})\b", text, re.I)
                if m:
                    try:
                        ep_num = int(m.group(1))
                    except Exception:
                        ep_num = None
                out.append((ep_num, href))

        return out

    def _collect_episodes_interactive(base_url: str) -> list[dict]:
        """
        Пытаемся собрать все эпизоды текущего сезона:
        - 1 раз парсим HTML целиком
        - если есть dots (owl-dot) — кликаем каждый и добираем
        - если есть next-стрелка — кликаем пока появляются новые ссылки
        """
        try:
            from selenium.webdriver.common.by import By
        except Exception:
            By = None

        seen: dict[str, int | None] = {}  # url -> ep_num

        def _merge(entries: list[tuple[int | None, str | None]]):
            for ep_num, href in entries:
                if not href:
                    continue
                if href not in seen:
                    seen[href] = ep_num
                else:
                    # если раньше номер не распарсили, а сейчас распарсили — обновим
                    if seen[href] is None and ep_num is not None:
                        seen[href] = ep_num

        # текущий HTML
        try:
            _merge(_extract_episodes_from_html(driver.page_source))
        except Exception:
            pass

        if not By:
            # без Selenium селекторов больше ничего не сделаем
            pass
        else:
            # dots
            try:
                dots = driver.find_elements(By.CSS_SELECTOR, "div.row.m-b .owl-dots button, div.row.m-b .owl-dots .owl-dot")
            except Exception:
                dots = []
            if dots and len(dots) > 1:
                for i, dot in enumerate(dots):
                    if _cancelled():
                        break
                    try:
                        driver.execute_script("arguments[0].click();", dot)
                        time.sleep(0.35)
                        _merge(_extract_episodes_from_html(driver.page_source))
                    except Exception:
                        continue

            # next-стрелка
            try:
                next_btns = driver.find_elements(By.CSS_SELECTOR, "div.row.m-b .owl-nav .owl-next, div.row.m-b .owl-next")
            except Exception:
                next_btns = []
            next_btn = next_btns[0] if next_btns else None
            if next_btn is not None:
                stagnation = 0
                for _ in range(40):
                    if _cancelled():
                        break
                    before = len(seen)
                    try:
                        driver.execute_script("arguments[0].click();", next_btn)
                    except Exception:
                        break
                    time.sleep(0.35)
                    try:
                        _merge(_extract_episodes_from_html(driver.page_source))
                    except Exception:
                        pass
                    if len(seen) <= before:
                        stagnation += 1
                        if stagnation >= 3:
                            break
                    else:
                        stagnation = 0

        # нормализуем: если для части ссылок не нашли номер — раздадим по порядку
        ordered_urls = list(seen.keys())
        # приоритет: те, у кого номер известен
        numbered = [(u, n) for u, n in seen.items() if n is not None]
        if numbered:
            # сортируем по номеру, затем остаток
            numbered.sort(key=lambda x: int(x[1] or 0))
            ordered_urls = [u for u, _ in numbered] + [u for u in ordered_urls if seen.get(u) is None]

        items: list[dict] = []
        next_auto = 1
        for u in ordered_urls:
            ep = seen.get(u)
            if ep is None:
                ep = next_auto
                next_auto += 1
            items.append({"episode": int(ep), "url": u})
        return items

    series_url = _ensure_abs(series_url)
    if not series_url:
        raise RuntimeError("Пустая ссылка на сериал.")

    if _cancelled():
        return {"title": "series", "seasons": {}}

    _log(status_cb, f"📺 Анализ сериала: {series_url}")
    driver.get(series_url)

    if not _ensure_cf_solved():
        raise RuntimeError("Cloudflare не пройден (таймаут).")

    try:
        WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass

    try:
        if "/user/login" in (driver.current_url or "").lower():
            raise RuntimeError("Требуется вход в Kino.pub")
    except Exception:
        pass

    html0 = driver.page_source
    title = _extract_title(html0)
    seasons = _parse_seasons_from_html(html0)
    season_elems = _find_season_elements()
    if not seasons:
        seasons = sorted(season_elems.keys()) if season_elems else [1]

    result: dict = {"title": title, "seasons": {}}

    # если сезоны не находятся/не кликаются — просто соберём «как есть» в сезон 1
    if not season_elems or len(seasons) <= 1:
        s_num = int(seasons[0] if seasons else 1)
        eps = _collect_episodes_interactive(series_url)
        # Важно: даже если сайт не меняет URL при выборе эпизода, делаем
        # стабильные ссылки через query params season/episode, чтобы:
        # - не было дублей между сезонами
        # - можно было скачать конкретный SxxExx
        try:
            for e in eps or []:
                try:
                    ep = int((e or {}).get("episode") or 1)
                except Exception:
                    ep = 1
                base = (e or {}).get("url") or series_url
                e["url"] = _series_episode_url(str(base), s_num, ep)
        except Exception:
            pass

        result["seasons"][s_num] = eps
        # если ссылки не нашлись — попробуем сгенерировать по шаблону
        if not result["seasons"].get(s_num):
            # fallback: хотя бы 1 эпизод по базовой ссылке
            result["seasons"][s_num] = [{"episode": 1, "url": _series_episode_url(series_url, s_num, 1)}]
        return result

    # полноценный проход по сезонам
    for s_num in seasons:
        if _cancelled():
            break
        _log(status_cb, f"📺 Сезон {s_num}…")
        el = season_elems.get(int(s_num))
        if el is not None:
            try:
                driver.execute_script("arguments[0].click();", el)
            except Exception:
                try:
                    el.click()
                except Exception:
                    pass
            time.sleep(0.6)

        eps = _collect_episodes_interactive(series_url)
        # если не нашлись явные ссылки — сгенерируем по шаблону season/episode
        if not eps:
            eps = [{"episode": 1, "url": _series_episode_url(series_url, int(s_num), 1)}]
        else:
            # Важно: даже если сайт не меняет URL при выборе эпизода, делаем
            # стабильные ссылки через query params season/episode, чтобы не было дублей между сезонами.
            for e in eps:
                try:
                    ep = int((e or {}).get("episode") or 1)
                except Exception:
                    ep = 1
                try:
                    base = (e or {}).get("url") or series_url
                    e["url"] = _series_episode_url(str(base), int(s_num), ep)
                except Exception:
                    pass

        result["seasons"][int(s_num)] = eps

    return result


def _kino_cookie_mtime() -> int | None:
    """
    Возвращает mtime файла cookies (в секундах) или None.
    Нужен, чтобы после логина обновлять cookies во всех драйверах пула.
    """
    try:
        from kino_parser import COOKIE_FILE, COOKIE_FILE_LEGACY

        path = COOKIE_FILE if os.path.exists(COOKIE_FILE) else COOKIE_FILE_LEGACY
        if os.path.exists(path):
            return int(os.path.getmtime(path) or 0)
    except Exception:
        return None
    return None


# -------------------------------------------------------
# Поиск по названию на сайте
# -------------------------------------------------------
def search_titles(query: str, limit=1, status_cb=None, driver=None, cancel_event=None) -> List[Tuple[str, str]]:
    # кооперативная отмена (если поддерживается вызывающим кодом)
    if getattr(cancel_event, "is_set", lambda: False)():
        return []
    if driver is None:
        raise RuntimeError("search_titles ожидает активный driver")

    from urllib.parse import quote_plus
    url = f"{KINOPUB_BASE}/item/search?query=" + quote_plus(query)
    _log(status_cb, f"🔍 Поиск: {url}")
    driver.get(url)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".item-title a"))
        )
    except Exception:
        _log(status_cb, "⚠️ Ничего не найдено или страница не загрузилась.")
        return []

    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a in soup.select(".item-title a[href*='/item/']")[:limit]:
        title = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = KINOPUB_BASE + href
        results.append((title, href))

    _log(status_cb, f"🔎 Найдено: {len(results)} результат(ов).")
    return results


# -------------------------------------------------------
# Извлечение “красивого” имени файла
# -------------------------------------------------------
def _extract_display_name(driver, item_url, cancel_event=None) -> str:
    """Возвращает 'Русское название (YYYY)' с чисткой служебных символов."""
    try:
        if getattr(cancel_event, "is_set", lambda: False)():
            return "video"
        driver.get(item_url)
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "meta[property='og:title'], h1, .item-title"))
        )
        if getattr(cancel_event, "is_set", lambda: False)():
            return "video"
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        title_h1 = soup.select_one("h1, .item-title")
        title_ru = title_h1.get_text(strip=True) if title_h1 else (driver.title or "").strip()

        title_ru = re.split(r"[_/]", title_ru)[0].strip()
        title_ru = re.sub(r'\s+\(\d{4}\)$', '', title_ru)

        year = None
        for tr in soup.select("table.table.table-striped tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                label = tds[0].get_text(" ", strip=True).lower()
                if any(k in label for k in ("год выхода", "год выпуска", "год")):
                    text = tds[1].get_text(" ", strip=True)
                    m = re.search(r"\b(19|20)\d{2}\b", text)
                    if m:
                        year = m.group(0)
                        break

        if not year:
            m = re.search(r"\b(19|20)\d{2}\b", html)
            year = m.group(0) if m else ""

        name = f"{title_ru} ({year})" if year else title_ru

        # Запрещённые символы Windows → пробел
        name = re.sub(r'[\\/:*?"<>|]', " ", name)

        # Схлопываем подряд идущие пробелы
        name = re.sub(r"\s{2,}", " ", name)

        # Убираем пробелы и точки по краям (Windows не любит такие имена)
        name = name.strip(" .")

        return name or "video"


    except Exception:
        slug = re.sub(r"[#?].*$", "", item_url).rstrip("/").split("/")[-1]
        return (slug.replace("-", " ").strip() or "video")

def _normalize_display_name(name: str) -> str:
    """
    Нормализует имя файла (без расширения) для Windows:
    - убирает .mp4 (если прилетело)
    - запрещённые символы -> пробел
    - схлопывает пробелы
    - триммит пробелы/точки по краям
    """
    name = (name or "").strip()
    if not name:
        return "video"

    try:
        if name.lower().endswith(".mp4"):
            name = name[:-4]
    except Exception:
        pass

    try:
        name = re.sub(r'[\\/:*?"<>|]', " ", name)
        name = re.sub(r"\s{2,}", " ", name)
        name = name.strip(" .")
    except Exception:
        pass

    return name or "video"


# -------------------------------------------------------
# ОДНО скачивание (с возможностью передать внешний driver из пула)
# -------------------------------------------------------
def download(
    query_or_url: str,
    out_dir=".",
    status_cb=None,
    driver=None,
    cancel_event=None,
    audio_select_cb=None,
    defer_mux: bool = False,
    display_name_override: str | None = None,
    audio_parallel_tracks: int | None = None,
) -> bool:
    """
    Скачивание одного фильма.
    Если driver передан (из DriverPool) — используем его, иначе сами поднимем скрытый UC.
    """
    os.makedirs(out_dir, exist_ok=True)

    internal_driver = None
    try:
        # ======= ЕСЛИ ПЕРЕДАН driver (пул UC) =======
        if driver is not None:
            try:
                cookie_mtime = _kino_cookie_mtime()
                loaded_mtime = int(getattr(driver, "_kino_cookies_mtime", 0) or 0)
                need_reload = (not getattr(driver, "_kino_cookies_loaded", False)) or (
                    cookie_mtime and cookie_mtime != loaded_mtime
                )

                if need_reload:
                    driver.get(KINOPUB_BASE + "/")
                    ok = bool(load_cookies(driver))
                    driver.refresh()
                    setattr(driver, "_kino_cookies_loaded", ok)
                    if cookie_mtime:
                        setattr(driver, "_kino_cookies_mtime", int(cookie_mtime))
                    # после перезаливки cookies лучше перепроверить логин
                    setattr(driver, "_kino_login_ok", False)
                    setattr(driver, "_kino_login_checked_at", 0)
            except Exception as e:
                _log(status_cb, f"⚠️ Ошибка подгрузки cookies в драйвер пула: {e}")

            # Быстрый путь: если этим драйвером уже недавно подтверждали логин —
            # не делаем лишних переходов (они заметно замедляют старт скачивания).
            try:
                checked_at = float(getattr(driver, "_kino_login_checked_at", 0) or 0)
                login_ok = bool(getattr(driver, "_kino_login_ok", False))
                if (not login_ok) or (time.time() - checked_at > 180):
                    if not _check_login_on(driver, status_cb):
                        # Иногда проверка ложно падает из-за CF/таймаута.
                        # Если cookies выглядят валидно — пробуем продолжить (реальная проверка всё равно будет на item_url).
                        try:
                            from kino_parser import has_valid_session

                            if has_valid_session():
                                _log(
                                    status_cb,
                                    "⚠️ Не удалось подтвердить сессию в браузере (возможно CF/таймаут) — продолжаю по cookies…",
                                )
                                setattr(driver, "_kino_login_ok", True)
                                setattr(driver, "_kino_login_checked_at", time.time())
                            else:
                                setattr(driver, "_kino_login_ok", False)
                                setattr(driver, "_kino_login_checked_at", time.time())
                                _log(status_cb, "⚠️ Сессия неактивна — требуется вход.")
                                return False
                        except Exception:
                            setattr(driver, "_kino_login_ok", False)
                            setattr(driver, "_kino_login_checked_at", time.time())
                            _log(status_cb, "⚠️ Сессия неактивна — требуется вход.")
                            return False
                    else:
                        setattr(driver, "_kino_login_ok", True)
                        setattr(driver, "_kino_login_checked_at", time.time())
            except Exception:
                if not _check_login_on(driver, status_cb):
                    try:
                        from kino_parser import has_valid_session

                        if not has_valid_session():
                            _log(status_cb, "⚠️ Сессия неактивна — требуется вход.")
                            return False
                        _log(
                            status_cb,
                            "⚠️ Не удалось подтвердить сессию в браузере (возможно CF/таймаут) — продолжаю по cookies…",
                        )
                    except Exception:
                        _log(status_cb, "⚠️ Сессия неактивна — требуется вход.")
                        return False
                try:
                    setattr(driver, "_kino_login_ok", True)
                    setattr(driver, "_kino_login_checked_at", time.time())
                except Exception:
                    pass

            use_driver = driver

        else:
            raise RuntimeError("Download() must be called with driver — internal UC driver forbidden.")

        # ======= ДАЛЬШЕ ИСПОЛЬЗУЕМ use_driver =======

        if getattr(cancel_event, "is_set", lambda: False)():
            return False

        # URL или поиск
        if not query_or_url.startswith("http"):
            results = search_titles(
                query_or_url,
                limit=1,
                status_cb=status_cb,
                driver=use_driver,
                cancel_event=cancel_event,
            )
            if not results:
                _log(status_cb, "❌ Ничего не найдено.")
                return False
            _, item_url = results[0]
        else:
            item_url = query_or_url

        if getattr(cancel_event, "is_set", lambda: False)():
            return False

        _log(status_cb, "📋 Извлекаю название...")
        if display_name_override:
            display_name = str(display_name_override)
        else:
            display_name = _extract_display_name(use_driver, item_url, cancel_event=cancel_event)
        display_name = _normalize_display_name(display_name)

        # формируем output
        out_path = os.path.join(out_dir, display_name + ".mp4")

        _log(status_cb, f"🎬 Файл: {os.path.basename(out_path)}")

        # Если файл уже существует — не качаем повторно (стабильность очереди после перезапуска).
        # Пользователь всегда может удалить файл вручную и запустить снова.
        try:
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                _log(status_cb, "✅ Уже скачано (файл существует)")
                return True
        except Exception:
            pass

        # --- здесь только запуск подготовки ---
        _log(status_cb, "🎬 Подготовка… (анализ HLS)")

        ok = download_by_item_url(
            item_url,
            out_path,
            driver=use_driver,
            status_cb=status_cb,
            cancel_event=cancel_event,
            audio_select_cb=audio_select_cb,
            defer_mux=defer_mux,
            audio_parallel_tracks=audio_parallel_tracks,
        )

        if getattr(cancel_event, "is_set", lambda: False)():
            return False

        # Теперь download_by_item_url() работает СИНХРОННО:
        # и анализ HLS, и скачивание, и MUX выполняются внутри него.
        # Здесь просто логируем общий результат.

        if not ok:
            _log(status_cb, "❌ Ошибка при скачивании.")
        else:
            if defer_mux:
                _log(status_cb, "🎞 Готово к конвертации.")
            else:
                _log(status_cb, "✅ Скачивание завершено.")
        return ok

    except Exception as e:
        _log(status_cb, f"❌ Ошибка: {e}")
        return False

    finally:
        if internal_driver:
            try:
                internal_driver.quit()
            except Exception:
                pass


# -------------------------------------------------------
# Параллельная загрузка по уже известным URL (быстрый батч)
# -------------------------------------------------------
def download_multiple(urls, out_dir, status_cb=None):
    """
    Простой батч: берём драйвер из пула → достаём m3u8 → синхронно качаем и муксуем.
    """
    os.makedirs(out_dir, exist_ok=True)
    pool = DriverPool(max_drivers=2, status_cb=status_cb)
    try:
        for url in urls:
            drv = pool.acquire(timeout=10)
            try:
                video_m3u8, hdrs, audios = get_hls_info(url, driver=drv)
                if not video_m3u8:
                    _log(status_cb, f"⚠️ Пропущено: нет потоков для {url}")
                    continue

                # Красивое имя (не через поиск — быстро)
                safe_name = _extract_display_name(drv, url)
                out_path = os.path.join(out_dir, safe_name + ".mp4")

                # start_hls_download теперь блокирующий
                start_hls_download(video_m3u8, audios, hdrs, out_path, status_cb)

            finally:
                pool.release(drv)

    finally:
        pool.close_all()



# =======================================================
#                ОЧЕРЕДЬ ЗАГРУЗОК (онлайн докидка)
# =======================================================
class QueueDownloader:
    """
    Многопоточная очередь загрузок:
      - add(url_or_query): докидывает задачу на лету
      - concurrency: сколько параллельных загрузок
      - внутри — DriverPool, каждый воркер берёт драйвер, получает m3u8 и запускает ffmpeg
      - драйвер освобождается сразу после старта ffmpeg (чтобы не держать CDP-сессию)
    """

    def __init__(self, out_dir: str, concurrency: int = 2, status_cb: Optional[Callable[[str], None]] = None):
        self.out_dir = out_dir
        self.status_cb = status_cb
        os.makedirs(self.out_dir, exist_ok=True)

        self.q: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.pool = DriverPool(max_drivers=max(1, concurrency), status_cb=status_cb)

        # поднимаем воркеры
        self.workers: list[threading.Thread] = []

        for i in range(max(1, concurrency)):
            t = threading.Thread(target=self._worker, name=f"dl-worker-{i+1}", daemon=True)
            t.start()
            self.workers.append(t)

        _log(self.status_cb, f"🧵 Очередь готова: параллельных загрузок = {concurrency}")

    def add(self, query_or_url: str):
        """Добавить задачу (URL или поисковый запрос)."""
        self.q.put(query_or_url)
        _log(self.status_cb, f"➕ В очередь: {query_or_url}")

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                task = self.q.get(timeout=0.2)
            except queue.Empty:
                continue

            drv = None
            try:
                drv = self.pool.acquire(timeout=15, profile_tag="run")

                # быстрый прогрев cookies (один раз на драйвер)
                try:
                    if not getattr(drv, "_kino_cookies_loaded", False):
                        drv.get("chrome://newtab")
                        load_cookies(drv)
                        drv.get(KINOPUB_BASE + "/")
                        setattr(drv, "_kino_cookies_loaded", True)

                except Exception as e:
                    _log(self.status_cb, f"⚠️ Ошибка подгрузки cookies: {e}")

                # если дали не URL — сначала ищем
                if not task.startswith("http"):
                    results = search_titles(task, limit=1, status_cb=self.status_cb, driver=drv)
                    if not results:
                        _log(self.status_cb, f"❌ Не найдено: {task}")
                        self.q.task_done()
                        self.pool.release(drv)
                        continue
                    _, item_url = results[0]
                else:
                    item_url = task

                # красивое имя файла (русское + год)
                try:
                    display_name = _extract_display_name(drv, item_url)
                except Exception:
                    # не критично — fallback
                    display_name = os.path.basename(item_url).split("?")[0]

                # --- НОРМАЛИЗАЦИЯ имени ---
                # иногда _extract_display_name() возвращает уже 'Название (2025).mp4'
                # из-за этого появляется '.mp4.mp4.part' → ffmpeg падает
                if display_name.lower().endswith(".mp4"):
                    display_name = display_name[:-4]

                out_path = os.path.join(self.out_dir, display_name + ".mp4")
                _log(self.status_cb, f"🎬 [{threading.current_thread().name}] → {os.path.basename(out_path)}")


                # получаем m3u8/заголовки/аудио
                video_m3u8, hdrs, audios = get_hls_info(item_url, driver=drv)
                if not video_m3u8:
                    _log(self.status_cb, f"⚠️ Пропуск (нет HLS): {item_url}")
                    self.q.task_done()
                    self.pool.release(drv)
                    continue

                # синхронно качаем и муксуем; драйвер больше не нужен
                start_hls_download(video_m3u8, audios, hdrs, out_path, self.status_cb)
                _log(self.status_cb, f"✅ Скачано: {out_path}")


            except Exception as e:
                _log(self.status_cb, f"❌ Ошибка воркера: {e}")

            finally:
                try:
                    if drv:
                        self.pool.release(drv)
                finally:
                    self.q.task_done()
    def wait_all(self):
        """Дождаться, когда очередь опустеет (все воркеры докачают своё)."""
        self.q.join()

    def stop(self):
        """Остановить воркеров и закрыть драйверы после завершения текущих задач."""
        self.stop_event.set()
        # дождаться обработки всех задач
        self.wait_all()

        # разбудить и аккуратно завершить воркеров
        for _ in self.workers:
            self.q.put_nowait("")
        for t in self.workers:
            t.join(timeout=1.0)

        # закрыть драйверы пула
        self.pool.close_all()
        _log(self.status_cb, "🧹 Очередь остановлена, драйверы закрыты.")

        # остановить воркеров
        for _ in self.workers:
            self.q.put_nowait("")  # разбудить
        for t in self.workers:
            t.join(timeout=1.0)

        # закрыть все драйверы пула
        self.pool.close_all()
        _log(self.status_cb, "🧹 Очередь остановлена, драйверы закрыты.")


# -------------------------------------------------------
# Синглтон-очередь (удобно дергать из GUI)
# -------------------------------------------------------
_queue_singleton: Optional[QueueDownloader] = None

def get_queue(out_dir: str, concurrency: int = 2, status_cb=None) -> QueueDownloader:
    """
    Получить (или создать) глобальную очередь загрузок.
    Пример:
        q = get_queue("Downloads", concurrency=2, status_cb=print)
        q.add("https://kino.pub/item/view/12345/...")
        q.add("https://kino.pub/item/view/67890/...")
        # можно докидывать ещё — по ходу
    """
    global _queue_singleton
    if _queue_singleton is None:
        _queue_singleton = QueueDownloader(out_dir=out_dir, concurrency=concurrency, status_cb=status_cb)
    return _queue_singleton
