# auto_update.py
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import messagebox, ttk

# ==== НАСТРОЙКИ ====
APP_VERSION = "1.0.2"  # Текущая версия приложения (меняй при релизе)
GITHUB_OWNER = "Ti0jei"  # Твой GitHub
GITHUB_REPO = "MediaSearch"  # Репозиторий
INSTALL_ARGS = []  # Например ['/VERYSILENT'] для тихой установки


# ---- prefs (куда запоминаем skip_version) ----
def _prefs_path():
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    appdir = os.path.join(base, "MediaSearch")
    os.makedirs(appdir, exist_ok=True)
    return os.path.join(appdir, "prefs.json")


PREFS_FILE = _prefs_path()


def _prefs_load():
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _prefs_save(d):
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[update] prefs save error:", e)


# ---- утилиты ----
def _version_tuple(v: str):
    parts = []
    for p in (v or "").strip().split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts or [0])


def _hash_file_sha256(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest().lower()


def _download_with_progress(url, dest, on_progress=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "MediaSearch-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if on_progress and total:
                    on_progress(done, total)


# ---- источники версии ----
def _fetch_latest_github_api(timeout=12):
    """Основной способ: GitHub API releases/latest."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest",
        headers={
            "User-Agent": "MediaSearch-Updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))

    version = (data.get("tag_name") or data.get("name") or "").lstrip("v")
    notes = data.get("body") or ""
    exe_url = None
    sha256 = None

    for a in data.get("assets", []):
        name = a.get("name", "")
        url = a.get("browser_download_url")
        if name.lower().endswith(".exe"):
            exe_url = url
        elif name.lower().endswith(".sha256"):
            try:
                with urllib.request.urlopen(url, timeout=timeout) as rr:
                    sha256 = (rr.read().decode("utf-8").strip().split()[0] or "").lower()
            except Exception:
                pass

    if not exe_url:
        raise RuntimeError("В релизе не найден .exe установщик (API).")
    return {"version": version, "notes": notes, "url": exe_url, "sha256": sha256}


def _fetch_latest_via_version_json(timeout=12):
    """
    Фоллбэк: берём version.json из latest/download/.
    Формат version.json:
      { "version":"1.0.0", "notes":"...", "installer":"MediaSearch_Setup_1.0.0.exe", "sha256": "..." }
    (installer и sha256 — опционально)
    """
    base = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest/download"
    vurl = f"{base}/version.json"
    req = urllib.request.Request(vurl, headers={"User-Agent": "MediaSearch-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))

    version = str(data.get("version") or data.get("tag") or "").lstrip("v")
    if not version:
        raise RuntimeError("version.json: отсутствует поле 'version'.")

    notes = data.get("notes") or ""
    installer_name = data.get("installer") or f"MediaSearch_Setup_{version}.exe"
    exe_url = f"{base}/{installer_name}"
    sha256 = (data.get("sha256") or "").lower() or None
    return {"version": version, "notes": notes, "url": exe_url, "sha256": sha256}


# ---- установка ----
def _run_installer_and_exit(installer_path):
    try:
        cmd = [installer_path] + INSTALL_ARGS
        subprocess.Popen(cmd, close_fds=True, shell=False)
    except Exception as e:
        messagebox.showerror("Обновление", f"Не удалось запустить установщик:\n{e}")
        return
    try:
        sys.exit(0)
    except SystemExit:
        pass


# ---- UI ----
def _show_prompt(root, latest: dict):
    win = tk.Toplevel(root)
    win.title(f"Доступно обновление {latest['version']}")
    win.transient(root)
    win.grab_set()
    win.resizable(False, False)

    frm = ttk.Frame(win, padding=12)
    frm.grid(row=0, column=0, sticky="nsew")

    ttk.Label(
        frm, text=f"Найдена новая версия: {latest['version']}", font=("Segoe UI", 11, "bold")
    ).grid(row=0, column=0, sticky="w")

    notes = latest.get("notes") or "Нет описания изменений."
    txt = tk.Text(frm, width=60, height=12, wrap="word")
    scr = ttk.Scrollbar(frm, command=txt.yview)
    txt.configure(yscrollcommand=scr.set)
    txt.insert("1.0", notes)
    txt.config(state="disabled")
    txt.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
    scr.grid(row=1, column=1, sticky="ns", pady=(8, 8))

    prog_var = tk.IntVar(value=0)
    prog = ttk.Progressbar(
        frm, orient="horizontal", mode="determinate", length=360, maximum=100, variable=prog_var
    )
    lblp = ttk.Label(frm, text="")
    btns = ttk.Frame(frm)
    btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(8, 0))
    btn_install = ttk.Button(btns, text="Установить", width=16)
    btn_skip = ttk.Button(btns, text="Пропустить версию", width=20)
    btn_later = ttk.Button(btns, text="Позже", width=12, command=win.destroy)
    btn_install.grid(row=0, column=0, padx=4)
    btn_skip.grid(row=0, column=1, padx=4)
    btn_later.grid(row=0, column=2, padx=4)

    def _disable_buttons():
        for b in (btn_install, btn_skip, btn_later):
            b.configure(state="disabled")

    def _enable_buttons():
        for b in (btn_install, btn_skip, btn_later):
            b.configure(state="normal")

    def on_skip():
        prefs = _prefs_load()
        prefs["skip_version"] = latest["version"]
        _prefs_save(prefs)
        win.destroy()

    btn_skip.configure(command=on_skip)

    def on_install():
        _disable_buttons()
        prog.grid(row=2, column=0, columnspan=2, sticky="ew")
        lblp.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def worker():
            try:
                tmp_dir = tempfile.gettempdir()
                dest = os.path.join(tmp_dir, f"MediaSearchSetup_{latest['version']}.exe")

                def _cb(done, total):
                    pct = max(0, min(100, int(done * 100 / total)))
                    root.after(
                        0, lambda: (prog_var.set(pct), lblp.config(text=f"Загрузка: {pct}%"))
                    )

                _download_with_progress(latest["url"], dest, on_progress=_cb)

                if latest.get("sha256"):
                    real = _hash_file_sha256(dest)
                    want = latest["sha256"].lower()
                    if real != want:
                        try:
                            os.remove(dest)
                        except Exception:
                            pass
                        raise RuntimeError("Хеш файла не совпал. Файл повреждён или подменён.")

                root.after(0, lambda: _run_installer_and_exit(dest))
            except Exception as e:
                root.after(
                    0,
                    lambda: (
                        _enable_buttons(),
                        prog.grid_remove(),
                        lblp.config(text=""),
                        messagebox.showerror("Обновление", f"Ошибка загрузки/установки:\n{e}"),
                    ),
                )


        threading.Thread(target=worker, daemon=True).start()

    btn_install.configure(command=on_install)


# ---- публичная функция ----
def check_for_updates_async(root, show_if_latest=False):
    def worker():
        last_err = None
        latest = None
        # 1) Пробуем API
        try:
            latest = _fetch_latest_github_api()
        except Exception as e:
            last_err = e
            print("[update] API failed:", e)
            # 2) Фоллбэк: version.json
            try:
                latest = _fetch_latest_via_version_json()
            except Exception as e2:
                last_err = e2
                print("[update] version.json fallback failed:", e2)

        if not latest:
            if show_if_latest:
                root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Обновление", f"Не удалось проверить обновления: {last_err}"
                    ),
                )
            return

        try:
            cur = _version_tuple(APP_VERSION)
            new = _version_tuple(latest["version"])
            if new <= cur:
                if show_if_latest:
                    root.after(
                        0, lambda: messagebox.showinfo("Обновление", "У вас последняя версия.")
                    )
                return
            if _prefs_load().get("skip_version") == latest["version"]:
                return
            root.after(0, lambda: _show_prompt(root, latest))
        except Exception as e:
            print("[update] compare failed:", e)
            if show_if_latest:
                root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Обновление", f"Не удалось проверить обновления: {e}"
                    ),
                )

    threading.Thread(target=worker, daemon=True).start()
