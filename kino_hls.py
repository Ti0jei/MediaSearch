import json
import os
import random
import re
import shlex
import subprocess as sp
import time
import urllib.parse
import urllib.request
import shutil
import threading  # ← просто импорт, без глобального семафора

if os.name == "nt":
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0  # на *nix просто игнорируется

_FFMPEG_LOCK = threading.Lock()  # пока про запас, если захочешь синхронизировать только лог-файлы
MUX_MAX_PARALLEL = 4  # максимум одновременных MUX (ffmpeg)
_MUX_SEMA = threading.Semaphore(MUX_MAX_PARALLEL)
HLS_SEGMENT_MAX_PARALLEL = 16  # общий лимит одновременных запросов сегментов (стабильность)
_HLS_SEGMENT_SEMA = threading.Semaphore(HLS_SEGMENT_MAX_PARALLEL)
_CF_SOLVE_LOCK = threading.Lock()  # показываем окно CF только одно за раз


class DownloadCancelled(Exception):
    pass


def _is_cancelled(cancel_event) -> bool:
    try:
        return cancel_event is not None and cancel_event.is_set()
    except Exception:
        return False


def _raise_if_cancelled(cancel_event) -> None:
    if _is_cancelled(cancel_event):
        raise DownloadCancelled()


def _origin_from_referer(ref: str) -> str:
    try:
        u = urllib.parse.urlsplit(ref or "")
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return ""

def _augment_headers(headers: dict) -> tuple[str, dict]:
    """
    Возвращает (hdr_str для ffmpeg -headers, headers_dict для urllib/fetch).
    Добавляет Origin (из Referer) и Accept (HLS), нормализует ключи UA.
    """
    h = dict(headers or {})

    # Нормализуем User-Agent
    if "User-agent" in h and "User-Agent" not in h:
        h["User-Agent"] = h.pop("User-agent")

    ref = h.get("Referer") or h.get("referer") or ""
    origin = _origin_from_referer(ref)
    if origin and "Origin" not in h:
        h["Origin"] = origin

    # HLS-friendly Accept
    h.setdefault("Accept", "application/vnd.apple.mpegurl,application/x-mpegURL,*/*")

    # ffmpeg ждёт CRLF-разделённую строку
    hdr_str = "".join(f"{k}: {v}\r\n" for k, v in h.items() if v)
    return hdr_str, h


def _running_inside_vscode() -> bool:
    # VS Code ставит эти переменные окружения
    return (
        os.environ.get("VSCODE_PID") is not None
        or os.environ.get("TERM_PROGRAM") == "vscode"
    )

def _run_ffmpeg(cmd, cancel_event=None, status_cb=None) -> int:
    """Безопасный запуск ffmpeg — без окна, лог в уникальный файл, stdin отключён."""
    ffmpeg_bin = cmd[0]
    if not os.path.isfile(ffmpeg_bin):
        alt = shutil.which("ffmpeg")
        if alt:
            cmd[0] = alt
        else:
            local_ff = os.path.join(os.path.dirname(__file__), "ffmpeg", "ffmpeg.exe")
            if os.path.isfile(local_ff):
                cmd[0] = local_ff
            else:
                raise FileNotFoundError(f"⚠️ ffmpeg не найден: {ffmpeg_bin}")

    def _fmt_hms(seconds: float) -> str:
        try:
            s = max(0, int(seconds))
        except Exception:
            s = 0
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    def _parse_hhmmss_us(value: str) -> int | None:
        try:
            t = (value or "").strip()
            if not t or t == "N/A":
                return None
            hh, mm, rest = t.split(":", 2)
            if "." in rest:
                ss, frac = rest.split(".", 1)
                frac = re.sub(r"\D", "", frac)[:6]
                frac = (frac + "000000")[:6]
            else:
                ss, frac = rest, "000000"
            total = (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1_000_000 + int(frac)
            return max(0, int(total))
        except Exception:
            return None

    def _probe_duration_us(media_path: str | None) -> int | None:
        try:
            if not media_path or not os.path.isfile(media_path):
                return None
        except Exception:
            return None

        ffprobe = None
        try:
            fb = str(cmd[0] or "")
            if fb.lower().endswith("ffmpeg.exe"):
                cand = os.path.join(os.path.dirname(fb), "ffprobe.exe")
                if os.path.isfile(cand):
                    ffprobe = cand
        except Exception:
            ffprobe = None
        if not ffprobe:
            ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None

        try:
            res = sp.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    media_path,
                ],
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode != 0:
                return None
            s = (res.stdout or "").strip().splitlines()[0].strip()
            sec = float(s)
            if sec <= 0:
                return None
            return int(sec * 1_000_000)
        except Exception:
            return None

    mux_acquired = False
    try:
        # ограничиваем количество одновременно запущенных ffmpeg (MUX)
        while True:
            if _is_cancelled(cancel_event):
                return -2
            if _MUX_SEMA.acquire(timeout=0.2):
                mux_acquired = True
                break

        import uuid

        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"ffmpeg_log_{uuid.uuid4().hex}.txt")

        total_us: int | None = None
        out_us: int | None = None
        start_ts = time.time()
        last_emit_pct = -1
        last_emit_at = 0.0

        if status_cb and "-progress" not in cmd:
            # Прогресс пишем в stdout (pipe:1), чтобы показать %/ETA.
            # Вставляем после -loglevel <lvl>, если он есть.
            insert_at = 1
            try:
                for i in range(len(cmd) - 1):
                    if cmd[i] == "-loglevel":
                        insert_at = i + 2
                        break
            except Exception:
                insert_at = 1

            cmd = list(cmd)
            cmd[insert_at:insert_at] = ["-nostats", "-progress", "pipe:1"]

            try:
                in_path = None
                if "-i" in cmd:
                    in_idx = cmd.index("-i")
                    if in_idx + 1 < len(cmd):
                        in_path = cmd[in_idx + 1]
                total_us = _probe_duration_us(in_path)
            except Exception:
                total_us = None

        def _emit_mux_progress(force: bool = False) -> None:
            nonlocal last_emit_pct, last_emit_at
            if not status_cb or not total_us or not out_us:
                return
            try:
                now = time.time()
                p = max(0.0, min(1.0, float(out_us) / float(total_us)))
                pct = int(round(p * 100))
                if (not force) and pct == last_emit_pct and (now - last_emit_at) < 1.0:
                    return
                eta = None
                elapsed = now - start_ts
                if p > 0.01 and elapsed >= 1.0:
                    eta = elapsed * (1.0 / p - 1.0)
                msg = f"🔀 MUX {pct}%"
                if eta is not None and eta < 48 * 3600:
                    msg += f" ETA {_fmt_hms(eta)}"
                try:
                    status_cb(msg)
                except Exception:
                    pass
                last_emit_pct = pct
                last_emit_at = now
            except Exception:
                pass

        proc = sp.Popen(
            cmd,
            stdout=sp.PIPE,
            stderr=sp.STDOUT,
            stdin=sp.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        def _reader():
            nonlocal out_us
            try:
                with open(log_path, "w", encoding="utf-8") as log:
                    if not proc.stdout:
                        return
                    for raw in proc.stdout:
                        try:
                            log.write(raw)
                        except Exception:
                            pass

                        if not status_cb or not total_us:
                            continue

                        line = (raw or "").strip()
                        if "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip()
                        if key == "out_time_ms":
                            # Исторически ffmpeg пишет microseconds в out_time_ms.
                            try:
                                out_us = int(val)
                            except Exception:
                                out_us = None
                            _emit_mux_progress()
                        elif key == "out_time":
                            parsed = _parse_hhmmss_us(val)
                            if parsed is not None:
                                out_us = parsed
                                _emit_mux_progress()
                        elif key == "progress" and val == "end":
                            out_us = total_us
                            _emit_mux_progress(force=True)
            except Exception:
                pass

        reader_th = threading.Thread(target=_reader, daemon=True)
        reader_th.start()

        while True:
            rc = proc.poll()
            if rc is not None:
                try:
                    reader_th.join(timeout=1.0)
                except Exception:
                    pass
                return rc
            if _is_cancelled(cancel_event):
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
                try:
                    reader_th.join(timeout=1.0)
                except Exception:
                    pass
                return -2
            time.sleep(0.2)

    except Exception as e:
        print(f"⚠️ Ошибка запуска ffmpeg: {e}")
        return -1
    finally:
        if mux_acquired:
            try:
                _MUX_SEMA.release()
            except Exception:
                pass


from urllib.parse import quote_plus

from rapidfuzz import fuzz
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# берём готовые помощники из твоего kino_parser.py
from kino_parser import (
    ensure_login,
    find_portable_browser,
    make_visible_driver,
    safe_quit,
)
BASE_URL = "https://kino.pub"
# --- window helpers -----------------------------------------------------------
def _minimize(driver):
    try:
        wid = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})["windowId"]
        driver.execute_cdp_cmd(
            "Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}}
        )
    except Exception:
        try:
            driver.minimize_window()
        except Exception:
            pass

def _show_maximized(driver):
    if _running_inside_vscode():
        try:
            driver.maximize_window()
        except Exception:
            pass
        return
# --- окно и защита/капча -----------------------------------------------------
def _ensure_shown(driver):
    # Отключаем агрессивные манипуляции окнами, если внутри VS Code
    if _running_inside_vscode():
        try:
            driver.maximize_window()
        except Exception:
            pass
        return

    # как было раньше:
    _show_maximized(driver)
    try:
        info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        wid = info.get("windowId")
        driver.execute_cdp_cmd(
            "Browser.setWindowBounds",
            {"windowId": wid, "bounds": {"windowState": "normal", "left": 80, "top": 60, "width": 1280, "height": 900}},
        )
    except Exception:
        try:
            driver.set_window_position(80, 60)
            driver.set_window_size(1280, 900)
        except Exception:
            pass



def _has_challenge(driver) -> bool:
    """Есть ли капча/Cloudflare на странице."""
    try:
        url = (driver.current_url or "").lower()
        html = (driver.page_source or "").lower()
        title = (getattr(driver, "title", "") or "").lower()
    except Exception:
        return False

    # Важно: не используем общий needle "cloudflare", потому что он часто встречается на обычных страницах
    # (например, Cloudflare Insights beacon) и даёт ложные срабатывания.

    url_needles = (
        "/cdn-cgi/",
        "cf_chl",
        "cf-captcha",
        "challenge-platform",
        "chk_captcha",
        "chk_jschl",
    )
    if any(n in url for n in url_needles):
        return True

    title_needles = (
        "just a moment",
        "checking your browser",
        "attention required",
    )
    if any(n in title for n in title_needles):
        return True

    html_needles = (
        "/cdn-cgi/challenge-platform/",
        "cf-challenge",
        "cf_chl_",
        "cf-please-wait",
        "cf-turnstile",
        "challenges.cloudflare.com/turnstile/",
        "g-recaptcha",
        "hcaptcha",
        "data-sitekey",
        "just a moment",
        "checking your browser",
        "attention required",
    )
    if any(n in html for n in html_needles):
        return True
    try:
        return bool(
            driver.find_elements(By.CSS_SELECTOR, "iframe[src*='captcha'],iframe[src*='recaptcha']")
        )
    except Exception:
        return False


def _wait_challenge_solved(driver, timeout: int = 30) -> bool:
    """
    Если защита есть — показываем окно, ждём исчезновения до timeout.
    Возвращает True, если челлендж был и исчез; False — если не было (или висит).
    По завершении, когда челлендж исчез — сворачивает окно.
    """
    if not _has_challenge(driver):
        return False

    _ensure_shown(driver)
    t0 = time.time()
    while time.time() - t0 < timeout:
        # иногда после решения CF страница перезагружается — дадим ей дойти до complete
        try:
            ready = driver.execute_script("return document.readyState") == "complete"
        except Exception:
            ready = False

        if ready and not _has_challenge(driver):
            _minimize(driver)
            return True

        time.sleep(0.8)

    # челлендж не ушёл — оставим окно раскрытым, чтобы пользователь мог дорешать
    return False


def _driver_is_suppressed(driver) -> bool:
    """
    Драйвер запущен в suppress=True (окно агрессивно прячется хуками win32),
    поэтому показать его пользователю для решения Cloudflare невозможно.
    """
    try:
        return bool(getattr(driver, "_win_event_hooks", None))
    except Exception:
        return False


def _solve_cloudflare_in_visible_browser(url: str, *, status_cb=None, timeout: int = 180) -> bool:
    """
    Открывает видимый браузер (профиль login), чтобы пользователь прошёл Cloudflare/капчу.
    После успешного прохождения сохраняет cookies в файл.
    """
    with _CF_SOLVE_LOCK:
        try:
            if status_cb:
                status_cb("🌐 Открываю браузер для прохождения Cloudflare…")
        except Exception:
            pass

        drv = None
        try:
            # ВАЖНО: импорт внутри, чтобы не ловить циклические импорты (uc_driver -> kino_hls).
            from uc_driver import _safe_get_driver, KINOPUB_BASE
            from kino_parser import load_cookies, save_cookies, safe_quit

            drv = _safe_get_driver(
                status_cb=status_cb,
                suppress=False,              # окно ДОЛЖНО быть видимым
                profile_tag="login",         # постоянный профиль: легче пройти CF один раз
                preload_kino_cookies=False,  # для login профиля подгрузим сами
            )

            # Подгружаем cookies (если есть) и обновляем страницу
            try:
                drv.get(KINOPUB_BASE + "/")
            except Exception:
                pass
            try:
                load_cookies(drv)
                drv.refresh()
            except Exception:
                pass

            try:
                drv.get(url)
            except Exception:
                pass

            _wait_challenge_solved(drv, timeout=int(timeout))
            if _has_challenge(drv):
                try:
                    if status_cb:
                        status_cb("❌ Защита Cloudflare не пройдена (таймаут)")
                except Exception:
                    pass
                return False

            try:
                save_cookies(drv)
            except Exception:
                pass

            try:
                if status_cb:
                    status_cb("✅ Cloudflare пройден, продолжаю…")
            except Exception:
                pass
            return True
        finally:
            try:
                if drv is not None:
                    safe_quit(drv)
            except Exception:
                try:
                    if drv is not None:
                        drv.quit()
                except Exception:
                    pass

FFMPEG_BIN = r"C:\Project\MovieYearFinder\ffmpeg\bin\ffmpeg.exe"
print("[FFMPEG USED]", FFMPEG_BIN)
# === Настройки битрейта итогового файла (HLS MUX) ===
# Общая целевая скорость потока, как в свойствах файла / плеере (кбит/с)
TARGET_TOTAL_KBPS = 6000        # здесь задаёшь нужный ОБЩИЙ битрейт

# Битрейт аудио (каждой дорожки), тоже в кбит/с
AUDIO_BITRATE_KBPS = 192

# Минимальный битрейт видео (на случай, если вдруг total < audio)
MIN_VIDEO_BITRATE_KBPS = 1000
# Флаг: перекодировать ли видео при MUX (NVENC) или только ремультиплексировать
ENABLE_REENCODE = True


def set_reencode(enabled: bool):
    """Глобальная настройка: включить/выключить перекодирование при MUX."""
    global ENABLE_REENCODE
    ENABLE_REENCODE = bool(enabled)



def _inject_m3u8_sniffer_js(driver):
    """Инъекция перехватчика fetch/XHR, сохраняет URL .m3u8 в window.__m3u8"""
    js = r"""
    (function(){
      try {
        if (window.__m3u8Hooked) return;
        window.__m3u8Hooked = true;
        window.__m3u8 = [];

        function pushUrl(u){
          try {
            if (typeof u === 'string' && u.includes('.m3u8') && window.__m3u8.indexOf(u) === -1){
              window.__m3u8.push(u);
            }
          } catch (e) {}
        }

        // fetch
        if (window.fetch) {
          const _fetch = window.fetch;
          window.fetch = function(){
            try { pushUrl(arguments[0]); } catch(e){}
            return _fetch.apply(this, arguments).then(function(resp){
              try { pushUrl(resp && resp.url); } catch(e){}
              return resp;
            });
          };
        }

        // XHR
        const _open = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url){
          try { pushUrl(url); } catch(e){}
          return _open.apply(this, arguments);
        };
      } catch (e) {}
    })();
    """
    try:
        driver.execute_script(js)
    except Exception:
        pass


def _dismiss_overlays(driver):
    # частые кнопки согласия/баннеры
    xpaths = [
        "//button[contains(., 'Принять') or contains(., 'Согласен')]",
        "//button[contains(., 'Принять все') or contains(., 'Agree')]",
        "//div[contains(@class,'cookie')]//button",
        "//div[contains(@class,'consent')]//button",
        "//button[contains(@class,'close') or contains(@class,'btn-close')]",
    ]
    for xp in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xp)
            if els:
                driver.execute_script("arguments[0].click()", els[0])
                time.sleep(0.2)
        except Exception:
            pass


def _read_m3u8_from_sniffer(driver) -> str | None:
    """Возвращает самый «правдоподобный» найденный URL .m3u8 из window.__m3u8"""
    try:
        arr = driver.execute_script("return (window.__m3u8||[]).slice();")
        if arr:
            arr = [s for s in arr if isinstance(s, str)]
            arr.sort(key=len, reverse=True)
            return arr[0]
    except Exception:
        pass
    return None


def _maybe_into_player_iframe(driver) -> bool:
    try:
        for fr in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            src = (fr.get_attribute("src") or "").lower()
            if any(k in src for k in ("player", "embed", "video", "stream")):
                driver.switch_to.frame(fr)
                return True
    except Exception:
        pass
    return False


def _back_from_iframe(driver):
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


def _start_playback(driver):
    # если на странице защита — покажем окно и подождём решения
    if _has_challenge(driver):
        _ensure_shown(driver)
        _wait_challenge_solved(driver, timeout=15)

    _dismiss_overlays(driver)

    in_iframe = _maybe_into_player_iframe(driver)

    # 1) клики по кнопкам play
    selectors = [
        ".vjs-big-play-button, .vjs-play-control",
        ".jw-display .jw-icon-play, .jw-icon-playback",
        ".plyr__control.plyr__control--overlaid",
        "button[aria-label*='Play'], button[title*='Play']",
        "[class*='big-play'], [class*='icon-play'], .fa-play",
        "div[class*='play'] svg, button[class*='play']",
    ]
    clicked = False
    for css in selectors:
        try:
            el = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.CSS_SELECTOR, css)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            driver.execute_script("arguments[0].click()", el)
            clicked = True
            break
        except Exception:
            continue

    # 2) клик в геометрический центр
    if not clicked:
        try:
            target = None
            for css in ("video", ".jw-media video", ".plyr__video-wrapper video", ".vjs-tech"):
                els = driver.find_elements(By.CSS_SELECTOR, css)
                if els:
                    target = els[0]
                    break
            if target is None:
                for css in (".jwplayer", ".plyr", ".video-js", ".player, .player__container"):
                    els = driver.find_elements(By.CSS_SELECTOR, css)
                    if els:
                        target = els[0]
                        break
            if target is not None:
                driver.execute_script(
                    """
                    const r = arguments[0].getBoundingClientRect();
                    const x = r.left + r.width/2, y = r.top + r.height/2;
                    const el = document.elementFromPoint(x, y);
                    if (el) el.click();
                """,
                    target,
                )
                clicked = True
        except Exception:
            pass

    # 3) программный play()
    try:
        driver.execute_script(
            """
            const v = document.querySelector('video');
            if (v) { v.muted = true; const p=v.play(); if (p && p.then) p.catch(()=>{}); }
        """
        )
    except Exception:
        pass

    time.sleep(0.8)
    if in_iframe:
        _back_from_iframe(driver)
    time.sleep(1.2)


RU_NUM_WORDS = {
    "0": "ноль",
    "1": "один",
    "2": "два",
    "3": "три",
    "4": "четыре",
    "5": "пять",
    "6": "шесть",
    "7": "семь",
    "8": "восемь",
    "9": "девять",
    "10": "десять",
}
EN_NUM_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
    "10": "ten",
}
ROMAN = {
    "2": "II",
    "3": "III",
    "4": "IV",
    "5": "V",
    "6": "VI",
    "7": "VII",
    "8": "VIII",
    "9": "IX",
    "10": "X",
}


def _strip_year(title: str) -> tuple[str, str | None]:
    m = re.search(r"\b(19|20)\d{2}\b", title)
    if m:
        year = m.group(0)
        base = (title[: m.start()] + title[m.end() :]).strip()
        return re.sub(r"\s+\(\d{4}\)\s*$", "", base).strip(), year
    return title, None

def _norm(s: str) -> str:
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = s.replace("²", "2")
    # привести все варианты "м2/m2/м 2/m 2" к m2
    s = re.sub(r"\b[мm]\s*[2]\b", "m2", s, flags=re.I)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _swap_digits_to_words(s: str):
    out = [s]
    t = s
    for d, w in RU_NUM_WORDS.items():
        t = re.sub(rf"\b{re.escape(d)}\b", w, t, flags=re.I)
    out.append(t)
    t2 = s
    for d, w in EN_NUM_WORDS.items():
        t2 = re.sub(rf"\b{re.escape(d)}\b", w, t2, flags=re.I)
    out.append(t2)
    t3 = s
    for d, r in ROMAN.items():
        t3 = re.sub(rf"\b{re.escape(d)}\b", r, t3, flags=re.I)
    out.append(t3)
    return out


def _swap_words_to_digits(s: str):
    out = [s]
    t = s
    for d, w in RU_NUM_WORDS.items():
        t = re.sub(rf"\b{w}\b", d, t, flags=re.I)
    out.append(t)
    t2 = s
    for d, w in EN_NUM_WORDS.items():
        t2 = re.sub(rf"\b{w}\b", d, t2, flags=re.I)
    out.append(t2)
    t3 = s
    rom2dig = {v: k for k, v in ROMAN.items()}
    for r, d in rom2dig.items():
        t3 = re.sub(rf"\b{r}\b", d, t3, flags=re.I)
    out.append(t3)
    return out


def kino_query_variants(title: str) -> list[str]:
    seen = set()
    out = []

    def add(x: str):
        x = re.sub(r"\s+", " ", x.strip())
        if not x:
            return
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)

    add(title)
    base, year = _strip_year(title)
    add(base)

    cleaned = _norm(base)
    add(cleaned)

    # 84 м2 (кириллица) — приоритет; остальные — запасные
    if re.search(r"\b\d+\s*m2\b", cleaned):
        add(cleaned.replace("m2", "м2"))  # «84 м2»
        add(re.sub(r"\s+", "", cleaned.replace("m2", "м2")))  # «84м2»
        add(re.sub(r"\s+", "", cleaned))  # «84m2»

    add(base.replace("м2", "м²").replace("m2", "м²"))

    for v in _swap_digits_to_words(cleaned):
        add(v)
    for v in _swap_words_to_digits(cleaned):
        add(v)

    # короткие числовые — добавляем вариант с пробелом
    for v in list(out):
        core = re.sub(r"[^\w]+", "", v)
        if core.isdigit() and len(core) <= 2 and not v.endswith(" "):
            add(v + " ")

    if year:
        add(f"{cleaned} {year}")
    return out


def _sniff_hls_with_cdp(driver, timeout: int = 25, cancel_event=None) -> tuple[str | None, dict | None]:
    try:
        driver.execute_cdp_cmd("Network.enable", {
            "maxTotalBufferSize": 100_000_000,
            "maxResourceBufferSize": 5_000_000
        })
    except Exception:
        pass

    ua = driver.execute_script("return navigator.userAgent;") or "Mozilla/5.0"
    cookies = "; ".join([f"{c['name']}={c['value']}" for c in driver.get_cookies()])
    referer = driver.current_url

    def _mk_headers():
        return {"User-Agent": ua, "Referer": referer, "Cookie": cookies}

    candidates = set()
    perf_dump = []
    t0 = time.time()

    def consider(url: str):
        if not isinstance(url, str):
            return
        u = url.strip()
        if not u:
            return
        # 1) прямые m3u8
        if ".m3u8" in u:
            candidates.add(u)
        # 2) ping.gif?mu=<encoded m3u8>
        m = re.search(r"[?&]mu=([^&]+)", u)
        if m:
            try:
                decoded = urllib.parse.unquote(m.group(1))
                if ".m3u8" in decoded:
                    candidates.add(decoded)
            except Exception:
                pass

    while time.time() - t0 < timeout:
        if _is_cancelled(cancel_event):
            return None, None
        # perf log
        try:
            logs = driver.get_log("performance")
        except Exception:
            logs = []
        for entry in logs:
            try:
                perf_dump.append(entry.get("message", ""))
                msg = json.loads(entry.get("message", "{}")).get("message", {})
                params = msg.get("params", {})
                if "request" in params:
                    consider(params["request"].get("url", ""))
                if "response" in params:
                    r = params["response"]
                    consider(r.get("url", ""))
                    if (r.get("mimeType") or "").lower().find("mpegurl") >= 0:
                        consider(r.get("url", ""))
            except Exception:
                continue

        # performance.getEntries()
        try:
            urls = driver.execute_script("""
                try {
                    const es = performance.getEntries() || [];
                    return es.map(e => e.name);
                } catch(e){ return []; }
            """) or []
            for u in urls:
                consider(u)
        except Exception:
            pass

        time.sleep(0.2)

    # JS-хук (если был)
    try:
        if driver.execute_script("return !!window.__m3u8Hooked;"):
            arr = driver.execute_script("return (window.__m3u8||[]).slice();") or []
            for u in arr:
                consider(u)
    except Exception:
        pass

    # поиск в HTML
    try:
        html = driver.page_source
        for m in re.finditer(r'https?://[^"\']+\.m3u8[^"\']*', html, re.I):
            consider(m.group(0))
        # и возможные mu= в html
        for m in re.finditer(r'(?:\?|&)mu=([^&]+)', html, re.I):
            try:
                decoded = urllib.parse.unquote(m.group(1))
                consider(decoded)
            except Exception:
                pass
    except Exception:
        pass

    # записать хвост перф-лога на всякий
    if perf_dump:
        try:
            with open("perf_log.txt", "w", encoding="utf-8") as f:
                for ln in perf_dump[-2000:]:
                    f.write(ln + "\n")
        except Exception:
            pass

    if not candidates:
        return None, None

    # --- Выбираем лучший master ---
    def score(u: str) -> int:
        s = 0
        if "/hls4/" in u: s += 100
        if "/master.m3u8" in u: s += 10
        if ".mp4/master.m3u8" in u: s -= 20
        host = urllib.parse.urlsplit(u).netloc
        if "ams-static" in host: s -= 5
        if "cdn2" in host or "cdn" in host: s += 5
        s += min(40, len(u)//50)  # длиннее путь — часто «старший» уровень
        return s

    # проверим топ-5 по score и возьмём тот, у кого больше вариантов/выше RESOLUTION
    ranked = sorted(candidates, key=score, reverse=True)[:5]
    best_url, best_n, best_h = None, -1, 0
    headers = _mk_headers()
    for u in ranked:
        try:
            if _is_cancelled(cancel_event):
                return None, None
            txt = _http_get_text(u, headers, driver=driver, cancel_event=cancel_event)
            lines = txt.splitlines()
            n = sum(1 for ln in lines if ln.startswith("#EXT-X-STREAM-INF"))
            hmax = 0
            for ln in lines:
                m = re.search(r"RESOLUTION=\d+x(\d+)", ln)
                if m:
                    hmax = max(hmax, int(m.group(1)))
            if (n > best_n) or (n == best_n and hmax > best_h):
                best_url, best_n, best_h = u, n, hmax
        except DownloadCancelled:
            return None, None
        except Exception:
            continue

    # последний шанс — просто лучший по рангу
    master = best_url or (ranked[0] if ranked else None)
    return (master, headers) if master else (None, None)



def _http_get_text(url: str, headers: dict, *, driver=None, cancel_event=None) -> str:
    """
    Надёжная загрузка текста плейлиста:
      1) пробуем через JS fetch в браузере (если driver есть);
      2) затем много попыток через urllib с гибким SSL-контекстом и ретраями до 200 OK;
      3) если всё равно не удалось — ещё одна попытка браузером и осмысленная ошибка.
    """
    _raise_if_cancelled(cancel_event)

    # 0) Нормализуем заголовки один раз
    _, hdict = _augment_headers(headers)

    # 1) сначала через браузер (если доступен)
    if driver is not None:
        _raise_if_cancelled(cancel_event)
        t = _http_get_text_via_browser(driver, url, hdict)
        if isinstance(t, str) and t:
            return t

    # 2) urllib c устойчивым SSL-контекстом + ретраи
    import ssl, urllib.error

    max_tries = 8          # фактически "до тех пор", но с предохранителем
    last_err = None

    # Гибкий SSL (TLS1.2/1.3, игнор неожиданных EOF, ослабленный seclevel)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except Exception:
        pass
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

    req = urllib.request.Request(url, headers=hdict or {})

    for attempt in range(1, max_tries + 1):
        _raise_if_cancelled(cancel_event)
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                status = getattr(r, "status", 200)
                body = r.read().decode("utf-8", "ignore")
                if 200 <= status < 300:
                    return body
                # не 2xx — считаем ошибкой и ретраим
                last_err = RuntimeError(f"HTTP {status}")
        except urllib.error.HTTPError as e:
            # 4xx/5xx — CDN может "передумать", продолжаем ретраи
            last_err = e
        except urllib.error.URLError as e:
            last_err = e
        except Exception as e:
            last_err = e

        # backoff, но с верхней границей, чтобы не уходить в космос
        _raise_if_cancelled(cancel_event)
        time.sleep(min(0.3 * attempt, 3.0))

    # 3) финальный резерв — ещё одна попытка браузером
    if driver is not None:
        _raise_if_cancelled(cancel_event)
        t = _http_get_text_via_browser(driver, url, hdict)
        if isinstance(t, str) and t:
            return t

    # если сюда дошли — поднимем осмысленную ошибку (чтобы видеть первопричину)
    raise RuntimeError(f"_http_get_text failed for {url}: {last_err}")



def _http_get_text_via_browser(driver, url: str, headers: dict) -> str | None:
    """
    Скачиваем текст через JS fetch в контексте браузера.
    Возвращаем текст или None. Теперь выводит полную HTTP-ошибку.
    """
    js = """
    const url = arguments[0];
    const headers = arguments[1] || {};
    const cb = arguments[2];
    fetch(url, { headers: headers, credentials: 'include' })
      .then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(t => cb([true, t]))
      .catch(e => cb([false, String(e)]));
    """
    try:
        ok, payload = driver.execute_async_script(js, url, headers or {})
        return payload if ok else None
    except Exception:
        return None


def _normalize_to_master(m3u8_url: str) -> str:
    if not m3u8_url:
        return m3u8_url

    parts = urllib.parse.urlsplit(m3u8_url)
    path = parts.path

    # Уже мастер? — вернём как есть
    if path.endswith("/master.m3u8"):
        return m3u8_url

    # Случай: .../index[-v1...].m3u8  -> .../master.m3u8 (сохраняем слэш)
    path2 = re.sub(r"(/)(?:index-[^/]+|index|playlist)\.m3u8$", r"\1master.m3u8", path)
    if path2 != path:
        path = path2
    else:
        # Случай: .../something.mp4/index[-v1...].m3u8 -> .../something.mp4/master.m3u8
        path2 = re.sub(r"(/)(?:index-[^/]+|index|playlist)\.m3u8$", r"\1master.m3u8", path)
        path = path2

    # Если вдруг путь оканчивается просто на .mp4 — добавим /master.m3u8
    if path.endswith(".mp4"):
        path = path + "/master.m3u8"

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _select_video_and_audios(driver, master_url: str, headers: dict, cancel_event=None):
    """
    Возвращает: (video_m3u8, [ {uri,name,lang,default}, ... ])
    Надёжный разбор master с внешними аудио. При отсутствии 1080p пробует
    подняться на верхний master в пределах /hls/.
    """
    _raise_if_cancelled(cancel_event)
    text = _http_get_text(master_url, headers, driver=driver, cancel_event=cancel_event)
    if not text:
        print("❌ Не удалось загрузить master.m3u8")
        return master_url, headers, []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # --- Аудио-группы ---
    audio_groups = {}
    for ln in lines:
        if ln.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in ln:
            def _get(rx, default=None):
                m = re.search(rx, ln)
                return m.group(1) if m else default
            gid  = _get(r'GROUP-ID="([^"]+)"')
            uri  = _get(r'URI="([^"]+)"')
            name = _get(r'NAME="([^"]+)"', "")
            lang = _get(r'LANGUAGE="([^"]+)"', "")
            dflt = _get(r"DEFAULT=(YES|NO)", "NO") == "YES"
            if gid and uri:
                abs_uri = urllib.parse.urljoin(master_url, uri)
                audio_groups.setdefault(gid, []).append({
                    "uri": abs_uri,
                    "name": name or "Audio",
                    "lang": lang or "und",
                    "default": dflt,
                })

    # --- Варианты видео ---
    variants = []
    best_url, best_score, chosen_gid = None, -1, None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXT-X-STREAM-INF"):
            info = ln
            url_line = lines[i + 1] if i + 1 < len(lines) else None
            i += 2
            if not url_line:
                continue
            w = h = 0
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", info)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            bw = 0
            m = re.search(r"BANDWIDTH=(\d+)", info)
            if m:
                bw = int(m.group(1))
            m = re.search(r'AUDIO="([^"]+)"', info)
            gid = m.group(1) if m else None
            url_abs = urllib.parse.urljoin(master_url, url_line)
            score = (w * h) or bw
            variants.append((url_abs, w, h, bw, gid))
            if score > best_score:
                best_score, best_url, chosen_gid = score, url_abs, gid
        else:
            i += 1

    print("🔎 Найдены варианты:")
    for url_abs, w, h, bw, gid in variants:
        print(f"   {w}x{h} | {bw/1000:.0f} kbps | AUDIO={gid or '-'}")

    # --- Итоговые аудио ---
    audios = []
    if chosen_gid and chosen_gid in audio_groups:
        audios = audio_groups[chosen_gid]
    elif not chosen_gid and audio_groups:
        # если группа не указана, берём все
        for v in audio_groups.values():
            audios.extend(v)
    else:
        print("ℹ️ Аудио-группы не найдены (звук может быть в видеопотоке).")

    # приоритизируем RU и default
    def _prio(a):
        n = (a.get("name") or "").lower()
        lang = (a.get("lang") or "").lower()
        p = 0
        if any(k in n for k in ("рус", "russian", "rus")) or lang in ("ru", "rus"):
            p -= 100
        if a.get("default"):
            p -= 10
        return p
    audios.sort(key=_prio)

    # --- Если нет ~1080p, пробуем «верхний» master в /hls/ ---
    if all(h < 1000 for _, _, h, _, _ in variants) and "/hls/" in master_url:
        alt = re.sub(r"/\d+/[^/]+\.m3u8.*$", "/master.m3u8", master_url)
        if alt != master_url:
            print(f"🔁 Попробуем верхний master: {alt}")
            try:
                return _select_video_and_audios(driver, alt, headers, cancel_event=cancel_event)
            except DownloadCancelled:
                raise
            except Exception:
                pass

    return best_url or master_url, headers, audios


# ---------- поиски и скачивание ----------
def _split_title_variants(orig: str) -> list[str]:
    """
    Из 'Дочь / Una figlia (2025)' получаем варианты запроса:
    ['Дочь (2025)', 'Дочь', 'Una figlia (2025)', 'Una figlia']
    """
    t = (orig or "").strip()
    # убираем двойные пробелы
    t = re.sub(r"\s+", " ", t)
    # части до и после слеша
    parts = [p.strip() for p in re.split(r"[\/|]", t) if p.strip()]
    variants = []

    def no_year(s: str) -> str:
        return re.sub(r"\((19|20)\d{2}\)", "", s).strip()

    # исходник
    variants.append(t)
    variants.append(no_year(t))

    # альтернативные названия по обе стороны слеша
    if len(parts) >= 2:
        left, right = parts[0], parts[1]
        variants.extend([right, no_year(right), left, no_year(left)])

    # удаляем дубликаты, пустые и слишком короткие
    seen = set()
    out = []
    for v in variants:
        v = v.strip()
        if len(v) < 2:
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out

def _go_search_page(driver, query: str):
    """Надёжно открыть выдачу по запросу на kino.pub."""
    # 1) Прямой переход на страницу поиска (как на твоём скрине)
    try:
        driver.get(f"{BASE_URL}/item/search?query=" + quote_plus(query))
        # ждём либо карточки, либо заголовок «Найдено», либо «Нет результатов»
        WebDriverWait(driver, 10).until(
            lambda d: (
                d.find_elements(By.XPATH, "//a[contains(@href,'/item/view/')]")
                or "Найдено" in d.page_source
                or "Нет результатов" in d.page_source
            )
        )
        return
    except Exception:
        pass

    # 2) Резерв: через поле поиска + Enter
    driver.get(BASE_URL)
    WebDriverWait(driver, 20).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.2)

    # иногда поиск спрятан за иконкой
    for sel in (
        "button.search-toggle",
        "a.search-toggle",
        "[data-bs-target='#search']",
        ".navbar .icon-search",
        "button[aria-label*='Поиск']",
    ):
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click()", el)
            time.sleep(0.2)
            break
        except Exception:
            continue

    # пытаемся найти поле ввода
    inp = None
    for css in (
        "input[name='query']",
        "input[name='q']",
        "input[type='search']",
        "input[placeholder*='Название']",
        "input[placeholder*='имя']",
    ):
        try:
            inp = driver.find_element(By.CSS_SELECTOR, css)
            break
        except Exception:
            continue

    if inp:
        # важно сгенерировать input/change, а не только .send_keys
        driver.execute_script(
            """
            arguments[0].focus();
            arguments[0].value = "";
        """,
            inp,
        )
        inp.send_keys(query)
        # на некоторых сборках помогает диспатч событий
        driver.execute_script(
            """
            arguments[0].dispatchEvent(new Event('input', {bubbles:true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
        """,
            inp,
        )
        inp.send_keys(Keys.ENTER)

        try:
            WebDriverWait(driver, 10).until(
                lambda d: (
                    d.find_elements(By.XPATH, "//a[contains(@href,'/item/view/')]")
                    or "Найдено" in d.page_source
                    or "Нет результатов" in d.page_source
                )
            )
        except Exception:
            # последний резерв — прямой URL ещё раз
            driver.get(f"{BASE_URL}/item/search?query=" + quote_plus(query))


def search_first_item_url(driver, title_query: str) -> str | None:
    variants = _split_title_variants(title_query)
    wanted_year = _strip_year(title_query)[1]
    RESULT_XPATH = "//a[contains(@href,'/item/view/')]"

    for q in variants:
        try:
            core = re.sub(r"[^\w]+", "", q)

            # короткие числовые — пробуем автокомплит
            if core.isdigit() and len(core) <= 2:
                driver.get(BASE_URL)
                WebDriverWait(driver, 20).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                if _type_and_pick(driver, q, wanted_title=title_query, wanted_year=wanted_year):
                    return driver.current_url

            # обычная страница поиска
            _go_search_page(driver, q)

            if "/item/view/" in driver.current_url:
                return driver.current_url

            links = driver.find_elements(By.XPATH, RESULT_XPATH)
            if links:
                base = _strip_year(title_query)[0]
                base_n = _norm(base)
                want_m2 = re.search(r"\b(\d+)\s*m2\b", base_n)

                best_el, best_s = None, 0
                for a in links:
                    try:
                        txt = (a.text or "").strip()
                    except Exception:
                        continue

                    name = _strip_year(txt)[0]
                    cand_n = _norm(name)
                    s = fuzz.token_set_ratio(base_n, cand_n)

                    # приоритет «84 m2/м2»
                    if want_m2 and re.search(r"\b" + want_m2.group(1) + r"\s*m2\b", cand_n):
                        s += 12

                    # приоритет по году
                    m = re.search(r"(19|20)\d{2}", txt)
                    year = m.group(0) if m else None
                    if wanted_year and year:
                        if year == wanted_year:
                            s += 8
                        elif abs(int(year) - int(wanted_year)) == 1:
                            s += 3

                    if s > best_s:
                        best_s, best_el = s, a

                if best_el and best_s >= 60:  # чуть мягче порог
                    # Сначала пробуем по href (надёжнее, чем клик)
                    href = best_el.get_attribute("href")
                    if href:
                        driver.get(href)
                    else:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", best_el
                        )
                        driver.execute_script("arguments[0].click()", best_el)

                    WebDriverWait(driver, 10).until(lambda d: "/item/view/" in d.current_url)
                    return driver.current_url

            # fallback: ещё раз автокомплит для коротких запросов
            if len(core) <= 3:
                driver.get(BASE_URL)
                WebDriverWait(driver, 20).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                if _type_and_pick(driver, q, wanted_title=title_query, wanted_year=wanted_year):
                    return driver.current_url

        except Exception:
            continue


def download_by_item_url(
    url: str,
    out_path: str,
    driver=None,
    status_cb=None,
    cancel_event=None,
    audio_select_cb=None,
    defer_mux: bool = False,
    audio_parallel_tracks: int | None = None,
) -> bool:
    """
    Основная функция скачивания: использует существующий driver, не создавая новое окно Chrome.
    """
    drv_created = False
    drv = driver

    # если драйвер не передан — создаём новый (редкий случай)
    if drv is None:
        from kino_parser import find_portable_browser, make_visible_driver
        portable, ver_main = find_portable_browser()
        drv = make_visible_driver(portable_path=portable, ver_main=ver_main, for_login=False)
        drv_created = True
        print("🚀 Создан новый экземпляр браузера (UC).")
    else:
        print("🔁 Используется уже запущенный driver (из downloader).")

    try:
        _raise_if_cancelled(cancel_event)
        # только если мы сами создали — можно показывать окно
        if drv_created:
            _ensure_shown(drv)

        # --- ЕДИНСТВЕННЫЙ вызов, без двойной нагрузки ---
        video_m3u8, hdrs2, audios = get_hls_info(
            url,
            driver=drv,
            cancel_event=cancel_event,
            status_cb=status_cb,
        )

        if not video_m3u8:
            print("❌ Не удалось получить HLS.")
            return False

        print(f"🎞  Video: {video_m3u8}")
        if audios:
            print("🎧 Audios:")
            for a in audios:
                print(f"   - {a.get('name') or 'Unknown'} [{a.get('lang') or '?'}]  {a['uri']}")
        else:
            print("🎧 Отдельные аудио не найдены (возможно звук в видеопотоке).")

        ok = start_hls_download(
            video_m3u8,
            audios,
            hdrs2.copy(),
            out_path,
            status_cb=status_cb,
            cancel_event=cancel_event,
            audio_select_cb=audio_select_cb,
            defer_mux=defer_mux,
            audio_parallel_tracks=audio_parallel_tracks,
        )
        return bool(ok)

    except DownloadCancelled:
        return False

    finally:
        if drv_created:
            from kino_parser import safe_quit
            safe_quit(drv)
def get_hls_info(
    url: str,
    driver=None,
    cancel_event=None,
    status_cb=None,
    sniff_timeout: int = 15,
) -> tuple[str | None, dict | None, list]:
    """
    Возвращает (video_m3u8, headers, audios) для данного item_url.
    Не запускает ffmpeg, просто извлекает ссылки.
    """
    if driver is None:
        raise RuntimeError("get_hls_info() требует активный driver (UC).")

    def _log_local(msg: str):
        try:
            print(msg)
        except Exception:
            pass
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    def _ensure_cf_solved() -> bool:
        # Cloudflare/капча: для suppress-драйвера открываем отдельное видимое окно.
        if not _has_challenge(driver):
            return True

        _log_local("🧩 Обнаружена защита (Cloudflare) — решите в открытом браузере…")

        # 1) если драйвер умеет показываться — ждём прямо в нём
        if not _driver_is_suppressed(driver):
            _wait_challenge_solved(driver, timeout=90)
            return not _has_challenge(driver)

        # 2) suppress-драйвер не показать: возможно, другой поток уже обновил cookies — пробуем подгрузить/refresh
        try:
            from kino_parser import load_cookies

            load_cookies(driver)
            driver.refresh()
        except Exception:
            pass

        if not _has_challenge(driver):
            return True

        # 3) открываем видимый браузер, сохраняем cookies, обновляем текущий драйвер
        ok = _solve_cloudflare_in_visible_browser(url, status_cb=status_cb, timeout=180)
        if not ok:
            return False

        try:
            from kino_parser import load_cookies

            load_cookies(driver)
            driver.refresh()
        except Exception:
            pass

        return not _has_challenge(driver)

    _raise_if_cancelled(cancel_event)
    driver.get(url)

    # CF/капча: ждём и не продолжаем, пока защита не пройдена (иначе получим «не найден m3u8»)
    if not _ensure_cf_solved():
        _log_local("❌ Защита Cloudflare не пройдена (таймаут)")
        return None, None, []

    try:
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass

    try:
        if "/user/login" in (driver.current_url or "").lower():
            _log_local("❌ Требуется вход в kino.pub")
            return None, None, []
    except Exception:
        pass

    master, hdrs = None, None
    # Небольшой внутренний ретрай: иногда CDP/хуки не успевают поймать запросы сразу.
    for attempt in range(1, 3):
        _raise_if_cancelled(cancel_event)
        if attempt > 1:
            _log_local(f"♻️ Повтор анализа HLS ({attempt}/2)…")
            try:
                driver.refresh()
            except Exception:
                try:
                    driver.get(url)
                except Exception:
                    pass

            if not _ensure_cf_solved():
                _log_local("❌ Защита Cloudflare не пройдена (таймаут)")
                return None, None, []

            try:
                WebDriverWait(driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass

        _inject_m3u8_sniffer_js(driver)
        _start_playback(driver)
        # даём плееру время дернуть master.m3u8
        for _ in range(12):
            _raise_if_cancelled(cancel_event)
            time.sleep(0.2)

        master, hdrs = _sniff_hls_with_cdp(
            driver, timeout=max(8, int(sniff_timeout)), cancel_event=cancel_event
        )
        if master:
            break

    if master:
        master = _normalize_to_master(master)
        master = master.replace(".mp4master.m3u8", ".mp4/master.m3u8")
        print(f"🛠️ Нормализовано: {master}")
    else:
        _log_local("❌ Не найден master.m3u8")
        return None, None, []

    video_m3u8, hdrs2, audios = _select_video_and_audios(driver, master, hdrs, cancel_event=cancel_event)
    return video_m3u8, hdrs2, audios

def _type_and_pick(driver, query: str, wanted_title: str, wanted_year: str | None):
    # поле поиска
    search = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[name='query'], input[type='search'], input[name='q']")
        )
    )
    driver.execute_script("arguments[0].value=''; arguments[0].focus();", search)
    search.send_keys(query)

    # короткие/цифровые — пробел, чтобы гарантировать автокомплит
    core = re.sub(r"[^\w]+", "", query)
    if core.isdigit() and len(core) <= 2:
        search.send_keys(Keys.SPACE)

    # 1) пробуем подсказки
    try:
        box = WebDriverWait(driver, 6).until(
            EC.visibility_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "div.search-suggest, ul[role='listbox'], .autocomplete, .typeahead, .ui-autocomplete, .dropdown-menu",
                )
            )
        )
        # ищем кликабельные элементы
        items = []
        for css in ("a[href*='/item/view/']", "li a[href]", "a[href]"):
            items = box.find_elements(By.CSS_SELECTOR, css)
            if items:
                break

        if items:
            idx = _best_match_index(items, wanted_title, wanted_year)
            if idx is None:
                idx = 0

            # сначала пытаемся JS-клик
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", items[idx])
                driver.execute_script("arguments[0].click()", items[idx])
                WebDriverWait(driver, 8).until(lambda d: "/item/view/" in d.current_url)
                return True
            except Exception:
                # если кликом не ушли — берём href и переходим напрямую
                href = items[idx].get_attribute("href")
                if href:
                    driver.get(href)
                    WebDriverWait(driver, 10).until(lambda d: "/item/view/" in d.current_url)
                    return True
    except Exception:
        pass

    # 2) fallback: страница поиска и первая карточка
    try:
        driver.get(f"{BASE_URL}/item/search?query=" + quote_plus(query))
        WebDriverWait(driver, 12).until(
            lambda d: d.find_elements(By.XPATH, "//a[contains(@href,'/item/view/')]")
            or "Нет результатов" in d.page_source
        )
        links = driver.find_elements(By.XPATH, "//a[contains(@href,'/item/view/')]")
        if links:
            href = links[0].get_attribute("href")
            if href:
                driver.get(href)
                WebDriverWait(driver, 10).until(lambda d: "/item/view/" in d.current_url)
                return True
    except Exception:
        pass

    # 3) последний резерв: Enter по полю поиска
    try:
        search.send_keys(Keys.ENTER)
        WebDriverWait(driver, 10).until(
            lambda d: "/item/search" in d.current_url or "/item/view/" in d.current_url
        )
        if "/item/search" in driver.current_url:
            links = driver.find_elements(By.XPATH, "//a[contains(@href,'/item/view/')]")
            if links:
                href = links[0].get_attribute("href")
                if href:
                    driver.get(href)
                    WebDriverWait(driver, 10).until(lambda d: "/item/view/" in d.current_url)
                    return True
        return "/item/view/" in driver.current_url
    except Exception:
        return False

def _best_match_index(elements, wanted_title: str, wanted_year: str | None):
    base = _strip_year(wanted_title)[0]
    base_n = _norm(base)
    best_i, best_s = None, 0

    for i, el in enumerate(elements):
        try:
            txt = el.text.strip()
        except Exception:
            continue
        # Вытащим год из строки
        m = re.search(r"(19|20)\d{2}", txt)
        year = m.group(0) if m else None
        name = _strip_year(txt)[0]
        want_m2 = re.search(r"\b(\d+)\s*m2\b", base_n)
        cand_n = _norm(name)
        score = fuzz.token_set_ratio(base_n, cand_n)
        if want_m2 and re.search(r"\b" + want_m2.group(1) + r"\s*m2\b", cand_n):
            score += 12
        # приоритет году
        if wanted_year and year:
            if year == wanted_year:
                score += 8
            elif abs(int(year) - int(wanted_year)) == 1:
                score += 3
        if score > best_s:
            best_s, best_i = score, i

    return best_i if (best_i is not None and best_s >= 70) else None

def search_and_download(title: str, out_path: str) -> bool:
    """
    Полный цикл: варианты запроса → карточка → плеер → master.m3u8 → 1080p + все аудио → ffmpeg (copy).
    """
    portable, ver_main = find_portable_browser()
    drv = make_visible_driver(portable_path=portable, ver_main=ver_main, for_login=False)

    try:
        _ensure_shown(drv)  # НЕ сворачиваем — пусть окно будет видно, если всплывёт CF/капча

        # 1) ищем URL карточки
        url = None
        for q in kino_query_variants(title):
            try:
                url = search_first_item_url(drv, q)
                if url:
                    print(f"[KINO] Нашли по запросу: {q} -> {url}")
                    break
            except Exception as e:
                print(f"[KINO] Ошибка поиска по '{q}': {e}")

        if not url:
            print(f"❌ Ничего не найдено по запросу: {title}")
            return False

        # 2) открываем карточку и готовим плеер
        drv.get(url)
        _wait_challenge_solved(drv, timeout=10 )

        WebDriverWait(drv, 25).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        _inject_m3u8_sniffer_js(drv)
        _start_playback(drv)
        time.sleep(2)

        # 3) master.m3u8 + заголовки
        master, hdrs = _sniff_hls_with_cdp(drv, timeout=10)
        if not master:
            print("❌ HLS master.m3u8 не найден.")
            return False

        master = _normalize_to_master(master)
        master = master.replace(".mp4master.m3u8", ".mp4/master.m3u8")
        print(f"🛠️ Нормализовано: {master}")
        video_m3u8, hdrs2, audios = _select_video_and_audios(drv, master, hdrs)



        if not video_m3u8:
            print("❌ Не удалось выбрать видео-вариант.")
            return False

        print(f"🎞  Video: {video_m3u8}")
        if audios:
            print("🎧 Audios:")
            for a in audios:
                print(f"   - {a.get('name') or 'Unknown'} [{a.get('lang') or '?'}]  {a['uri']}")
        else:
            print("🎧 Отдельные аудио не найдены (возможно, звук в видеопотоке).")

    finally:
        safe_quit(drv)

def search_and_download_with_driver(
    drv, title: str, out_path: str, sniff_timeout: int = 40
) -> bool:
    """То же, что search_and_download, но переиспользует уже созданный драйвер."""
    try:
        # 1) ищем карточку
        url = None
        for q in kino_query_variants(title):
            try:
                url = search_first_item_url(drv, q)
                if url:
                    print(f"[KINO] Нашли по '{q}' -> {url}")
                    break
            except Exception as e:
                print(f"[KINO] Ошибка поиска '{q}': {e}")
        if not url:
            print(f"❌ Ничего не найдено по: {title}")
            return False

        # 2) открываем карточку и готовим плеер
        drv.get(url)
        _wait_challenge_solved(drv, timeout=15)
        WebDriverWait(drv, 35).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        _inject_m3u8_sniffer_js(drv)
        _start_playback(drv)
        time.sleep(2.0)

        # 3) master.m3u8 + заголовки
        master, hdrs = _sniff_hls_with_cdp(drv, timeout=sniff_timeout)
        if not master:
            print(f"[KINO] master.m3u8 не найден: {title}")
            return False

        # 4) видео 1080p + все аудио
        video_m3u8, hdrs2, audios = _select_video_and_audios(drv, master, hdrs)
        if not video_m3u8:
            print(f"[KINO] Не удалось выбрать видео-вариант: {title}")
            return False

        # 5) ffmpeg: сначала все дорожки; если не вышло — только видео
        start_hls_download(video_m3u8, audios, hdrs.copy(), out_path)
        return True
    except Exception as e:
        print(f"[KINO] Исключение при скачке '{title}': {e}")
        return False

# ---------- пакетная закачка (параллельно) ----------
def batch_search_and_download(
    titles: list[str],
    out_dir: str,
    max_parallel: int = 2,
    retries: int = 2,
    sleep_range: tuple[float, float] = (1.6, 2.5),
    cooldown_every: int = 10,
    cooldown_sec_range: tuple[float, float] = (12.0, 20.0),
):
    """
    Качает список titles параллельно (max_parallel потоков).
    На каждый тайтл делает до `retries` попыток. Между попытками — случайная пауза.
    Возвращает два словаря: (ok, fail), где ok[title] = путь, fail[title] = "последняя ошибка/причина".
    """
    import os
    import re
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    os.makedirs(out_dir, exist_ok=True)

    def _safe_name(t: str) -> str:
        s = re.sub(r'[\\/:*?"<>|]', " - ", t).strip()
        return s if s.lower().endswith(".mp4") else s + ".mp4"

    ok, fail = {}, {}

    def _one(title: str) -> tuple[str, bool, str | None]:
        # 2 варианта запроса: очищенный и исходный
        q_clean = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", title).strip()
        queries = [q_clean] if q_clean == title else [q_clean, title]

        out_path = os.path.join(out_dir, _safe_name(title))
        last_err = None

        for q in queries:
            for attempt in range(1, retries + 1):
                try:
                    time.sleep(random.uniform(*sleep_range))
                    if search_and_download(q, out_path):
                        return (title, True, out_path)
                except Exception as e:
                    last_err = str(e)
                # backoff между попытками
                time.sleep(0.8 * attempt)

        return (title, False, last_err or "download failed")

    # Основной проход — параллельно
    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = [ex.submit(_one, t) for t in titles]
        for idx, fut in enumerate(as_completed(futures), 1):
            t, ok_flag, info = fut.result()
            if ok_flag:
                ok[t] = info
            else:
                fail[t] = info

            # Длинная передышка каждые N фильмов
            if cooldown_every > 0 and idx % cooldown_every == 0 and idx < len(titles):
                time.sleep(random.uniform(*cooldown_sec_range))

    return ok, fail
import threading

import threading
import shutil



# -------------------------------------------------------------------------
#  НАДЁЖНЫЙ PYTHON HLS DOWNLOADER (замена ffmpeg-download)
# -------------------------------------------------------------------------
import concurrent.futures

def _http_download(url: str, headers: dict, attempt=1, max_tries=50, cancel_event=None):
    """Скачивание сегмента с жесткими ретраями до 200 OK."""
    _raise_if_cancelled(cancel_event)
    _, hdict = _augment_headers(headers)
    import ssl, urllib.error

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try: ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except: pass
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

    req = urllib.request.Request(url, headers=hdict)
    last_err = None
    last_code = None
    forbidden_streak = 0

    for i in range(1, max_tries + 1):
        _raise_if_cancelled(cancel_event)
        try:
            with _HLS_SEGMENT_SEMA:
                with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                    code = int(getattr(r, "status", 200) or 0)
                    if 200 <= code < 300:
                        return r.read()
                    last_code = code
                    last_err = RuntimeError(f"HTTP {code}")
        except urllib.error.HTTPError as e:
            last_code = int(getattr(e, "code", 0) or 0)
            last_err = e
        except Exception as e:
            last_code = None
            last_err = e

        _raise_if_cancelled(cancel_event)

        # если постоянно 401/403 — токен/куки могли протухнуть, бессмысленно ждать 50 попыток
        if last_code in (401, 403):
            forbidden_streak += 1
        else:
            forbidden_streak = 0

        if forbidden_streak >= 8:
            break

        # backoff + jitter (429/5xx дольше)
        base_sleep = 0.25 * i
        if last_code == 429:
            base_sleep = 0.8 * i
            try:
                ra = getattr(last_err, "headers", None)
                ra = ra.get("Retry-After") if ra else None
                if ra:
                    base_sleep = max(base_sleep, float(ra))
            except Exception:
                pass
        elif isinstance(last_code, int) and last_code >= 500:
            base_sleep = 0.6 * i

        time.sleep(min(base_sleep, 6.0) + random.uniform(0.0, 0.35))

    raise RuntimeError(f"SEGMENT FAIL ({last_code or last_err}): {url}")


def _download_hls_stream(m3u8_url: str, headers: dict, out_path: str,
                          status_cb=None, label="Видео", workers=8, cancel_event=None):
    """
    Скачивает HLS-видео/аудио в mp4, БЕЗ ffmpeg.
    """
    print(f"⬇️ {label}")
    if status_cb:
        status_cb(f"⬇️ {label}")

    if _is_cancelled(cancel_event):
        return False
    try:
        text = _http_get_text(m3u8_url, headers, cancel_event=cancel_event)
    except DownloadCancelled:
        return False
    base = m3u8_url.rsplit("/", 1)[0]

    segments = []
    for ln in text.splitlines():
        if _is_cancelled(cancel_event):
            return False
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            segments.append(urllib.parse.urljoin(m3u8_url, ln))

    if not segments:
        print("❌ Нет сегментов!")
        return False

    # скачиваем параллельно (с кооперативной отменой), но пишем на диск ПО МЕРЕ готовности,
    # чтобы не держать весь файл в RAM (важно для стабильности при параллельных загрузках).
    total = len(segments)
    done_cnt = 0
    last_pct = -1
    last_ts = 0.0
    start_ts = time.time()
    bytes_done = 0

    def _fmt_speed(bps: float) -> str:
        try:
            bps = float(bps)
        except Exception:
            bps = 0.0
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f}MB/s"
        if bps >= 1024:
            return f"{bps / 1024:.0f}KB/s"
        return f"{bps:.0f}B/s"

    def _fmt_eta(seconds: float | None) -> str:
        try:
            if seconds is None:
                return ""
            seconds = int(max(0, float(seconds) + 0.5))
        except Exception:
            return ""
        if seconds <= 0:
            return "00:00"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def load(i, url):
        chunk = _http_download(url, headers, cancel_event=cancel_event)
        return i, chunk

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    pending = set()
    shutdown_wait = True
    tmp = out_path + ".part"
    try:
        for i, seg_url in enumerate(segments):
            _raise_if_cancelled(cancel_event)
            pending.add(ex.submit(load, i, seg_url))

        pending_chunks = {}
        next_write = 0

        with open(tmp, "wb") as f_out:
            while pending:
                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=0.2,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                for fut in done:
                    try:
                        idx, chunk = fut.result()
                    except Exception:
                        # если исключение в сегменте — пробросится ниже
                        fut.result()
                        continue
                    pending_chunks[int(idx)] = chunk
                    try:
                        bytes_done += int(len(chunk) if chunk is not None else 0)
                    except Exception:
                        pass

                done_cnt += len(done)

                # пишем на диск строго по порядку сегментов
                while next_write in pending_chunks:
                    _raise_if_cancelled(cancel_event)
                    f_out.write(pending_chunks.pop(next_write))
                    next_write += 1

                if status_cb and total > 0:
                    try:
                        pct = int(done_cnt * 100 / total)
                        pct = max(0, min(100, pct))
                        now = time.time()
                        if pct >= 100 or pct - last_pct >= 5 or (now - last_ts) >= 1.2:
                            last_pct = pct
                            last_ts = now
                            speed = ""
                            eta_txt = ""
                            try:
                                elapsed = max(0.25, now - start_ts)
                                speed = _fmt_speed(bytes_done / elapsed) if bytes_done > 0 else ""
                                if 0 < done_cnt < total:
                                    eta_txt = _fmt_eta((total - done_cnt) * (elapsed / max(1, done_cnt)))
                            except Exception:
                                speed = ""
                                eta_txt = ""
                            if speed:
                                if eta_txt:
                                    status_cb(f"⬇️ {label} {pct}% ({speed}, ETA {eta_txt})")
                                else:
                                    status_cb(f"⬇️ {label} {pct}% ({speed})")
                            else:
                                if eta_txt:
                                    status_cb(f"⬇️ {label} {pct}% (ETA {eta_txt})")
                                else:
                                    status_cb(f"⬇️ {label} {pct}%")
                    except Exception:
                        pass

                _raise_if_cancelled(cancel_event)

    except DownloadCancelled:
        shutdown_wait = False
        for f in pending:
            f.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

    except Exception:
        shutdown_wait = False
        for f in pending:
            f.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise

    finally:
        if shutdown_wait:
            ex.shutdown(wait=True)

    try:
        if status_cb and total > 0 and last_pct < 100:
            now = time.time()
            speed = ""
            try:
                elapsed = max(0.25, now - start_ts)
                speed = _fmt_speed(bytes_done / elapsed) if bytes_done > 0 else ""
            except Exception:
                speed = ""
            if speed:
                status_cb(f"⬇️ {label} 100% ({speed})")
            else:
                status_cb(f"⬇️ {label} 100%")
    except Exception:
        pass

    # атомарно публикуем результат
    try:
        if _is_cancelled(cancel_event):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

    print(f"{label} скачано")
    return True

def start_hls_download(
    video_m3u8,
    audios,
    headers,
    out_path,
    status_cb=None,
    cancel_event=None,
    audio_select_cb=None,
    defer_mux: bool = False,
    audio_parallel_tracks: int | None = None,
):
    """
    Стойкий режим:
    1) Python скачивает VIDEO HLS (без ffmpeg)
    2) Python скачивает все AUDIO HLS
    3) ffmpeg делает только быстрый MUX
    """

    def worker() -> bool:
        tmp_dir = out_path + ".parts"
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_out = None
        mux_started = False
        mux_ok = False

        try:
            _raise_if_cancelled(cancel_event)

            video_file = os.path.join(tmp_dir, "video.ts")
            audio_files = []
            audio_meta = []

            # --- AUDIO список (нужен для проверки кеша и повторов) ---
            audios_valid = []
            try:
                for a in (audios or []):
                    try:
                        url = a.get("uri") or a.get("url")
                    except Exception:
                        url = None
                    if url:
                        audios_valid.append(a)
            except Exception:
                audios_valid = list(audios or [])

            total_audio = len(audios_valid)
            audio_uris = []
            try:
                for a in audios_valid:
                    try:
                        url = a.get("uri") or a.get("url")
                    except Exception:
                        url = None
                    if url:
                        audio_uris.append(str(url))
            except Exception:
                audio_uris = []

            def _file_ok(p: str) -> bool:
                try:
                    return os.path.isfile(p) and os.path.getsize(p) > 0
                except Exception:
                    return False

            # если в mux_meta.json есть сигнатура (m3u8 + список аудио) и она НЕ совпадает —
            # значит .parts от другого источника, лучше скачать заново
            force_redownload = False
            try:
                import json

                mux_meta_path = os.path.join(tmp_dir, "mux_meta.json")
                if os.path.isfile(mux_meta_path):
                    with open(mux_meta_path, "r", encoding="utf-8") as f:
                        old = json.load(f) or {}
                    old_v = old.get("video_m3u8")
                    old_a = old.get("audio_uris")
                    if isinstance(old_v, str) and isinstance(old_a, list):
                        if old_v != str(video_m3u8) or [str(x) for x in old_a] != audio_uris:
                            force_redownload = True
            except Exception:
                pass

            # --- VIDEO ---
            video_cached = (not force_redownload) and _file_ok(video_file)
            if video_cached:
                try:
                    print("♻️ Видео уже скачано — пропускаю.")
                    if status_cb:
                        status_cb("♻️ Видео уже скачано")
                except Exception:
                    pass
            else:
                print("🎞 Скачиваю видео...")
                ok = _download_hls_stream(
                    video_m3u8,
                    headers,
                    video_file,
                    status_cb,
                    "Видео",
                    cancel_event=cancel_event,
                )
                if not ok:
                    if _is_cancelled(cancel_event):
                        return False
                    if status_cb:
                        status_cb("❌ Ошибка видео")
                    return False

            _raise_if_cancelled(cancel_event)

            # --- AUDIO ---
            print("🎧 Скачиваю аудио...")
            try:
                ap = int(audio_parallel_tracks or 1)
            except Exception:
                ap = 1
            ap = max(1, min(4, ap))

            # При параллели уменьшаем число воркеров на поток, чтобы не взрывать количество тредов.
            try:
                audio_seg_workers = 8 if ap <= 1 else 4
            except Exception:
                audio_seg_workers = 8

            # Собираем задания и заранее учитываем уже скачанные файлы.
            audio_slots: list[str | None] = [None] * max(0, int(total_audio))
            audio_meta_slots: list[tuple[str, str] | None] = [None] * max(0, int(total_audio))
            tasks: list[tuple[int, str, str, str, str, str]] = []  # (idx, url, apath, label, title, lang)

            for idx, a in enumerate(audios_valid, start=1):
                _raise_if_cancelled(cancel_event)
                url = a.get("uri") or a.get("url")
                title = a.get("name") or f"Audio {idx}"
                lang = a.get("lang") or "und"
                if not url:
                    continue

                apath = os.path.join(tmp_dir, f"audio_{idx}.aac")

                label = f"Аудио {idx}/{max(1, total_audio)}"
                if title:
                    label += f" ({title})"

                # если этот аудиофайл уже есть — не качаем повторно
                if (not force_redownload) and _file_ok(apath):
                    try:
                        if status_cb:
                            status_cb(f"♻️ {label} уже скачано")
                    except Exception:
                        pass
                    if 0 <= idx - 1 < len(audio_slots):
                        audio_slots[idx - 1] = apath
                        audio_meta_slots[idx - 1] = (title, lang)
                    continue

                tasks.append((idx, str(url), apath, label, str(title), str(lang),))

            # Если параллель отключена — скачиваем как раньше (последовательно).
            if ap <= 1 or len(tasks) <= 1:
                for idx, url, apath, label, title, lang in tasks:
                    _raise_if_cancelled(cancel_event)
                    ok = _download_hls_stream(
                        url,
                        headers,
                        apath,
                        status_cb,
                        label,
                        workers=audio_seg_workers,
                        cancel_event=cancel_event,
                    )
                    if ok:
                        if 0 <= idx - 1 < len(audio_slots):
                            audio_slots[idx - 1] = apath
                            audio_meta_slots[idx - 1] = (title, lang)
                    elif _is_cancelled(cancel_event):
                        return False
            else:
                # Параллельно по N аудиодорожек (ускоряет тайтлы с 10–20 дорожками).
                ex = concurrent.futures.ThreadPoolExecutor(max_workers=ap)
                fut_map: dict[concurrent.futures.Future, tuple[int, str, str, str, str]] = {}
                try:
                    for idx, url, apath, label, title, lang in tasks:
                        _raise_if_cancelled(cancel_event)
                        fut = ex.submit(
                            _download_hls_stream,
                            url,
                            headers,
                            apath,
                            status_cb,
                            label,
                            audio_seg_workers,
                            cancel_event,
                        )
                        fut_map[fut] = (idx, apath, title, lang, label)

                    pending = set(fut_map.keys())
                    while pending:
                        _raise_if_cancelled(cancel_event)
                        done, pending = concurrent.futures.wait(
                            pending,
                            timeout=0.25,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        for fut in done:
                            idx, apath, title, lang, _label = fut_map.get(fut, (0, "", "", "und", ""))
                            ok = fut.result()  # может выбросить исключение как и раньше
                            if ok:
                                if 0 <= idx - 1 < len(audio_slots):
                                    audio_slots[idx - 1] = apath
                                    audio_meta_slots[idx - 1] = (title, lang)
                            elif _is_cancelled(cancel_event):
                                # дадим остальным потокам корректно завершиться по cancel_event
                                return False
                finally:
                    try:
                        ex.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        try:
                            ex.shutdown(wait=False)
                        except Exception:
                            pass

            # Собираем в исходном порядке (важно для map в ffmpeg).
            for i in range(min(len(audio_slots), len(audio_meta_slots))):
                p = audio_slots[i]
                meta = audio_meta_slots[i]
                if p and meta:
                    audio_files.append(p)
                    audio_meta.append(meta)

            _raise_if_cancelled(cancel_event)

            # Сохраним метаданные для возможного «Повторить MUX» (если ffmpeg упадёт).
            try:
                import json

                mux_meta_path = os.path.join(tmp_dir, "mux_meta.json")
                meta = {
                    "video_file": os.path.basename(video_file),
                    "audio_files": [os.path.basename(p) for p in audio_files],
                    "audio_meta": list(audio_meta),
                    "video_m3u8": str(video_m3u8),
                    "audio_uris": list(audio_uris),
                }
                try:
                    all_audio_ok = True
                    for i in range(1, total_audio + 1):
                        if not _file_ok(os.path.join(tmp_dir, f"audio_{i}.aac")):
                            all_audio_ok = False
                            break
                    meta["complete"] = bool(_file_ok(video_file) and (all_audio_ok or total_audio == 0))
                except Exception:
                    pass
                with open(mux_meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            # --- DEFER MUX (manual convert) ---
            if defer_mux:
                try:
                    if status_cb:
                        status_cb("🎞 Готово к конвертации")
                except Exception:
                    pass
                return True

            # --- AUDIO SELECT (optional, before MUX) ---
            try:
                if callable(audio_select_cb) and len(audio_files) > 1 and (not _is_cancelled(cancel_event)):
                    try:
                        if status_cb:
                            status_cb("🎧 Выбор аудиодорожки…")
                    except Exception:
                        pass

                    try:
                        choice = audio_select_cb(
                            out_path=out_path,
                            video_file=video_file,
                            audio_files=list(audio_files),
                            audio_meta=list(audio_meta),
                            cancel_event=cancel_event,
                            status_cb=status_cb,
                        )
                    except Exception:
                        choice = None

                    if _is_cancelled(cancel_event):
                        return False

                    # Важно: 0 == False, поэтому проверяем именно bool False, а не "любое равное False".
                    if choice == "cancel" or choice is False:
                        return False

                    idxs = None
                    if isinstance(choice, int):
                        idxs = [choice]
                    elif isinstance(choice, (list, tuple, set)):
                        idxs = list(choice)

                    if idxs:
                        norm: list[int] = []
                        for x in idxs:
                            try:
                                norm.append(int(x))
                            except Exception:
                                continue
                        # ожидаем 0-based индексы
                        norm = sorted({i for i in norm if 0 <= i < len(audio_files)})
                        if norm:
                            audio_files = [audio_files[i] for i in norm]
                            audio_meta = [audio_meta[i] for i in norm if i < len(audio_meta)]
            except Exception:
                pass

            # --- MUX ---
            base, _ = os.path.splitext(out_path)
            tmp_out = base + ".mp4.part"

            # считаем битрейт
            total_kbps  = TARGET_TOTAL_KBPS
            audio_kbps  = AUDIO_BITRATE_KBPS if audio_files else 0
            video_kbps  = max(MIN_VIDEO_BITRATE_KBPS, total_kbps - audio_kbps)

            v_bitrate = f"{video_kbps}k"
            a_bitrate = f"{audio_kbps}k" if audio_kbps else None
            v_bufsize = f"{video_kbps * 2}k"

            cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-i", video_file]

            for ap in audio_files:
                cmd += ["-i", ap]
            if not audio_files:
                print("⚠️ Нет аудиодорожек, MUX только видео.")

            # Маппинг дорожек
            cmd += ["-map", "0:v:0"]
            for i in range(len(audio_files)):
                cmd += ["-map", f"{i+1}:a:0"]

            if ENABLE_REENCODE:
                # ВИДЕО — перекод через NVENC с таргет-битрейтами
                cmd += [
                    "-c:v", "h264_nvenc",
                    "-pix_fmt", "yuv420p",
                    "-preset", "p4",
                    "-profile:v", "high",
                    "-tune", "hq",
                    "-spatial_aq", "1",
                    "-temporal_aq", "1",
                    "-rc", "vbr_hq",
                    "-b:v", v_bitrate,
                    "-maxrate", v_bitrate,
                    "-bufsize", v_bufsize,
                ]

                # АУДИО — AAC в фиксированный битрейт (чтобы общий bitrate был предсказуем)
                if audio_files:
                    cmd += ["-c:a", "aac", "-b:a", a_bitrate]
                else:
                    cmd += ["-an"]
            else:
                # Без перекодирования: быстрый ремультиплекс
                cmd += ["-c:v", "copy"]
                if audio_files:
                    cmd += ["-c:a", "copy"]
                else:
                    cmd += ["-an"]

            # метаданные по аудио
            for i, (title, lang) in enumerate(audio_meta):
                cmd += ["-metadata:s:a:{0}".format(i), f"title={title}"]
                cmd += ["-metadata:s:a:{0}".format(i), f"language={lang}"]

            if audio_files:
                cmd += ["-disposition:a:0", "default"]

            cmd += [
                "-map_metadata", "-1",
                "-sn",
                "-movflags", "+faststart",
                "-f", "mp4",
                tmp_out,
            ]

            if status_cb:
                status_cb("🟣 MUX…")

            # показываем точную команду ffmpeg
            cmd_quoted = [f'"{str(c)}"' if " " in str(c) else str(c) for c in cmd]
            if ENABLE_REENCODE:
                print("🧩 Муксую (перекодирование NVENC)…")
            else:
                print("🧩 Муксую (без перекодирования, copy)…")
            print("MUX CMD:", " ".join(cmd_quoted))

            _raise_if_cancelled(cancel_event)
            mux_started = True
            rc = _run_ffmpeg(cmd, cancel_event=cancel_event, status_cb=status_cb)
            if _is_cancelled(cancel_event):
                return False

            if rc == 0 and os.path.exists(tmp_out):
                os.replace(tmp_out, out_path)
                mux_ok = True
                print("✅ Готово!", out_path)
                if status_cb:
                    status_cb(f"✅ {os.path.basename(out_path)}")
                return True

            print(f"❌ Ошибка MUX (rc={rc})")
            if status_cb:
                status_cb(f"❌ Ошибка MUX (код {rc})")
            return False

        except DownloadCancelled:
            return False

        finally:
            if tmp_out and os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except Exception:
                    pass
            # Если упал только MUX — смысла качать сегменты заново нет: сохраняем .parts,
            # чтобы пользователь мог нажать «Повторить MUX».
            keep_parts = False
            try:
                keep_on_cancel = False
                try:
                    keep_on_cancel = bool(getattr(cancel_event, "_keep_parts", False))
                except Exception:
                    keep_on_cancel = False

                if (not mux_ok) and (keep_on_cancel or (not _is_cancelled(cancel_event))):
                    if mux_started:
                        keep_parts = True
                    else:
                        # если что-то уже скачали (например, video.ts) — оставим, чтобы не качать повторно
                        try:
                            if os.path.isfile(video_file) and os.path.getsize(video_file) > 0:
                                keep_parts = True
                            else:
                                for fn in os.listdir(tmp_dir):
                                    if not (fn.startswith("audio_") and fn.endswith(".aac")):
                                        continue
                                    p = os.path.join(tmp_dir, fn)
                                    try:
                                        if os.path.isfile(p) and os.path.getsize(p) > 0:
                                            keep_parts = True
                                            break
                                    except Exception:
                                        continue
                        except Exception:
                            pass
            except Exception:
                keep_parts = False

            if not keep_parts:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    return worker()


def retry_mux(out_path: str, status_cb=None, cancel_event=None, audio_select_cb=None) -> bool | str:
    """
    Повторяет ТОЛЬКО стадию MUX, используя уже скачанные файлы в папке `{out_path}.parts`.
    Полезно, когда ffmpeg упал, но сегменты уже скачаны.
    """
    tmp_dir = out_path + ".parts"
    if not os.path.isdir(tmp_dir):
        if status_cb:
            status_cb("❌ Нет данных для MUX (.parts не найден)")
        return False

    meta_path = os.path.join(tmp_dir, "mux_meta.json")
    video_file = os.path.join(tmp_dir, "video.ts")
    audio_files: list[str] = []
    audio_meta: list[tuple[str, str]] = []

    try:
        if os.path.isfile(meta_path):
            import json

            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
            try:
                vf = str(meta.get("video_file") or "").strip()
                if vf:
                    video_file = os.path.join(tmp_dir, vf)
            except Exception:
                pass
            try:
                audio_files = [
                    os.path.join(tmp_dir, str(p))
                    for p in (meta.get("audio_files") or [])
                    if str(p).strip()
                ]
            except Exception:
                audio_files = []
            try:
                raw_meta = meta.get("audio_meta") or []
                audio_meta = [(str(t or ""), str(l or "und")) for t, l in raw_meta]
            except Exception:
                audio_meta = []
    except Exception:
        pass

    if not os.path.isfile(video_file):
        if status_cb:
            status_cb("❌ Нет video.ts для MUX")
        return False

    if not audio_files:
        try:
            # audio_1.aac, audio_2.aac...
            cand = [os.path.join(tmp_dir, fn) for fn in os.listdir(tmp_dir) if fn.startswith("audio_") and fn.endswith(".aac")]
            def _key(p: str):
                bn = os.path.basename(p)
                m = re.search(r"audio_(\d+)", bn)
                return int(m.group(1)) if m else 10**9
            audio_files = sorted([p for p in cand if os.path.isfile(p)], key=_key)
        except Exception:
            audio_files = []

    # --- AUDIO SELECT (optional, before MUX) ---
    try:
        if callable(audio_select_cb) and len(audio_files) > 1 and (not _is_cancelled(cancel_event)):
            try:
                if status_cb:
                    status_cb("🎧 Выбор аудиодорожки…")
            except Exception:
                pass

            try:
                choice = audio_select_cb(
                    out_path=out_path,
                    video_file=video_file,
                    audio_files=list(audio_files),
                    audio_meta=list(audio_meta),
                    cancel_event=cancel_event,
                    status_cb=status_cb,
                )
            except Exception:
                choice = None

            if _is_cancelled(cancel_event):
                return False

            # Важно: 0 == False, поэтому проверяем именно bool False, а не "любое равное False".
            if choice == "cancel" or choice is False:
                return "cancel"

            idxs = None
            if isinstance(choice, int):
                idxs = [choice]
            elif isinstance(choice, (list, tuple, set)):
                idxs = list(choice)

            if idxs:
                norm: list[int] = []
                for x in idxs:
                    try:
                        norm.append(int(x))
                    except Exception:
                        continue
                # ожидаем 0-based индексы
                norm = sorted({i for i in norm if 0 <= i < len(audio_files)})
                if norm:
                    audio_files = [audio_files[i] for i in norm]
                    audio_meta = [audio_meta[i] for i in norm if i < len(audio_meta)]
    except Exception:
        pass

    base, _ = os.path.splitext(out_path)
    tmp_out = base + ".mp4.part"

    # считаем битрейт (как в основном MUX)
    total_kbps = TARGET_TOTAL_KBPS
    audio_kbps = AUDIO_BITRATE_KBPS if audio_files else 0
    video_kbps = max(MIN_VIDEO_BITRATE_KBPS, total_kbps - audio_kbps)
    v_bitrate = f"{video_kbps}k"
    a_bitrate = f"{audio_kbps}k" if audio_kbps else None
    v_bufsize = f"{video_kbps * 2}k"

    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-i", video_file]
    for ap in audio_files:
        cmd += ["-i", ap]

    # Маппинг дорожек
    cmd += ["-map", "0:v:0"]
    for i in range(len(audio_files)):
        cmd += ["-map", f"{i+1}:a:0"]

    if ENABLE_REENCODE:
        cmd += [
            "-c:v", "h264_nvenc",
            "-pix_fmt", "yuv420p",
            "-preset", "p4",
            "-profile:v", "high",
            "-tune", "hq",
            "-spatial_aq", "1",
            "-temporal_aq", "1",
            "-rc", "vbr_hq",
            "-b:v", v_bitrate,
            "-maxrate", v_bitrate,
            "-bufsize", v_bufsize,
        ]
        if audio_files:
            cmd += ["-c:a", "aac", "-b:a", a_bitrate]
        else:
            cmd += ["-an"]
    else:
        cmd += ["-c:v", "copy"]
        if audio_files:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]

    # метаданные по аудио (если есть)
    try:
        for i, (title, lang) in enumerate(audio_meta[: len(audio_files)]):
            if title:
                cmd += ["-metadata:s:a:{0}".format(i), f"title={title}"]
            if lang:
                cmd += ["-metadata:s:a:{0}".format(i), f"language={lang}"]
    except Exception:
        pass

    if audio_files:
        cmd += ["-disposition:a:0", "default"]

    cmd += [
        "-map_metadata", "-1",
        "-sn",
        "-movflags", "+faststart",
        "-f", "mp4",
        tmp_out,
    ]

    if status_cb:
        status_cb("🟣 MUX…")

    try:
        _raise_if_cancelled(cancel_event)
        rc = _run_ffmpeg(cmd, cancel_event=cancel_event, status_cb=status_cb)
        if _is_cancelled(cancel_event):
            return False
        if rc == 0 and os.path.exists(tmp_out):
            os.replace(tmp_out, out_path)
            if status_cb:
                status_cb(f"✅ {os.path.basename(out_path)}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return True
        if status_cb:
            status_cb(f"❌ Ошибка MUX (код {rc})")
        return False
    finally:
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass



# ---------- хелпер для GUI ----------
def download_one_title_ui(root, title_text: str, default_name: str | None = None):
    from tkinter import filedialog, messagebox

    if not ensure_login():
        messagebox.showwarning("Kino.pub", "Не выполнен вход. Сначала войдите.")
        return

    fname = default_name or (title_text.replace(":", " -").replace("/", "-"))
    if not fname.lower().endswith(".mp4"):
        fname += ".mp4"

    out_path = filedialog.asksaveasfilename(
        parent=root,
        title="Куда сохранить",
        initialfile=fname,
        defaultextension=".mp4",
        filetypes=[("MP4", "*.mp4"), ("Все файлы", "*.*")],
    )
    if not out_path:
        return

    def task():
        ok = search_and_download(title_text, out_path)
        root.after(
            0, lambda: messagebox.showinfo("Kino.pub", "Готово!" if ok else "Не удалось скачать.")
        )

    import threading
    threading.Thread(target=task, daemon=True).start()

def download_from_item_url(url: str, out_path: str) -> bool:
    # просто переиспользуем готовую реализацию
    return download_by_item_url(url, out_path, driver=None, status_cb=None)
