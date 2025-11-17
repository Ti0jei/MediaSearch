# uc_driver.py
import os
import time
import threading
from pathlib import Path
import ctypes
import subprocess
from ctypes import wintypes   # ← нужен для WNDPROC/WinEventProcType и т.п.
import win32process           # ← нужен для GetWindowThreadProcessId и обхода окон
import win32gui
import win32con
import undetected_chromedriver as uc
import sys
import logging
# psutil нужен, чтобы найти реальные PID-ы Chromium (а не chromedriver)
try:
    import psutil
except Exception:
    psutil = None



# ===================== LOG =====================
def _log(status_cb, msg: str):
    msg = str(msg)
    # в консоль (dev-режим) / никуда (exe без консоли)
    print(msg, flush=True)
    # в лог-файл logs/app.log
    try:
        logging.info(msg)
    except Exception:
        pass
    # в UI-коллбек, если есть
    if status_cb:
        try:
            status_cb(msg)
        except Exception:
            pass



# ============= Chromium discovery =============
def _app_root() -> Path:
    """
    Базовая папка приложения.

    - при обычном запуске .py: папка, где лежит этот файл
    - при PyInstaller-onefile: временная папка sys._MEIPASS
    - при PyInstaller-onedir: папка, где лежит exe
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            # onefile: всё распакуётся сюда
            return Path(meipass)
        # onedir: просто папка exe
        return Path(sys.executable).resolve().parent

    # dev-запуск: рядом с исходниками
    return Path(__file__).resolve().parent


def _find_chromium_exe() -> str | None:
    """Ищем chrome.exe/Chromium рядом с приложением или в стандартных путях."""
    env = os.environ.get("CHROMIUM_PATH")
    base = _app_root()

    def norm_path(p: Path) -> Path | None:
        try:
            # Если это директория — ищем chrome.exe/chrome внутри
            if p.is_dir():
                for name in ("chrome.exe", "chrome"):
                    c = p / name
                    if c.is_file():
                        return c
            # Если уже файл — просто проверяем, существует ли
            if p.is_file():
                return p
        except Exception:
            pass
        return None

    guesses = []

    # 1) Явно заданный путь через переменную среды
    if env:
        guesses.append(Path(env))

    # 2) Рядом с приложением (и в dev, и в exe)
    guesses += [
        base / "browser" / "bin",         # dist\browser\bin\chrome.exe
        base / "browser",                 # dist\browser\chrome.exe
        base / "browser" / "chromium",    # dist\browser\chromium\chrome.exe
    ]

    # 3) Установленный Chromium/Chrome в системе
    guesses += [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Chromium" / "Application",
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Chromium" / "Application",
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Chromium" / "Application",
    ]

    for g in guesses:
        found = norm_path(g)
        if found:
            return str(found)

    return None



def _parse_major_from_text(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    # ищем первый «числовой.точечный» токен
    for tok in text.split():
        if tok and tok[0].isdigit():
            major = tok.split(".")[0]
            if major.isdigit():
                return int(major)
    # запасной проход с конца
    for tok in reversed(text.split()):
        if tok and tok[0].isdigit():
            major = tok.split(".")[0]
            if major.isdigit():
                return int(major)
    return None


def _get_browser_major_version(browser_path: str) -> int | None:
    # 1) Пробуем прочитать версию из ресурсов файла (version.dll)
    try:
        from ctypes import windll, wintypes, byref, create_string_buffer, sizeof, c_void_p

        GetFileVersionInfoSizeW = windll.version.GetFileVersionInfoSizeW
        GetFileVersionInfoW     = windll.version.GetFileVersionInfoW
        VerQueryValueW          = windll.version.VerQueryValueW

        filename = wintypes.LPCWSTR(browser_path)
        dummy = wintypes.DWORD(0)
        size = GetFileVersionInfoSizeW(filename, byref(dummy))
        if size:
            buf = create_string_buffer(size)
            if GetFileVersionInfoW(filename, 0, size, buf):
                # VS_FIXEDFILEINFO по пути "\\"
                lptr = c_void_p()
                lsize = wintypes.UINT(0)
                if VerQueryValueW(buf, wintypes.LPCWSTR("\\"), byref(lptr), byref(lsize)):
                    # структура VS_FIXEDFILEINFO: первые 4 байта — Signature, затем dwStrucVersion,
                    # затем dwFileVersionMS, dwFileVersionLS (каждое по 4 байта)
                    import struct
                    data = (ctypes.string_at(lptr.value, lsize.value))
                    # Смещение до dwFileVersionMS: 8 байт после Signature(4) + StrucVersion(4)
                    dwFileVersionMS, dwFileVersionLS = struct.unpack_from("<II", data, offset=8)
                    def HIWORD(d): return (d >> 16) & 0xFFFF
                    # def LOWORD(d): return d & 0xFFFF  # если понадобится
                    major = HIWORD(dwFileVersionMS)
                    if isinstance(major, int) and major > 0:
                        return major
    except Exception:
        pass

    # 2) Fallback: твоя прежняя эвристика по папкам рядом с exe
    try:
        exe = Path(browser_path)
        bin_dir = exe.parent
        candidates = []
        for child in bin_dir.iterdir():
            if child.is_dir():
                parts = child.name.split(".")
                if parts and parts[0].isdigit():
                    candidates.append(int(parts[0]))
        if candidates:
            return max(candidates)
    except Exception:
        pass

    # 3) Не удалось определить
    return None



_CHROMIUM_EXE = _find_chromium_exe()
_VERSION_MAIN = None  # ← узнаем позже, уже внутри _safe_get_driver

if _CHROMIUM_EXE:
    _log(None, f"[UC] Chromium exe: {_CHROMIUM_EXE}")
else:
    _log(None, "[UC] Chromium exe: <не найден>")

# Не трогаем версию здесь! Никаких запусков chrome.exe на этапе импорта.



# ====== Основной конструктор UC/Chromium ======
def _clean_profile_leftovers(profile_dir: Path, exe_path: str):
    """Гасит РОВНО наш portable-Chromium на данном профиле и чистит lock-файлы."""
    if not psutil:
        return
    prof = str(profile_dir).replace("\\", "/").lower()
    exe_ref = (exe_path or "").replace("\\", "/").lower()

    # мягко закрываем
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            ex = (p.info.get("exe") or "").replace("\\", "/",).lower()
            cl = " ".join(p.info.get("cmdline") or []).replace("\\", "/").lower()
            if ex == exe_ref and f"--user-data-dir={prof}" in cl:
                p.terminate()
        except Exception:
            pass

    time.sleep(0.5)
    # добиваем упрямые
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            ex = (p.info.get("exe") or "").replace("\\", "/").lower()
            cl = " ".join(p.info.get("cmdline") or []).replace("\\", "/").lower()
            if ex == exe_ref and f"--user-data-dir={prof}" in cl:
                p.kill()
        except Exception:
            pass

    # чистим локи профиля
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except Exception:
            pass

def _safe_get_driver(status_cb=None, headless: bool = False, suppress: bool = True,
                     need_login_hint: bool = False, profile_tag: str = "run",
                     preload_kino_cookies: bool = False,
                     profile_name: str | None = None):

    base_dir = Path(os.environ["LOCALAPPDATA"]) / "MediaSearch"
    if profile_tag == "login":
        profile_dir = base_dir / "UC_PROFILE_LOGIN"
    else:
        # уникальный профиль для каждого драйвера
        if not profile_name:
            profile_name = f"UC_PROFILE_RUN_{int(time.time()*1000)%100000}_{threading.get_ident()%1000}"
        profile_dir = base_dir / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)


    if not _CHROMIUM_EXE:
        _log(status_cb, "⚠ Не найден Chromium. Укажите CHROMIUM_PATH или положите exe в .\\browser\\bin\\chrome.exe")

    driver = None
    last_error = None

    for attempt in range(1, 4):
        try:
            # НЕЛЬЗЯ переиспользовать ChromeOptions → создаём новый объект каждый раз
            # перед запуском аккуратно гасим хвосты ровно нашего portable-Chromium на этом профиле
            try:
                _clean_profile_leftovers(profile_dir, _CHROMIUM_EXE or "")
            except Exception:
                pass

            # НЕЛЬЗЯ переиспользовать ChromeOptions → создаём новый объект каждый раз
            options = uc.ChromeOptions()

            options.add_argument("--mute-audio")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-logging")
            options.add_argument("--log-level=3")
            options.add_argument("--lang=ru-RU")
            options.add_argument("--no-first-run")
            options.add_argument("--no-service-autorun")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--disable-background-timer-throttling")
            options.add_argument("--disable-gpu")
            options.add_argument(f"--user-data-dir={profile_dir}")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-session-crashed-bubble")
            options.add_argument("--disable-notifications")
            options.add_argument("--disable-features=Translate,MediaRouter,AutofillServerCommunication,OptimizationHints,CalculateNativeWinOcclusion,UserEducationExperience")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--hide-crash-restore-bubble")
            options.add_argument("--window-size=1280,900")
            options.add_argument("--remote-debugging-port=0")
            options.add_argument("--noerrdialogs")
            options.add_argument("--disable-crash-reporter")


            if suppress:
                # для download-режима — сворачиваем/уводим
                options.add_argument("--app=data:,")   
                options.add_argument("--start-minimized")
                options.add_argument("--window-position=-32000,-32000")
                headless = False  # headless ломает авторизацию/куки

            _log(status_cb, f"🚀 Запуск Chromium через UC (попытка {attempt}/3)")

            kwargs = dict(
    options=options,
    headless=False,
    use_subprocess=True,
    browser_executable_path=_CHROMIUM_EXE
)

            # ЛЕНИВО узнаём версию ОДИН РАЗ и только сейчас — без запуска chrome.exe
            global _VERSION_MAIN
            if _VERSION_MAIN is None and _CHROMIUM_EXE:
                try:
                    _VERSION_MAIN = _get_browser_major_version(_CHROMIUM_EXE)
                    if _VERSION_MAIN:
                        _log(status_cb, f"[UC] Chromium major (lazy): {_VERSION_MAIN}")
                except Exception:
                    _VERSION_MAIN = None

            if _VERSION_MAIN:
                kwargs["version_main"] = _VERSION_MAIN
            else:
                # Можно залогировать, но не пытаться ничего запускать
                _log(status_cb, "[UC] Chromium major: <не определена> — используем auto")

            driver = uc.Chrome(**kwargs)
            driver.set_page_load_timeout(20)

            # ← ДО любых переходов!
            if preload_kino_cookies and profile_tag != "login":
                try:
                    driver.execute_cdp_cmd("Network.enable", {})
                    from kino_parser import load_cookies
                    load_cookies(driver)
                except Exception:
                    pass


            last_error = None
            break
        except Exception as e:
            last_error = e
            _log(status_cb, f"⚠ Ошибка запуска: {e}")
            time.sleep(1.0)

    if not driver:
        raise last_error

        # ====== Хук, запрещающий разворот окна (suppress=True) ======
    pid = driver.service.process.pid          # это PID chromedriver, не браузера!
    chromedriver_pid = pid
    target_pids = set()                        # сюда соберём все PID-ы chromium.exe

    # --- помощники для распознавания процессов/окон Chromium ---
    def _is_chrome_like_name(name: str) -> bool:
        n = (name or "").lower()
        return (
            n.startswith(("chrome", "chromium", "msedge"))
            or n in ("chrome.exe", "chromium.exe", "msedge.exe")
        )

    def _is_chrome_widget(hwnd) -> bool:
        try:
            cls = win32gui.GetClassName(hwnd) or ""
            return cls.startswith(("Chrome_WidgetWin", "Chromium_WidgetWin"))
        except Exception:
            return False

    def _refresh_target_pids():
        """Собираем PID'ы ТОЛЬКО нашего portable-Chromium (точное совпадение exe)
        ИЛИ запущенных с нашим профилем (--user-data-dir=<наш профиль>)."""
        nonlocal target_pids
        if not psutil:
            target_pids = set()
            return

        s = set()
        exe_ref = (str(_CHROMIUM_EXE or "")).replace("\\", "/").lower()
        prof_ref = str(profile_dir).replace("\\", "/").lower()

        def _match(exe_path: str, cmdline_list):
            ex = (exe_path or "").replace("\\", "/").lower()
            if ex == exe_ref:
                return True
            cl = " ".join(cmdline_list or []).replace("\\", "/").lower()
            return f"--user-data-dir={prof_ref}" in cl

        # 1) дети chromedriver
        try:
            parent = psutil.Process(chromedriver_pid)
            for ch in parent.children(recursive=True):
                try:
                    if _match(ch.exe(), ch.cmdline()):
                        s.add(ch.pid)
                except Exception:
                    pass
        except Exception:
            pass

        # 2) подстраховка общим обходом
        for p in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                if _match(p.info.get("exe"), p.info.get("cmdline")):
                    s.add(p.info["pid"])
            except Exception:
                pass

        target_pids = s


    def _pid_refresh_loop():
        while True:
            try:
                _refresh_target_pids()
                time.sleep(0.5)
            except Exception:
                break

    # первичное наполнение + фоновое обновление
    _refresh_target_pids()
    threading.Thread(target=_pid_refresh_loop, daemon=True).start()

    if suppress:
        User32 = ctypes.windll.user32

        Ole32 = ctypes.windll.ole32
        Ole32.CoInitialize(0)

        SetWindowLongPtr = User32.SetWindowLongPtrW
        GetWindowLongPtr = User32.GetWindowLongPtrW
        CallWindowProc = User32.CallWindowProcW
        GetForegroundWindow = User32.GetForegroundWindow
        EnumChildWindows = User32.EnumChildWindows
        SetWinEventHook = User32.SetWinEventHook
        UnhookWinEvent = User32.UnhookWinEvent

        GWL_WNDPROC = -4
        GWL_STYLE = -16
        GWL_EXSTYLE = -20

        WM_SYSCOMMAND = 0x0112
        WM_SIZE = 0x0005
        WM_SHOWWINDOW = 0x0018
        WM_WINDOWPOSCHANGING = 0x0046
        WM_WINDOWPOSCHANGED = 0x0047
        WM_ACTIVATE = 0x0006
        WM_NCACTIVATE = 0x0086
        WM_MOUSEACTIVATE = 0x0021
        WM_QUERYOPEN = 0x0013
        WM_SETFOCUS = 0x0007
        WM_KILLFOCUS = 0x0008
        WM_NCDESTROY = 0x0082

        SC_RESTORE = 0xF120
        SC_MAXIMIZE = 0xF030

        MA_NOACTIVATE = 3

        WS_DISABLED = 0x08000000
        WS_MAXIMIZEBOX = 0x00010000
        WS_THICKFRAME = 0x00040000
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_APPWINDOW  = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080

        EVENT_SYSTEM_FOREGROUND = 0x0003
        EVENT_OBJECT_SHOW = 0x8002
        EVENT_OBJECT_CREATE = 0x8000
        EVENT_OBJECT_FOCUS = 0x8005
        WINEVENT_OUTOFCONTEXT = 0x0000
        WINEVENT_SKIPOWNPROCESS = 0x0002

        SWP_BLOCK = win32con.SWP_NOACTIVATE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        # В ctypes.wintypes нет HWINEVENTHOOK → используем HANDLE
        WinEventProcType = ctypes.WINFUNCTYPE(
            None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
        )

        hooked = {}

        # ---------- CDP helpers ----------
        def _cdp_minimize_now():
            """Сворачивает текущее окно Chrome через CDP (Browser.setWindowBounds)."""
            try:
                info = driver.execute_cdp_cmd('Browser.getWindowForTarget', {})
                wid = info.get('windowId')
                if wid:
                    driver.execute_cdp_cmd('Browser.setWindowBounds', {
                        'windowId': wid,
                        'bounds': {'windowState': 'minimized'}
                    })
            except:
                pass

        def _hold_minimized_for(sec: float = 2.0):
            t0 = time.time()
            while time.time() - t0 < sec:
                try:
                    _cdp_minimize_now()
                except:
                    pass
                time.sleep(0.15)

        # ---------- Window helpers ----------
        def _style_harden(hwnd):
            try:
                ex = GetWindowLongPtr(hwnd, GWL_EXSTYLE)
                # убираем APPWINDOW, добавляем TOOLWINDOW и NOACTIVATE — окно не попадает в Alt+Tab и не активируется
                ex = (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
                User32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
            except:
                pass
            try:
                st = GetWindowLongPtr(hwnd, GWL_STYLE)
                # запрещаем размаксимизацию и толстую рамку, плюс делаем окно «disabled»
                st = (st | WS_DISABLED) & ~WS_MAXIMIZEBOX & ~WS_THICKFRAME
                User32.SetWindowLongPtrW(hwnd, GWL_STYLE, st)
            except:
                pass

        def _force_hide(hwnd):
            try:
                # Полностью скрываем (без мигания) и уводим за экран на всякий случай
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, -32000, -32000, 0, 0, SWP_BLOCK)
                win32gui.EnableWindow(hwnd, False)
            except:
                pass
            try:
                _cdp_minimize_now()
            except:
                pass

        # ---------- Subclass ----------
        def _subclass(hwnd):
            if hwnd in hooked:
                return
            try:
                old_proc = GetWindowLongPtr(hwnd, GWL_WNDPROC)

                def wndproc(h, msg, wp, lp):
                    if msg == WM_SYSCOMMAND:
                        if (wp & 0xFFF0) in (SC_RESTORE, SC_MAXIMIZE):
                            _style_harden(h); _force_hide(h); return 0
                    if msg in (WM_SIZE, WM_SHOWWINDOW, WM_WINDOWPOSCHANGING, WM_WINDOWPOSCHANGED,
                               WM_ACTIVATE, WM_NCACTIVATE, WM_QUERYOPEN, WM_SETFOCUS, WM_MOUSEACTIVATE):
                        _style_harden(h); _force_hide(h)
                        if msg == WM_MOUSEACTIVATE:
                            return MA_NOACTIVATE
                        return 0
                    if msg == WM_KILLFOCUS:
                        _force_hide(h)

                    if msg == WM_NCDESTROY:
                        hooked.pop(h, None)
                        return CallWindowProc(old_proc, h, msg, wp, lp)
                    return CallWindowProc(old_proc, h, msg, wp, lp)

                new = WNDPROC(wndproc)
                SetWindowLongPtr(hwnd, GWL_WNDPROC, new)
                hooked[hwnd] = new
                _style_harden(hwnd)
                _force_hide(hwnd)

            except:
                pass

        # ---------- Enum + guardian ----------
        def _enum_all_for_pid():
            def hook_tree(root):
                _subclass(root)

                @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                def child_cb(ch, _l):
                    _subclass(ch)
                    return True
                try:
                    EnumChildWindows(root, child_cb, 0)
                except:
                    pass

            def cb(wnd, _l):
                if not win32gui.IsWindow(wnd):
                    return
                try:
                    _, p = win32process.GetWindowThreadProcessId(wnd)
                    # таргетим окна браузера (Chromium/Chrome/Edge) по PID-ам или по классу
                    # ТОЛЬКО по нашим PID
                    if p in target_pids:
                        hook_tree(wnd)
                except:
                    pass


            win32gui.EnumWindows(cb, 0)

        def _guardian():
            while True:
                try:
                    _enum_all_for_pid()
                    try:
                        fg = GetForegroundWindow()
                        if fg and fg in hooked:
                            _force_hide(fg)
                    except:
                        pass
                    for h in list(hooked.keys()):
                        if win32gui.IsWindow(h):
                            _style_harden(h)
                            # если вдруг стало видимым — прячем и сворачиваем
                            if win32gui.IsWindowVisible(h):
                                _force_hide(h)
                    time.sleep(0.07)
                except:
                    break

        threading.Thread(target=_guardian, daemon=True).start()

        # ---------- WinEvent hooks ----------
        def _win_event_proc(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
            try:
                if not hwnd or not win32gui.IsWindow(hwnd):
                    return
                _, p = win32process.GetWindowThreadProcessId(hwnd)
                if p not in target_pids:
                    return

                _subclass(hwnd)
                _force_hide(hwnd)

            except:
                pass

        _WinEventProc = WinEventProcType(_win_event_proc)
        hooks = [
            SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, _WinEventProc, 0, 0,
                            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS),
            SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE, 0, _WinEventProc, 0, 0,
                            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS),
            SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, 0, _WinEventProc, 0, 0,
                            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS),
            SetWinEventHook(EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, 0, _WinEventProc, 0, 0,
                            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS),
        ]
        driver._win_event_proc = _WinEventProc
        driver._win_event_hooks = hooks

        # Первичное сворачивание + короткое удержание
        _cdp_minimize_now()
        threading.Thread(target=_hold_minimized_for, args=(3.0,), daemon=True).start()

        # Оборачиваем навигацию, чтобы не «всплывало» при переходах
        try:
            _orig_get = driver.get

            def _get_hidden(url, *a, **k):
                try:
                    _cdp_minimize_now()
                except:
                    pass
                try:
                    return _orig_get(url, *a, **k)
                finally:
                    _cdp_minimize_now()
                    _hold_minimized_for(1.0)

            driver.get = _get_hidden
        except:
            pass

    else:
        # suppress=False → показываем окно логина
        def _find_main():
            wins = []
            def cb(wnd, _l):
                if not win32gui.IsWindow(wnd):
                    return
                try:
                    _, p = win32process.GetWindowThreadProcessId(wnd)
                    if p == pid and win32gui.GetParent(wnd) == 0:
                        wins.append(wnd)
                except:
                    pass
            win32gui.EnumWindows(cb, 0)
            return wins[0] if wins else None

        hwnd = _find_main()
        if hwnd:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(hwnd)
            except:
                pass

    return driver


# ===================== LOGIN CHECKS =====================
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By

KINOPUB_BASE = "https://kino.pub"


def _check_login_on(driver, status_cb=None):
    try:
        driver.get(KINOPUB_BASE + "/")
        WebDriverWait(driver, 6).until(lambda d: d.execute_script("return document.readyState") == "complete")
        driver.get(KINOPUB_BASE + "/user/profile")
        WebDriverWait(driver, 6).until(lambda d: d.execute_script("return document.readyState") == "complete")
        if "/user/login" in driver.current_url.lower():
            return False
        if driver.find_elements(By.CSS_SELECTOR, ".user-menu, .user-avatar, a[href*='/logout']"):
            return True
        return "/user/profile" in driver.current_url.lower()
    except:
        return False


def _check_login(status_cb=None) -> bool:
    from kino_parser import load_cookies
    driver = _safe_get_driver(status_cb, suppress=True)
    try:
        from kino_parser import load_cookies

        try:
            # Разрешаем CDP и подгружаем куки ДО переходов
            driver.execute_cdp_cmd("Network.enable", {})
            load_cookies(driver)                      # ← CDP setCookies, работает для httpOnly/SameSite
        except:
            pass

        driver.get(KINOPUB_BASE + "/user/profile")
        WebDriverWait(driver, 6).until(lambda d: d.execute_script("return document.readyState") == "complete")

        WebDriverWait(driver, 6).until(lambda d: d.execute_script("return document.readyState") == "complete")
        return "/user/login" not in driver.current_url.lower()
    except:
        return False
    finally:
        driver.quit()


# ======================= LOGIN WINDOW =======================
def login_to_kino(status_cb=None):
    from kino_parser import save_cookies
    import tkinter as tk
    from tkinter import messagebox

    driver = _safe_get_driver(status_cb, suppress=False, profile_tag="login")

    try:
        driver.get(KINOPUB_BASE + "/user/login")    
        _log(status_cb, "🔓 Открыта страница входа...")
        logged = False
        hint_shown = False
        for i in range(90):
            try:
                if driver.find_elements(By.CSS_SELECTOR, ".user-menu, .user-avatar, a[href*='/logout']"):
                    logged = True
                    break
            except:
                pass
            if i == 10 and not hint_shown:
                root = tk.Tk(); root.withdraw()
                messagebox.showinfo("Авторизация", "Войдите вручную, затем закройте окно.")
                root.destroy(); hint_shown = True
            time.sleep(1)
        if not logged:
            _log(status_cb, "❌ Авторизация не выполнена.")
            driver.quit(); return False
        try:
            save_cookies(driver)
        except:
            pass
        driver.quit(); return True
    except Exception as e:
        _log(status_cb, f"❌ Ошибка login_to_kino: {e}")
        try: driver.quit()
        except: pass
        return False
# ========================= DRIVER POOL =========================
class DriverPool:
    def __init__(self, max_drivers=3, status_cb=None):
        import queue
        self.max_drivers = max_drivers
        self.status_cb = status_cb
        self.q = queue.Queue()
        self._total = 0
        self._counter = 0  # ← добавили

    def _new_driver(self):
        self._counter += 1
        prof_name = f"UC_PROFILE_RUN_{self._counter}"
        drv = _safe_get_driver(self.status_cb,
                               suppress=True,
                               profile_tag="run",
                               preload_kino_cookies=True,
                               profile_name=prof_name)
        try:
            # прогрев сессии/CF на ЭТОМ профиле
            drv.get(KINOPUB_BASE)
            setattr(drv, "_kino_cookies_loaded", True)
        except Exception:
            setattr(drv, "_kino_cookies_loaded", False)
        return drv



    def acquire(self, timeout=None):
        try:
            return self.q.get_nowait()
        except Exception:
            if self._total < self.max_drivers:
                drv = self._new_driver()
                self._total += 1
                return drv
            # если уже исчерпали лимит — ждём освобождения
            return self.q.get(timeout=timeout)

    def release(self, drv):
        self.q.put(drv)

    def close_all(self):
        while not self.q.empty():
            drv = self.q.get()
            try:
                hooks = getattr(drv, "_win_event_hooks", None)
                if hooks:
                    User32 = ctypes.windll.user32
                    for h in hooks:
                        try:
                            User32.UnhookWinEvent(h)
                        except:
                            pass
                drv.quit()
            except:
                pass
# ===================== MULTIPLE DOWNLOADS =====================
from kino_hls import get_hls_info, start_hls_download

def download_multiple(urls, out_dir, status_cb=None):
    os.makedirs(out_dir, exist_ok=True)
    pool = DriverPool(max_drivers=2, status_cb=status_cb)
    threads = []
    try:
        for url in urls:
            drv = pool.acquire(timeout=10)
            try:
                video_m3u8, hdrs, audios = get_hls_info(url, driver=drv)
                if not video_m3u8:
                    _log(status_cb, f"⚠ Пропущено: {url}")
                    continue
                name = os.path.basename(url).split("?")[0]
                out_path = os.path.join(out_dir, f"{name}.mp4")
                t = start_hls_download(video_m3u8, audios, hdrs, out_path, status_cb)
                threads.append(t)
            finally:
                pool.release(drv)
        for t in threads:
            t.join()
    finally:
        pool.close_all()
