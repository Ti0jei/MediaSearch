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
APP_VERSION = "1.0.5"  # Текущая версия приложения (меняй при релизе)
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


# ===================== КРАСИВЫЙ UI ОБНОВЛЕНИЯ =====================

# Цвета в стиле Movie Tools
BG_WINDOW = "#05051A"   # тёмный фон
BG_PANEL = "#101427"    # карточка
ACCENT = "#FF4FA3"      # розовый акцент
ACCENT_2 = "#7C4DFF"    # сиреневый прогресс
FG_TEXT = "#F5F5FF"
FG_MUTED = "#9AA0C2"
BORDER = "#272C45"


class UpdateDialog(tk.Toplevel):
    """Окно обновления в фирменном стиле."""

    def __init__(self, parent, latest: dict, on_install, on_skip, on_later):
        super().__init__(parent)
        self.latest = latest
        self.on_install_cb = on_install
        self.on_skip_cb = on_skip
        self.on_later_cb = on_later

        self.title(f"Доступно обновление {latest['version']}")
        self.configure(bg=BG_WINDOW)
        self.resizable(False, False)
        # Иконка окна (та же, что у основного EXE)
        try:
            if getattr(sys, "frozen", False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(__file__)
            icon_path = os.path.join(base_dir, "icon.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception:
            # Если что-то пойдёт не так — просто остаётся дефолтная иконка
            pass

        # Центрировать относительно родителя
        self.update_idletasks()
        if parent is not None:
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            w, h = 520, 360
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
            self.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self.geometry("520x360")

        self.transient(parent)
        self.grab_set()

        # стили
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("Update.TFrame", background=BG_WINDOW)
        style.configure("Card.TFrame", background=BG_PANEL, bordercolor=BORDER,
                        borderwidth=1, relief="solid")
        style.configure("Title.TLabel", background=BG_WINDOW, foreground=FG_TEXT,
                        font=("Segoe UI Semibold", 16))
        style.configure("SubTitle.TLabel", background=BG_WINDOW, foreground=FG_MUTED,
                        font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=BG_PANEL, foreground=FG_TEXT,
                        font=("Segoe UI Semibold", 11))
        style.configure("CardText.TLabel", background=BG_PANEL, foreground=FG_MUTED,
                        font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10),
                        foreground=FG_TEXT, background=ACCENT,
                        borderwidth=0, focusthickness=0, padding=(14, 6))
        style.map("Accent.TButton", background=[("active", "#ff6ab1")])
        style.configure("Ghost.TButton", font=("Segoe UI", 10),
                        foreground=FG_MUTED, background=BG_PANEL,
                        borderwidth=1, bordercolor=BORDER, padding=(12, 5))
        style.map("Ghost.TButton", background=[("active", "#191f35")])
        style.configure("Update.Horizontal.TProgressbar",
                        troughcolor=BG_PANEL, bordercolor=BG_PANEL,
                        background=ACCENT_2, thickness=6)

        # корневой фрейм
        root_frame = ttk.Frame(self, style="Update.TFrame", padding=16)
        root_frame.pack(fill="both", expand=True)

        # заголовок
        header = ttk.Frame(root_frame, style="Update.TFrame")
        header.pack(fill="x")

        ttk.Label(
            header,
            text="Доступно обновление Movie Tools",
            style="Title.TLabel",
        ).pack(anchor="w")

        ttk.Label(
            header,
            text=f"Найдена новая версия: {latest['version']}",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 10))

        # карточка с changelog
        card = ttk.Frame(root_frame, style="Card.TFrame", padding=12)
        card.pack(fill="both", expand=True)

        ttk.Label(
            card,
            text="Описание изменений",
            style="CardTitle.TLabel",
        ).pack(anchor="w")

        self.txt = tk.Text(
            card,
            bg=BG_PANEL,
            fg=FG_TEXT,
            insertbackground=FG_TEXT,
            relief="flat",
            height=7,
            font=("Consolas", 9),
            wrap="word",
        )
        self.txt.pack(fill="both", expand=True, pady=(4, 8))

        notes = latest.get("notes") or "Нет описания изменений."
        self.txt.insert("1.0", notes)
        self.txt.configure(state="disabled")

        # прогресс
        bottom_card = ttk.Frame(card, style="Card.TFrame")
        bottom_card.pack(fill="x")

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(
            bottom_card,
            style="Update.Horizontal.TProgressbar",
            maximum=100.0,
            variable=self.progress_var,
        )
        self.progress.pack(fill="x", pady=(4, 2))

        self.status_var = tk.StringVar(value="Ожидание начала загрузки…")
        self.lbl_status = ttk.Label(
            bottom_card,
            textvariable=self.status_var,
            style="CardText.TLabel",
        )
        self.lbl_status.pack(anchor="w")

        # кнопки
        buttons = ttk.Frame(root_frame, style="Update.TFrame")
        buttons.pack(fill="x", pady=(10, 0))

        self.btn_install = ttk.Button(
            buttons,
            text="Установить",
            style="Accent.TButton",
            command=self._on_install_clicked,
        )
        self.btn_install.pack(side="right")

        self.btn_later = ttk.Button(
            buttons,
            text="Позже",
            style="Ghost.TButton",
            command=self._on_later_clicked,
        )
        self.btn_later.pack(side="right", padx=(0, 8))

        self.btn_skip = ttk.Button(
            buttons,
            text="Пропустить версию",
            style="Ghost.TButton",
            command=self._on_skip_clicked,
        )
        self.btn_skip.pack(side="left")

    # --- публичные методы для обновления из потока ---

    def set_progress(self, percent: float):
        self.progress_var.set(max(0.0, min(100.0, percent)))

    def set_status(self, text: str):
        self.status_var.set(text)

    def set_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for b in (self.btn_install, self.btn_skip, self.btn_later):
            self._set_btn_state(b, state)

    @staticmethod
    def _set_btn_state(btn, state: str):
        btn["state"] = state

    # --- обработчики кнопок ---

    def _on_install_clicked(self):
        if callable(self.on_install_cb):
            self.on_install_cb(self)

    def _on_skip_clicked(self):
        if callable(self.on_skip_cb):
            self.on_skip_cb(self)

    def _on_later_clicked(self):
        if callable(self.on_later_cb):
            self.on_later_cb(self)


# ---- UI-обвязка вокруг UpdateDialog ----
def _show_prompt(root, latest: dict):
    """Создаёт красивое окно и вешает на него логику скачивания/установки."""

    def do_skip(dlg: UpdateDialog):
        prefs = _prefs_load()
        prefs["skip_version"] = latest["version"]
        _prefs_save(prefs)
        dlg.destroy()

    def do_later(dlg: UpdateDialog):
        dlg.destroy()

    def do_install(dlg: UpdateDialog):
        dlg.set_buttons_enabled(False)
        dlg.set_status("Подготовка к загрузке…")
        dlg.set_progress(0.0)

        def worker():
            try:
                tmp_dir = tempfile.gettempdir()
                dest = os.path.join(
                    tmp_dir, f"MediaSearchSetup_{latest['version']}.exe"
                )

                def _cb(done, total):
                    pct = max(0, min(100, int(done * 100 / total)))
                    root.after(
                        0,
                        lambda: (
                            dlg.set_progress(pct),
                            dlg.set_status(f"Загрузка: {pct}%"),
                        ),
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
                        raise RuntimeError(
                            "Хеш файла не совпал. Файл повреждён или подменён."
                        )

                # Успех: запускаем установщик и выходим
                root.after(0, lambda: _run_installer_and_exit(dest))
            except Exception as e:
                def _on_err():
                    dlg.set_buttons_enabled(True)
                    dlg.set_progress(0.0)
                    dlg.set_status("Ошибка загрузки.")
                    messagebox.showerror(
                        "Обновление",
                        f"Ошибка загрузки/установки:\n{e}",
                    )
                root.after(0, _on_err)

        threading.Thread(target=worker, daemon=True).start()

    UpdateDialog(root, latest, on_install=do_install, on_skip=do_skip, on_later=do_later)


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
                        0,
                        lambda: messagebox.showinfo(
                            "Обновление", "У вас последняя версия."
                        ),
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
