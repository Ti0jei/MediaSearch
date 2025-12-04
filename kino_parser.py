# kino_parser.py
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import win32gui, win32con, win32process

def _force_hide_uc_window(driver):
    """Полностью скрывает окно UC (невидимо, без Alt-Tab, не фокусируется)."""
    try:
        pid = driver.service.process.pid

        def enum_handler(hwnd, hwnds):
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid == pid and win32gui.IsWindowVisible(hwnd):
                hwnds.append(hwnd)

        hwnds = []
        win32gui.EnumWindows(enum_handler, hwnds)
        for hwnd in hwnds:
            # Убираем из Alt-Tab и панели задач
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            style |= win32con.WS_EX_TOOLWINDOW
            style &= ~win32con.WS_EX_APPWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
            # Скрываем окно и блокируем фокус
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            win32gui.SetWindowPos(
                hwnd,
                0, 0, 0, 0, 0,
                win32con.SWP_NOSIZE | win32con.SWP_NOMOVE | win32con.SWP_NOACTIVATE
            )
        print("🕶 UC окно полностью скрыто.")
        return True
    except Exception as e:
        print(f"⚠️ Ошибка при скрытии UC окна: {e}")
        return False

# ---------- базовые пути ----------
def _media_base_dir() -> str:
    # где лежат profile_* и kino_cookies
    return os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "MediaSearch")


def _cookie_db_candidates():
    base = _media_base_dir()
    cands = []
    for prof in ("profile_visible", "profile_worker"):
        d = os.path.join(base, prof, "Default")
        # Chromium 110+:
        cands.append(os.path.join(d, "Network", "Cookies"))
        # Старые сборки:
        cands.append(os.path.join(d, "Cookies"))
    return cands


def _cookie_jar_path():
    # твой файл с куками рядом с профилями
    return os.path.join(_media_base_dir(), "kino_cookies")


def has_valid_session() -> bool:
    """
    Валидная сессия, если:
    - в любом из файлов cookies (новом/старом) есть неистёкшие куки для kino.pub
    - или есть непустая SQLite-база Cookies в профиле браузера
    """
    now = int(time.time())

    # 1) проверяем оба cookie-файла
    for p in (COOKIE_FILE, COOKIE_FILE_LEGACY):
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                items = data if isinstance(data, list) else (data.get("cookies") or [])
                for c in items:
                    dom = c.get("domain") or c.get("host") or ""
                    name = (c.get("name") or "").lower()
                    exp = int(c.get("expires") or c.get("expiry") or c.get("expirationDate") or 0)
                    if "kino.pub" in dom and name and (exp == 0 or exp > now):
                        return True
        except Exception:
            pass

    # 2) проверяем SQLite-базы Chromium в наших профилях
    candidates = [
        os.path.join(VISIBLE_PROFILE, "Default", "Network", "Cookies"),
        os.path.join(VISIBLE_PROFILE, "Default", "Cookies"),
        os.path.join(WORKER_PROFILE, "Default", "Network", "Cookies"),
        os.path.join(WORKER_PROFILE, "Default", "Cookies"),
    ]
    for p in candidates:
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 10_000:
                return True
        except Exception:
            pass

    return False


def _base_dir() -> str:
    # где лежат ресурсы рядом с программой (или MEIPASS в one-file)
    return getattr(sys, "_MEIPASS", os.getcwd())


def _persist_dir() -> str:
    # постоянное место для данных пользователя (куки/профиль)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        d = os.path.join(root, "MediaSearch")
    else:
        d = os.path.join(os.path.expanduser("~"), ".medisearch")
    os.makedirs(d, exist_ok=True)
    return d


BASE_URL = "https://kino.pub"

# временные артефакты (html/скрины) — рядом с программой
SANDBOX_DIR = os.path.join(_base_dir(), "sandbox")
FILM_DIR = os.path.join(_base_dir(), "film")
os.makedirs(SANDBOX_DIR, exist_ok=True)
os.makedirs(FILM_DIR, exist_ok=True)

# ПЕРСИСТЕНТНЫЕ данные — в профиле пользователя
PERSIST_DIR = _persist_dir()
COOKIE_FILE = os.path.join(PERSIST_DIR, "kino_cookies.json")
COOKIE_FILE_LEGACY = os.path.join(PERSIST_DIR, "kino_cookies")  # старое имя без .json
VISIBLE_PROFILE = os.path.join(PERSIST_DIR, "profile_visible")
WORKER_PROFILE = os.path.join(PERSIST_DIR, "profile_worker")
PROFILE_DIR = VISIBLE_PROFILE  # алиас для обратной совместимости
os.makedirs(VISIBLE_PROFILE, exist_ok=True)
os.makedirs(WORKER_PROFILE, exist_ok=True)

FALLBACK_MAJOR = 138  # если не смогли определить версию portable Chromium

BROWSER_CANDIDATES: list[str] = [
    os.path.join(_base_dir(), "browser", "bin", "chrome.exe"),
    os.path.join(_base_dir(), "browser", "Chromium", "Application", "chrome.exe"),
    os.path.join(_base_dir(), "browser", "Chrome", "Application", "chrome.exe"),
    os.path.join(_base_dir(), "browser", "chrome.exe"),
]

CARD_SELECTORS = [
    "div.item-title.text-ellipsis > a",
    "div.item-info > div.item-title > a",
    "a.item-title",
    "div.item-title > a",
]


# ---------- утилиты ----------
def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\- ]", " ", name)   # всё лишнее → пробел
    name = re.sub(r"\s{2,}", " ", name)     # схлопываем
    return name.strip(" .")



def _detect_major_via_cmd(exe_path: str) -> int | None:
    try:
        out = subprocess.check_output([exe_path, "--version"], text=True, timeout=4)
        m = re.search(r"\b(\d{2,3})\.\d+\.\d+\.\d+\b", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _detect_major_via_powershell(exe_path: str) -> int | None:
    try:
        ps = f"(Get-Item '{exe_path}').VersionInfo.ProductVersion"
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps], text=True, timeout=4
        )
        m = re.search(r"\b(\d{2,3})\.\d+\.\d+\.\d+\b", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def get_browser_major_version(exe_path: str) -> int | None:
    return _detect_major_via_cmd(exe_path) or _detect_major_via_powershell(exe_path)


def find_portable_browser() -> tuple[str | None, int | None]:
    for p in BROWSER_CANDIDATES:
        if os.path.isfile(p):
            ver = get_browser_major_version(p)
            print(f"[DEBUG] check {p} → major={ver}")
            return p, ver
    return None, None
def log_and_save_cookies(driver, status_cb=None):
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except:
        pass

    try:
        save_cookies(driver)
        msg = f"💾 Cookies (CDP) сохранены → {COOKIE_FILE}"
        if status_cb:
            status_cb(msg)
        else:
            print(msg)
    except Exception as e:
        msg = f"⚠️ Ошибка сохранения cookies: {e}"
        if status_cb:
            status_cb(msg)
        else:
            print(msg)

# --- CDP cookies helpers (полный набор, включая HttpOnly) ---
def save_cookies_cdp(driver) -> None:
    driver.execute_cdp_cmd("Network.enable", {})
    data = driver.execute_cdp_cmd("Network.getAllCookies", {})
    cookies = data.get("cookies", [])
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"💾 Cookies (CDP) сохранены → {COOKIE_FILE}")

def load_cookies_cdp(driver) -> bool:
    path = COOKIE_FILE if os.path.exists(COOKIE_FILE) else COOKIE_FILE_LEGACY
    if not os.path.exists(path):
        print(f"[COOKIES] Файл {path} не найден.")
        print(f"[🍪] Загрузка куки в CDP: {len(cookies)} шт.")
        return False

    with open(path, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    # Фильтруем только под kino.pub и живые куки
    now = time.time()
    filtered = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if "kino.pub" not in dom:
            continue
        # CDP ждёт expires в секундах (float) либо 0/отсутствует
        exp = c.get("expires") or c.get("expiry") or c.get("expirationDate")
        if isinstance(exp, (int, float)) and exp != 0 and exp < now:
            continue
        item = {
            "name": c["name"],
            "value": c.get("value", ""),
            "domain": dom,
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", c.get("httponly", False))),
            "sameSite": c.get("sameSite") or "Lax",
        }
        if exp:
            item["expires"] = float(exp)
        filtered.append(item)

    driver.get("about:blank")
    driver.execute_cdp_cmd("Network.enable", {})
    if filtered:
        driver.execute_cdp_cmd("Network.setCookies", {"cookies": filtered})
        print(f"✅ Загружено {len(filtered)} cookies (CDP)")
        return True
    print("⚠️ Нет подходящих cookies для загрузки (CDP).")
    return False

def save_cookies(driver) -> None:
    """
    Сохраняем все куки (включая httpOnly/SameSite/Secure) через CDP.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    cookies = []
    try:
        data = driver.execute_cdp_cmd("Network.getAllCookies", {}) or {}
        cookies = data.get("cookies", []) or []
    except Exception as e:
        print(f"⚠️ Network.getAllCookies error: {e}")

    # Оставим только поля, которые потом поймёт setCookies
    out = []
    for c in cookies:
        if "kino.pub" not in (c.get("domain") or ""):
            continue
        item = {
            "name":     c.get("name"),
            "value":    c.get("value"),
            "domain":   c.get("domain") or ".kino.pub",
            "path":     c.get("path") or "/",
            "expires":  c.get("expires"),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": c.get("sameSite") if c.get("sameSite") in ("Strict","Lax","None") else None,
        }
        # подчистим None
        item = {k:v for k,v in item.items() if v is not None}
        out.append(item)

    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        # legacy файл можно не трогать, но оставлю как у тебя
        with open(COOKIE_FILE_LEGACY, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"💾 Cookies (CDP) сохранены → {COOKIE_FILE}")
    except Exception as e:
        print(f"⚠️ Не удалось сохранить cookies: {e}")



def load_cookies(driver) -> bool:
    """
    Грузим куки через CDP Network.setCookies (можно ДО первого driver.get()).
    Это восстанавливает httpOnly/SameSite/Secure.
    """
    path = COOKIE_FILE if os.path.exists(COOKIE_FILE) else COOKIE_FILE_LEGACY
    if not os.path.exists(path):
        print(f"[COOKIES] Файл {path} не найден.")
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f) or []
    except Exception as e:
        print(f"⚠️ Ошибка чтения cookies: {e}")
        return False

    # Санитизируем под CDP: name,value,domain,path,expires,httpOnly,secure,sameSite
    prepared = []
    for c in cookies:
        name  = c.get("name")
        value = c.get("value")
        domain = c.get("domain") or ".kino.pub"
        path = c.get("path") or "/"
        if not name or value is None:
            continue
        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
        }
        # опциональные
        if isinstance(c.get("expires"), (int, float)):
            item["expires"] = int(c["expires"])
        if isinstance(c.get("httpOnly"), bool):
            item["httpOnly"] = c["httpOnly"]
        if isinstance(c.get("secure"), bool):
            item["secure"] = c["secure"]
        ss = c.get("sameSite")
        if ss in ("Strict", "Lax", "None"):
            item["sameSite"] = ss

        prepared.append(item)

    if not prepared:
        return False

    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Network.setCookies", {"cookies": prepared})
        print(f"✅ Загружено {len(prepared)} cookies (CDP) из {path}")
        return True
    except Exception as e:
        print(f"⚠️ setCookies error: {e}")
        # иногда помогает переинициализация
        try:
            driver.execute_cdp_cmd("Network.disable", {})
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Network.setCookies", {"cookies": prepared})
            print(f"✅ Загружено {len(prepared)} cookies (после re-enable)")
            return True
        except Exception as e2:
            print(f"⚠️ setCookies retry error: {e2}")
            return False




def safe_quit(driver):
    try:
        driver.quit()
    except Exception:
        pass
    try:
        svc = getattr(driver, "service", None)
        proc = getattr(svc, "process", None)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass


# ---------- драйвер ----------
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException


def _unlock_profile(path: str) -> None:
    try:
        if not os.path.isdir(path):
            return
        for name in os.listdir(path):
            if name.startswith("Singleton") or name in ("DevToolsActivePort",):
                try:
                    os.remove(os.path.join(path, name))
                except Exception:
                    pass
    except Exception:
        pass


def _build_opts(profile_dir: str, visible: bool, enable_perf: bool):
    opts = uc.ChromeOptions()
    if enable_perf and not os.environ.get("KINO_DISABLE_PERF"):
        try:
            opts.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
            opts.set_capability(
                "goog:perfLoggingPrefs", {"enableNetwork": True, "enablePage": False}
            )
        except Exception:
            pass
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--remote-debugging-port=0")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--lang=ru-RU")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-features=IsolateOrigins,site-per-process")
    opts.add_argument("--blink-settings=imagesEnabled=true")
    opts.add_argument("--autoplay-policy=no-user-gesture-required")
    if visible:
        opts.add_argument("--start-maximized")
    else:
        # вместо полного скрытия — просто нормальное окно, но без фокуса
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--start-maximized")

    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument(f"--disk-cache-dir={os.path.join(profile_dir, 'Cache')}")
    return opts


def make_visible_driver(portable_path=None, ver_main=None, for_login=False):
    r"""
    Создаёт undetected_chromedriver с постоянным профилем MediaSearch\uc_profile.
    Этот профиль хранит Cloudflare токены и авторизацию kino.pub.
    """

    import undetected_chromedriver as uc
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    import shutil

    base_dir = os.path.join(os.environ["LOCALAPPDATA"], "MediaSearch")
    user_data_dir = os.path.join(base_dir, "uc_profile")
    os.makedirs(user_data_dir, exist_ok=True)

    # разблокируем профиль, если остались "SingletonLock" и т.п.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        try:
            os.remove(os.path.join(user_data_dir, name))
        except FileNotFoundError:
            pass

    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={user_data_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--start-maximized")
    opts.add_argument("--lang=ru-RU")
    opts.add_argument("--disable-blink-features=AutomationControlled")

    # ГАРАНТИРОВАННО видимое окно
    opts.add_argument("--window-position=100,100")
    opts.add_argument("--window-size=1400,900")

    # отключаем headless принудительно
    if "--headless" in opts.arguments:
        opts.arguments.remove("--headless")

    driver = uc.Chrome(
        options=opts,
        version_main=ver_main or None,
        headless=False,
        use_subprocess=True
    )

    try:
        driver.set_window_position(100, 100)
        driver.set_window_size(1400, 900)
    except Exception:
        pass

    print(f"🚀 UC профиль: {user_data_dir}")
        # --- если это режим скачивания, прячем окно ---
    if not for_login:
        _force_hide_uc_window(driver)

    return driver




# ---------- помощь: ожидания ----------
def wait_ready(driver, timeout=30):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass


def wait_cards(driver, timeout=60) -> int:
    wait_ready(driver, timeout=30)
    end = time.time() + timeout
    last = 0
    while time.time() < end:
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.25)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass
        for css in CARD_SELECTORS:
            try:
                cnt = len(driver.find_elements(By.CSS_SELECTOR, css))
            except Exception:
                cnt = 0
            if cnt > 0:
                return cnt
            last = max(last, cnt)
        time.sleep(0.5)
    return last


# ---------- открытие страниц через пагинацию ----------
def open_list_page(driver, target_page: int) -> None:
    """
    Открыть /new?page=N. Для N>1 сначала грузим /new?page=1, затем кликаем по пагинации на нужный номер.
    Если клик не удался — делаем прямой переход с cache-buster’ом.
    """
    if target_page == 1:
        url = f"{BASE_URL}/new?page=1"
        print(f"\n🌍 Открываю: {url}")
        driver.get(url)
        wait_ready(driver)
        return

    # 1: обязательно прийти со страницы 1 (иногда сайт проверяет реферер/сессию)
    first = f"{BASE_URL}/new?page=1"
    print(f"\n🌍 Открываю: {first}")
    driver.get(first)
    wait_ready(driver)
    time.sleep(0.7)

    # 2: пробуем клик по номеру страницы
    try:
        # разные варианты пагинации
        candidates = [
            (
                By.XPATH,
                f"//ul[contains(@class,'pagination')]//a[normalize-space(text())='{target_page}']",
            ),
            (By.CSS_SELECTOR, f"ul.pagination a[href*='?page={target_page}']"),
            (
                By.XPATH,
                f"//a[contains(@href,'?page={target_page}') and not(contains(@class,'disabled'))]",
            ),
        ]
        clicked = False
        for by, sel in candidates:
            els = driver.find_elements(by, sel)
            if els:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", els[0])
                except Exception:
                    pass
                els[0].click()
                clicked = True
                break

        if clicked:
            wait_ready(driver)
            return
        else:
            raise NoSuchElementException("номер страницы не найден")
    except Exception:
        # 3: запасной путь — прямой переход с cache-buster
        url = f"{BASE_URL}/new?page={target_page}&t={int(time.time()*1000)}&r={random.randint(1,999999)}"
        print(f"↪️ Переход напрямую: {url}")
        driver.get(url)
        wait_ready(driver)


# ---------- основной парсер ----------
def get_kino_titles(save_to: str = "kino_pub_titles.txt", pages: int = 1, auto_login: bool = True):
    """
    Один невидимый видимый драйвер: авторизация (если нужна) + парсинг.
    Переход на страницы >1 делаем через пагинацию, с запасным прямым переходом.
    """
    portable, ver_main = find_portable_browser()
    if portable:
        print(f"🧰 Используем встроенный браузер: {portable} (v{ver_main})")
    else:
        print("🌐 Portable-браузер не найден — UC попытается использовать/скачать Chromium.")

    driver = make_visible_driver(portable_path=portable, ver_main=ver_main)
    all_titles: list[str] = []
    try:
        if has_cookies():
            load_cookies(driver)
        elif auto_login:
            driver.get(BASE_URL)
            print("🔓 Открылось окно kino.pub. Войдите и (если надо) пройдите капчу…")
            t0 = time.time()
            while time.time() - t0 < 180:
                try:
                    WebDriverWait(driver, 3).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "a[href*='logout'], .user, .navbar a[href*='logout']")
                        )
                    )
                    break
                except TimeoutException:
                    time.sleep(1)
            save_cookies(driver)

        # цикл по страницам
        for page in range(1, pages + 1):
            open_list_page(driver, page)
            count = wait_cards(driver, timeout=60)
            if count == 0:
                dbg_html = os.path.join(SANDBOX_DIR, f"debug_page_{page}.html")
                with open(dbg_html, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                try:
                    driver.save_screenshot(os.path.join(SANDBOX_DIR, f"debug_page_{page}.png"))
                except Exception:
                    pass
                print(f"⚠️ Карточки не появились. HTML → {os.path.basename(dbg_html)}")
            else:
                print(f"🔗 Найдено фильмов на странице {page}: {count}")

            # парсим DOM
            soup = BeautifulSoup(driver.page_source, "html.parser")
            title_tags = None
            for css in CARD_SELECTORS:
                title_tags = soup.select(css)
                if title_tags:
                    break

            for tag in title_tags or []:
                title = tag.get_text(strip=True)
                href = tag.get("href", "")
                if not href.startswith("http"):
                    href = BASE_URL + href

                print(f"🔎 Открываем карточку: {title} — {href}")
                try:
                    driver.get(href)
                    wait_ready(driver, timeout=20)
                    time.sleep(0.6)

                    # 1) сохраняем HTML на диск (как раньше)
                    safe_title = sanitize_filename(title)
                    html_path = os.path.join(FILM_DIR, f"{safe_title}.html")
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(driver.page_source)

                    # 2) читаем СТРОГО из сохранённого HTML и вытаскиваем год по нескольким стратегиям
                    with open(html_path, encoding="utf-8") as f:
                        html_text = f.read()
                    doc = BeautifulSoup(html_text, "html.parser")

                    year = None

                    # — основная стратегия: таблица характеристик (разные варианты подписей)
                    for row in doc.select("tr"):
                        cells = row.find_all(["td", "th"])
                        if len(cells) >= 2:
                            head = cells[0].get_text(" ", strip=True).lower()
                            val = cells[1].get_text(" ", strip=True)
                            if any(
                                k in head
                                for k in ("год", "год выхода", "year", "release", "release year")
                            ):
                                m = re.search(r"\b(19|20)\d{2}\b", val)
                                if m:
                                    year = m.group(0)
                                    break

                    # — микроразметка / метатеги
                    if not year:
                        meta = doc.select_one(
                            '[itemprop="datePublished"], [itemprop="releaseDate"], meta[property="og:release_date"], meta[name="date"]'
                        )
                        if meta:
                            content = meta.get("content") or meta.get_text(strip=True)
                            m = re.search(r"\b(19|20)\d{2}\b", content or "")
                            if m:
                                year = m.group(0)

                    # — поиск по общему тексту рядом со словами "Год / Year / Release"
                    if not year:
                        full_text = doc.get_text(" ", strip=True)
                        m = re.search(
                            r"(?:Год(?:\s*выхода)?|Year|Release)[^\d]{0,20}\b((?:19|20)\d{2})\b",
                            full_text,
                            re.I,
                        )
                        if m:
                            year = m.group(1)

                    # — fallback: первый год, встретившийся на странице
                    if not year:
                        m = re.search(r"\b(19|20)\d{2}\b", full_text)
                        if m:
                            year = m.group(0)

                    # 3) формируем строку так, как было раньше
                    full_title = f"{title} ({year})" if year else title
                    all_titles.append(full_title)
                    print(f"✅ Добавлено: {full_title}")

                except Exception as e:
                    print(f"⚠️ Ошибка карточки {href}: {e}")
                    all_titles.append(title)

    finally:
        safe_quit(driver)

    with open(save_to, "w", encoding="utf-8") as f:
        f.writelines((t + "\n") for t in all_titles)

    print(f"\n🎬 Сохранено фильмов: {len(all_titles)} → {save_to}")
    return all_titles


def has_cookies() -> bool:
    return any(
        os.path.exists(p) and os.path.getsize(p) > 0 for p in (COOKIE_FILE, COOKIE_FILE_LEGACY)
    )


def interactive_login(timeout_sec: int = 180) -> bool:
    """Открыть ВИДИМЫЙ браузер для входа и сохранить cookies."""
    portable, ver_main = find_portable_browser()
    drv = make_visible_driver(portable_path=portable, ver_main=ver_main, for_login=True)
    try:
        drv.get(BASE_URL)
        print("🔓 Войдите в Kino.pub (если нужна капча — решите её)...")
        try:
            WebDriverWait(drv, timeout_sec).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "a[href*='logout'], .user, .navbar a[href*='logout']")
                )
            )
        except TimeoutException:
            print("⏱️ Время ожидания входа истекло.")
            return False
        save_cookies(drv)
        return True
    finally:
        safe_quit(drv)


def ensure_login(timeout_sec: int = 180) -> bool:
    """Проверить логин; если нет — выполнить интерактивный вход."""
    if has_cookies():
        return True
    return interactive_login(timeout_sec=timeout_sec)


# самостоятельный запуск
if __name__ == "__main__":
    # парсит N страниц; теперь >1 берёт по клику пагинации
    get_kino_titles(pages=2, auto_login=True)
