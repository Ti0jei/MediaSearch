import os
import time
import logging
import tkinter as tk
import shutil
import json
import secrets
import socketserver
import queue
import subprocess
import re
import difflib
import random
from datetime import datetime
from bs4 import BeautifulSoup
from auto_update import check_for_updates_async
from download_manager import DownloadManager
from uc_driver import DriverPool, _safe_get_driver, KINOPUB_BASE
from tkinter import messagebox, filedialog, simpledialog, ttk
from pathlib import Path
from file_actions import export_and_load_index, normalize_name, VIDEO_EXTENSIONS, RELATED_EXTENSIONS
from file_actions import load_index_from_efu
from threaded_tasks import threaded_save_checked
from kino_pub_downloader import login_to_kino as real_login_to_kino
from urllib.parse import urljoin, quote_plus   # <── ДОБАВИЛИ quote_plus
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys 
# === НОВОЕ: Selenium для реального поиска ===
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import threading
from kino_hls import set_reencode as set_hls_reencode, retry_mux as hls_retry_mux
def ui_card(parent, *, title=None, subtitle=None, width=None):
    outer = tk.Frame(parent, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    tk.Frame(outer, bg=ACCENT, height=3).pack(fill="x", side="top")

    if title or subtitle:
        head = tk.Frame(outer, bg=BG_SURFACE)
        head.pack(fill="x", padx=18, pady=(16, 10))

        if title:
            tk.Label(head, text=title, bg=BG_SURFACE, fg=ACCENT,
                     font=("Segoe UI Semibold", 18)).pack(anchor="w")
        if subtitle:
            tk.Label(head, text=subtitle, bg=BG_SURFACE, fg=SUBTEXT,
                     font=("Segoe UI", 10), wraplength=700, justify="left").pack(anchor="w", pady=(6, 0))

    body = tk.Frame(outer, bg=BG_SURFACE)
    body.pack(fill="both", expand=True, padx=18, pady=(0, 16))

    if width:
        outer.configure(width=width)
        outer.pack_propagate(False)

    return outer, body

def open_settings():
    # На Windows overlay-снежок рисуется отдельным прозрачным окном.
    # Во время модальных диалогов (grab_set) он может блокировать клики,
    # поэтому временно отключаем overlay, оставляя безопасный SnowStrip.
    try:
        _prev_snow_suspend = bool(getattr(root, "_snow_overlay_suspended", False))
    except Exception:
        _prev_snow_suspend = False
    try:
        root._snow_overlay_suspended = True
    except Exception:
        pass
    try:
        ov = getattr(root, "_snow_overlay", None)
        if ov is not None:
            ov.set_enabled(False)
    except Exception:
        pass

    dlg = tk.Toplevel(root)
    try:
        dlg.iconbitmap(get_app_icon())
    except Exception:
        pass

    dlg.title("Настройки")
    dlg.transient(root)
    dlg.grab_set()
    dlg.resizable(True, True)
    dlg.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

    try:
        scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
    except Exception:
        scale = 1.0
    scale = max(1.0, min(3.0, scale))
    w, h = int(520 * scale), int(650 * scale)
    try:
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
        w = min(w, max(520, sw - 120))
        h = min(h, max(650, sh - 140))
    except Exception:
        pass
    x = root.winfo_rootx() + (root.winfo_width() - w)//2
    y = root.winfo_rooty() + (root.winfo_height() - h)//2
    dlg.geometry(f"{w}x{h}+{x}+{y}")
    try:
        dlg.minsize(520, 650)
    except Exception:
        pass

    tk.Frame(dlg, bg=ACCENT, height=3).pack(fill="x")

    body = tk.Frame(dlg, bg=BG_SURFACE)
    body.pack(fill="both", expand=True, padx=16, pady=12)

    tk.Label(body, text="Настройки", bg=BG_SURFACE, fg=TEXT, font=("Segoe UI Semibold", 14))\
        .pack(anchor="w", pady=(0, 8))

    # --- ТЕМА ---
    s = load_settings()
    theme_var = tk.StringVar(value=s.get("theme", "dark"))  # "dark" / "light"

    tk.Label(body, text="Тема:", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))\
        .pack(anchor="w")

    row_theme = tk.Frame(body, bg=BG_SURFACE)
    row_theme.pack(anchor="w", pady=(6, 10))

    def set_theme(name: str):
        ss = load_settings()
        ss["theme"] = name
        save_settings(ss)
        apply_theme(root, name)   # <-- живое применение
        try:
            if hasattr(root, "_update_sidebar_status"):
                root._update_sidebar_status()
        except Exception:
            pass


    rb1 = tk.Radiobutton(row_theme, text="Тёмная", value="dark", variable=theme_var,
                         bg=BG_SURFACE, fg=TEXT, selectcolor=BG_CARD, activebackground=BG_SURFACE,
                         command=lambda: set_theme(theme_var.get()))
    rb2 = tk.Radiobutton(row_theme, text="Светлая", value="light", variable=theme_var,
                         bg=BG_SURFACE, fg=TEXT, selectcolor=BG_CARD, activebackground=BG_SURFACE,
                         command=lambda: set_theme(theme_var.get()))
    rb1.pack(side="left", padx=(0, 12))
    rb2.pack(side="left")

    tk.Label(
        body,
        text="🎄 Новогодняя тема включается автоматически: 24 декабря — 31 января.",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(2, 0))

    # --- КОНВЕРТАЦИЯ HLS ---
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=10)

    hls_var = tk.BooleanVar(value=bool(s.get("hls_reencode", True)))

    def on_hls_toggle():
        v = bool(hls_var.get())
        ss = load_settings()
        ss["hls_reencode"] = v
        save_settings(ss)
        try:
            set_hls_reencode(v)
        except Exception:
            pass

    chk = tk.Checkbutton(
        body,
        text="Перекодировать HLS в фиксированный битрейт (NVENC)",
        variable=hls_var,
        command=on_hls_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    chk.pack(anchor="w")

    # --- KINO.PUB DOWNLOADER ---
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=10)

    tk.Label(body, text="Kino.pub:", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))\
        .pack(anchor="w")

    def _clamp_parallel(v) -> int:
        try:
            v = int(v)
        except Exception:
            v = 2
        return max(1, min(4, v))

    max_parallel_var = tk.IntVar(value=_clamp_parallel(s.get("kino_max_parallel", 2)))

    row_mp = tk.Frame(body, bg=BG_SURFACE)
    row_mp.pack(anchor="w", pady=(6, 0))
    tk.Label(row_mp, text="Параллельные загрузки:", bg=BG_SURFACE, fg=TEXT, font=("Segoe UI", 10))\
        .pack(side="left")

    sp = tk.Spinbox(
        row_mp,
        from_=1,
        to=4,
        width=4,
        textvariable=max_parallel_var,
        bg=FIELD_BG,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        font=("Segoe UI", 10),
        justify="center",
    )
    sp.pack(side="left", padx=(8, 0))
    try:
        style_entry(sp)
    except Exception:
        pass

    tk.Label(
        body,
        text="Применится после перезапуска приложения.",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
    ).pack(anchor="w", pady=(4, 0))

    def _save_max_parallel(*_):
        try:
            v = _clamp_parallel(max_parallel_var.get())
        except Exception:
            return
        try:
            if int(max_parallel_var.get()) != v:
                max_parallel_var.set(v)
        except Exception:
            pass
        ss = load_settings()
        ss["kino_max_parallel"] = v
        save_settings(ss)

    max_parallel_var.trace_add("write", _save_max_parallel)

    def _clamp_audio_parallel(v) -> int:
        try:
            v = int(v)
        except Exception:
            v = 1
        return max(1, min(4, v))

    audio_parallel_var = tk.IntVar(value=_clamp_audio_parallel(s.get("kino_audio_parallel_tracks", 1)))

    row_ap = tk.Frame(body, bg=BG_SURFACE)
    row_ap.pack(anchor="w", pady=(10, 0))
    tk.Label(row_ap, text="Параллельные аудиодорожки (на файл):", bg=BG_SURFACE, fg=TEXT, font=("Segoe UI", 10))\
        .pack(side="left")

    sp_ap = tk.Spinbox(
        row_ap,
        from_=1,
        to=4,
        width=4,
        textvariable=audio_parallel_var,
        bg=FIELD_BG,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        font=("Segoe UI", 10),
        justify="center",
    )
    sp_ap.pack(side="left", padx=(8, 0))
    try:
        style_entry(sp_ap)
    except Exception:
        pass

    tk.Label(
        body,
        text="Ускоряет тайтлы с большим числом дорожек, но повышает нагрузку на сеть.",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
    ).pack(anchor="w", pady=(4, 0))

    def _save_audio_parallel(*_):
        try:
            v = _clamp_audio_parallel(audio_parallel_var.get())
        except Exception:
            return
        try:
            if int(audio_parallel_var.get()) != v:
                audio_parallel_var.set(v)
        except Exception:
            pass
        ss = load_settings()
        ss["kino_audio_parallel_tracks"] = v
        save_settings(ss)
        try:
            root._kino_audio_parallel_tracks = int(v)
        except Exception:
            pass
        try:
            cb = getattr(root, "_update_kino_queue_rowheight", None)
            if callable(cb):
                cb()
        except Exception:
            pass

    audio_parallel_var.trace_add("write", _save_audio_parallel)

    queue_persist_var = tk.BooleanVar(value=bool(s.get("kino_queue_persist", True)))
    queue_autostart_var = tk.BooleanVar(value=bool(s.get("kino_queue_autostart_after_login", True)))

    def on_queue_persist_toggle():
        v = bool(queue_persist_var.get())
        ss = load_settings()
        ss["kino_queue_persist"] = v
        if not v:
            ss.pop("kino_queue", None)
        save_settings(ss)
        try:
            auto_chk.config(state=("normal" if v else "disabled"))
        except Exception:
            pass

    def on_queue_autostart_toggle():
        v = bool(queue_autostart_var.get())
        ss = load_settings()
        ss["kino_queue_autostart_after_login"] = v
        save_settings(ss)

    queue_chk = tk.Checkbutton(
        body,
        text="Сохранять очередь загрузок между запусками",
        variable=queue_persist_var,
        command=on_queue_persist_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    queue_chk.pack(anchor="w", pady=(8, 0))

    auto_chk = tk.Checkbutton(
        body,
        text="Авто-запускать очередь после входа в Kino.pub",
        variable=queue_autostart_var,
        command=on_queue_autostart_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    auto_chk.pack(anchor="w")
    if not bool(queue_persist_var.get()):
        auto_chk.config(state="disabled")

    auto_convert_var = tk.BooleanVar(value=bool(s.get("kino_auto_convert_all_audio", False)))

    def on_auto_convert_toggle():
        v = bool(auto_convert_var.get())
        ss = load_settings()
        ss["kino_auto_convert_all_audio"] = v
        save_settings(ss)
        try:
            setattr(root, "_kino_auto_convert_all_audio", v)
        except Exception:
            pass

    auto_convert_chk = tk.Checkbutton(
        body,
        text="Авто-конвертация (MUX) после загрузки — все аудиодорожки",
        variable=auto_convert_var,
        command=on_auto_convert_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    auto_convert_chk.pack(anchor="w", pady=(8, 0))
    tk.Label(
        body,
        text="Если выключено — после загрузки будет статус «Готово к конвертации» и нужно нажать «Конвертировать».",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    # --- KINO.PUB PROFILE PURGE ON STARTUP ---
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=10)

    purge_var = tk.BooleanVar(value=bool(s.get("purge_kino_profile_on_startup", True)))

    def on_purge_toggle():
        v = bool(purge_var.get())
        ss = load_settings()
        ss["purge_kino_profile_on_startup"] = v
        save_settings(ss)

    purge_chk = tk.Checkbutton(
        body,
        text="Удалять профиль Kino.pub при запуске",
        variable=purge_var,
        command=on_purge_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    purge_chk.pack(anchor="w")
    tk.Label(
        body,
        text="Рекомендуется для стабильной работы (сброс кеша/профиля браузера и UC-драйвера).",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    # --- BUILT-IN KINO.PUB ACCOUNT (NOT RECOMMENDED) ---
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=10)

    builtin_acc_var = tk.BooleanVar(value=bool(s.get("kino_use_builtin_account", False)))

    def on_builtin_acc_toggle():
        v = bool(builtin_acc_var.get())
        ss = load_settings()
        ss["kino_use_builtin_account"] = v
        save_settings(ss)

    tk.Checkbutton(
        body,
        text="Использовать встроенную учётную запись Kino.pub (не рекомендуется)",
        variable=builtin_acc_var,
        command=on_builtin_acc_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    ).pack(anchor="w")
    tk.Label(
        body,
        text="Включайте только при необходимости: логин/пароль и резервный код будут автоматически заполнены на странице входа.",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    def open_tm_instructions():
        # Требуем активный логин в Kino.pub (иначе кнопка не работает)
        try:
            if not kino_logged_in:
                show_login_required()
                return
        except Exception:
            pass

        token = ensure_bridge_token()
        script = f"""// ==UserScript==
// @name         Kino.pub -> Movie Tools
// @namespace    movie-tools
// @description  Отправка карточки Kino.pub в Movie Tools
// @version      1.0.3
// @match        https://kino.pub/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function () {{
  const TOKEN = '{token}';
  const ENDPOINT = 'http://127.0.0.1:47832/api/kino/add';

  const BTN_CSS = `
    .mt-dl-btn {{
      display: inline-flex; align-items: center; gap: 8px;
      padding: 10px 14px; margin-top: 8px;
      background: linear-gradient(135deg, #2c3a4a 0%, #1f2835 100%);
      color: #e8f5e1; border: 1px solid #2e3f50; border-radius: 10px;
      font: 14px "Segoe UI", sans-serif; cursor: pointer;
      box-shadow: 0 4px 12px rgba(0,0,0,0.25);
      transition: all 0.18s ease-in-out; text-decoration: none;
    }}
    .mt-dl-btn:hover {{
      background: linear-gradient(135deg, #3c5c3c 0%, #2f4a31 100%);
      border-color: #3fa46a; transform: translateY(-1px);
      box-shadow: 0 8px 18px rgba(0,0,0,0.32);
    }}
    .mt-dl-btn:active {{ transform: translateY(0); box-shadow: 0 4px 12px rgba(0,0,0,0.25); }}
    .mt-dl-btn .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #3fa46a; box-shadow: 0 0 8px rgba(63,164,106,0.7); }}
  `;
  const style = document.createElement('style');
  style.textContent = BTN_CSS;
  document.head.appendChild(style);

  function addButton(titleNode) {{
    if (!titleNode || titleNode.dataset.mtBtn) return;
    const btn = document.createElement('button');
    btn.className = 'mt-dl-btn';
    btn.innerHTML = '<span class="dot"></span><span>Скачать в Movie Tools</span>';
    btn.onclick = () => {{
      const title = (titleNode.innerText || document.title || '').trim();
      GM_xmlhttpRequest({{
        method: 'POST',
        url: ENDPOINT,
        headers: {{'Content-Type': 'application/json', 'X-Bridge-Token': TOKEN}},
        data: JSON.stringify({{ url: location.href, title }}),
        onload: (res) => {{
          if (res.status === 200) alert('Отправлено в очередь');
          else alert('Ошибка: ' + res.status + ' ' + (res.responseText || ''));
        }},
        onerror: (e) => alert('Не удалось отправить: ' + (e.error || 'net error')),
        timeout: 5000,
        ontimeout: () => alert('Таймаут запроса')
      }});
    }};
    titleNode.dataset.mtBtn = '1';
    titleNode.parentElement.insertBefore(btn, titleNode.nextSibling);
  }}

  function scan() {{
    const node = document.querySelector('div.page-content h3');
    if (node) addButton(node);
  }}
  scan();
  const mo = new MutationObserver(scan);
  mo.observe(document.body, {{ childList: true, subtree: true }});
}})();
"""
        try:
            downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        except Exception:
            downloads = os.path.expanduser("~")
        out_path = os.path.join(downloads, "kino_pub_movie_tools.user.js")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(script)
        except Exception as e:
            messagebox.showerror("Расширение", f"Не удалось сохранить файл:\n{e}")
            return

        info = (
            "Файл скрипта сохранён.\n\n"
            f"1) Путь: {out_path}\n"
            "2) Установите Tampermonkey: https://www.tampermonkey.net/\n"
            "3) Откройте файл (двойной клик или через Tampermonkey → Создать новый → вставить содержимое) и сохраните.\n"
            "4) Откройте карточку Kino.pub — кнопка «Скачать в Movie Tools» появится рядом с заголовком.\n\n"
            "Токен уже подставлен автоматически."
        )
        try:
            msg = tk.Toplevel(root)
            msg.title("Расширение")
            msg.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
            tk.Frame(msg, bg=ACCENT, height=2).pack(fill="x", side="top")
            body = tk.Frame(msg, bg=BG_SURFACE)
            body.pack(fill="both", expand=True, padx=12, pady=10)
            tk.Label(body, text="Инструкция по расширению", font=("Segoe UI Semibold", 12),
                     bg=BG_SURFACE, fg=TEXT).pack(anchor="w", pady=(0, 6))
            txt = tk.Text(body, width=80, height=10, wrap="word", bg=BG_SURFACE, fg=TEXT,
                          relief="flat", highlightthickness=1, highlightbackground=BORDER)
            txt.pack(fill="both", expand=True)
            txt.insert("1.0", info)
            txt.configure(state="disabled")
            link = tk.Label(body, text="Открыть Tampermonkey.net", fg=ACCENT, bg=BG_SURFACE,
                            cursor="hand2", font=("Segoe UI", 10, "underline"))
            link.pack(anchor="w", pady=(6, 0))
            link.bind("<Button-1>", lambda _e: webbrowser.open("https://www.tampermonkey.net/"))
            tk.Button(body, text="OK", command=msg.destroy, bg=BG_CARD, fg=TEXT,
                      relief="flat", highlightthickness=1, highlightbackground=BORDER).pack(pady=(8, 2))
            try:
                msg.grab_set()
            except Exception:
                pass
        except Exception:
            try:
                messagebox.showinfo("Расширение", info)
            except Exception:
                pass
        try:
            os.startfile(out_path)
        except Exception:
            pass

    btn_tm = tk.Button(body, text="Инструкция: расширение (Tampermonkey)", command=open_tm_instructions)
    style_secondary(btn_tm)
    btn_tm.pack(anchor="w", pady=(6, 0))
    try:
        if not kino_logged_in:
            btn_tm.config(state="disabled")
    except Exception:
        pass

    # --- POPUP NOTIFICATIONS (BOTTOM-RIGHT) ---
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=10)

    popups_var = tk.BooleanVar(value=bool(s.get("popup_notifications", True)))

    def on_popups_toggle():
        v = bool(popups_var.get())
        ss = load_settings()
        ss["popup_notifications"] = v
        save_settings(ss)

    pop_chk = tk.Checkbutton(
        body,
        text="Показывать всплывающие уведомления (внизу справа)",
        variable=popups_var,
        command=on_popups_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    pop_chk.pack(anchor="w")

    # --- WINDOWS SYSTEM TOASTS ---
    win_toasts_var = tk.BooleanVar(value=bool(s.get("win_toast_notifications", False)))

    def on_win_toasts_toggle():
        v = bool(win_toasts_var.get())
        ss = load_settings()
        ss["win_toast_notifications"] = v
        save_settings(ss)

    win_chk = tk.Checkbutton(
        body,
        text="Показывать системные уведомления Windows (Toast)",
        variable=win_toasts_var,
        command=on_win_toasts_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    win_chk.pack(anchor="w", pady=(6, 0))
    tk.Label(
        body,
        text="Работает только на Windows и зависит от системных настроек уведомлений.",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    # --- SYSTEM (TRAY / AUTOSTART) ---
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=10)

    tk.Label(body, text="Система:", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))\
        .pack(anchor="w")

    tray_var = tk.BooleanVar(value=bool(s.get("minimize_to_tray", False)))
    start_tray_var = tk.BooleanVar(value=bool(s.get("start_minimized_to_tray", False)))
    autostart_var = tk.BooleanVar(value=bool(s.get("autostart_windows", False)))

    def _apply_system_now():
        try:
            if hasattr(root, "_apply_system_settings"):
                root._apply_system_settings()
        except Exception:
            pass

    def on_tray_toggle():
        v = bool(tray_var.get())
        ss = load_settings()
        ss["minimize_to_tray"] = v
        # Старт в трее отключили: приложение всегда запускается развернутым в панели задач.
        ss["start_minimized_to_tray"] = False
        save_settings(ss)
        try:
            start_tray_var.set(False)
            start_chk.config(state="disabled")
        except Exception:
            pass
        _apply_system_now()

    def on_start_tray_toggle():
        # Старт в трее отключили: приложение всегда запускается развернутым в панели задач.
        v = False
        try:
            start_tray_var.set(False)
        except Exception:
            pass
        ss = load_settings()
        ss["start_minimized_to_tray"] = v
        save_settings(ss)

    def on_autostart_toggle():
        v = bool(autostart_var.get())
        ok, err = _set_windows_autostart(v)
        if not ok:
            try:
                autostart_var.set(not v)
            except Exception:
                pass
            messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск:\n{err}")
            return
        ss = load_settings()
        ss["autostart_windows"] = v
        save_settings(ss)

    tray_chk = tk.Checkbutton(
        body,
        text="Сворачивать в трей при закрытии (крестик)",
        variable=tray_var,
        command=on_tray_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    tray_chk.pack(anchor="w", pady=(6, 0))

    start_chk = tk.Checkbutton(
        body,
        text="Запускать свернутым в трей (отключено)",
        variable=start_tray_var,
        command=on_start_tray_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    start_chk.pack(anchor="w")
    start_chk.config(state="disabled")

    autostart_chk = tk.Checkbutton(
        body,
        text="Автозапуск с Windows",
        variable=autostart_var,
        command=on_autostart_toggle,
        bg=BG_SURFACE,
        fg=TEXT,
        activebackground=BG_SURFACE,
        activeforeground=TEXT,
        selectcolor=BG_CARD,
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )
    autostart_chk.pack(anchor="w")
    if os.name != "nt":
        tray_chk.config(state="disabled")
        start_chk.config(state="disabled")
        autostart_chk.config(state="disabled")

    tk.Frame(body, bg=BG_SURFACE).pack(fill="both", expand=True)

    btn_row = tk.Frame(body, bg=BG_SURFACE)
    btn_row.pack(fill="x", pady=(18, 0))

    def _open_folder(path: str):
        try:
            if not path:
                return
            if os.name == "nt":
                subprocess.Popen(["explorer", path])
            else:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    btn_logs = tk.Button(btn_row, text="Папка логов", command=lambda: _open_folder(os.path.join(os.getcwd(), "logs")))
    style_secondary(btn_logs)
    btn_logs.pack(side="left")

    btn_settings_dir = tk.Button(btn_row, text="Папка настроек", command=lambda: _open_folder(SETTINGS_DIR))
    style_secondary(btn_settings_dir)
    btn_settings_dir.pack(side="left", padx=(8, 0))

    b = tk.Button(btn_row, text="Закрыть", command=dlg.destroy)
    style_secondary(b)
    b.pack(side="right")

    dlg.bind("<Escape>", lambda e: dlg.destroy())
    dlg.bind("<Return>", lambda e: dlg.destroy())

    def _close_settings():
        try:
            dlg.grab_release()
        except Exception:
            pass
        try:
            dlg.destroy()
        except Exception:
            pass

        try:
            root._snow_overlay_suspended = _prev_snow_suspend
        except Exception:
            pass

        # Возвращаем overlay только если он не был ранее "засуспенжен"
        if not _prev_snow_suspend:
            try:
                enabled = bool(getattr(root, "_holiday_theme", False))
            except Exception:
                enabled = False
            try:
                ov2 = getattr(root, "_snow_overlay", None)
                if ov2 is not None:
                    ov2.set_enabled(enabled)
            except Exception:
                pass

    try:
        dlg.protocol("WM_DELETE_WINDOW", _close_settings)
    except Exception:
        pass
    try:
        b.config(command=_close_settings)
    except Exception:
        pass
    try:
        dlg.bind("<Escape>", lambda e: _close_settings())
        dlg.bind("<Return>", lambda e: _close_settings())
    except Exception:
        pass
    
APP_ICON = None
def get_app_icon() -> str:
    global APP_ICON
    if APP_ICON:
        return APP_ICON
    APP_ICON = resource_path("icon.ico")
    return APP_ICON

def resource_path(rel_path: str) -> str:
    # PyInstaller: onedir держит файлы рядом с exe (dist\\...\\icon.ico),
    # а sys._MEIPASS может указывать на dist\\...\\_internal. Поддерживаем оба.
    bases: list[str] = []
    try:
        if getattr(sys, "frozen", False):
            bases.append(os.path.dirname(sys.executable))
            meipass = getattr(sys, "_MEIPASS", None)
            if isinstance(meipass, str) and meipass:
                bases.append(meipass)
    except Exception:
        pass
    try:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    try:
        bases.append(os.getcwd())
    except Exception:
        pass

    for base in bases:
        try:
            full = os.path.join(base, rel_path)
            if os.path.exists(full):
                return full
        except Exception:
            continue

    # fallback: возвращаем путь из первого base (или относительный)
    for base in bases:
        try:
            return os.path.join(base, rel_path)
        except Exception:
            continue
    return rel_path

META_EXTENSIONS = set(RELATED_EXTENSIONS) | {
    ".nfo", ".xml", ".jpg", ".jpeg", ".png", ".webp", ".tbn"
}
VIDEO_EXTENSIONS = set(VIDEO_EXTENSIONS)
# --- Настройки (последняя папка сохранения и т.п.) ---
SETTINGS_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "MediaSearch")
os.makedirs(SETTINGS_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
AUTOSTART_REG_NAME = "MovieTools"

def _get_windows_autostart_command() -> str:
    try:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
    except Exception:
        pass

    # script mode: prefer pythonw.exe to avoid console
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    run_exe = pyw if pyw.exists() else exe
    script = Path(__file__).resolve()
    return f'"{run_exe}" "{script}"'

def _set_windows_autostart(enabled: bool) -> tuple[bool, str | None]:
    if os.name != "nt":
        return False, "Доступно только на Windows."
    try:
        import winreg
    except Exception as e:
        return False, f"winreg недоступен: {e}"

    try:
        cmd = _get_windows_autostart_command()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass
        return True, None
    except Exception as e:
        return False, str(e)

YEAR_RE = re.compile(r"^(.*?)[\s\u00A0]*\((\d{4})\)\s*$")
NOTIFICATIONS_ENABLED = True
# Ищем год в виде (YYYY) в ЛЮБОМ месте строки
YEAR_RE = re.compile(r"\((\d{4})\)")
year_cache: dict[str, str | None] = {}

YEAR_LINK_SELECTOR = "div.table-responsive table.table-striped a.text-success[href*='years=']"

# --- ОТДЕЛЬНЫЙ UC-драйвер ДЛЯ ПОИСКА/НОВИНОК ---
search_driver = None

def get_search_driver():
    """
    Отдельный UC-драйвер для поиска, на том же portable Chromium,
    с теми же куками, скрытый (как в загрузчике).
    """
    global search_driver
    if search_driver is None:
        search_driver = _safe_get_driver(
            status_cb=lambda msg: logging.info("[SEARCH] " + msg),
            suppress=True,                 # скрытое окно, как в DriverPool
            profile_tag="run",             # рабочий профиль
            preload_kino_cookies=True,     # сразу подгружаем куки kino.pub
            profile_name="UC_PROFILE_SEARCH",
        )
        try:
            # лёгкий прогрев домена / CF
            search_driver.get(KINOPUB_BASE)
        except Exception as e:
            logging.warning("SEARCH warmup failed: %s", e)

    return search_driver


def fetch_year_from_card(url: str) -> str | None:
    if url in year_cache:
        return year_cache[url]

    drv = get_search_driver()  # твой UC-драйвер для поиска/новинок
    try:
        drv.get(url)
        WebDriverWait(drv, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        html = drv.page_source
        soup = BeautifulSoup(html, "html.parser")

        # Ищем ссылку вида "…/movie?years=2025%3B2025" с текстом "2025"
        a = soup.select_one(YEAR_LINK_SELECTOR)
        if a:
            txt = a.get_text(strip=True)
            m = re.search(r"(19|20)\d{2}", txt)
            if m:
                year = m.group(0)
                year_cache[url] = year
                return year
    except Exception as e:
        logging.warning("fetch_year_from_card(%s) failed: %s", url, e)

    year_cache[url] = None
    return None

YEAR_TAIL_RE = re.compile(r"^(.*?)[\s\u00A0]*\((\d{4})\)\s*$")
YEAR_ANY_RE  = re.compile(r"\((\d{4})\)")

def split_title_year(line: str):
    line = (line or "").strip()
    if not line:
        return "", None

    # 1) сначала строгий вариант: "Название (2025)" в конце
    m = YEAR_TAIL_RE.match(line)
    if m:
        title = (m.group(1) or "").strip()
        year  = m.group(2)
        return (title or line), year

    # 2) иначе: год где угодно "(2025)" внутри
    m = YEAR_ANY_RE.search(line)
    if not m:
        return line, None

    year = m.group(1)
    title = line[:m.start()].strip()
    return (title or line), year

def cleanup_title(s: str) -> str:
    """Убираем спецсимволы, приводим к единому виду для индекса/поиска."""
    if not s:
        return ""
    # ё → е, чтобы "веселые" и "весёлые" совпадали
    s = s.replace("ё", "е").replace("Ё", "Е")
    # всё, что не буква/цифра/пробел -> пробел
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    # схлопываем пробелы
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("Ошибка сохранения настроек: %s", e)


DOWNLOAD_HISTORY_KEY = "download_history"
DOWNLOAD_HISTORY_MAX = 300

def ensure_bridge_token() -> str:
    try:
        s = load_settings()
    except Exception:
        s = {}
    tok = s.get("browser_bridge_token")
    if not tok:
        tok = secrets.token_hex(16)
        s["browser_bridge_token"] = tok
        try:
            save_settings(s)
        except Exception:
            pass
    return tok


def get_download_history() -> list[dict]:
    try:
        s = load_settings()
        hist = s.get(DOWNLOAD_HISTORY_KEY) or []
        if not isinstance(hist, list):
            return []
        return [h for h in hist if isinstance(h, dict)]
    except Exception:
        return []


def set_download_history(items: list[dict]):
    try:
        s = load_settings()
        s[DOWNLOAD_HISTORY_KEY] = items
        save_settings(s)
    except Exception:
        pass


def append_download_history(event: dict):
    try:
        if not isinstance(event, dict):
            return
        hist = get_download_history()
        hist.insert(0, event)
        if len(hist) > DOWNLOAD_HISTORY_MAX:
            del hist[DOWNLOAD_HISTORY_MAX:]
        set_download_history(hist)
    except Exception:
        pass


def clear_download_history():
    set_download_history([])


SHOW_QUEUE_CONTROLS = False  # скрыть блок: Импорт списка / Удалить / Запустить всё / Остановить
# --- Режим окна при старте ---
START_MAXIMIZED  = True   # развернуть на весь экран (обычный «максимизированный» режим)
START_FULLSCREEN = False  # полноэкранный режим без рамок (F11/ESC для выхода)
# UI scale factor (обновляется в dpi_scaling)
UI_SCALE = 1.0
# --- Логирование ---
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename=os.path.join("logs", "app.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
logging.info("=== Запуск Movie Tools ===")

# --- Данные/состояния ---
EFU_FILE = "all_movies.efu"
movie_index = []
found_files = []
checked_vars = []
index_loaded = False
current_page = 1
items_per_page = 100
search_meta = {}
request_rows_meta: dict[str, dict] = {}
req_checked_items: set[str] = set()
kino_urls_for_requests: dict[str, str] = {}
req_checked_items: set[str] = set()
search_driver = None
kino_logged_in = False  # есть ли рабочий логин в Kino.pub

# =========================
# THEME SYSTEM (Light/Dark)
# =========================

def _hex_to_rgb(h: str):
    h = (h or "").lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

def _rgb_to_hex(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"

def _mix(hex_a: str, hex_b: str, t: float):
    # t=0 -> a, t=1 -> b
    ar, ag, ab = _hex_to_rgb(hex_a)
    br, bg, bb = _hex_to_rgb(hex_b)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    b = int(ab + (bb - ab) * t)
    return _rgb_to_hex(r, g, b)

THEMES = {
    "light": {
        # нейтрали (можно потом подвинуть "теплее/холоднее")
        "BG_WINDOW":  "#f4f7fb",
        "BG_SURFACE": "#ffffff",
        "BG_CARD":    "#eef3fa",
        # делаем границы чуть контрастнее — в светлой теме иначе плохо видно поля ввода
        "BORDER":     "#b9c6d6",
        "TEXT":       "#0b1220",
        "SUBTEXT":    "#53637a",
        "TEXT_ON_ACCENT": "#ffffff",
        "FIELD_BG":   "#ffffff",
        "FIELD_BG_2": "#f3f6fc",
        "HOVER_BG":   "#e9f0fb",
        "ACTIVE_BG":  "#dceaff",
        "HEADER_BG":  "#eaf1ff",
        "MENU_BG":    "#ffffff",

        # палитра с картинки (LIGHT)
        "ACCENT":        "#bb86fc",   # purple
        "ACCENT_SECOND": "#bb86fc",   # blue
        "OK":            "#50e3c2",   # teal
        "ERROR":         "#ff5436",   # red/orange
        "WARN":          "#e39801",   # orange
        "OK2":           "#8eaf20",   # green

        # hover для акцента
        "ACCENT_HOVER":  "#bb86fc",
    },

    "dark": {
        # нейтрали (под тёмный UI)
        "BG_WINDOW":  "#0f1720",
        "BG_SURFACE": "#131f2b",
        "BG_CARD":    "#172637",
        "BORDER":     "#26384b",
        "TEXT":       "#f3f7ff",
        "SUBTEXT":    "#a6b2c5",
        "TEXT_ON_ACCENT": "#0b1220",
        "FIELD_BG":   "#101a24",
        "FIELD_BG_2": "#12202c",
        "HOVER_BG":   "#1b2b3a",
        "ACTIVE_BG":  "#203449",
        "HEADER_BG":  "#182636",
        "MENU_BG":    "#121c26",

        # палитра с картинки (DARK)
        "ACCENT":        "#bb86fc",   # purple
        "ACCENT_SECOND": "#bb86fc",   # blue
        "OK":            "#50e3c2",   # teal
        "ERROR":         "#ff5436",   # red/orange
        "WARN":          "#f1c40f",   # yellow
        "OK2":           "#a5c53c",   # green

        "ACCENT_HOVER":  "#bb86fc",
    }
}

CURRENT_THEME = "dark"

# -----------------
# Seasonal themes 🎄
# -----------------
HOLIDAY_OVERRIDES = {
    # Новогодняя палитра: красный/зелёный/белый
    "ACCENT": "#d32f2f",        # red 700
    "ACCENT_HOVER": "#ef5350",  # red 400
    "ACCENT_SECOND": "#2e7d32", # green 800
    "TEXT_ON_ACCENT": "#ffffff",
}


def _is_holiday_season(dt: datetime | None = None) -> bool:
    """
    Новогодняя тема активна автоматически: 24 декабря — 31 января (включительно).
    """
    try:
        dt = dt or datetime.now()
        m = int(getattr(dt, "month", 0) or 0)
        d = int(getattr(dt, "day", 0) or 0)
    except Exception:
        return False

    if m == 12 and d >= 24:
        return True
    if m == 1 and d <= 31:
        return True
    return False


def _build_theme_palette(theme_name: str, *, holiday: bool = False) -> dict:
    base = THEMES.get(theme_name, THEMES["dark"]).copy()
    if holiday:
        base.update(HOLIDAY_OVERRIDES)
    return base

# Глобальные цвета (как у тебя сейчас)
BG_WINDOW  = THEMES[CURRENT_THEME]["BG_WINDOW"]
BG_SURFACE = THEMES[CURRENT_THEME]["BG_SURFACE"]
BG_CARD    = THEMES[CURRENT_THEME]["BG_CARD"]
BORDER     = THEMES[CURRENT_THEME]["BORDER"]
TEXT       = THEMES[CURRENT_THEME]["TEXT"]
SUBTEXT    = THEMES[CURRENT_THEME]["SUBTEXT"]

ACCENT        = THEMES[CURRENT_THEME]["ACCENT"]
ACCENT_HOVER  = THEMES[CURRENT_THEME]["ACCENT_HOVER"]
ACCENT_SECOND = THEMES[CURRENT_THEME]["ACCENT_SECOND"]

HOVER_BG   = THEMES[CURRENT_THEME]["HOVER_BG"]
ACTIVE_BG  = THEMES[CURRENT_THEME]["ACTIVE_BG"]
FIELD_BG   = THEMES[CURRENT_THEME]["FIELD_BG"]
FIELD_BG_2 = THEMES[CURRENT_THEME]["FIELD_BG_2"]
HEADER_BG  = THEMES[CURRENT_THEME]["HEADER_BG"]
MENU_BG    = THEMES[CURRENT_THEME]["MENU_BG"]

ERROR      = THEMES[CURRENT_THEME]["ERROR"]
WARN       = THEMES[CURRENT_THEME]["WARN"]
OK         = THEMES[CURRENT_THEME]["OK"]
OK2        = THEMES[CURRENT_THEME]["OK2"]


def _apply_globals_from_theme(theme_name: str):
    _apply_globals_from_palette(theme_name, THEMES[theme_name])


def _apply_globals_from_palette(theme_name: str, palette: dict):
    global CURRENT_THEME
    global BG_WINDOW, BG_SURFACE, BG_CARD, BORDER, TEXT, SUBTEXT
    global ACCENT, ACCENT_HOVER, ACCENT_SECOND
    global HOVER_BG, ACTIVE_BG, FIELD_BG, FIELD_BG_2, HEADER_BG, MENU_BG
    global ERROR, WARN, OK, OK2
    global TEXT_ON_ACCENT
    t = palette
    CURRENT_THEME = theme_name
    TEXT_ON_ACCENT = t["TEXT_ON_ACCENT"]
    BG_WINDOW  = t["BG_WINDOW"]
    BG_SURFACE = t["BG_SURFACE"]
    BG_CARD    = t["BG_CARD"]
    BORDER     = t["BORDER"]
    TEXT       = t["TEXT"]
    SUBTEXT    = t["SUBTEXT"]

    ACCENT        = t["ACCENT"]
    ACCENT_HOVER  = t["ACCENT_HOVER"]
    ACCENT_SECOND = t["ACCENT_SECOND"]

    HOVER_BG   = t["HOVER_BG"]
    ACTIVE_BG  = t["ACTIVE_BG"]
    FIELD_BG   = t["FIELD_BG"]
    FIELD_BG_2 = t["FIELD_BG_2"]
    HEADER_BG  = t["HEADER_BG"]
    MENU_BG    = t["MENU_BG"]

    ERROR      = t["ERROR"]
    WARN       = t["WARN"]
    OK         = t["OK"]
    OK2        = t["OK2"]


# ---------- UI helpers ----------
# ---------- live theme apply ----------
class ModernCheckbox(tk.Frame):
    """
    Красивый чекбокс на Canvas (без 'плюсика' внутри).
    """
    def __init__(self, parent, variable: tk.BooleanVar, command=None,
                 size=18, bg=None, state="normal"):
        super().__init__(parent, bg=(bg or BG_SURFACE), highlightthickness=0, bd=0)

        self.var = variable
        self.command = command
        self.size = int(size)
        self.state = state
        self._hover = False
        self._bg = (bg or BG_SURFACE)

        self.cv = tk.Canvas(
            self, width=self.size, height=self.size,
            bg=self._bg, highlightthickness=0, bd=0
        )
        self.cv.pack()

        self.cv.bind("<Button-1>", self._toggle)
        self.cv.bind("<Enter>", lambda e: self._set_hover(True))
        self.cv.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._toggle)

        self.var.trace_add("write", lambda *_: self.redraw())
        self.redraw()

    def set_bg(self, bg: str):
        self._bg = bg
        self.configure(bg=bg)
        self.cv.configure(bg=bg)
        self.redraw()

    def set_state(self, state: str):
        self.state = state
        self.redraw()

    def _set_hover(self, v: bool):
        self._hover = v
        self.redraw()

    def _toggle(self, _=None):
        if self.state != "normal":
            return "break"
        self.var.set(not bool(self.var.get()))
        if callable(self.command):
            try:
                self.command()
            except Exception:
                pass
        return "break"

    # ---- drawing helpers ----
    def _round_fill(self, x1, y1, x2, y2, r, fill):
        cv = self.cv
        cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")
        cv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="")

        cv.create_arc(x1, y1, x1 + 2*r, y1 + 2*r, start=90,  extent=90,  style="pieslice",
                      fill=fill, outline="")
        cv.create_arc(x2 - 2*r, y1, x2, y1 + 2*r, start=0,   extent=90,  style="pieslice",
                      fill=fill, outline="")
        cv.create_arc(x1, y2 - 2*r, x1 + 2*r, y2, start=180, extent=90,  style="pieslice",
                      fill=fill, outline="")
        cv.create_arc(x2 - 2*r, y2 - 2*r, x2, y2, start=270, extent=90,  style="pieslice",
                      fill=fill, outline="")

    def _round_stroke(self, x1, y1, x2, y2, r, color, w=1):
        cv = self.cv
        # стороны
        cv.create_line(x1 + r, y1, x2 - r, y1, fill=color, width=w)
        cv.create_line(x1 + r, y2, x2 - r, y2, fill=color, width=w)
        cv.create_line(x1, y1 + r, x1, y2 - r, fill=color, width=w)
        cv.create_line(x2, y1 + r, x2, y2 - r, fill=color, width=w)
        # углы дугами (без пересечений в центре)
        cv.create_arc(x1, y1, x1 + 2*r, y1 + 2*r, start=90,  extent=90, style="arc",
                      outline=color, width=w)
        cv.create_arc(x2 - 2*r, y1, x2, y1 + 2*r, start=0,   extent=90, style="arc",
                      outline=color, width=w)
        cv.create_arc(x1, y2 - 2*r, x1 + 2*r, y2, start=180, extent=90, style="arc",
                      outline=color, width=w)
        cv.create_arc(x2 - 2*r, y2 - 2*r, x2, y2, start=270, extent=90, style="arc",
                      outline=color, width=w)

    def redraw(self):
        cv = self.cv
        cv.delete("all")

        s = self.size
        pad = 2
        r = max(4, int(s * 0.22))

        checked = bool(self.var.get())
        disabled = (self.state != "normal")

        if disabled:
            border = BORDER
            fill = BG_CARD
            mark = SUBTEXT
            ring = None
        else:
            border = ACCENT_SECOND if self._hover else BORDER
            fill = ACCENT if checked else BG_CARD
            mark = TEXT_ON_ACCENT
            ring = ACCENT_SECOND if (self._hover or checked) else None

        x1, y1, x2, y2 = pad, pad, s - pad, s - pad

        # subtle ring (наружный)
        if ring:
            rx1, ry1, rx2, ry2 = x1 - 1, y1 - 1, x2 + 1, y2 + 1
            self._round_stroke(rx1, ry1, rx2, ry2, r + 1, ring, w=1)

        # тело
        self._round_fill(x1, y1, x2, y2, r, fill)

        # рамка
        self._round_stroke(x1, y1, x2, y2, r, border, w=1)

        # галка
        if checked:
            p1 = (s * 0.28, s * 0.55)
            p2 = (s * 0.43, s * 0.70)
            p3 = (s * 0.74, s * 0.34)
            cv.create_line(
                p1[0], p1[1], p2[0], p2[1], p3[0], p3[1],
                fill=mark,
                width=max(2, s // 7),
                capstyle="round",
                joinstyle="round"
            )


_THEMED = {
    "primary_buttons": set(),
    "secondary_buttons": set(),
    "entries": set(),
    "texts": set(),
    "menus": set(),
}

def _remember(kind: str, w):
    try:
        _THEMED[kind].add(w)
    except Exception:
        pass

def _walk_widgets(root_widget):
    stack = [root_widget]
    while stack:
        w = stack.pop()
        yield w
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass

def _color_map_update(root_widget, old: dict, new: dict):
    props = (
        "bg", "fg",
        "activebackground", "activeforeground",
        "highlightbackground", "highlightcolor",
        "selectcolor", "insertbackground",
        "disabledforeground",
        "disabledbackground",
        "readonlybackground",
        "buttonbackground",
        "selectbackground",
        "selectforeground",
    )

    cmap = {old[k]: new[k] for k in old.keys() if old.get(k) and new.get(k)}

    for w in _walk_widgets(root_widget):
        for p in props:
            try:
                cur = w.cget(p)
            except Exception:
                continue

            # ВАЖНО: Tcl_Obj -> str, иначе "unhashable type"
            try:
                cur = str(cur)
            except Exception:
                continue

            new_val = cmap.get(cur)
            if new_val:
                try:
                    w.configure(**{p: new_val})
                except Exception:
                    pass


def register_menu(m: tk.Menu):
    _remember("menus", m)
    _restyle_menu(m)

def _restyle_menu(m: tk.Menu):
    # Меню на Windows может частично игнорить цвета — но пробуем
    try:
        m.configure(
            bg=MENU_BG,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=TEXT_ON_ACCENT,
            relief="flat",
            bd=0,
            tearoff=0,
        )
    except Exception:
        pass

def _restyle_all_menus():
    for m in list(_THEMED["menus"]):
        try:
            if m.winfo_exists():
                _restyle_menu(m)
        except Exception:
            pass
def apply_ttk_theme():
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass

        st.configure("TFrame", background=BG_SURFACE)
        st.configure("TLabel", background=BG_SURFACE, foreground=TEXT)

        st.configure("TRadiobutton", background=BG_SURFACE, foreground=TEXT)
        st.configure("TCheckbutton", background=BG_SURFACE, foreground=TEXT)

        # часто работает в clam:
        try:
            st.map("TRadiobutton",
                indicatorcolor=[("selected", ACCENT), ("!selected", BORDER)])
            st.map("TCheckbutton",
                indicatorcolor=[("selected", ACCENT), ("!selected", BORDER)])
        except Exception:
            pass
def apply_theme(root: tk.Tk, theme_name: str):
    if theme_name not in THEMES:
        return

    try:
        old = getattr(root, "_theme_palette", None) or THEMES.get(CURRENT_THEME, THEMES["dark"]).copy()
    except Exception:
        old = THEMES.get(CURRENT_THEME, THEMES["dark"]).copy()

    try:
        try:
            root._holiday_theme = _is_holiday_season()
        except Exception:
            root._holiday_theme = False
        holiday = bool(getattr(root, "_holiday_theme", False))
    except Exception:
        holiday = False

    new = _build_theme_palette(theme_name, holiday=holiday)
    _apply_globals_from_palette(theme_name, new)
    apply_ttk_theme()
    try:
        root._theme_palette = new.copy()
    except Exception:
        pass

    # прокатываем замену цветов по всему дереву виджетов
    _color_map_update(root, old, new)

    # ttk (Treeview/Progressbar)
    try:
        st = ttk.Style()
        style_tree(st)
        st.configure("TProgressbar", troughcolor=BG_CARD, background=ACCENT)
    except Exception:
        pass

    # перестайлим “важные” виджеты, чтобы обновились hover/bindings
    for b in list(_THEMED["primary_buttons"]):
        try:
            if b.winfo_exists():
                style_primary(b)
        except Exception:
            pass

    for b in list(_THEMED["secondary_buttons"]):
        try:
            if b.winfo_exists():
                style_secondary(b)
        except Exception:
            pass

    for e in list(_THEMED["entries"]):
        try:
            if e.winfo_exists():
                style_entry(e)
        except Exception:
            pass

    for t in list(_THEMED["texts"]):
        try:
            if t.winfo_exists():
                style_text(t)
        except Exception:
            pass

    _restyle_all_menus()

    try:
        root.configure(bg=BG_WINDOW)
    except Exception:
        pass

    # Новогодний "снежок" (если включён)
    try:
        enabled = bool(getattr(root, "_holiday_theme", False))
    except Exception:
        enabled = False
    try:
        overlay_allowed = enabled and (not bool(getattr(root, "_snow_overlay_suspended", False)))
    except Exception:
        overlay_allowed = enabled

    # безопасная полоска всегда ок
    try:
        strip = getattr(root, "_snow_strip", None)
        if strip is not None:
            try:
                strip.update_theme()
            except Exception:
                pass
            try:
                strip.set_enabled(enabled)
            except Exception:
                pass
    except Exception:
        pass

    # overlay по всему окну может быть отключён во время модальных окон
    try:
        ov = getattr(root, "_snow_overlay", None)
        if ov is not None:
            try:
                ov.update_theme()
            except Exception:
                pass
            try:
                ov.set_enabled(overlay_allowed)
            except Exception:
                pass
    except Exception:
        pass

    # Снеговик в шапке
    try:
        snowman = getattr(root, "_snowman_label", None)
        if snowman is not None:
            try:
                snowman.configure(bg=BG_SURFACE, fg=TEXT)
            except Exception:
                pass
            try:
                if bool(getattr(root, "_holiday_theme", False)):
                    if not snowman.winfo_ismapped():
                        snowman.pack(side="left", padx=(6, 0), pady=10)
                else:
                    if snowman.winfo_ismapped():
                        snowman.pack_forget()
            except Exception:
                pass
    except Exception:
        pass
    

def style_tree(style: ttk.Style):
    try:
        style.theme_use("clam")
    except Exception:
        pass

    try:
        scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
    except Exception:
        scale = 1.0
    scale = max(1.0, min(3.0, scale))
    rowheight = max(22, int(26 * scale))

    style.configure(
        "Treeview",
        background=BG_SURFACE,
        foreground=TEXT,
        rowheight=rowheight,
        fieldbackground=BG_SURFACE,
        font=("Segoe UI", 10),
        borderwidth=0,
    )
    style.configure(
        "Treeview.Heading",
        background=HEADER_BG,
        foreground=TEXT,
        font=("Segoe UI Semibold", 10),
        relief="flat",
    )
    style.map(
        "Treeview",
        background=[("selected", ACCENT)],
        foreground=[("selected", TEXT_ON_ACCENT)],
    )

def dpi_scaling(root: tk.Tk):
    try:
        px = root.winfo_fpixels("1i")
        factor = max(1.0, round(px / 96, 2))
        root.tk.call("tk", "scaling", factor)
        try:
            global UI_SCALE
            UI_SCALE = float(factor)
        except Exception:
            pass
        logging.info(f"UI scaling set to {factor}")
        return factor
    except Exception as e:
        logging.warning(f"Scaling failed: {e}")
        return 1.0

def enable_windows_dpi_awareness():
    """
    Делает приложение DPI-aware на Windows, чтобы интерфейс не был "мыльным/пиксельным"
    при масштабировании (125%/150% и т.д.).
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        # Windows 10+ (per-monitor v2)
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass

        # Windows 8.1+
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass

        # Windows Vista+
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass

def fade_in(window, alpha=0.0):
    alpha += 0.05
    if alpha <= 1.0:
        window.attributes("-alpha", alpha)
        window.after(20, lambda: fade_in(window, alpha))


class SnowStrip:
    """
    Небольшой декоративный "снежок" под шапкой.
    Рисуется на Canvas и не перекрывает элементы управления.
    """

    def __init__(self, root: tk.Tk, *, after_widget: tk.Widget, height: int = 28):
        self.root = root
        self.after_widget = after_widget
        self.height = max(18, int(height))

        self.frame = tk.Frame(root, bg=BG_SURFACE, height=self.height)
        self.frame.pack_propagate(False)

        self.canvas = tk.Canvas(
            self.frame,
            bg=BG_SURFACE,
            height=self.height,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self._enabled = False
        self._job = None
        self._flakes: list[dict] = []
        self.frame.bind("<Configure>", lambda _e: self._ensure_flakes())

    def _flake_color(self) -> str:
        try:
            if CURRENT_THEME == "light":
                return _mix(SUBTEXT, BG_SURFACE, 0.35)
            return _mix(TEXT, BG_SURFACE, 0.15)
        except Exception:
            return "#ffffff" if CURRENT_THEME != "light" else "#90a4ae"

    def show(self):
        try:
            if self.frame.winfo_ismapped():
                return
        except Exception:
            pass
        try:
            self.frame.pack(after=self.after_widget, side="top", fill="x")
        except Exception:
            try:
                self.frame.pack(side="top", fill="x")
            except Exception:
                pass

    def hide(self):
        try:
            if self._job is not None:
                try:
                    self.root.after_cancel(self._job)
                except Exception:
                    pass
                self._job = None
            self.canvas.delete("all")
            self._flakes.clear()
        except Exception:
            pass
        try:
            self.frame.pack_forget()
        except Exception:
            pass

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self._enabled:
            if enabled:
                self.update_theme()
            return

        self._enabled = enabled
        if not enabled:
            self.hide()
            return

        self.show()
        self.update_theme()
        self._ensure_flakes()
        self._tick()

    def update_theme(self):
        try:
            self.frame.configure(bg=BG_SURFACE)
            self.canvas.configure(bg=BG_SURFACE)
        except Exception:
            pass

        try:
            c = self._flake_color()
            for f in self._flakes:
                try:
                    self.canvas.itemconfigure(f["id"], fill=c, outline="")
                except Exception:
                    pass
        except Exception:
            pass

    def _ensure_flakes(self):
        if not self._enabled:
            return

        try:
            w = int(self.canvas.winfo_width() or 0)
            h = int(self.canvas.winfo_height() or self.height or 0)
        except Exception:
            w, h = 0, self.height

        if w < 50 or h < 10:
            try:
                self.root.after(120, self._ensure_flakes)
            except Exception:
                pass
            return

        desired = max(18, min(90, int(w / 22)))
        c = self._flake_color()

        while len(self._flakes) < desired:
            r = random.randint(1, 3)
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            vy = random.uniform(0.6, 1.9) + (r * 0.2)
            vx = random.uniform(-0.25, 0.25)
            fid = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=c, outline="")
            self._flakes.append({"id": fid, "x": x, "y": y, "r": r, "vy": vy, "vx": vx})

        while len(self._flakes) > desired:
            f = self._flakes.pop()
            try:
                self.canvas.delete(f["id"])
            except Exception:
                pass

    def _tick(self):
        if not self._enabled:
            return

        try:
            w = int(self.canvas.winfo_width() or 0)
            h = int(self.canvas.winfo_height() or self.height or 0)
        except Exception:
            w, h = 0, self.height

        if w <= 0 or h <= 0:
            self._job = self.root.after(120, self._tick)
            return

        for f in list(self._flakes):
            try:
                f["y"] += float(f["vy"])
                f["x"] += float(f["vx"])

                r = float(f["r"])
                if f["y"] > (h + r + 2):
                    f["y"] = -r
                    f["x"] = random.uniform(0, w)
                    f["vy"] = random.uniform(0.6, 1.9) + (r * 0.2)
                    f["vx"] = random.uniform(-0.25, 0.25)

                if f["x"] < -r:
                    f["x"] = w + r
                elif f["x"] > (w + r):
                    f["x"] = -r

                self.canvas.coords(f["id"], f["x"] - r, f["y"] - r, f["x"] + r, f["y"] + r)
            except Exception:
                pass

        self._job = self.root.after(33, self._tick)


class SnowOverlay:
    """
    Снежок поверх всего окна (Windows):
    - отдельный прозрачный Toplevel с Canvas
    - click-through через WS_EX_TRANSPARENT, чтобы не ломать клики/скролл
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self._enabled = False
        self._job = None
        self._sync_job = None
        self._flakes: list[dict] = []
        self._key = "#010203"  # «ключевой» цвет для прозрачности

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        # Не делаем transient: так модальные окна (настройки/диалоги) остаются кликабельными сверху.
        try:
            self.win.configure(bg=self._key)
        except Exception:
            pass
        try:
            self.win.wm_attributes("-transparentcolor", self._key)
        except Exception:
            # Если прозрачность недоступна — просто отключим «везде»,
            # оставив возможность использовать SnowStrip.
            self.win.destroy()
            raise

        self.canvas = tk.Canvas(self.win, bg=self._key, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._ensure_flakes())

        # Если вдруг click-through не сработает и overlay начнёт перехватывать клики —
        # ловим событие и аварийно отключаем overlay, возвращая кликабельность UI.
        try:
            self.win.bind("<Button-1>", self._panic_disable, add="+")
            self.win.bind("<Button-2>", self._panic_disable, add="+")
            self.win.bind("<Button-3>", self._panic_disable, add="+")
            self.win.bind("<MouseWheel>", self._panic_disable, add="+")
        except Exception:
            pass

        try:
            self.root.bind("<Configure>", self._on_root_configure, add="+")
            self.root.bind("<Map>", self._on_root_map, add="+")
            self.root.bind("<Unmap>", self._on_root_unmap, add="+")
        except Exception:
            pass

        try:
            self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        except Exception:
            pass

        # click-through (Windows): чтобы окно не перехватывало мышь
        try:
            self.win.update_idletasks()
            self._set_clickthrough()
        except Exception:
            pass

    def _panic_disable(self, _event=None):
        # Если это событие пришло — overlay реально перехватил мышь.
        # Мгновенно выключаем overlay и оставляем только SnowStrip.
        try:
            self.set_enabled(False)
        except Exception:
            pass
        try:
            enabled = bool(getattr(self.root, "_holiday_theme", False))
        except Exception:
            enabled = False
        try:
            strip = getattr(self.root, "_snow_strip", None)
            if strip is not None:
                strip.set_enabled(enabled)
        except Exception:
            pass
        return "break"

    def _on_root_configure(self, event):
        try:
            if event.widget is not self.root:
                return
        except Exception:
            return
        self._schedule_sync()

    def _on_root_map(self, event):
        try:
            if event.widget is not self.root:
                return
        except Exception:
            return
        if not self._enabled:
            return
        try:
            self.show()
        except Exception:
            self._schedule_sync()

    def _on_root_unmap(self, event):
        try:
            if event.widget is not self.root:
                return
        except Exception:
            return
        try:
            self.win.withdraw()
        except Exception:
            pass

    def _on_root_destroy(self, event):
        try:
            if event.widget is not self.root:
                return
        except Exception:
            return
        self._safe_destroy()

    def _safe_destroy(self):
        try:
            if self._job is not None:
                self.root.after_cancel(self._job)
        except Exception:
            pass
        self._job = None
        try:
            if self._sync_job is not None:
                self.root.after_cancel(self._sync_job)
        except Exception:
            pass
        self._sync_job = None
        try:
            if self.win.winfo_exists():
                self.win.destroy()
        except Exception:
            pass

    def _set_clickthrough(self):
        """
        Возвращает True, если click-through успешно включён.
        Если False — overlay лучше выключить, иначе он может блокировать клики по UI.
        """
        if os.name != "nt":
            return False
        try:
            hwnd = int(self.win.winfo_id() or 0)
            if not hwnd:
                return False

            # 1) pywin32 (чаще работает стабильнее)
            try:
                import win32gui
                import win32con

                style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
                WS_EX_NOACTIVATE = 0x08000000
                new_style = int(style) | int(win32con.WS_EX_LAYERED) | int(win32con.WS_EX_TRANSPARENT) | int(
                    win32con.WS_EX_TOOLWINDOW
                ) | int(WS_EX_NOACTIVATE)

                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, new_style)
                win32gui.SetWindowPos(
                    hwnd,
                    0,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE
                    | win32con.SWP_NOSIZE
                    | win32con.SWP_NOZORDER
                    | win32con.SWP_FRAMECHANGED
                    | win32con.SWP_NOACTIVATE,
                )
                after = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
                return bool(after & int(win32con.WS_EX_TRANSPARENT))
            except Exception:
                pass

            # 2) ctypes fallback
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000

            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020

            GetLong = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            SetLong = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            try:
                GetLong.argtypes = [wintypes.HWND, wintypes.INT]
                GetLong.restype = ctypes.c_ssize_t
                SetLong.argtypes = [wintypes.HWND, wintypes.INT, ctypes.c_ssize_t]
                SetLong.restype = ctypes.c_ssize_t
            except Exception:
                pass

            style = int(GetLong(hwnd, GWL_EXSTYLE))
            new_style = int(style) | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            SetLong(hwnd, GWL_EXSTYLE, new_style)
            try:
                user32.SetWindowPos(
                    hwnd,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE,
                )
            except Exception:
                pass

            after = int(GetLong(hwnd, GWL_EXSTYLE))
            return bool(after & WS_EX_TRANSPARENT)
        except Exception:
            return False

    def _flake_color(self) -> str:
        try:
            if CURRENT_THEME == "light":
                return _mix(SUBTEXT, BG_SURFACE, 0.35)
            return _mix(TEXT, BG_SURFACE, 0.15)
        except Exception:
            return "#ffffff" if CURRENT_THEME != "light" else "#90a4ae"

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self._enabled:
            if enabled:
                self.update_theme()
                self._ensure_flakes()
                try:
                    if not self._set_clickthrough():
                        self._enabled = False
                        self.hide()
                except Exception:
                    pass
            return

        self._enabled = enabled
        if not enabled:
            self.hide()
            return

        self.show()
        self.update_theme()
        self._ensure_flakes()
        self._tick()

    def show(self):
        self._schedule_sync()
        try:
            if self.win.state() == "withdrawn":
                self.win.deiconify()
        except Exception:
            try:
                self.win.deiconify()
            except Exception:
                pass
        try:
            self.win.update_idletasks()
            self._ensure_flakes()
        except Exception:
            pass
        try:
            if not self._set_clickthrough():
                self._enabled = False
                self.hide()
                return
        except Exception:
            pass
        try:
            self.win.lift(self.root)
        except Exception:
            pass

    def hide(self):
        try:
            if self._job is not None:
                try:
                    self.root.after_cancel(self._job)
                except Exception:
                    pass
                self._job = None
        except Exception:
            pass
        try:
            self.canvas.delete("all")
            self._flakes.clear()
        except Exception:
            pass
        try:
            self.win.withdraw()
        except Exception:
            pass

    def update_theme(self):
        try:
            self.win.configure(bg=self._key)
            self.canvas.configure(bg=self._key)
        except Exception:
            pass
        try:
            c = self._flake_color()
            for f in self._flakes:
                try:
                    self.canvas.itemconfigure(f["id"], fill=c, outline="")
                except Exception:
                    pass
        except Exception:
            pass

    def _schedule_sync(self):
        if self._sync_job is not None:
            return
        try:
            self._sync_job = self.root.after(60, self._sync_geometry)
        except Exception:
            self._sync_job = None
            self._sync_geometry()

    def _sync_geometry(self):
        self._sync_job = None
        if not self._enabled:
            return

        try:
            if str(self.root.state()) == "iconic" or not bool(self.root.winfo_viewable()):
                self.win.withdraw()
                return
        except Exception:
            pass

        try:
            x = int(self.root.winfo_rootx())
            y = int(self.root.winfo_rooty())
            w = int(self.root.winfo_width())
            h = int(self.root.winfo_height())
        except Exception:
            return

        if w < 200 or h < 200:
            try:
                self._sync_job = self.root.after(120, self._sync_geometry)
            except Exception:
                pass
            return

        try:
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            try:
                if self.win.state() == "withdrawn":
                    self.win.deiconify()
            except Exception:
                pass
            try:
                self.win.update_idletasks()
                self._ensure_flakes()
            except Exception:
                pass
            try:
                if not self._set_clickthrough():
                    self._enabled = False
                    self.hide()
                    return
            except Exception:
                pass
            self.win.lift(self.root)
        except Exception:
            pass

    def _ensure_flakes(self):
        if not self._enabled:
            return

        try:
            w = int(self.canvas.winfo_width() or 0)
            h = int(self.canvas.winfo_height() or 0)
        except Exception:
            w, h = 0, 0

        if w < 200 or h < 200:
            return

        try:
            # масштабируем по площади окна, но держим в разумных пределах
            desired = int((w * h) / 15000)
            desired = max(90, min(260, desired))
        except Exception:
            desired = 140

        c = self._flake_color()

        while len(self._flakes) < desired:
            r = random.randint(1, 3)
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            vy = random.uniform(0.7, 2.3) + (r * 0.25)
            vx = random.uniform(-0.35, 0.35)
            fid = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=c, outline="")
            self._flakes.append({"id": fid, "x": x, "y": y, "r": r, "vy": vy, "vx": vx})

        while len(self._flakes) > desired:
            f = self._flakes.pop()
            try:
                self.canvas.delete(f["id"])
            except Exception:
                pass

    def _tick(self):
        if not self._enabled:
            return

        try:
            w = int(self.canvas.winfo_width() or 0)
            h = int(self.canvas.winfo_height() or 0)
        except Exception:
            w, h = 0, 0

        if w <= 0 or h <= 0:
            self._job = self.root.after(120, self._tick)
            return

        for f in list(self._flakes):
            try:
                f["y"] += float(f["vy"])
                f["x"] += float(f["vx"])

                r = float(f["r"])
                if f["y"] > (h + r + 6):
                    f["y"] = -r
                    f["x"] = random.uniform(0, w)
                    f["vy"] = random.uniform(0.7, 2.3) + (r * 0.25)
                    f["vx"] = random.uniform(-0.35, 0.35)

                if f["x"] < -r:
                    f["x"] = w + r
                elif f["x"] > (w + r):
                    f["x"] = -r

                self.canvas.coords(f["id"], f["x"] - r, f["y"] - r, f["x"] + r, f["y"] + r)
            except Exception:
                pass

        self._job = self.root.after(33, self._tick)

def slide_switch(frame_out: tk.Frame, frame_in: tk.Frame, root: tk.Tk, direction="right"):
    # Не делаем place_forget() у frame_out, иначе потом tkraise() не вернёт экран.
    # Держим оба экрана "припаркованными" в layout, анимируем только frame_in.
    try:
        frame_out.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
    except Exception:
        pass

    start_x = 1.0 if direction == "right" else -1.0
    frame_in.place(relx=start_x, rely=0, relwidth=1.0, relheight=1.0)
    try:
        frame_in.tkraise()
    except Exception:
        pass

    try:
        prev = getattr(root, "_slide_job", None)
        if prev is not None:
            root.after_cancel(prev)
    except Exception:
        pass

    steps = 16

    def _step(i=0):
        try:
            if not frame_in.winfo_exists():
                return
        except Exception:
            return

        x = start_x * (1.0 - (i / steps))
        try:
            frame_in.place_configure(relx=x)
        except Exception:
            return

        if i >= steps:
            try:
                frame_in.place_configure(relx=0.0)
            except Exception:
                pass
            try:
                root._slide_job = None
            except Exception:
                pass
            return

        try:
            root._slide_job = root.after(12, lambda: _step(i + 1))
        except Exception:
            pass

    _step(0)

def style_primary(btn: tk.Button):
    _remember("primary_buttons", btn)
    btn.config(
        bg=ACCENT, fg=TEXT_ON_ACCENT,
        activebackground=ACCENT_HOVER, activeforeground=TEXT_ON_ACCENT,
        relief="flat", borderwidth=0, cursor="hand2",
        font=("Segoe UI Semibold", 11),
        padx=18, pady=10,
        highlightthickness=0,
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_HOVER))
    btn.bind("<Leave>", lambda e: btn.config(bg=ACCENT))

def style_secondary(btn: tk.Button):
    _remember("secondary_buttons", btn)
    btn.config(
        bg=BG_CARD, fg=TEXT,
        activebackground=HOVER_BG, activeforeground=TEXT,
        relief="flat", borderwidth=0, cursor="hand2",
        font=("Segoe UI", 10),
        padx=14, pady=8,
        highlightthickness=1, highlightbackground=BORDER,
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=HOVER_BG, highlightbackground=ACCENT_SECOND))
    btn.bind("<Leave>", lambda e: btn.config(bg=BG_CARD, highlightbackground=BORDER))

def style_entry(e):
    _remember("entries", e)
    try:
        th = 2 if CURRENT_THEME == "light" else 1
    except Exception:
        th = 1
    e.config(
        bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
        relief="flat",
        highlightthickness=th,
        highlightbackground=BORDER,
        highlightcolor=ACCENT_SECOND,
        disabledbackground=FIELD_BG,
        disabledforeground=SUBTEXT,
        readonlybackground=FIELD_BG_2,
    )
    try:
        if str(e.winfo_class()) == "Spinbox":
            btn_bg = BG_CARD if CURRENT_THEME == "light" else _mix(FIELD_BG, SUBTEXT, 0.75)
            e.config(buttonbackground=btn_bg, buttoncursor="hand2")
    except Exception:
        pass

def style_text(t):
    _remember("texts", t)
    try:
        th = 2 if CURRENT_THEME == "light" else 1
    except Exception:
        th = 1
    t.config(bg=FIELD_BG, fg=TEXT, insertbackground=TEXT, relief="flat",
             highlightthickness=th, highlightbackground=BORDER, highlightcolor=ACCENT_SECOND)


class BusyOverlay:
    def __init__(self, root: tk.Tk, message: str = "Загрузка…", *, title: str = "Загрузка", modal: bool = False):
        self.root = root
        self._old_cursor = None

        win = tk.Toplevel(root)
        self.win = win
        try:
            win.iconbitmap(get_app_icon())
        except Exception:
            pass

        win.title(title)
        win.transient(root)
        win.resizable(False, False)
        win.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Frame(win, bg=ACCENT, height=3).pack(fill="x")
        body = tk.Frame(win, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=16, pady=14)

        self.label = tk.Label(
            body,
            text=message,
            bg=BG_SURFACE,
            fg=TEXT,
            font=("Segoe UI", 10),
            wraplength=520,
            justify="left",
        )
        self.label.pack(anchor="w")

        self.pb = ttk.Progressbar(body, mode="indeterminate")
        self.pb.pack(fill="x", pady=(10, 0))
        try:
            self.pb.start(10)
        except Exception:
            pass

        try:
            self._old_cursor = root.cget("cursor")
            root.configure(cursor="watch")
        except Exception:
            pass

        try:
            win.update_idletasks()
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        scale = max(1.0, min(3.0, scale))

        try:
            w, h = int(540 * scale), int(130 * scale)

            # Пытаемся центрировать относительно главного окна (если размеры адекватные),
            # иначе — по центру экрана (чтобы не улетало в угол).
            try:
                root.update_idletasks()
            except Exception:
                pass

            rw = rh = 0
            rx = ry = 0
            try:
                rw = int(root.winfo_width())
                rh = int(root.winfo_height())
                rx = int(root.winfo_rootx())
                ry = int(root.winfo_rooty())
            except Exception:
                rw = rh = 0
                rx = ry = 0

            sw = int(win.winfo_screenwidth())
            sh = int(win.winfo_screenheight())

            if rw >= 240 and rh >= 240:
                x = rx + (rw - w) // 2
                y = ry + (rh - h) // 2
            else:
                x = (sw - w) // 2
                y = (sh - h) // 2

            # clamp в пределах экрана (с небольшими отступами)
            x = max(10, min(int(x), sw - w - 10))
            y = max(10, min(int(y), sh - h - 10))
            win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

        if modal:
            try:
                win.grab_set()
            except Exception:
                pass

        try:
            win.lift()
            win.attributes("-topmost", True)
            win.after(200, lambda: win.attributes("-topmost", False))
        except Exception:
            pass

    def set_message(self, message: str):
        def _do():
            try:
                if self.label.winfo_exists():
                    self.label.config(text=message)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            _do()

    def close(self):
        def _do():
            try:
                self.pb.stop()
            except Exception:
                pass
            try:
                if self.win.winfo_exists():
                    self.win.destroy()
            except Exception:
                pass
            try:
                self.root.configure(cursor=(self._old_cursor or ""))
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            _do()


def run_async(
    root: tk.Tk,
    task,
    *,
    title: str = "Загрузка",
    message: str = "Загрузка…",
    modal: bool = False,
    on_done=None,
    on_error=None,
):
    """
    Запускает task(busy) в фоне и показывает индикатор занятости.
    ВНИМАНИЕ: task НЕ должен трогать tkinter напрямую (только через busy.set_message/root.after).
    """
    busy = BusyOverlay(root, message, title=title, modal=modal)

    def _worker():
        try:
            result = task(busy)
        except Exception as e:
            def _err():
                try:
                    if on_error:
                        on_error(e, busy)
                    else:
                        messagebox.showerror("Ошибка", str(e))
                finally:
                    busy.close()
            try:
                root.after(0, _err)
            except Exception:
                _err()
            return

        def _ok():
            if on_done:
                on_done(result, busy)
            else:
                busy.close()
        try:
            root.after(0, _ok)
        except Exception:
            _ok()

    threading.Thread(target=_worker, daemon=True).start()
    return busy


def pill(parent, text, color):
    f = tk.Frame(parent, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
    lbl = tk.Label(f, text=text, bg=BG_CARD, fg=color, font=("Segoe UI Semibold", 9))
    lbl.pack(padx=10, pady=6)
    return f

def pill_button(parent, text, command, kind="primary", **pack_opts):
    wrap = tk.Frame(parent, bg=BG_WINDOW)
    wrap.pack(fill="x", padx=60, pady=10, **pack_opts)
    btn = tk.Button(wrap, text=text)
    style_primary(btn) if kind == "primary" else style_secondary(btn)
    btn.config(command=command)
    btn.pack(fill="x", ipady=3)
    return btn

# ---------- Логика (как было) ----------
def render_page(frame, canvas, page_label, nav_frame, update_copy_button_text):
    global current_page, found_files, checked_vars

    for w in frame.winfo_children():
        w.destroy()

    if not found_files:
        page_label.config(text="")
        for w in nav_frame.winfo_children():
            w.destroy()
        nav_frame.pack_forget()
        return

    start = (current_page - 1) * items_per_page
    end = min(len(found_files), start + items_per_page)
    page_items = list(zip(found_files[start:end], checked_vars[start:end]))

    for idx, ((name, path), var) in enumerate(page_items, start=start + 1):
        base_bg = BG_SURFACE if idx % 2 else FIELD_BG_2

        card = tk.Frame(frame, bg=base_bg, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=8, pady=6)

        # чекбокс
        chk = ModernCheckbox(card, var, command=None, size=18, bg=base_bg)
        chk.pack(side="left", padx=10, pady=10)

        # текстовый блок
        info = tk.Frame(card, bg=base_bg)
        info.pack(side="left", fill="both", expand=True, pady=8)

        title_lbl = tk.Label(
            info, text=f"{idx}. {name}",
            font=("Segoe UI", 11, "bold"),
            fg=TEXT, bg=base_bg, anchor="w"
        )
        title_lbl.pack(anchor="w", fill="x")

        path_lbl = tk.Label(
            info, text=path,
            font=("Segoe UI", 9),
            fg=SUBTEXT, bg=base_bg,
            anchor="w", wraplength=760, justify="left"
        )
        path_lbl.pack(anchor="w")

        def apply_state(v=var, c=card, cb=chk, base=base_bg, inf=info, tl=title_lbl, pl=path_lbl):
            if v.get():
                card_bg = ACTIVE_BG
                c.config(bg=card_bg, highlightbackground=ACCENT, highlightcolor=ACCENT, highlightthickness=2)
            else:
                card_bg = base
                c.config(bg=card_bg, highlightbackground=BORDER, highlightcolor=BORDER, highlightthickness=1)

            # фон детей
            cb.set_bg(card_bg)
            inf.config(bg=card_bg)
            tl.config(bg=card_bg)
            pl.config(bg=card_bg)

        def on_toggle():
            apply_state()
            update_copy_button_text()

        # подключаем команду после создания
        chk.command = on_toggle

        # чтобы клик по карточке тоже переключал
        for w in (card, info, title_lbl, path_lbl):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", chk._toggle)

        # реагируем на смену var (например, "выделить всё")
        var.trace_add("write", lambda *_: (apply_state(), update_copy_button_text()))

        # первичная отрисовка
        apply_state()

    frame.update_idletasks()
    bbox = canvas.bbox("all")
    if bbox:
        canvas.configure(scrollregion=bbox)

    total_pages = (len(found_files) + items_per_page - 1) // items_per_page
    page_label.config(text=f"Страница {current_page} из {total_pages}", fg=SUBTEXT, bg=BG_SURFACE)

    for w in nav_frame.winfo_children():
        w.destroy()

    def prev_page():
        global current_page
        if current_page > 1:
            current_page -= 1
            render_page(frame, canvas, page_label, nav_frame, update_copy_button_text)

    def next_page():
        global current_page
        if current_page < total_pages:
            current_page += 1
            render_page(frame, canvas, page_label, nav_frame, update_copy_button_text)

    btn_prev = tk.Button(nav_frame, text="← Назад", command=prev_page)
    btn_next = tk.Button(nav_frame, text="Вперёд →", command=next_page)
    style_secondary(btn_prev)
    style_secondary(btn_next)
    btn_prev.pack(side="left", padx=6)
    btn_next.pack(side="left", padx=6)
    nav_frame.pack(side="right")


def search_by_year(year, frame, canvas, count_label, page_label, nav_frame, update_copy_button_text):
    global found_files, checked_vars, current_page
    y = str(year).strip()
    found_files, checked_vars = [], []

    try:
        count_label.config(text=f"⏳ Поиск фильмов за {y}…", fg=ACCENT_SECOND, bg=BG_WINDOW)
    except Exception:
        pass

    def _task(busy: BusyOverlay):
        busy.set_message(f"⏳ Поиск по году {y}…")
        token = f"({y})"
        res = []
        seen = set()
        for name, path in movie_index:
            try:
                ext = os.path.splitext(name)[1].lower()
            except Exception:
                ext = ""
            # В индексе есть и метафайлы (постеры/clearlogo/nfo и т.п.) — в поиске по году показываем только видео.
            if ext not in VIDEO_EXTENSIONS:
                continue
            if token in name:
                base = normalize_name(name)
                if base in seen:
                    continue
                seen.add(base)
                res.append((name, path))
        return res

    def _done(res, busy: BusyOverlay):
        global found_files, checked_vars, current_page
        found_files = res
        checked_vars = [tk.BooleanVar(value=False) for _ in found_files]

        try:
            count_label.config(text=f"Найдено фильмов: {len(found_files)}", fg=ACCENT_SECOND, bg=BG_WINDOW)
        except Exception:
            pass

        if not found_files:
            messagebox.showinfo("Результат", f"Фильмы за {y} не найдены")
            busy.close()
            return

        current_page = 1
        render_page(frame, canvas, page_label, nav_frame, update_copy_button_text)
        busy.close()

    def _err(e: Exception, _busy: BusyOverlay):
        try:
            count_label.config(text="❌ Ошибка поиска", fg=ERROR, bg=BG_WINDOW)
        except Exception:
            pass
        messagebox.showerror("Ошибка", f"Ошибка при поиске:\n{e}")

    try:
        r = globals().get("root")
    except Exception:
        r = None

    if r is None:
        messagebox.showerror("Ошибка", "Не найден корневой окно приложения (root).")
        return

    run_async(
        r,
        _task,
        title="Поиск по году",
        message=f"⏳ Поиск фильмов за {y}…",
        modal=False,
        on_done=_done,
        on_error=_err,
    )

def copy_selected(root):
    if not found_files:
        messagebox.showwarning("Копирование", "Нет найденных фильмов для копирования"); return
    selected_count = sum(v.get() for v in checked_vars)
    if selected_count == 0:
        messagebox.showinfo("Копирование", "Выберите хотя бы один фильм"); return
    root.after(50, lambda: threaded_save_checked(root, found_files, checked_vars, movie_index, include_related=False))

def toggle_select_all():
    if not checked_vars: return
    state = any(not v.get() for v in checked_vars)
    for v in checked_vars: v.set(state)

# ---------- UI ----------
def update_row_title(tree, item_id, new_title: str):
    vals = list(tree.item(item_id, "values"))
    if len(vals) == 3:
        tree.item(item_id, values=(vals[0], new_title, vals[2]))
# --- Animated GIF icon helper (Tkinter) ---
class AnimatedGifLabel(tk.Label):
    def __init__(self, parent, gif_path: str, bg: str, fps_ms: int = 140, autostart: bool = True, **kw):
        super().__init__(parent, bg=bg, **kw)
        self.gif_path = gif_path
        self.fps_ms = fps_ms
        self.frames = []
        self._after = None
        self._i = 0
        self._load_frames()

        if self.frames:
            self.config(image=self.frames[0])

        self.bind("<Destroy>", lambda e: self.stop())
        if autostart:
            self.start()

    def _load_frames(self):
        i = 0
        while True:
            try:
                fr = tk.PhotoImage(file=self.gif_path, format=f"gif -index {i}")
                self.frames.append(fr)
                i += 1
            except tk.TclError:
                break

    def start(self):
        if not self.frames or self._after is not None:
            return
        self._tick()

    def stop(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
        self._after = None

    def _tick(self):
        if not self.winfo_exists() or not self.frames:
            return
        self._i = (self._i + 1) % len(self.frames)
        self.config(image=self.frames[self._i])
        self._after = self.after(self.fps_ms, self._tick)

def show_screen(screens: dict[str, tk.Frame], name: str):
    f = screens.get(name)
    if not f:
        return
    # Защита: если какой-то экран был спрятан через place_forget(),
    # tkraise() сам по себе его не покажет. Возвращаем экран в layout перед поднятием.
    try:
        f.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
    except Exception:
        pass
    f.tkraise()
def _set_bg_recursive(w, bg):
    try:
        w.configure(bg=bg)
    except Exception:
        pass
    for ch in w.winfo_children():
        _set_bg_recursive(ch, bg)

def set_nav_active(nav_items: dict[str, tk.Frame], active: str):
    for key, item in nav_items.items():
        bg = ACTIVE_BG if key == active else BG_SURFACE
        _set_bg_recursive(item, bg)


def main():
    global root
    enable_windows_dpi_awareness()
    root = tk.Tk()
    
    s = load_settings()
    try:
        root._kino_auto_convert_all_audio = bool(s.get("kino_auto_convert_all_audio", False))
    except Exception:
        pass
    try:
        v = int(s.get("kino_audio_parallel_tracks", 1) or 1)
    except Exception:
        v = 1
    try:
        root._kino_audio_parallel_tracks = max(1, min(4, int(v)))
    except Exception:
        root._kino_audio_parallel_tracks = 1
    try:
        if bool(s.get("start_minimized_to_tray", False)):
            # Старт в трее отключили: приложение всегда запускается развернутым в панели задач.
            s["start_minimized_to_tray"] = False
            save_settings(s)
    except Exception:
        pass

    try:
        root._holiday_theme = _is_holiday_season()
    except Exception:
        root._holiday_theme = False

    theme_name = s.get("theme", "dark")
    try:
        set_hls_reencode(bool(s.get("hls_reencode", True)))
    except Exception:
        pass

    apply_theme(root, theme_name)

    root.title("Movie Tools")
    try:
        root.iconbitmap(default=get_app_icon())
    except Exception:
        try:
            root.iconbitmap(get_app_icon())
        except Exception:
            logging.info("icon.ico not found, using default icon")


    root.geometry("1000x680")
    root.configure(bg=BG_WINDOW)
    # --- Развернуть окно при старте ---
    if START_FULLSCREEN:
        # Полноэкранный режим (без рамок)
        root.attributes("-fullscreen", True)
    else:
        # Обычное «максимизированное» окно (Windows)
        try:
            root.state("zoomed")
        except Exception:
            # Linux/BSD некоторые WM понимают -zoomed
            try:
                root.attributes("-zoomed", True)
            except Exception:
                # Фоллбек: вручную на весь экран
                w, h = root.winfo_screenwidth(), root.winfo_screenheight()
                root.geometry(f"{w}x{h}+0+0")

    dpi_scaling(root)
    # root.attributes("-alpha", 0.0); fade_in(root)
    root.attributes("-alpha", 1.0)
    # авто-проверка обновлений через 2 секунды после старта
    root.after(
        2000,
        lambda: check_for_updates_async(root, show_if_latest=False, notify_cb=push_notification),
    )

    # --- Шапка ---
    appbar = tk.Frame(root, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    appbar.pack(side="top", fill="x")
    title_lbl = tk.Label(appbar, text="Movie Tools", bg=BG_SURFACE, fg=ACCENT,
         font=("Segoe UI Semibold", 20))
    title_lbl.pack(side="left", padx=16, pady=10)

    # Снеговик в шапке (показываем только в новогодней теме)
    snowman_lbl = tk.Label(appbar, text="⛄", bg=BG_SURFACE, fg=TEXT, font=("Segoe UI Emoji", 18))
    try:
        root._snowman_label = snowman_lbl
        if bool(getattr(root, "_holiday_theme", False)):
            snowman_lbl.pack(side="left", padx=(6, 0), pady=10)
    except Exception:
        pass
    right_appbar = tk.Frame(appbar, bg=BG_SURFACE)
    right_appbar.pack(side="right", padx=12, pady=8)

    # Новогодний "снежок": всегда рисуем полоску под шапкой,
    # а если поддерживается — ещё и overlay по всему окну (Windows).
    try:
        try:
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        root._snow_strip = SnowStrip(root, after_widget=appbar, height=int(28 * max(1.0, min(3.0, scale))))
        root._snow_strip.set_enabled(bool(getattr(root, "_holiday_theme", False)))
    except Exception:
        pass

    try:
        root._snow_overlay = SnowOverlay(root)
        root._snow_overlay.set_enabled(bool(getattr(root, "_holiday_theme", False)))
    except Exception:
        # overlay может быть недоступен (например, прозрачность не поддерживается)
        root._snow_overlay = None

    _anim_job = {"id": None}

    def animate_nav_indicator(target_item: tk.Widget):
        # отменяем прошлую анимацию
        if _anim_job["id"] is not None:
            try:
                root.after_cancel(_anim_job["id"])
            except Exception:
                pass
            _anim_job["id"] = None

        # важно: позиции доступны только после отрисовки
        root.update_idletasks()

        ty = target_item.winfo_y() + 8
        th = max(18, target_item.winfo_height() - 16)

        sy = nav_indicator.winfo_y()
        sh = max(0, nav_indicator.winfo_height())

        steps = 14
        dur = 10  # ms на шаг

        i = 0
        def tick():
            nonlocal i
            i += 1
            t = i / steps
            y = int(sy + (ty - sy) * t)
            h = int(sh + (th - sh) * t)
            nav_indicator.place_configure(y=y, height=h)
            if i < steps:
                _anim_job["id"] = root.after(dur, tick)

        tick()

    def icon_btn(parent, text, command):
        b = tk.Button(parent, text=text, command=command)
        style_secondary(b)
        b.config(font=("Segoe UI Emoji", 12), padx=10, pady=6)
        return b

    notify_count_var = tk.IntVar(value=0)

    bell_wrap = tk.Frame(right_appbar, bg=BG_SURFACE)
    bell_wrap.pack(side="left", padx=6)

    btn_bell = icon_btn(bell_wrap, "🔔", lambda: show_notifications())
    btn_bell.pack(side="left")

    badge = tk.Label(
        bell_wrap,
        textvariable=notify_count_var,
        bg=ACCENT, fg=TEXT_ON_ACCENT,
        font=("Segoe UI Semibold", 8),
        padx=6, pady=1
    )
    # бейдж поверх колокольчика
    _badge_place = {"relx": 1.0, "rely": 0.0, "x": -2, "y": 2, "anchor": "ne"}
    badge.place(**_badge_place)

    notifications: list[dict] = []
    toast_windows: list[tk.Toplevel] = []

    def _refresh_badge():
        try:
            unread = sum(1 for n in notifications if n.get("unread"))
            notify_count_var.set(unread)
            if unread > 0:
                if not badge.winfo_ismapped():
                    badge.place(**_badge_place)
            else:
                if badge.winfo_ismapped():
                    badge.place_forget()
        except Exception:
            pass

    def _reposition_toasts():
        try:
            margin = 18
            try:
                scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
            except Exception:
                scale = 1.0
            scale = max(1.0, min(3.0, scale))
            w = int(360 * scale)
            h = int(110 * scale)
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()

            alive: list[tk.Toplevel] = []
            for t in toast_windows:
                try:
                    if t.winfo_exists():
                        alive.append(t)
                except Exception:
                    pass
            toast_windows[:] = alive

            for idx, t in enumerate(toast_windows):
                x = sw - margin - w
                y = sh - margin - h - idx * (h + 10)
                y = max(10, y)
                try:
                    t.geometry(f"{w}x{h}+{x}+{y}")
                except Exception:
                    pass
        except Exception:
            pass

    def _show_windows_toast(title: str, message: str):
        if os.name != "nt":
            return
        try:
            if not bool(load_settings().get("win_toast_notifications", False)):
                return
        except Exception:
            return

        try:
            title = str(title or "")[:64]
            message = str(message or "")[:220]

            script = (
                "& { param($title,$msg) "
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
                "$t=[System.Security.SecurityElement]::Escape($title); "
                "$m=[System.Security.SecurityElement]::Escape($msg); "
                "$xml=New-Object Windows.Data.Xml.Dom.XmlDocument; "
                "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>\"); "
                "$toast=[Windows.UI.Notifications.ToastNotification]::new($xml); "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Movie Tools').Show($toast) }"
            )

            kwargs = {}
            try:
                cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if cf:
                    kwargs["creationflags"] = cf
            except Exception:
                pass

            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                    title,
                    message,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
        except Exception:
            pass

    def _show_toast(title: str, message: str):
        try:
            if not bool(load_settings().get("popup_notifications", True)):
                return
        except Exception:
            pass

        # окно без рамки, поверх всех, в правом нижнем углу
        t = tk.Toplevel(root)
        try:
            t.overrideredirect(True)
        except Exception:
            pass
        try:
            t.attributes("-topmost", True)
        except Exception:
            pass
        try:
            t.attributes("-toolwindow", True)
        except Exception:
            pass

        t.configure(bg=BG_WINDOW)

        try:
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        scale = max(1.0, min(3.0, scale))
        wrap_w = max(240, int(360 * scale) - 30)
        frame = tk.Frame(t, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        frame.pack(fill="both", expand=True)
        tk.Frame(frame, bg=ACCENT, height=3).pack(fill="x", side="top")

        content = tk.Frame(frame, bg=BG_CARD)
        content.pack(fill="both", expand=True, padx=12, pady=10)

        head = tk.Frame(content, bg=BG_CARD)
        head.pack(fill="x")
        tk.Label(
            head,
            text=title,
            bg=BG_CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", anchor="w")

        btn_close = tk.Label(
            head,
            text="✕",
            bg=BG_CARD,
            fg=SUBTEXT,
            font=("Segoe UI", 11),
            cursor="hand2",
        )
        btn_close.pack(side="right", anchor="e")

        msg_lbl = tk.Label(
            content,
            text=message,
            bg=BG_CARD,
            fg=SUBTEXT,
            font=("Segoe UI", 9),
            wraplength=wrap_w,
            justify="left",
        )
        msg_lbl.pack(anchor="w", pady=(6, 0))

        toast_windows.insert(0, t)
        _reposition_toasts()

        # лёгкая анимация появления (слайд + fade), чтобы выглядело современнее
        try:
            geo = str(t.geometry())
            m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo)
            if m:
                w0, h0, x0, y0 = map(int, m.groups())
                try:
                    sh = int(root.winfo_screenheight())
                except Exception:
                    sh = y0 + h0 + 60
                start_y = min(sh, y0 + 60)
                try:
                    t.attributes("-alpha", 0.0)
                except Exception:
                    pass
                try:
                    t.geometry(f"{w0}x{h0}+{x0}+{start_y}")
                except Exception:
                    pass

                steps = 12

                def _anim(i=0):
                    try:
                        if not t.winfo_exists():
                            return
                    except Exception:
                        return

                    frac = (i + 1) / steps
                    cur_y = int(start_y + (y0 - start_y) * frac)
                    try:
                        t.geometry(f"{w0}x{h0}+{x0}+{cur_y}")
                    except Exception:
                        pass
                    try:
                        t.attributes("-alpha", min(1.0, frac))
                    except Exception:
                        pass

                    if i + 1 < steps:
                        try:
                            t.after(15, lambda: _anim(i + 1))
                        except Exception:
                            pass

                _anim(0)
        except Exception:
            pass

        def _destroy():
            try:
                if t in toast_windows:
                    toast_windows.remove(t)
            except Exception:
                pass
            try:
                if t.winfo_exists():
                    t.destroy()
            except Exception:
                pass
            _reposition_toasts()

        def _open_notifications(_e=None):
            try:
                show_notifications()
            except Exception:
                pass
            _destroy()

        for wdg in (frame, content, head, msg_lbl):
            try:
                wdg.bind("<Button-1>", _open_notifications)
            except Exception:
                pass
        btn_close.bind("<Button-1>", lambda e: _destroy())

        # авто-закрытие
        t.after(4500, _destroy)

    def push_notification(title: str, message: str, action=None, *, unread: bool = True):
        def _do():
            if not NOTIFICATIONS_ENABLED:
                return
            from datetime import datetime

            notifications.insert(
                0,
                {
                    "title": title,
                    "message": message,
                    "ts": datetime.now().strftime("%H:%M"),
                    "unread": bool(unread),
                    "action": action,
                },
            )
            if len(notifications) > 50:
                del notifications[50:]
            _refresh_badge()
            _show_windows_toast(title, message)
            _show_toast(title, message)

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                root.after(0, _do)
        except Exception:
            pass

    def mark_all_notifications_read():
        for n in notifications:
            n["unread"] = False
        _refresh_badge()

    def clear_notifications():
        notifications.clear()
        _refresh_badge()

    _refresh_badge()

    btn_gear = icon_btn(right_appbar, "⚙", lambda: open_settings())
    btn_gear.pack(side="left", padx=6)

    # ===== Body layout: sidebar + content =====
    body_root = tk.Frame(root, bg=BG_WINDOW)
    body_root.pack(fill="both", expand=True)

    try:
        sidebar_w = int(280 * float(globals().get("UI_SCALE", 1.0) or 1.0))
    except Exception:
        sidebar_w = 280
    sidebar_w = max(280, min(420, sidebar_w))

    sidebar = tk.Frame(body_root, bg=BG_SURFACE, width=sidebar_w, highlightthickness=1, highlightbackground=BORDER)
    sidebar.pack(side="left", fill="y")
    sidebar.pack_propagate(False)
    nav_items = {}
    nav_indicator = tk.Frame(sidebar, bg=ACCENT, width=4)
    nav_indicator.place(x=6, y=0, height=0)   # позицию выставим позже
    # ===== Bottom status/actions (NAS + Kino.pub) =====
    sidebar_status = tk.Frame(sidebar, bg=BG_SURFACE)
    sidebar_status.pack(side="bottom", fill="x", padx=10, pady=12)

    tk.Frame(sidebar_status, bg=BORDER, height=1).pack(fill="x", pady=(0, 10))

    def _status_row(parent, title_left: str):
        row = tk.Frame(parent, bg=BG_SURFACE)
        row.pack(fill="x", pady=4)

        dot = tk.Label(row, text="●", bg=BG_SURFACE, fg=ERROR, font=("Segoe UI", 10))
        dot.pack(side="left")

        txt = tk.Label(
            row,
            text=title_left,
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 9),
            wraplength=max(140, sidebar_w - 140),
            justify="left",
        )
        txt.pack(side="left", padx=6, fill="x", expand=True)

        btn = tk.Button(row, text="...", padx=10)
        style_secondary(btn)
        btn.pack(side="right")

        return dot, txt, btn

    dot_nas, txt_nas, btn_nas = _status_row(sidebar_status, "NAS: не проверен")
    dot_kino, txt_kino, btn_kino = _status_row(sidebar_status, "Kino.pub: не вошли")

    def update_sidebar_status():
        # NAS
        if index_loaded:
            dot_nas.config(fg=OK)
            txt_nas.config(text="NAS: проверен")
            btn_nas.config(text="Перепроверить")
        else:
            dot_nas.config(fg=ERROR)
            txt_nas.config(text="NAS: не проверен")
            btn_nas.config(text="Проверить")

        # Kino.pub
        if kino_logged_in:
            dot_kino.config(fg=OK)
            txt_kino.config(text="Kino.pub: залогинен")
            btn_kino.config(text="Выйти")
            btn_kino.config(command=lambda: logout_kino())
        else:
            dot_kino.config(fg=ERROR)
            txt_kino.config(text="Kino.pub: не вошли")
            btn_kino.config(text="Войти")
            btn_kino.config(command=lambda: login_to_kino())


    # кнопки действий
    btn_nas.config(command=lambda: prepare_index())
    btn_kino.config(command=lambda: login_to_kino())

    # чтобы можно было дергать при смене темы
    root._update_sidebar_status = update_sidebar_status

    # первичная отрисовка
    update_sidebar_status()

    def nav_item(key: str, text: str, icon: str, target: str):
        item = tk.Frame(sidebar, bg=BG_SURFACE)
        item.pack(fill="x", padx=10, pady=6)

        row = tk.Frame(item, bg=BG_SURFACE)
        row.pack(fill="x", padx=10, pady=10)

        lbl_i = tk.Label(row, text=icon, bg=BG_SURFACE, fg=ACCENT_SECOND, font=("Segoe UI Emoji", 16))
        lbl_i.pack(side="left")

        lbl_t = tk.Label(
            row,
            text=text,
            bg=BG_SURFACE,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
            wraplength=max(160, sidebar_w - 120),
            justify="left",
        )
        lbl_t.pack(side="left", padx=10, fill="x", expand=True)

        def on_enter(_=None):
            if key != _active[0]:
                for w in (item, row, lbl_i, lbl_t):
                    w.configure(bg=HOVER_BG)

        def on_leave(_=None):
            if key != _active[0]:
                for w in (item, row, lbl_i, lbl_t):
                    w.configure(bg=BG_SURFACE)

        def on_click(_=None):
            _active[0] = key
            set_nav_active(nav_items, key)
            animate_nav_indicator(item)      # <<< добавили
            show_screen(screens, target)


        for w in (item, row, lbl_i, lbl_t):
            w.configure(cursor="hand2")
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)

        nav_items[key] = item
        return item

    _active = ["home"]


    nav_item("finder",   "Поиск по году",     "🔎", "finder")
    nav_item("kino",     "Kino.pub",          "🎬", "kino")
    nav_item("requests", "Работа с запросами","📝", "requests")


    _active = ["kino"]
    set_nav_active(nav_items, "kino")
    
    content = tk.Frame(body_root, bg=BG_WINDOW)
    content.pack(side="left", fill="both", expand=True)


    # --- Экраны ---
    finder      = tk.Frame(content, bg=BG_WINDOW)
    kino        = tk.Frame(content, bg=BG_WINDOW)
    kino_search = tk.Frame(content, bg=BG_WINDOW)
    requests    = tk.Frame(content, bg=BG_WINDOW)

    screens = {
        "finder": finder,
        "kino": kino,
        "kino_search": kino_search,
        "requests": requests,
    }


    for f in screens.values():
        f.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

    show_screen(screens, "kino")

    # ========== Finder ==========
    commandbar = tk.Frame(finder, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    commandbar.pack(side="top", fill="x", pady=(0, 6))
    tk.Label(commandbar, text="🎞 Поиск фильмов по году в медиатеке", bg=BG_SURFACE, fg=ACCENT_SECOND,
             font=("Segoe UI Semibold", 16)).pack(side="left", padx=12, pady=8)

    right_controls = tk.Frame(commandbar, bg=BG_SURFACE); right_controls.pack(side="right", padx=12, pady=8)
    tk.Label(right_controls, text="Год:", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 11)).pack(side="left")
    year_entry = tk.Entry(right_controls, font=("Segoe UI", 11), width=8, state="disabled",
                          bg=FIELD_BG, fg=TEXT, insertbackground=TEXT, relief="flat")
    year_entry.pack(side="left", padx=(6, 8))
    # Enter в поле года запускает поиск (и не пробрасывается на другие обработчики)
    year_entry.bind("<Return>", lambda e: (on_search() or "break"))
    btn_find_year = tk.Button(right_controls, text="Найти", state="disabled")
    style_secondary(btn_find_year)
    btn_find_year.pack(side="left")



    count_bar = tk.Frame(finder, bg=BG_WINDOW); count_bar.pack(fill="x", padx=12, pady=(6, 0))
    count_label = tk.Label(count_bar, text="Найдено фильмов: 0", bg=BG_WINDOW, fg=ACCENT_SECOND, font=("Segoe UI", 11))
    count_label.pack(side="left", padx=4)

    container = tk.Frame(finder, bg=BG_WINDOW); container.pack(fill="both", expand=True, padx=10, pady=8)
    canvas = tk.Canvas(container, bg=BG_WINDOW, highlightthickness=0)
    vscroll = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
    results_frame = tk.Frame(canvas, bg=BG_WINDOW)
    results_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=results_frame, anchor="nw")
    canvas.configure(yscrollcommand=vscroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    vscroll.pack(side="right", fill="y")

    footer = tk.Frame(finder, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    footer.pack(side="bottom", fill="x")
    page_label = tk.Label(footer, text="", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))
    page_label.pack(side="left", padx=12, pady=8)
    nav_frame = tk.Frame(footer, bg=BG_SURFACE); nav_frame.pack(side="left", padx=6, pady=8)
    actions = tk.Frame(footer, bg=BG_SURFACE); actions.pack(side="right", padx=10, pady=6)

    btn_toggle = tk.Button(actions, text="Выделить всё / снять всё"); style_secondary(btn_toggle)
    btn_toggle.config(command=lambda: (toggle_select_all(),
                                    render_page(results_frame, canvas, page_label, nav_frame, update_copy_button_text)))
    btn_toggle.pack(side="left", padx=6)


    btn_copy = tk.Button(actions, text="Скопировать отмеченные")
    style_secondary(btn_copy)
    btn_copy.pack(side="left", padx=6)

    def update_copy_button_text():
        try:
            selected = sum(v.get() for v in checked_vars)
            total = len(found_files)
            btn_copy.config(text=f"Скопировать ({selected}/{total})" if selected else "Скопировать отмеченные")
        except Exception: pass

    def on_search():
        if not index_loaded:
            messagebox.showerror("Ошибка", "Сначала проверь данные на NAS"); return
        y = year_entry.get().strip()
        if not y.isdigit():
            messagebox.showerror("Ошибка", "Введите год числом"); return
        search_by_year(y, results_frame, canvas, count_label, page_label, nav_frame, update_copy_button_text)
    def prepare_index():
        """Экспорт Everything -> all_movies.efu -> загрузка в movie_index."""
        global movie_index, index_loaded

        # Собираем запрос для Everything по нужным расширениям (видео + мета)
        exts = set(VIDEO_EXTENSIONS) | set(META_EXTENSIONS)
        query = "|".join([f"ext:{e.lstrip('.')}" for e in sorted(exts)])
        cmd = ["es.exe", query, "-n", "9999999", "-export-efu", EFU_FILE]

        try:
            count_label.config(text="⏳ Индексация NAS... подождите", fg=ACCENT_SECOND)
        except Exception:
            pass

        try:
            btn_nas.config(state="disabled")
        except Exception:
            pass

        def _task(busy: BusyOverlay):
            busy.set_message("⏳ Индексация NAS...")
            subprocess.run(cmd, check=True)
            return load_index_from_efu(EFU_FILE)

        def _done(idx, busy: BusyOverlay):
            global movie_index, index_loaded
            movie_index = idx
            index_loaded = True
            update_sidebar_status()

            year_entry.config(state="normal")
            btn_find_year.config(state="normal")

            try:
                count_label.config(text=f"✅ Индекс загружен: {len(movie_index)} файлов", fg=ACCENT_SECOND)
            except Exception:
                pass

            try:
                messagebox.showinfo("Индекс", f"✅ Загружено файлов: {len(movie_index)}\nФайл: {EFU_FILE}")
            except Exception:
                pass

            try:
                btn_nas.config(state="normal")
            except Exception:
                pass

            busy.close()

        def _err(e: Exception, _busy: BusyOverlay):
            global index_loaded
            index_loaded = False

            try:
                btn_nas.config(state="normal")
            except Exception:
                pass

            try:
                count_label.config(text="❌ Ошибка индексации NAS", fg=ERROR)
            except Exception:
                pass

            if isinstance(e, FileNotFoundError):
                messagebox.showerror(
                    "Ошибка",
                    "Не найден es.exe (Everything CLI).\n\n"
                    "Положи es.exe рядом с программой или добавь Everything в PATH."
                )
                return

            if isinstance(e, subprocess.CalledProcessError):
                messagebox.showerror("Ошибка", f"Everything (es.exe) завершился с ошибкой:\n{e}")
                return

            messagebox.showerror("Ошибка", f"Не удалось подготовить индекс:\n{e}")

        run_async(
            root,
            _task,
            title="Проверка NAS",
            message="⏳ Индексация NAS...",
            modal=False,
            on_done=_done,
            on_error=_err,
        )

    btn_find_year.config(command=on_search)
    btn_copy.config(command=lambda: copy_selected(root))

    def _on_mousewheel(event):
        if event.num == 5 or event.delta == -120: canvas.yview_scroll(1, "units")
        if event.num == 4 or event.delta == 120:  canvas.yview_scroll(-1, "units")

    canvas.bind_all("<MouseWheel>", _on_mousewheel)
    canvas.bind_all("<Button-4>", _on_mousewheel)
    canvas.bind_all("<Button-5>", _on_mousewheel)
    root.bind("<Control-a>", lambda e: toggle_select_all())

    if START_FULLSCREEN:
        # Esc — выйти из полноэкранного; F11 — вернуть
        root.bind("<Escape>", lambda e: root.attributes("-fullscreen", False))
        root.bind("<F11>",   lambda e: root.attributes("-fullscreen",
                                                    not bool(root.attributes("-fullscreen"))))
    else:
        root.bind("<Escape>", lambda e: root.iconify())

    # сброс профиля MediaSearch + UC-драйвера для Kino.pub
    def _purge_kino_profile(silent: bool = False) -> bool:
        """
        Удаляет папки профиля MediaSearch + undetected_chromedriver.
        Возвращает True если что-то удалили.
        silent=True -> без confirm/alert.
        """
        local = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        media_profile = os.path.join(local, "MediaSearch")

        roaming = os.getenv("APPDATA") or os.path.expanduser("~")
        uc_profile = os.path.join(roaming, "undetected_chromedriver")

        targets = [media_profile, uc_profile]

        if not silent:
            msg = (
                "Будут удалены папки профиля:\n\n"
                f"{media_profile}\n"
                f"{uc_profile}\n\n"
                "Это сбросит кеш/профиль браузера и UC-драйвера.\n"
                "Продолжить?"
            )
            if not messagebox.askyesno("Сброс профиля", msg):
                return False

        def _norm(p: str) -> str:
            return (p or "").replace("\\", "/").lower()

        def _kill_leftovers() -> int:
            """
            Если приложение ранее падало, могли остаться процессы Chromium/undetected_chromedriver,
            которые блокируют удаление профиля. Здесь пытаемся их завершить.
            """
            if os.name != "nt":
                return 0
            try:
                import psutil  # type: ignore
            except Exception:
                return 0

            base_ref = _norm(media_profile)
            uc_ref = _norm(uc_profile)

            procs = {}
            for p in psutil.process_iter(["pid", "exe", "cmdline"]):
                try:
                    pid = int(p.info.get("pid") or 0)
                    if not pid:
                        continue
                    exe = _norm(p.info.get("exe") or "")
                    cl = _norm(" ".join(p.info.get("cmdline") or []))

                    # 1) сам undetected_chromedriver.exe из Roaming\undetected_chromedriver
                    if uc_ref and exe.startswith(uc_ref + "/"):
                        procs[pid] = p
                        continue

                    # 2) Chromium, запущенный с --user-data-dir внутри LocalAppData\MediaSearch\...
                    if base_ref and ("--user-data-dir=" in cl) and (base_ref in cl):
                        procs[pid] = p
                except Exception:
                    pass

            if not procs:
                return 0

            plist = list(procs.values())
            for p in plist:
                try:
                    p.terminate()
                except Exception:
                    pass

            try:
                _, alive = psutil.wait_procs(plist, timeout=1.5)
            except Exception:
                alive = []

            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass

            try:
                psutil.wait_procs(alive, timeout=1.0)
            except Exception:
                pass

            return len(plist)

        def _rmtree_force(path: str):
            def _onerror(func, p, _exc_info):
                try:
                    os.chmod(p, 0o700)
                except Exception:
                    pass
                try:
                    func(p)
                except Exception:
                    pass

            shutil.rmtree(path, onerror=_onerror)

        # Сначала пробуем прибрать "висячие" процессы, чтобы rmtree не падал с AccessDenied.
        try:
            killed = _kill_leftovers()
            if killed:
                logging.info("Purge: terminated %d leftover UC/Chromium processes", killed)
        except Exception:
            pass

        removed_any = False
        for path in targets:
            if not os.path.isdir(path):
                continue
            last_err = None
            for attempt in range(2):
                try:
                    _rmtree_force(path)
                    removed_any = True
                    logging.info("Удалена папка профиля: %s", path)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    logging.error("Ошибка удаления профиля %s (attempt %s): %s", path, attempt + 1, e)
                    # 1-я попытка могла упасть из-за "висячих" процессов -> пробуем закрыть и повторить
                    try:
                        _kill_leftovers()
                    except Exception:
                        pass
                    try:
                        time.sleep(0.2)
                    except Exception:
                        pass

            if last_err is not None and silent:
                try:
                    push_notification(
                        "⚠ Kino.pub",
                        "Не удалось очистить профиль (папка занята процессом). "
                        "Закройте оставшиеся chrome.exe/undetected_chromedriver.exe и повторите.",
                        unread=True,
                    )
                except Exception:
                    pass

        if (not silent) and removed_any:
            messagebox.showinfo(
                "Профиль сброшен",
                "Папки профиля удалены.\n\n"
                "Рекомендуется перезапустить программу перед\n"
                "повторной работой с Kino.pub."
            )
        return removed_any

    # --- Скрытно сбрасываем профиль Kino.pub при старте ---
    try:
        if bool(load_settings().get("purge_kino_profile_on_startup", True)):
            _purge_kino_profile(silent=True)
    except Exception as e:
        logging.error("Silent purge failed: %s", e)


    def logout_kino():
        """
        'Выйти' = логика как 'обновить профиль':
        - стоп загрузок
        - закрытие драйверов
        - чистка профиля
        - kino_logged_in=False
        - отключаем уведомления
        """
        global kino_logged_in, search_driver, NOTIFICATIONS_ENABLED

        # 1) останавливаем загрузки
        try:
            manager.stop_all(show_message=False)
        except Exception:
            pass

        # 2) закрываем драйверы пула
        try:
            if hasattr(pool, "close_all"):
                pool.close_all()
            elif hasattr(pool, "shutdown"):
                pool.shutdown()
        except Exception:
            pass

        # 3) закрываем драйвер поиска
        try:
            if search_driver is not None:
                search_driver.quit()
                search_driver = None
        except Exception:
            pass

        # 4) чистим профиль
        _purge_kino_profile(silent=False)

        # 5) статус "не залогинен"
        kino_logged_in = False
        try:
            update_sidebar_status()
        except Exception:
            pass

        # 6) уведомления выключаем + бейдж в 0
        NOTIFICATIONS_ENABLED = False
        try:
            clear_notifications()
            notify_count_var.set(0)
        except Exception:
            pass

            
    # ========== Requests: проверка списка фильмов в медиатеке ==========
    from tkinter import ttk  # на случай, если выше не импортнулось

    req_top = tk.Frame(requests, bg=BG_SURFACE,
                       highlightbackground=BORDER, highlightthickness=1)
    req_top.pack(side="top", fill="x", pady=(0, 6))

    tk.Label(
        req_top,
        text="📝 Работа с запросами",
        bg=BG_SURFACE,
        fg=ACCENT_SECOND,
        font=("Segoe UI Semibold", 16),
    ).pack(side="left", padx=12, pady=8)


    

    # --- Тело экрана ---
    req_body = tk.Frame(requests, bg=BG_WINDOW)
    req_body.pack(fill="both", expand=True, padx=10, pady=8)

    # Левая колонка фиксированная (ввод), правая — максимально широкая (результаты)
    try:
        _req_scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
    except Exception:
        _req_scale = 1.0
    _req_scale = max(1.0, min(3.0, _req_scale))

    req_body.columnconfigure(0, weight=0, minsize=int(320 * _req_scale))
    req_body.columnconfigure(1, weight=1)
    req_body.rowconfigure(0, weight=1)

    # Левая часть: ввод списка
    req_left = tk.Frame(req_body, bg=BG_WINDOW)
    req_left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

    tk.Label(
        req_left,
        text="Список фильмов (по одному в строке):",
        bg=BG_WINDOW,
        fg=SUBTEXT,
        font=("Segoe UI", 10),
    ).pack(anchor="w")

    req_text = tk.Text(
        req_left,
        height=12,
        bg=FIELD_BG,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        font=("Segoe UI", 10),
        wrap="none",
    )
    req_text.pack(fill="both", expand=True, pady=(4, 0))

    req_btn_row = tk.Frame(req_left, bg=BG_WINDOW)
    req_btn_row.pack(fill="x", pady=(6, 0))

    # Кнопка "Проверить в медиатеке"
    btn_req_check = tk.Button(req_btn_row, text="Проверить в медиатеке")
    style_secondary(btn_req_check)
    btn_req_check.pack(side="left", padx=(0, 8))

    # Кнопка "Очистить"
    btn_req_clear = tk.Button(req_btn_row, text="Очистить")
    style_secondary(btn_req_clear)
    btn_req_clear.pack(side="left", padx=(0, 8))

    # Кнопка "Загрузить из TXT"
    btn_req_txt = tk.Button(req_btn_row, text="Загрузить из TXT")
    style_secondary(btn_req_txt)
    btn_req_txt.pack(side="left")

    # Правая часть: результаты
    req_right = tk.Frame(req_body, bg=BG_WINDOW)
    req_right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

    # Карточка для таблицы запросов
    card_req = tk.Frame(
        req_right,
        bg=BG_SURFACE,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    card_req.pack(fill="both", expand=True)
    tk.Frame(card_req, bg=ACCENT, height=2).pack(fill="x", side="top")

    # --- Панель опций над таблицей ---
    req_options = tk.Frame(card_req, bg=BG_SURFACE)
    # уменьшенный зазор сверху/снизу, чтобы не было огромной дыры
    req_options.pack(fill="x", padx=12, pady=(4, 4))

    req_select_all_var = tk.BooleanVar(value=False)
    req_copy_meta_var  = tk.BooleanVar(value=False)

    # набор отмеченных строк (по id элемента в Treeview)
    req_checked_items: set[str] = set()

    def req_toggle_select_all():
        """Выделить / снять выделение всех строк (галочки слева)."""
        items = req_tree.get_children()
        if not items:
            return

        if req_select_all_var.get():
            # включили чекбокс "Выделить все" — ставим галочки всем
            for item in items:
                if item not in req_checked_items:
                    req_checked_items.add(item)
                    vals = list(req_tree.item(item, "values"))
                    if vals:
                        vals[0] = "☑"
                        req_tree.item(item, values=vals)
        else:
            # выключили — снимаем
            req_checked_items.clear()
            for item in items:
                vals = list(req_tree.item(item, "values"))
                if vals:
                    vals[0] = "☐"
                    req_tree.item(item, values=vals)

    def req_on_copy_meta():
        # при переключении "Копировать метафайлы" — пересчитать пути
        for item in req_tree.get_children():
            update_row_paths(item)

    def make_req_chk(text, var, cmd):
        cb = tk.Checkbutton(
            req_options,
            text=text,
            variable=var,
            command=cmd,
            bg=BG_SURFACE,
            fg=TEXT,
            activebackground=BG_SURFACE,
            activeforeground=TEXT,
            selectcolor=ACCENT,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            font=("Segoe UI", 10),
        )
        cb.pack(side="left", padx=(0, 18))
        return cb

    def req_toggle_item_check(item_id: str):
        """Переключить галочку в первой колонке для указанной строки."""
        if not item_id:
            return
        vals = list(req_tree.item(item_id, "values"))
        if not vals:
            return

        if item_id in req_checked_items:
            req_checked_items.remove(item_id)
            vals[0] = "☐"
        else:
            req_checked_items.add(item_id)
            vals[0] = "☑"

        req_tree.item(item_id, values=vals)

    chk_req_select_all = make_req_chk("Выделить все", req_select_all_var, req_toggle_select_all)
    chk_req_copy_meta  = make_req_chk("Копировать метафайлы", req_copy_meta_var, req_on_copy_meta)

    # --- Таблица результатов ---
    req_table_frame = tk.Frame(card_req, bg=BG_SURFACE)
    req_table_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    req_scroll = tk.Scrollbar(req_table_frame)
    req_scroll.pack(side="right", fill="y")

    # колонки: маленькая для галочки, справа — стрелка путей
    req_columns = ("sel", "req_title", "status", "found_title", "path", "paths_btn")

    req_tree = ttk.Treeview(
        req_table_frame,
        columns=req_columns,
        show="headings",
        height=10,
        yscrollcommand=req_scroll.set,
    )
    req_scroll.config(command=req_tree.yview)

    req_tree.heading("sel",         text="",                 anchor="center")
    req_tree.heading("req_title",   text="Запрос",           anchor="w")
    req_tree.heading("status",      text="Статус",           anchor="center")
    req_tree.heading("found_title", text="Найденный фильм",  anchor="w")
    req_tree.heading("path",        text="Путь",             anchor="w")
    req_tree.heading("paths_btn",   text="",                 anchor="center")

    try:
        _scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
    except Exception:
        _scale = 1.0
    _scale = max(1.0, min(3.0, _scale))

    req_tree.column("sel",         width=int(26 * _scale), minwidth=int(24 * _scale), anchor="center", stretch=False)
    req_tree.column("req_title",   width=int(260 * _scale), minwidth=int(160 * _scale), anchor="w", stretch=True)
    req_tree.column("status",      width=int(160 * _scale), minwidth=int(130 * _scale), anchor="center", stretch=False)
    req_tree.column("found_title", width=int(280 * _scale), minwidth=int(200 * _scale), anchor="w", stretch=True)
    req_tree.column("path",        width=int(520 * _scale), minwidth=int(260 * _scale), anchor="w", stretch=True)
    req_tree.column("paths_btn",   width=int(30 * _scale), minwidth=int(26 * _scale), anchor="center", stretch=False)

    req_tree.pack(fill="both", expand=True)

    def find_metas_for_video(video_path: str) -> list[str]:
        """
        Ищем метафайлы рядом с указанным видео-файлом:
        nfo, jpg, png, webp и т.п. с тем же базовым именем.
        """
        base_dir = os.path.dirname(video_path)
        base_stem = os.path.splitext(os.path.basename(video_path))[0].lower()

        if not base_dir or not os.path.isdir(base_dir):
            return []

        metas: list[str] = []
        try:
            for fname in os.listdir(base_dir):
                full = os.path.join(base_dir, fname)
                if not os.path.isfile(full):
                    continue

                ext = os.path.splitext(fname)[1].lower()
                if ext not in META_EXTENSIONS:
                    continue

                stem = os.path.splitext(fname)[0].lower()
                if stem.startswith(base_stem) or base_stem.startswith(stem):
                    metas.append(full)
        except Exception as e:
            logging.error("find_metas_for_video(%s) failed: %s", video_path, e)

        return metas

    def show_paths_popup(item_id: str):
        """Всплывающее окошко: выбор основного пути + просмотр метафайлов рядом с файлом."""
        meta = request_rows_meta.get(item_id)
        if not meta:
            return

        videos = meta.get("videos") or []
        if not videos:
            return

        popup = tk.Toplevel(root)
        popup.title("Варианты путей")
        try:
            popup.iconbitmap(get_app_icon())
        except Exception:
            pass

        popup.transient(root)
        popup.grab_set()
        popup.resizable(False, False)
        popup.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        W, H = 900, 420
        try:
            bbox = req_tree.bbox(item_id, column="path") or req_tree.bbox(item_id)
            if bbox:
                x, y, w, h = bbox
                px = req_tree.winfo_rootx() + x
                py = req_tree.winfo_rooty() + y + h
                popup.geometry(f"{W}x{H}+{px}+{py}")
            else:
                popup.geometry(f"{W}x{H}")
        except Exception:
            popup.geometry(f"{W}x{H}")

        tk.Frame(popup, bg=ACCENT, height=3).pack(fill="x", side="top")

        body = tk.Frame(popup, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=10)

        tk.Label(
            body,
            text="Выберите основной путь:",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")

        lb_paths = tk.Listbox(
            body,
            height=min(8, max(2, len(videos))),
            bg=FIELD_BG,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground=TEXT_ON_ACCENT,
            activestyle="none",
            font=("Segoe UI", 9),
        )
        lb_paths.pack(fill="x", pady=(2, 6))

        for i, rec in enumerate(videos):
            p = rec["path"]
            lb_paths.insert("end", f"{i+1}. {p}")

        tk.Label(
            body,
            text="Метафайлы рядом с выбранным файлом:",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        lb_meta = tk.Listbox(
            body,
            height=9,
            bg=FIELD_BG,
            fg=SUBTEXT,
            selectbackground=ACCENT,
            selectforeground=TEXT_ON_ACCENT,
            activestyle="none",
            font=("Segoe UI", 9),
        )
        lb_meta.pack(fill="both", expand=True, pady=(2, 6))

        def refresh_meta_for_selected():
            lb_meta.delete(0, "end")
            sel = lb_paths.curselection()
            if not sel:
                return

            rec = videos[sel[0]]
            video_path = rec["path"]

            meta_files = find_metas_for_video(video_path)
            meta_records = [{"path": p} for p in meta_files]
            meta["metas"] = meta_records

            for p in meta_files:
                lb_meta.insert("end", os.path.basename(p))

        sel_idx = meta.get("selected_video_index")
        if sel_idx is None and videos:
            sel_idx = 0
        if sel_idx is not None and 0 <= sel_idx < len(videos):
            lb_paths.selection_set(sel_idx)
            lb_paths.see(sel_idx)
        refresh_meta_for_selected()

        lb_paths.bind("<<ListboxSelect>>", lambda e: refresh_meta_for_selected())

        btn_row = tk.Frame(body, bg=BG_SURFACE)
        btn_row.pack(fill="x", pady=(4, 0))

        def apply_and_close():
            sel = lb_paths.curselection()
            if videos and sel:
                meta["selected_video_index"] = sel[0]
                update_row_paths(item_id)
            popup.destroy()

        btn_ok = tk.Button(btn_row, text="Выбрать", command=apply_and_close)
        try:
            th = 2 if CURRENT_THEME == "light" else 1
        except Exception:
            th = 1
        btn_ok.config(
            text="OK",
            bg=BG_SURFACE,
            fg=TEXT,
            activebackground=HOVER_BG,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Segoe UI", 11),
            padx=26,
            pady=12,
            highlightthickness=th,
            highlightbackground=ACCENT_SECOND,
            highlightcolor=ACCENT_SECOND,
        )
        btn_ok.bind("<Enter>", lambda e: btn_ok.config(bg=HOVER_BG))
        btn_ok.bind("<Leave>", lambda e: btn_ok.config(bg=BG_SURFACE))
        btn_ok.pack(side="right", padx=4)

        btn_cancel = tk.Button(btn_row, text="Отмена", command=popup.destroy)
        style_secondary(btn_cancel)
        btn_cancel.pack(side="right", padx=4)

        popup.bind("<Return>", lambda e: apply_and_close())
        popup.bind("<Escape>", lambda e: popup.destroy())

    def on_req_click(event):
        """Клик по строке: первая колонка — галочка, последняя — стрелка."""
        region = req_tree.identify("region", event.x, event.y)
        if region != "cell":
            return

        col = req_tree.identify_column(event.x)  # "#1", "#2", ...
        row = req_tree.identify_row(event.y)
        if not row:
            return

        if col == "#1":  # колонка с галочкой
            req_toggle_item_check(row)
            return "break"

        if col == "#6":  # колонка с кнопкой-стрелкой
            show_paths_popup(row)
            return "break"

    req_tree.bind("<Button-1>", on_req_click)

    def on_req_row_double_click(event):
        """Двойной клик по строке — открыть путь в Проводнике и выделить файл."""
        item_id = req_tree.identify_row(event.y)
        if not item_id:
            return

        vals = req_tree.item(item_id, "values")
        if len(vals) < 5:
            return

        path = vals[4]
        if not path:
            return

        path = str(path).split(";")[0].strip()
        open_in_explorer(path)

    req_tree.bind("<Double-1>", on_req_row_double_click)

    # --- Футер ПОД таблицей (внутри card_req, чтобы не было лишней пустоты) ---
    req_footer = tk.Frame(card_req, bg=BG_SURFACE)
    req_footer.pack(fill="x", padx=8, pady=(0, 6))

    req_summary = tk.Label(
        req_footer,
        text="Всего запросов: 0 | найдено: 0 | нет в медиатеке: 0",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 9),
    )
    req_summary.pack(side="left", padx=8)

    # ⚡ Новые кнопки
    btn_req_dl_selected = tk.Button(req_footer, text="Найти выбранные")
    style_secondary(btn_req_dl_selected)
    btn_req_dl_selected.pack(side="right", padx=8)

    btn_req_dl_missing = tk.Button(req_footer, text="Найти не найденные")
    style_secondary(btn_req_dl_missing)
    btn_req_dl_missing.pack(side="right", padx=8)

    btn_req_copy = tk.Button(req_footer, text="Скопировать выделенные")
    style_secondary(btn_req_copy)
    btn_req_copy.pack(side="right", padx=8)



    # --- Логика работы с запросами ---
    def update_row_paths(item_id: str):
        """
        Обновляем колонку 'Путь' и список paths_last для строки запросов.
        """
        meta = request_rows_meta.get(item_id)
        if not meta:
            return

        matches = meta.get("matches") or []
        videos  = meta.get("videos") or []

        if not matches:
            return

        chosen = None
        sel_idx = meta.get("selected_video_index")
        if videos:
            if sel_idx is not None and 0 <= sel_idx < len(videos):
                chosen = videos[sel_idx]
            elif meta.get("chosen") in videos:
                chosen = meta["chosen"]
            else:
                chosen = videos[0]
        else:
            chosen = meta.get("chosen") or matches[0]

        main_path = chosen["path"] if chosen else ""
        meta["chosen"] = chosen

        paths: list[str] = []
        if main_path:
            paths.append(main_path)
            if req_copy_meta_var.get():
                for p in find_metas_for_video(main_path):
                    paths.append(p)

        meta["paths_last"] = paths

        vals = list(req_tree.item(item_id, "values"))
        if len(vals) >= 5:
            vals[4] = main_path
            req_tree.item(item_id, values=vals)

    def build_index_map():
        """
        Индекс по нормализованному названию.
        """
        idx = {}
        for name, path in movie_index:
            base, y = split_title_year(name)
            base = base or name

            cleaned = cleanup_title(base)
            key = normalize_name(cleaned)

            ext = os.path.splitext(name)[1].lower()
            is_video = ext in VIDEO_EXTENSIONS
            is_meta = (ext in META_EXTENSIONS) or (ext and not is_video)

            rec = {
                "name": name,
                "path": path,
                "year": y,
                "ext": ext,
                "is_video": is_video,
                "is_meta": is_meta,
            }
            idx.setdefault(key, []).append(rec)
        return idx

    def open_in_explorer(path: str):
        """Открыть файл/папку в проводнике и по возможности выделить файл."""
        if not path:
            return

        path = os.path.normpath(path)

        if os.name == "nt":
            if os.path.exists(path):
                subprocess.Popen(
                    ["explorer", "/select,", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                folder = os.path.dirname(path) or path
                if os.path.isdir(folder):
                    subprocess.Popen(
                        ["explorer", folder],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        else:
            if not os.path.exists(path):
                return
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", path])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(path) or path])
            except Exception as e:
                logging.error("open_in_explorer failed: %s", e)

    def check_requests():
        global request_rows_meta, kino_urls_for_requests

        if not index_loaded:
            messagebox.showerror(
                "Ошибка",
                "Сначала проверь данные на NAS (внизу слева: NAS → «Проверить»).",
            )
            return

        lines = req_text.get("1.0", "end").splitlines()

        # чистим таблицу и служебные структуры
        for item in req_tree.get_children():
            req_tree.delete(item)
        request_rows_meta.clear()
        req_checked_items.clear()

        index_map = build_index_map()

        total = 0         # всего запросов (непустых строк)
        found_cnt = 0     # «Найдено»
        missing_cnt = 0   # «Нет в медиатеке»

        for line in lines:
            original = line.strip()
            if not original:
                continue
            total += 1

            pre_url = kino_urls_for_requests.get(original)

            title, req_year = split_title_year(original)
            title = title or original

            norm_title = normalize_name(cleanup_title(title))
            matches = index_map.get(norm_title, [])

            # небольшие вариации названия
            if not matches and "," in original:
                no_comma = original.replace(",", " ")
                alt_title, _ = split_title_year(no_comma)
                alt_title = alt_title or no_comma
                norm_alt = normalize_name(cleanup_title(alt_title))
                matches = index_map.get(norm_alt, [])

            if not matches:
                cleaned_orig = cleanup_title(original)
                if cleaned_orig and cleaned_orig != title:
                    norm3 = normalize_name(cleanup_title(cleaned_orig))
                    matches = index_map.get(norm3, [])

            was_fuzzy = False
            if not matches:
                # пробуем «похожий» поиск
                key_for_fuzzy = norm_title
                all_keys = list(index_map.keys())
                close = difflib.get_close_matches(
                    key_for_fuzzy, all_keys, n=3, cutoff=0.8
                )
                for k in close:
                    matches.extend(index_map.get(k, []))
                if close and matches:
                    was_fuzzy = True

            videos: list[dict] = []
            metas: list[dict] = []
            chosen = None
            path_str = ""
            status = ""
            arrow = ""
            display_title = ""

            if not matches:
                status = "❌ Нет в медиатеке"
                missing_cnt += 1
            else:
                videos = [r for r in matches if r["is_video"]]
                metas  = [r for r in matches if r["is_meta"]]

                # выбираем конкретный релиз
                for rec in videos:
                    if req_year and rec["year"] == req_year:
                        chosen = rec
                        break
                if chosen is None:
                    chosen = videos[0] if videos else matches[0]

                display_title = chosen["name"]
                main_path = chosen["path"]
                path_str = main_path or ""

                y = chosen.get("year")
                if req_year and y != req_year:
                    status = f"⚠️ Найдено, год {y or '—'}"
                else:
                    status = " Найдено (≈)" if was_fuzzy else " Найдено"
                    found_cnt += 1

                if len(videos) > 1 or metas:
                    arrow = "▸"

            item_id = req_tree.insert(
                "",
                "end",
                values=("☐", original, status, display_title, path_str, arrow),
            )

            request_rows_meta[item_id] = {
                "original": original,
                "req_year": req_year,
                "matches": matches,
                "videos": videos,
                "metas": metas,
                "chosen": chosen,
                "selected_video_index": (
                    videos.index(chosen) if (chosen and chosen in videos) else None
                ),
                "paths_last": [],
                "kino_url": pre_url,
            }

            update_row_paths(item_id)

        req_summary.config(
            text=f"Всего запросов: {total} | найдено: {found_cnt} | нет в медиатеке: {missing_cnt}"
        )



    def copy_selected_requests():
        """Скопировать файлы из отмеченных строк (основной путь + метафайлы, если включено)."""
        items = list(req_checked_items)
        if not items:
            messagebox.showinfo(
                "Копирование",
                "Отметьте галочкой слева хотя бы один фильм."
            )
            return

        all_paths: list[str] = []
        seen: set[str] = set()

        for item_id in items:
            meta = request_rows_meta.get(item_id)
            if not meta:
                continue
            paths = meta.get("paths_last") or []
            for p in paths:
                p = str(p).strip()
                if not p:
                    continue
                if p not in seen:
                    seen.add(p)
                    all_paths.append(p)

        if not all_paths:
            messagebox.showinfo(
                "Копирование",
                "Для отмеченных строк нет путей для копирования."
            )
            return

        target_dir = filedialog.askdirectory(
            title="Куда скопировать файлы"
        )
        if not target_dir:
            return

        target_dir = os.path.normpath(target_dir)

        # окно прогресса с нашей иконкой
        progress_win = tk.Toplevel(root)
        progress_win.title("Копирование")
        try:
            progress_win.iconbitmap(get_app_icon())
        except Exception:
            pass

        progress_win.transient(root)
        progress_win.grab_set()
        progress_win.resizable(False, False)
        progress_win.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        tk.Frame(progress_win, bg=ACCENT, height=2).pack(fill="x", side="top")

        body = tk.Frame(progress_win, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        lbl = tk.Label(
            body,
            text=f"Копирование файлов: 0 / {len(all_paths)}",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
        )
        lbl.pack(anchor="w")

        progress = ttk.Progressbar(body, mode="determinate", maximum=len(all_paths))
        progress.pack(fill="x", pady=(8, 0))

        def worker():
            copied = 0
            skipped = 0
            for i, src in enumerate(all_paths, start=1):
                try:
                    if not os.path.exists(src):
                        skipped += 1
                    else:
                        fname = os.path.basename(src)
                        dst = os.path.join(target_dir, fname)

                        base, ext = os.path.splitext(fname)
                        cnt = 1
                        while os.path.exists(dst):
                            dst = os.path.join(target_dir, f"{base} ({cnt}){ext}")
                            cnt += 1

                        shutil.copy2(src, dst)
                        copied += 1
                except Exception as e:
                    logging.error("Ошибка копирования %s: %s", src, e)
                    skipped += 1

                def _update(i=i, copied=copied, skipped=skipped):
                    progress["value"] = i
                    lbl.config(
                        text=f"Копирование файлов: {i} / {len(all_paths)} "
                             f"(успешно: {copied}, пропущено: {skipped})"
                    )
                root.after(0, _update)

            def _finish():
                progress_win.destroy()
                messagebox.showinfo(
                    "Готово",
                    f"Скопировано файлов: {copied}\nПропущено/ошибок: {skipped}"
                )

            root.after(0, _finish)

        threading.Thread(target=worker, daemon=True).start()

    btn_req_copy.config(command=copy_selected_requests)

    def clear_requests(reset_urls: bool = True):
        req_text.delete("1.0", "end")
        for item in req_tree.get_children():
            req_tree.delete(item)

        req_checked_items.clear()
        request_rows_meta.clear()

        if reset_urls:
            # Полная очистка: забываем привязки "строка -> kino_url"
            kino_urls_for_requests.clear()

        req_summary.config(
            text="Всего запросов: 0 | найдено: 0 | нет в медиатеке: 0"
        )


    def load_requests_from_txt():
        path = filedialog.askopenfilename(
            title="Выберите TXT со списком фильмов",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return

        clear_requests(True)               # <-- вот сюда
        req_text.insert("1.0", content)

    def search_requests(mode: str):
        """
        mode = 'selected'  -> использовать строки, отмеченные галочками
        mode = 'missing'   -> использовать строки, которые считаются «не найденными»

        1) Если у строк есть сохранённые kino_url (списки пришли из новинок/поиска) —
           переносим их во вкладку поиска Kino.pub и НЕ дёргаем Selenium лишний раз.
        2) Если ссылок нет (список введён/загружен вручную) —
           делаем поиск на Kino.pub по названиям через search_by_list().
        """
        global kino_logged_in

        if not kino_logged_in:
            show_login_required()
            return

        # 1) Определяем, какие строки брать
        if mode == "selected":
            items = list(req_checked_items)
            if not items:
                messagebox.showinfo(
                    "Поиск",
                    "Отметьте галочкой слева хотя бы один фильм."
                )
                return

        elif mode == "missing":
            items: list[str] = []
            for item in req_tree.get_children():
                vals = req_tree.item(item, "values")
                if len(vals) >= 3:
                    status = str(vals[2]).strip().lower()
                    # всё, что НЕ начинается с "найдено" — считаем «не найденным»
                    if "нет в медиатеке" in status:
                        items.append(item)

            if not items:
                messagebox.showinfo(
                    "Поиск",
                    "Нет строк, которые считаются «не найденными»."
                )
                return
        else:
            return

        # 2) Чистим таблицу поиска Kino.pub
        for row in tree_search.get_children():
            tree_search.delete(row)
        search_meta.clear()
        checked_items.clear()

        used_urls: set[str] = set()
        fallback_titles: list[str] = []
        seen_titles: set[str] = set()

        # 3) Переносим строки в kino_search, используя уже сохранённый kino_url.
        #    Параллельно собираем названия для возможного fallback-поиска.
        for item_id in items:
            meta = request_rows_meta.get(item_id) or {}

            original = (meta.get("original") or "").strip()
            if not original:
                vals = req_tree.item(item_id, "values")
                if len(vals) >= 2:
                    original = str(vals[1]).strip()
            if not original:
                continue

            url = (meta.get("kino_url") or "").strip()

            if url:
                base_title, year = split_title_year(original)
                base_title = base_title or original

                if url in used_urls:
                    continue
                used_urls.add(url)

                display_title = f"{base_title} ({year})" if year else base_title

                row_id = tree_search.insert(
                    "",
                    "end",
                    values=("☐", original, display_title, year or "", url),
                )
                search_meta[row_id] = {
                    "query": original,
                    "title": base_title,
                    "year":  year,
                    "url":   url,
                    "eng_title": None,
                }
            else:
                if original not in seen_titles:
                    seen_titles.add(original)
                    fallback_titles.append(original)

        # 4а) Если есть сохранённые URL — ведём себя как раньше
        if used_urls:
            try:
                _active[0] = "kino"
                set_nav_active(nav_items, "kino")
                animate_nav_indicator(nav_items["kino"])
            except Exception:
                pass
            slide_switch(requests, kino_search, root, "right")
            return

        # 4б) Если URL нет, но есть названия — делаем поиск по списку на Kino.pub
        if fallback_titles:
            try:
                list_text.delete("1.0", "end")
                list_text.insert("1.0", "\n".join(fallback_titles))
            except Exception:
                messagebox.showerror(
                    "Ошибка",
                    "Не удалось подготовить список для поиска на Kino.pub."
                )
                return

            # стандартный поиск по списку
            search_by_list()
            try:
                _active[0] = "kino"
                set_nav_active(nav_items, "kino")
                animate_nav_indicator(nav_items["kino"])
            except Exception:
                pass
            slide_switch(requests, kino_search, root, "right")
            return

        # 4в) Вообще ничего нет — показываем старое сообщение
        messagebox.showinfo(
            "Поиск",
            "Для выбранных строк нет сохранённых ссылок Kino.pub.\n"
            "Обычно они появляются, если список был получен с экрана новинок или поиска Kino.pub."
        )





    btn_req_check.config(command=check_requests)
    btn_req_clear.config(command=lambda: clear_requests(True))
    btn_req_txt.config(command=load_requests_from_txt)
    btn_req_copy.config(command=copy_selected_requests)

    btn_req_dl_selected.config(
        command=lambda: search_requests("selected")
    )
    btn_req_dl_missing.config(
        command=lambda: search_requests("missing")
    )



        # ========== Kino.pub Tools ==========
    kino_top = tk.Frame(kino, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    kino_top.pack(side="top", fill="x")

    tk.Label(
        kino_top,
        text="Kino.pub Tools",
        bg=BG_SURFACE,
        fg=ACCENT_SECOND,
        font=("Segoe UI Semibold", 16),
    ).pack(side="left", padx=12, pady=10)



    # кнопка "Поиск" — открывает отдельный экран поиска kino_search
    btn_kino_search = tk.Button(kino_top, text="Поиск")
    style_secondary(btn_kino_search)

    def open_kino_search():
        show_screen(screens, "kino_search")
        set_nav_active(nav_items, "kino")  # остаёмся на разделе Kino.pub


    btn_kino_search.config(command=open_kino_search)
    btn_kino_search.pack(side="left", padx=6)

    btn_kino_history = tk.Button(kino_top, text="🕘 История")
    style_secondary(btn_kino_history)
    btn_kino_history.pack(side="left", padx=6)

    
    

    # карточка загрузчика
    card_kino = tk.Frame(kino, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    card_kino.pack(fill="both", expand=True, padx=18, pady=(14, 18))
    tk.Frame(card_kino, bg=ACCENT, height=3).pack(fill="x", side="top")

    dl_pane = tk.PanedWindow(
        card_kino,
        orient="horizontal",
        bg=BG_SURFACE,
        bd=0,
        relief="flat",
        sashwidth=10,
        sashrelief="flat",
    )
    dl_pane.pack(fill="both", expand=True, padx=18, pady=18)

    dl_left = tk.Frame(dl_pane, bg=BG_SURFACE)
    dl_right = tk.Frame(dl_pane, bg=BG_SURFACE)
    dl_pane.add(dl_left, minsize=340, stretch="never")
    dl_pane.add(dl_right, minsize=420, stretch="always")

    top_part = tk.Frame(dl_left, bg=BG_SURFACE); top_part.pack(fill="x", pady=(20, 10))
    tk.Label(top_part, text="🎬 Kino.pub Downloader", bg=BG_SURFACE, fg=ACCENT,
             font=("Segoe UI Semibold", 20)).pack(pady=(0, 10))
    tk.Label(top_part, text="Введите запрос или URL карточки — будет скачано видео",
             bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10), wraplength=520, justify="center").pack(pady=(0, 14))

    input_frame = tk.Frame(top_part, bg=BG_SURFACE); input_frame.pack(fill="x", padx=40)
    tk.Label(input_frame, text="🔍URL с kino.pub:", bg=BG_SURFACE, fg=SUBTEXT,
             font=("Segoe UI", 10)).pack(anchor="w")
    # строка: поле ввода + кнопка "Скачать" справа
    input_row = tk.Frame(input_frame, bg=BG_SURFACE)
    input_row.pack(fill="x", pady=(4, 0))

    kino_input = tk.Entry(
        input_row,
        bg=FIELD_BG,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        font=("Segoe UI", 11),
        
    )
    style_entry(kino_input)
    kino_input.pack(side="left", fill="x", expand=True, ipady=4)

    # кнопка "Скачать" такого же размера/стиля, как "Выбрать"
    btn_download = tk.Button(input_row, text="⬇️ Скачать")
    style_secondary(btn_download)          # тот же стиль, что и у "Выбрать"
    btn_download.pack(side="left", padx=(8, 0), ipady=2)

    btn_download_series = tk.Button(input_row, text="📺 Сериал")
    style_secondary(btn_download_series)
    btn_download_series.pack(side="left", padx=(8, 0), ipady=2)


    path_frame = tk.Frame(top_part, bg=BG_SURFACE); path_frame.pack(fill="x", padx=40, pady=(10, 8))
    tk.Label(path_frame, text="📂 Папка сохранения:", bg=BG_SURFACE, fg=SUBTEXT,
             font=("Segoe UI", 10)).pack(anchor="w")
    settings = load_settings()
    default_dir = settings.get("last_download_dir") or os.path.join(os.getcwd(), "Downloads")
    out_dir_var = tk.StringVar(value=default_dir)
    path_entry = tk.Entry(path_frame, textvariable=out_dir_var, bg=FIELD_BG, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=("Segoe UI", 10), )
    style_entry(path_entry)
    path_entry.pack(side="left", fill="x", expand=True, ipady=4, pady=(4, 0))

    def _normalize_out_dir(p: str) -> str:
        p = (p or "").strip()
        if not p:
            return ""
        p = os.path.expandvars(os.path.expanduser(p))
        if os.name == "nt":
            # частая опечатка: "C/Film" -> "C:/Film"
            if len(p) >= 2 and p[0].isalpha() and p[1] in ("/", "\\"):
                p = p[0] + ":" + p[1:]
        return os.path.abspath(os.path.normpath(p))

    def _get_out_dir(*, create: bool = False) -> str:
        raw = out_dir_var.get()
        out_dir = _normalize_out_dir(raw)
        if not out_dir:
            return ""
        if create:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Папка", f"Не удалось открыть/создать папку:\n{out_dir}\n\n{e}")
                return ""
        if out_dir != (raw or "").strip():
            try:
                out_dir_var.set(out_dir)
            except Exception:
                pass
        try:
            s = load_settings()
            s["last_download_dir"] = out_dir
            save_settings(s)
        except Exception:
            pass
        return out_dir

    def choose_folder():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return

        d = filedialog.askdirectory(title="Выберите папку сохранения")
        if d:
            d = _normalize_out_dir(d)
            out_dir_var.set(d)
            s = load_settings()
            s["last_download_dir"] = d
            save_settings(s)
    choose_btn = tk.Button(path_frame, text="Выбрать", command=choose_folder); style_secondary(choose_btn)
    choose_btn.pack(side="left", padx=(8, 0))
    kino_status = tk.Label(top_part, text="", bg=BG_SURFACE, fg=ACCENT_SECOND, font=("Segoe UI", 10))
    kino_status.pack(pady=(8, 4))
    queue_part = tk.Frame(dl_right, bg=BG_SURFACE); queue_part.pack(fill="both", expand=True, padx=36, pady=(8, 12))

    from tkinter import ttk
    queue_toolbar = tk.Frame(queue_part, bg=BG_SURFACE)
    queue_toolbar.pack(fill="x", pady=(0, 6))

    btn_cancel_all = tk.Button(queue_toolbar, text="⛔ Отменить всё")
    style_secondary(btn_cancel_all)
    btn_cancel_all.pack(side="left", padx=4)

    btn_clear_all = tk.Button(queue_toolbar, text="🧹 Убрать всё")
    style_secondary(btn_clear_all)
    btn_clear_all.pack(side="left", padx=4)

    btn_clear_done = tk.Button(queue_toolbar, text="✅ Убрать готовые")
    style_secondary(btn_clear_done)
    btn_clear_done.pack(side="left", padx=4)

    btn_pause = tk.Button(queue_toolbar, text="⏸ Пауза")
    style_secondary(btn_pause)
    btn_pause.pack(side="left", padx=(16, 4))

    btn_resume = tk.Button(queue_toolbar, text="▶ Возобновить")
    style_secondary(btn_resume)
    btn_resume.pack(side="left", padx=4)

    btn_resume_all = tk.Button(queue_toolbar, text="▶ Возобновить всё")
    style_secondary(btn_resume_all)
    btn_resume_all.pack(side="left", padx=4)

    btn_convert = tk.Button(queue_toolbar, text="🎞 Конвертировать")
    style_secondary(btn_convert)
    btn_convert.pack(side="left", padx=4)
    try:
        btn_convert.config(state="disabled")
    except Exception:
        pass

    table_frame = tk.Frame(queue_part, bg=BG_SURFACE); table_frame.pack(fill="both", expand=True, pady=(4, 6))
    scrollbar = ttk.Scrollbar(table_frame); scrollbar.pack(side="right", fill="y")
    columns = ("#", "title", "status")
    tree = ttk.Treeview(
        table_frame,
        columns=columns,
        show="tree headings",
        height=6,
        yscrollcommand=scrollbar.set,
        style="KinoQueue.Treeview",
    )
    # ========== ПКМ МЕНЮ ДЛЯ ПОВТОРА ==========
        # ========== ПКМ МЕНЮ ДЛЯ ПОВТОРА / ПЕРЕЗАПУСКА ==========
    context_menu = tk.Menu(root, tearoff=0)
    register_menu(context_menu)

    def _top_item(iid: str) -> str:
        try:
            p = tree.parent(iid)
            while p:
                iid = p
                p = tree.parent(iid)
        except Exception:
            pass
        return iid

    _drag_state = {"iid": None, "y": 0, "moved": False}

    def _drag_start(event):
        try:
            region = str(tree.identify_region(event.x, event.y) or "")
        except Exception:
            region = ""
        if region not in ("cell", "tree"):
            _drag_state["iid"] = None
            return
        iid = tree.identify_row(event.y)
        if not iid:
            _drag_state["iid"] = None
            return
        iid = _top_item(iid)
        try:
            if tree.parent(iid):
                iid = _top_item(iid)
        except Exception:
            pass
        _drag_state["iid"] = iid
        _drag_state["y"] = int(event.y)
        _drag_state["moved"] = False
        try:
            tree.selection_set(iid)
        except Exception:
            pass

    def _drag_motion(event):
        iid = _drag_state.get("iid")
        if not iid:
            return
        try:
            dy = abs(int(event.y) - int(_drag_state.get("y") or 0))
        except Exception:
            dy = 0
        if dy < 6 and not _drag_state.get("moved"):
            return

        target = tree.identify_row(event.y)
        if not target:
            return
        target = _top_item(target)
        if not target or target == iid:
            return

        try:
            new_index = int(tree.index(target))
        except Exception:
            return

        try:
            cur_index = int(tree.index(iid))
        except Exception:
            cur_index = None

        if cur_index == new_index:
            return

        try:
            tree.move(iid, "", new_index)
            _drag_state["moved"] = True
        except Exception:
            return

    def _drag_end(_event):
        iid = _drag_state.get("iid")
        moved = bool(_drag_state.get("moved"))
        _drag_state["iid"] = None
        _drag_state["moved"] = False
        if not iid or not moved:
            return

        try:
            reindex_rows()
        except Exception:
            pass

        try:
            cb = getattr(manager, "reschedule_pending", None)
            if callable(cb):
                cb()
        except Exception:
            pass

        try:
            _schedule_kino_queue_save(0)
        except Exception:
            pass

        try:
            tree.selection_set(iid)
        except Exception:
            pass

    # drag&drop reorder (не ломает стандартный выбор строк)
    try:
        tree.bind("<ButtonPress-1>", _drag_start, add="+")
        tree.bind("<B1-Motion>", _drag_motion, add="+")
        tree.bind("<ButtonRelease-1>", _drag_end, add="+")
    except Exception:
        pass

    def retry_selected():
        """Перезапустить выделенный элемент с самого начала, в любом состоянии."""
        try:
            item = _top_item(tree.selection()[0])
        except Exception:
            return

        # Берём исходный текст/URL
        url = manager.url_by_item.get(item) or tree.set(item, "title")

        # Сбрасываем статус и запускаем заново
        tree.set(item, "status", "🟡 Подготовка...")
        out_dir = _get_out_dir()
        item_out_dir = None
        try:
            op = getattr(manager, "out_path_by_item", {}).get(item)
            if op:
                item_out_dir = os.path.dirname(os.path.normpath(str(op)))
        except Exception:
            item_out_dir = None
        try:
            if (not item_out_dir) and getattr(manager, "out_dir_by_item", {}).get(item):
                item_out_dir = str(getattr(manager, "out_dir_by_item", {}).get(item))
        except Exception:
            pass
        if not item_out_dir:
            item_out_dir = out_dir
        _start_kino_item(item, url, item_out_dir)

    def retry_mux_selected():
        """Повторить только MUX (если остались {out_path}.parts после ошибки ffmpeg)."""
        try:
            item = _top_item(tree.selection()[0])
        except Exception:
            return

        try:
            out_path = getattr(manager, "out_path_by_item", {}).get(item)
        except Exception:
            out_path = None

        if not out_path:
            messagebox.showerror("Ошибка", "Не удалось определить файл для MUX.\nПовторите загрузку целиком.")
            return

        parts_dir = out_path + ".parts"
        if not os.path.isdir(parts_dir):
            messagebox.showerror(
                "Ошибка",
                "Нет папки .parts для MUX.\nНечего муксовать — нужно скачать заново.",
            )
            return

        try:
            display_name = str(tree.set(item, "title") or "")
        except Exception:
            display_name = os.path.basename(out_path)

        ev = getattr(manager, "cancel_events", {}).get(item)
        if ev is None:
            ev = threading.Event()
            try:
                manager.cancel_events[item] = ev
            except Exception:
                pass
        else:
            try:
                ev.clear()
            except Exception:
                pass

        try:
            manager.final_status.pop(item, None)
        except Exception:
            pass

        manager.set_status(item, "🟣 MUX…")

        def _task():
            try:
                try:
                    auto_convert = bool(getattr(root, "_kino_auto_convert_all_audio", False))
                except Exception:
                    auto_convert = False

                def _audio_select_proxy(**kwargs):
                    cb = getattr(manager, "audio_select_cb", None)
                    if not callable(cb):
                        return None
                    return cb(item_id=item, **kwargs)

                def _cb(msg):
                    try:
                        t = str(msg or "")
                    except Exception:
                        return
                    if t.startswith("🔀 MUX"):
                        manager.set_status(item, t)
                    elif t.startswith("🎧"):
                        manager.set_status(item, t)
                    elif t.startswith("🟣"):
                        manager.set_status(item, "🟣 MUX…")
                    elif t.startswith("✅"):
                        manager.set_status(item, "✅ Готово")
                    elif t.startswith("❌") or ("Ошибка" in t):
                        manager.set_status(item, "❌ Ошибка MUX")

                result = hls_retry_mux(
                    out_path,
                    status_cb=_cb,
                    cancel_event=ev,
                    audio_select_cb=(None if auto_convert else _audio_select_proxy),
                )
                if getattr(ev, "is_set", lambda: False)():
                    return
                if result == "cancel":
                    manager.set_status(item, "🎞 Готово к конвертации")
                    return
                if result:
                    try:
                        manager.final_status[item] = "✅"
                    except Exception:
                        pass
                    manager.set_status(item, "✅ Готово")
                    try:
                        manager._notify("✅ Kino.pub", f"Готово (MUX): {display_name}")
                    except Exception:
                        pass
                else:
                    try:
                        manager.final_status[item] = "❌"
                    except Exception:
                        pass
                    manager.set_status(item, "❌ Ошибка MUX")
            except Exception as e:
                if getattr(ev, "is_set", lambda: False)():
                    return
                try:
                    manager.final_status[item] = "❌"
                except Exception:
                    pass
                manager.set_status(item, f"❌ {e}")

        threading.Thread(target=_task, daemon=True).start()
    

    def convert_selected():
        """Запустить стадию MUX вручную (для статуса «🎞 Готово к конвертации» или после ошибки MUX)."""
        try:
            item = _top_item(tree.selection()[0])
        except Exception:
            return

        try:
            s = str(tree.set(item, "status") or "")
        except Exception:
            s = ""

        if not (s.startswith("🎞") or ("Ошибка MUX" in s) or s.startswith("❌ Ошибка MUX")):
            try:
                messagebox.showinfo(
                    "Kino.pub",
                    "Сначала дождитесь окончания загрузки — появится статус «🎞 Готово к конвертации».",
                )
            except Exception:
                pass
            return

        retry_mux_selected()

    def open_download_dir():
        out_dir = _get_out_dir(create=True)
        if not out_dir:
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", out_dir])
        else:
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", out_dir])
                else:
                    subprocess.Popen(["xdg-open", out_dir])
            except Exception:
                pass

    def pause_selected():
        sel = tree.selection()
        if not sel:
            return
        targets = []
        try:
            targets = sorted({_top_item(i) for i in sel if i})
        except Exception:
            targets = list(sel)
        for iid in targets:
            try:
                manager.pause_item(iid)
            except Exception:
                pass
        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    def resume_selected():
        sel = tree.selection()
        if not sel:
            return
        targets = []
        try:
            targets = sorted({_top_item(i) for i in sel if i})
        except Exception:
            targets = list(sel)

        out_dir = _get_out_dir()
        for iid in targets:
            try:
                s = str(tree.set(iid, "status") or "")
            except Exception:
                s = ""
            if s.startswith(("✅", "❌", "⛔", "🎞")):
                continue

            url = manager.url_by_item.get(iid) or tree.set(iid, "title")

            item_out_dir = None
            try:
                op = getattr(manager, "out_path_by_item", {}).get(iid)
                if op:
                    item_out_dir = os.path.dirname(os.path.normpath(str(op)))
            except Exception:
                item_out_dir = None
            try:
                if (not item_out_dir) and getattr(manager, "out_dir_by_item", {}).get(iid):
                    item_out_dir = str(getattr(manager, "out_dir_by_item", {}).get(iid))
            except Exception:
                pass
            if not item_out_dir:
                item_out_dir = out_dir

            try:
                _start_kino_item(iid, url, item_out_dir)
            except Exception:
                pass

        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    def resume_all():
        items = list(tree.get_children(""))
        if not items:
            return

        out_dir = _get_out_dir()
        for iid in items:
            try:
                s = str(tree.set(iid, "status") or "")
            except Exception:
                s = ""
            if s.startswith(("✅", "❌", "⛔", "🎞")):
                continue

            url = manager.url_by_item.get(iid) or tree.set(iid, "title")

            item_out_dir = None
            try:
                op = getattr(manager, "out_path_by_item", {}).get(iid)
                if op:
                    item_out_dir = os.path.dirname(os.path.normpath(str(op)))
            except Exception:
                item_out_dir = None
            try:
                if (not item_out_dir) and getattr(manager, "out_dir_by_item", {}).get(iid):
                    item_out_dir = str(getattr(manager, "out_dir_by_item", {}).get(iid))
            except Exception:
                pass
            if not item_out_dir:
                item_out_dir = out_dir

            try:
                _start_kino_item(iid, url, item_out_dir)
            except Exception:
                pass

        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    def cancel_selected():
        sel = tree.selection()
        if not sel:
            return
        targets = []
        try:
            targets = sorted({ _top_item(i) for i in sel if i })
        except Exception:
            targets = list(sel)

        for item in targets:
            try:
                manager.cancel_item(item)
            except Exception:
                pass
            try:
                if hasattr(manager, "url_by_item"):
                    manager.url_by_item.pop(item, None)
                if hasattr(manager, "out_path_by_item"):
                    manager.out_path_by_item.pop(item, None)
                if hasattr(manager, "name_override_by_item"):
                    manager.name_override_by_item.pop(item, None)
                if hasattr(manager, "out_dir_by_item"):
                    manager.out_dir_by_item.pop(item, None)
                if hasattr(manager, "threads"):
                    manager.threads.pop(item, None)
            except Exception:
                pass
            try:
                tree.delete(item)
            except Exception:
                pass
        try:
            reindex_rows()
        except Exception:
            pass
        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    def cancel_all():
        items = list(tree.get_children())
        if not items:
            return
        if not messagebox.askyesno("Подтверждение", "Отменить все загрузки в очереди?"):
            return
        for item in items:
            try:
                manager.cancel_item(item)
            except Exception:
                pass
        try:
            _schedule_kino_queue_save(0)
        except Exception:
            pass

    def clear_all():
        items = list(tree.get_children())
        if not items:
            return
        if not messagebox.askyesno(
            "Подтверждение",
            "Убрать все из очереди?\nТекущие загрузки будут отменены.",
        ):
            return

        for item in items:
            try:
                t = getattr(manager, "threads", {}).get(item)
                is_alive = bool(t and t.is_alive())
            except Exception:
                is_alive = False

            try:
                manager.cancel_item(item)
            except Exception:
                pass

            try:
                if not is_alive and hasattr(manager, "cancel_events"):
                    manager.cancel_events.pop(item, None)
                if hasattr(manager, "url_by_item"):
                    manager.url_by_item.pop(item, None)
                if hasattr(manager, "out_path_by_item"):
                    manager.out_path_by_item.pop(item, None)
                if hasattr(manager, "name_override_by_item"):
                    manager.name_override_by_item.pop(item, None)
                if hasattr(manager, "out_dir_by_item"):
                    manager.out_dir_by_item.pop(item, None)
                if hasattr(manager, "threads"):
                    manager.threads.pop(item, None)
            except Exception:
                pass

            try:
                tree.delete(item)
            except Exception:
                pass

        try:
            reindex_rows()
        except Exception:
            pass
        try:
            _schedule_kino_queue_save(0)
        except Exception:
            pass

    def clear_done():
        items = list(tree.get_children())
        if not items:
            return
        done = []
        for item in items:
            try:
                s = str(tree.set(item, "status") or "")
            except Exception:
                continue
            if s.startswith("✅"):
                done.append(item)
        if not done:
            return
        if not messagebox.askyesno("Подтверждение", f"Убрать готовые из очереди? ({len(done)})"):
            return

        for item in done:
            try:
                if hasattr(manager, "cancel_events"):
                    manager.cancel_events.pop(item, None)
                if hasattr(manager, "url_by_item"):
                    manager.url_by_item.pop(item, None)
                if hasattr(manager, "out_path_by_item"):
                    manager.out_path_by_item.pop(item, None)
                if hasattr(manager, "name_override_by_item"):
                    manager.name_override_by_item.pop(item, None)
                if hasattr(manager, "out_dir_by_item"):
                    manager.out_dir_by_item.pop(item, None)
                if hasattr(manager, "threads"):
                    manager.threads.pop(item, None)
                if hasattr(manager, "final_status"):
                    manager.final_status.pop(item, None)
            except Exception:
                pass
            try:
                tree.delete(item)
            except Exception:
                pass

        try:
            reindex_rows()
        except Exception:
            pass
        try:
            manager._update_counter_label()
        except Exception:
            pass
        try:
            _schedule_kino_queue_save(0)
        except Exception:
            pass

    btn_cancel_all.config(command=cancel_all)
    btn_clear_all.config(command=clear_all)
    btn_clear_done.config(command=clear_done)
    btn_pause.config(command=pause_selected)
    btn_resume.config(command=resume_selected)
    btn_resume_all.config(command=resume_all)
    btn_convert.config(command=convert_selected)
                   
    context_menu.add_command(label="Повторить / перезапустить загрузку",
                             command=retry_selected)
    context_menu.add_command(label="🎞 Конвертировать (MUX без докачки)",
                             command=convert_selected)
    context_menu.add_separator()
    context_menu.add_command(label="⏸ Пауза", command=pause_selected)
    context_menu.add_command(label="▶ Возобновить", command=resume_selected)
    context_menu.add_command(label="Отменить и убрать из очереди",
                             command=cancel_selected)
    context_menu.add_command(
        label="Открыть папку загрузки",
        command=open_download_dir,
    )

    def on_right_click(event):
        item = tree.identify_row(event.y)
        if not item:
            return
        tree.selection_set(_top_item(item))
        # Раньше меню показывалось только при ошибке,
        # теперь — всегда, чтобы можно было перезапустить в любой момент.
        context_menu.tk_popup(event.x_root, event.y_root)

    tree.bind("<Button-3>", on_right_click)
    # ========================================================

    # ==========================================

    scrollbar.config(command=tree.yview)
    tree.bind("<Button-3>", on_right_click)
    tree.heading("#0", text="", anchor="center")
    tree.heading("#", text="№", anchor="center")
    tree.heading("title", text="Название / URL", anchor="w")
    tree.heading("status", text="Статус", anchor="center")
    try:
        scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
    except Exception:
        scale = 1.0
    scale = max(1.0, min(3.0, scale))
    tree.column("#0", width=int(26 * scale), minwidth=int(18 * scale), anchor="center", stretch=False)
    tree.column("#", width=int(40 * scale), minwidth=int(30 * scale), anchor="center", stretch=False)
    tree.column("title", width=int(360 * scale), minwidth=int(200 * scale), anchor="w", stretch=True)
    tree.column("status", width=int(200 * scale), minwidth=int(140 * scale), anchor="center", stretch=False)
    tree.pack(fill="both", expand=True)

    style = ttk.Style()
    style_tree(style)

    def _update_kino_queue_rowheight():
        rowheight = max(22, int(26 * scale))
        try:
            style.configure("KinoQueue.Treeview", rowheight=rowheight)
        except Exception:
            pass

    _update_kino_queue_rowheight()

    try:
        root._kino_queue_tree = tree
        root._update_kino_queue_rowheight = _update_kino_queue_rowheight
    except Exception:
        pass
    
    # --- Кнопки управления очередью (скрываем, если флаг False) ---
    if SHOW_QUEUE_CONTROLS:
        controls = tk.Frame(queue_part, bg=BG_SURFACE); controls.pack(fill="x", pady=(6, 2))

        def style_btn(b, accent=False):
            b.config(font=("Segoe UI", 10), padx=12, pady=6, borderwidth=0, relief="flat", cursor="hand2")
            if accent:
                b.config(bg=ACCENT, fg=TEXT, activebackground=ACCENT_HOVER, activeforeground=TEXT_ON_ACCENT)
            else:
                b.config(bg=BG_CARD, fg=ACCENT_SECOND, activebackground=HOVER_BG, activeforeground=ACCENT_SECOND)

        btn_import = tk.Button(controls, text="📂 Импорт списка"); style_btn(btn_import, True);  btn_import.pack(side="left", padx=4)
        btn_delete = tk.Button(controls, text="🗑 Удалить");        style_btn(btn_delete);       btn_delete.pack(side="left", padx=4)
        btn_run    = tk.Button(controls, text="⏩ Запустить всё");  style_btn(btn_run, True);    btn_run.pack(side="left", padx=4)
        btn_stop   = tk.Button(controls, text="⏹ Остановить");     style_btn(btn_stop);         btn_stop.pack(side="left", padx=4)


    def _get_kino_max_parallel() -> int:
        try:
            v = int(load_settings().get("kino_max_parallel", 2))
        except Exception:
            v = 2
        return max(1, min(4, v))

    kino_max_parallel = _get_kino_max_parallel()

    counter_bar = tk.Frame(queue_part, bg=BG_SURFACE); counter_bar.pack(fill="x", pady=(2, 0))
    active_counter = tk.Label(counter_bar, text=f"Активно: 0 / {kino_max_parallel}", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))
    active_counter.pack(side="right", padx=6)

    # ========== DownloadManager ==========
    def _pool_status_cb(m):
        try:
            msg = str(m)
        except Exception:
            msg = ""

        def _do():
            try:
                kino_status.config(text=msg[-80:], fg=ACCENT_SECOND)
            except Exception:
                pass

        try:
            root.after(0, _do)
        except Exception:
            pass

    _history_ui = {"win": None, "refresh": None}

    def on_download_history_event(event: dict):
        try:
            if not isinstance(event, dict):
                return
            if not event.get("ts"):
                event["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_download_history(event)
        except Exception:
            pass

        try:
            cb = _history_ui.get("refresh")
            if callable(cb):
                cb()
        except Exception:
            pass

    pool = DriverPool(max_drivers=kino_max_parallel, status_cb=_pool_status_cb)
    manager = DownloadManager(
        root,
        tree,
        active_counter,
        max_parallel=kino_max_parallel,
        pool=pool,
        notify_cb=push_notification,
        history_cb=on_download_history_event,
    )

    def _start_kino_item(item_id, url, out_dir, name_override=None):
        """Запустить загрузку, только если есть вход в Kino.pub."""
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return False
        try:
            manager.start_item(item_id, url, out_dir, name_override=name_override)
            return True
        except Exception:
            return False

    # --- Browser bridge (Tampermonkey -> app) ---
    def _start_browser_bridge():
        try:
            s = load_settings()
        except Exception:
            s = {}
        enabled = bool(s.get("browser_bridge_enabled", True))
        if not enabled:
            return
        token = s.get("browser_bridge_token") or ensure_bridge_token()
        try:
            port = int(s.get("browser_bridge_port") or 47832)
        except Exception:
            port = 47832

        def _enqueue_from_bridge(url: str, title: str):
            global kino_logged_in
            if not kino_logged_in:
                return False, "not_logged_in"
            try:
                item_id = add_row(title or url, status="🟡 Подготовка...")
                try:
                    if hasattr(manager, "url_by_item"):
                        manager.url_by_item[item_id] = url
                except Exception:
                    pass
                ok = _start_kino_item(item_id, url, _get_out_dir())
                try:
                    _schedule_kino_queue_save()
                except Exception:
                    pass
                return ok, "queued" if ok else "failed"
            except Exception as e:
                return False, str(e)

        class _BridgeHandler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                return

            def _send(self, code: int, payload: dict):
                try:
                    data = json.dumps(payload).encode("utf-8")
                except Exception:
                    data = b"{}"
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def do_POST(self):
                if self.path not in ("/api/kino/add", "/api/add"):
                    self._send(404, {"error": "not_found"})
                    return
                if self.headers.get("X-Bridge-Token") != token:
                    self._send(401, {"error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except Exception:
                    length = 0
                body = b""
                if length > 0:
                    try:
                        body = self.rfile.read(length)
                    except Exception:
                        body = b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    self._send(400, {"error": "bad_json"})
                    return
                url = (payload.get("url") or "").strip()
                title = (payload.get("title") or "").strip()
                if not url:
                    self._send(400, {"error": "url_required"})
                    return

                done = threading.Event()
                result = {}

                def _ui():
                    ok, msg = _enqueue_from_bridge(url, title or url)
                    result["ok"] = ok
                    result["msg"] = msg
                    done.set()

                try:
                    root.after(0, _ui)
                except Exception:
                    _ui()

                done.wait(timeout=5.0)
                if not result.get("ok"):
                    err = result.get("msg") or "failed"
                    if err == "not_logged_in":
                        self._send(409, {"error": "not_logged_in"})
                    else:
                        self._send(500, {"error": err})
                    return
                self._send(200, {"status": "ok"})

        class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

        try:
            srv = _ThreadedHTTPServer(("127.0.0.1", port), _BridgeHandler)
        except Exception as e:
            logging.error("Browser bridge failed to start: %s", e)
            return
        try:
            root._bridge_server = srv
        except Exception:
            pass
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logging.info("Browser bridge: listening on http://127.0.0.1:%s (token set)", port)

    _start_browser_bridge()

    def _update_convert_button_state_once(*_):
        try:
            sel = tree.selection()
            enabled = False
            if sel:
                iid = _top_item(sel[0])
                try:
                    s = str(tree.set(iid, "status") or "")
                except Exception:
                    s = ""
                if s.startswith("🎞") or ("Ошибка MUX" in s) or s.startswith("❌ Ошибка MUX"):
                    out_path = getattr(manager, "out_path_by_item", {}).get(iid)
                    if out_path and os.path.isdir(out_path + ".parts"):
                        enabled = True
            btn_convert.config(state="normal" if enabled else "disabled")
        except Exception:
            pass

    def _poll_convert_button_state():
        _update_convert_button_state_once()
        try:
            root.after(400, _poll_convert_button_state)
        except Exception:
            pass

    try:
        tree.bind("<<TreeviewSelect>>", _update_convert_button_state_once)
    except Exception:
        pass
    _poll_convert_button_state()

    _queue_save_job = {"id": None}

    def _kqueue_snapshot() -> list[dict]:
        items: list[dict] = []
        for item in tree.get_children():
            try:
                vals = list(tree.item(item, "values"))
                title = str(vals[1]) if len(vals) >= 2 else str(tree.set(item, "title"))
                status = str(vals[2]) if len(vals) >= 3 else str(tree.set(item, "status"))
            except Exception:
                continue
            try:
                fin = getattr(manager, "final_status", {}).get(item)
                if fin == "✅":
                    status = "✅ Готово"
                elif fin == "⛔":
                    status = "⛔ Отменено"
                elif fin == "❌" and not str(status).startswith("❌"):
                    status = "❌ Ошибка"
            except Exception:
                pass
            q = manager.url_by_item.get(item) or title
            if q:
                entry = {"q": str(q), "display": title, "status": status}
                try:
                    no = getattr(manager, "name_override_by_item", {}).get(item)
                    if no:
                        entry["name_override"] = str(no)
                except Exception:
                    pass
                try:
                    od = getattr(manager, "out_dir_by_item", {}).get(item)
                    if od:
                        entry["out_dir"] = str(od)
                except Exception:
                    pass
                try:
                    op = getattr(manager, "out_path_by_item", {}).get(item)
                    if op:
                        entry["out_path"] = str(op)
                except Exception:
                    pass
                items.append(entry)
        # ограничим размер, чтобы settings.json не разрастался бесконечно
        try:
            if len(items) > 200:
                items = items[:200]
        except Exception:
            pass
        return items

    def _save_kino_queue_now():
        try:
            s = load_settings()
            if not bool(s.get("kino_queue_persist", True)):
                s.pop("kino_queue", None)
                save_settings(s)
                return
            s["kino_queue"] = _kqueue_snapshot()
            save_settings(s)
        except Exception:
            pass

    def _schedule_kino_queue_save(delay_ms: int = 450):
        try:
            if _queue_save_job["id"] is not None:
                root.after_cancel(_queue_save_job["id"])
        except Exception:
            pass
        _queue_save_job["id"] = root.after(delay_ms, _save_kino_queue_now)

    # Сохраняем очередь при смене статуса на финальный (иначе после перезапуска
    # может восстановиться промежуточный статус и элемент запустится заново).
    try:
        _dm_set_status_orig = manager.set_status

        def _dm_set_status_persist(item_id, text):
            try:
                _dm_set_status_orig(item_id, text)
            except Exception:
                pass

            try:
                t = str(text or "")
            except Exception:
                t = ""

            if not t:
                return

            # финальные + пауза сохраняем сразу
            if t.startswith(("✅", "❌", "⛔", "🎞", "⏸")):
                try:
                    root.after(0, lambda: _schedule_kino_queue_save(0))
                except Exception:
                    pass
                return

            # прогресс/промежуточные статусы сохраняем реже, чтобы после краша/перезапуска
            # не терялся последний % и этап.
            try:
                if "%" in t or t.startswith(("🔵", "🟣", "🔀", "⏳", "🧩", "🟡")):
                    root.after(0, lambda: _schedule_kino_queue_save(800))
            except Exception:
                pass

        manager.set_status = _dm_set_status_persist
    except Exception:
        pass

    def _autostart_kino_queue_after_login():
        try:
            s = load_settings()
            if not bool(s.get("kino_queue_persist", True)):
                return
            if not bool(s.get("kino_queue_autostart_after_login", True)):
                return
        except Exception:
            return

        out_dir = _get_out_dir()
        if not out_dir:
            return

        for item in tree.get_children():
            try:
                status = str(tree.set(item, "status"))
            except Exception:
                continue
            if "Ожидает входа" not in status and not status.startswith("⏸"):
                continue
            q = manager.url_by_item.get(item) or tree.set(item, "title")
            item_out_dir = None
            try:
                op = getattr(manager, "out_path_by_item", {}).get(item)
                if op:
                    item_out_dir = os.path.dirname(os.path.normpath(str(op)))
            except Exception:
                item_out_dir = None
            try:
                if (not item_out_dir) and getattr(manager, "out_dir_by_item", {}).get(item):
                    item_out_dir = str(getattr(manager, "out_dir_by_item", {}).get(item))
            except Exception:
                pass
            if not item_out_dir:
                item_out_dir = out_dir
            try:
                _start_kino_item(item, q, item_out_dir)
            except Exception:
                pass

        try:
            _schedule_kino_queue_save()
        except Exception:
            pass
        # --- Драйвер для поиска кино (отдельный от менеджера загрузок) ---
    



    _tray = {"obj": None}
    _tray_tip_shown = {"v": False}
    _tray_events = queue.Queue()
    _tray_pump_started = {"v": False}

    def _poll_tray_events():
        try:
            while True:
                cb = _tray_events.get_nowait()
                if callable(cb):
                    try:
                        cb()
                    except Exception:
                        pass
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            if root.winfo_exists():
                root.after(120, _poll_tray_events)
        except Exception:
            pass

    def _ensure_tray_event_pump():
        if _tray_pump_started["v"]:
            return
        _tray_pump_started["v"] = True
        try:
            root.after(120, _poll_tray_events)
        except Exception:
            pass

    class _WinTray:
        def __init__(self, *, icon_path: str, on_show, on_exit, on_notifications=None, events_queue=None):
            self.icon_path = icon_path
            self.on_show = on_show
            self.on_exit = on_exit
            self.on_notifications = on_notifications
            self._events = events_queue
            self.hwnd = None
            self.hicon = None
            self._class_name = "MovieToolsTrayWindow"
            self._msg = None
            self._thread = None
            self._stop = threading.Event()
            self._ready = threading.Event()

        def create(self):
            if os.name != "nt":
                return
            if self._thread is not None:
                try:
                    if self._thread.is_alive():
                        return
                except Exception:
                    return
                self._thread = None
            self._stop.clear()
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="win_tray", daemon=True)
            self._thread.start()
            try:
                self._ready.wait(0.75)
            except Exception:
                pass

        def destroy(self):
            if os.name != "nt" or self._thread is None:
                return
            self._stop.set()
            try:
                import win32con
                import win32gui
                if self.hwnd is not None:
                    win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
            try:
                if self._thread is not None and self._thread.is_alive():
                    self._thread.join(timeout=2.0)
            except Exception:
                pass
            self._thread = None
            self.hwnd = None
            self.hicon = None
            self._ready.clear()

        def is_ready(self) -> bool:
            try:
                return bool(self._ready.is_set())
            except Exception:
                return False

        def _emit(self, cb):
            try:
                if callable(cb) and self._events is not None:
                    self._events.put_nowait(cb)
            except Exception:
                pass

        def _run(self):
            try:
                import win32api
                import win32con
                import win32gui
            except Exception:
                return

            try:
                self._msg = win32con.WM_USER + 20

                message_map = {
                    self._msg: self._on_notify,
                    win32con.WM_COMMAND: self._on_command,
                    win32con.WM_DESTROY: self._on_destroy,
                    win32con.WM_CLOSE: self._on_close,
                }

                wc = win32gui.WNDCLASS()
                wc.hInstance = win32api.GetModuleHandle(None)
                wc.lpszClassName = self._class_name
                wc.lpfnWndProc = message_map
                try:
                    win32gui.RegisterClass(wc)
                except Exception:
                    pass

                self.hwnd = win32gui.CreateWindow(
                    self._class_name,
                    self._class_name,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    wc.hInstance,
                    None,
                )

                try:
                    flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
                    self.hicon = win32gui.LoadImage(
                        0,
                        self.icon_path,
                        win32con.IMAGE_ICON,
                        0,
                        0,
                        flags,
                    )
                except Exception:
                    self.hicon = None

                if not self.hicon:
                    try:
                        self.hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
                    except Exception:
                        self.hicon = None

                try:
                    nid = (
                        self.hwnd,
                        0,
                        win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                        self._msg,
                        self.hicon,
                        "Movie Tools",
                    )
                    win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
                except Exception:
                    pass

                self._ready.set()

                while not self._stop.is_set():
                    try:
                        win32gui.PumpWaitingMessages()
                    except Exception:
                        pass
                    time.sleep(0.05)

            finally:
                try:
                    if self.hwnd is not None:
                        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
                except Exception:
                    pass
                try:
                    if self.hwnd is not None:
                        win32gui.DestroyWindow(self.hwnd)
                except Exception:
                    pass

        def _show_menu(self):
            try:
                import win32api
                import win32con
                import win32gui

                menu = win32gui.CreatePopupMenu()
                win32gui.AppendMenu(menu, win32con.MF_STRING, 1023, "Открыть")
                if self.on_notifications:
                    win32gui.AppendMenu(menu, win32con.MF_STRING, 1024, "Уведомления")
                win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, None)
                win32gui.AppendMenu(menu, win32con.MF_STRING, 1025, "Закрыть")

                x, y = win32gui.GetCursorPos()
                win32gui.SetForegroundWindow(self.hwnd)
                win32gui.TrackPopupMenu(
                    menu,
                    win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                    x,
                    y,
                    0,
                    self.hwnd,
                    None,
                )
                win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
                win32gui.DestroyMenu(menu)
            except Exception:
                pass

        def _on_command(self, hwnd, msg, wparam, lparam):
            cmd_id = int(wparam) & 0xFFFF
            if cmd_id == 1023:
                self._emit(self.on_show)
            elif cmd_id == 1024 and self.on_notifications:
                self._emit(self.on_notifications)
            elif cmd_id == 1025:
                self._emit(self.on_exit)
            return 0

        def _on_notify(self, hwnd, msg, wparam, lparam):
            try:
                import win32con
                if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                    self._emit(self.on_show)
                elif lparam in (
                    win32con.WM_RBUTTONUP,
                    win32con.WM_RBUTTONDOWN,
                    win32con.WM_RBUTTONDBLCLK,
                    win32con.WM_CONTEXTMENU,
                ):
                    # ПКМ по иконке трея -> меню (Открыть / Уведомления / Закрыть)
                    self._show_menu()
            except Exception:
                pass
            return 0

        def _on_close(self, hwnd, msg, wparam, lparam):
            try:
                import win32gui
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            return 0

        def _on_destroy(self, hwnd, msg, wparam, lparam):
            try:
                import win32gui
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
            except Exception:
                pass
            return 0

    def _tray_enabled() -> bool:
        try:
            if os.name != "nt":
                return False
            return bool(load_settings().get("minimize_to_tray", False))
        except Exception:
            return False

    def _apply_system_settings():
        enabled = _tray_enabled()
        if not enabled:
            try:
                if _tray.get("obj") is not None:
                    _tray["obj"].destroy()
            except Exception:
                pass
            _tray["obj"] = None
            try:
                if str(root.state()) == "withdrawn":
                    root.deiconify()
            except Exception:
                pass
            return

        if _tray.get("obj") is None:
            try:
                _tray["obj"] = _WinTray(
                    icon_path=get_app_icon(),
                    on_show=lambda: _show_from_tray(),
                    on_exit=lambda: on_close(force_exit=True),
                    on_notifications=lambda: show_notifications(),
                    events_queue=_tray_events,
                )
                _tray["obj"].create()
                _ensure_tray_event_pump()
            except Exception:
                _tray["obj"] = None

    def _show_from_tray():
        try:
            root.deiconify()
            try:
                root.update_idletasks()
            except Exception:
                pass
            if START_FULLSCREEN:
                try:
                    root.attributes("-fullscreen", True)
                except Exception:
                    pass
            else:
                def _max():
                    try:
                        root.state("zoomed")
                    except Exception:
                        try:
                            root.state("normal")
                        except Exception:
                            pass
                _max()
                try:
                    root.after(0, _max)
                except Exception:
                    pass
            root.lift()
            root.focus_force()
            # иногда Windows не даёт сфокусировать свернутое окно — кратко делаем topmost
            try:
                root.attributes("-topmost", True)
                root.after(80, lambda: root.attributes("-topmost", False))
            except Exception:
                pass
        except Exception:
            pass

    def _hide_to_tray() -> bool:
        if not _tray_enabled():
            return False
        try:
            _apply_system_settings()
        except Exception:
            pass
        try:
            if _tray.get("obj") is None or (hasattr(_tray["obj"], "is_ready") and (not _tray["obj"].is_ready())):
                return False
        except Exception:
            pass
        try:
            root.withdraw()
        except Exception:
            return False

        if not _tray_tip_shown["v"]:
            _tray_tip_shown["v"] = True
            try:
                push_notification(
                    "🖥️ Movie Tools",
                    "Свернуто в трей. Для выхода используйте меню значка в трее.",
                    unread=False,
                )
            except Exception:
                pass
        return True

    root._apply_system_settings = _apply_system_settings

    # Важно: НЕ прячем в трей по кнопке "Свернуть" — это должно оставаться в панели задач.
    # В трей прячем только по крестику (WM_DELETE_WINDOW -> on_close()).

    def on_close(force_exit: bool = False):
        if (not force_exit) and _tray_enabled():
            if _hide_to_tray():
                return

        logging.info("Запрошено закрытие окна, останавливаем загрузки и драйверы")

        # Останавливаем новые загрузки
        try:
            manager.stop_all(show_message=False)
        except Exception as e:
            logging.error("Ошибка при stop_all(): %s", e)

        try:
            manager.shutdown(cancel_active=True, pause_active=True, timeout=2.5)
        except Exception:
            pass

        # сохраняем очередь уже после остановки/завершения потоков
        try:
            _save_kino_queue_now()
        except Exception:
            pass

        # Пробуем закрыть драйверы пула
        try:
            if hasattr(pool, "close_all"):
                pool.close_all()
            elif hasattr(pool, "shutdown"):
                pool.shutdown()
        except Exception as e:
            logging.error("Ошибка при закрытии DriverPool: %s", e)

        # --- НОВОЕ: закрыть драйвер поиска ---
        global search_driver
        try:
            if search_driver is not None:
                search_driver.quit()
                search_driver = None

        except Exception as e:
            logging.error("Ошибка при закрытии search_driver: %s", e)
        # ------------------------------------

        # Добиваем процессы ffmpeg / Chromium (Windows)
        if os.name == "nt":
            for proc in ("ffmpeg.exe",
                         "chromium.exe",
                         "chrome.exe",
                         "undetected_chromedriver.exe",
                         "chromedriver.exe"):
                try:
                    subprocess.run(
                        ["taskkill", "/IM", proc, "/F", "/T"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except Exception as e:
                    logging.error("taskkill %s failed: %s", proc, e)

        try:
            try:
                if _tray.get("obj") is not None:
                    _tray["obj"].destroy()
            except Exception:
                pass
            _tray["obj"] = None
            root.destroy()
        except Exception:
            pass

    # Позволяем внешним модулям (например, автообновлению) закрыть приложение
    # «по-настоящему», минуя сворачивание в трей.
    try:
        root._force_exit_app = lambda: on_close(force_exit=True)
    except Exception:
        pass


    root.protocol("WM_DELETE_WINDOW", on_close)
    def show_login_required():
        """Окно в нашем стиле: нужно сначала войти в Kino.pub."""
        dlg = tk.Toplevel(root)
        dlg.title("Ошибка")
        try:
            dlg.iconbitmap(get_app_icon())
        except Exception:
            pass

        dlg.transient(root)
        dlg.grab_set()
        dlg.resizable(True, True)
        dlg.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        # центрируем
        dlg.update_idletasks()
        try:
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        scale = max(1.0, min(3.0, scale))
        w, h = int(640 * scale), int(280 * scale)
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
        w = min(w, max(420, sw - 80))
        h = min(h, max(220, sh - 120))
        x = (sw - w) // 2
        y = (sh - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        try:
            dlg.minsize(520, 240)
        except Exception:
            pass

        content = tk.Frame(dlg, bg=BG_SURFACE)
        content.pack(fill="both", expand=True, padx=24, pady=22)

        row = tk.Frame(content, bg=BG_SURFACE)
        row.pack(fill="both", expand=True)

        icon_size = int(72 * scale)
        icon = tk.Canvas(
            row,
            width=icon_size,
            height=icon_size,
            bg=BG_SURFACE,
            highlightthickness=0,
            bd=0,
        )
        try:
            pad = max(10, int(icon_size * 0.28))
            stroke = max(4, int(icon_size * 0.10))
            icon.create_oval(2, 2, icon_size - 2, icon_size - 2, fill=ERROR, outline=ERROR)
            icon.create_line(
                pad, pad, icon_size - pad, icon_size - pad,
                fill="#ffffff", width=stroke, capstyle="round",
            )
            icon.create_line(
                pad, icon_size - pad, icon_size - pad, pad,
                fill="#ffffff", width=stroke, capstyle="round",
            )
        except Exception:
            pass
        icon.pack(side="left", padx=(0, 18), pady=(4, 0))

        text_col = tk.Frame(row, bg=BG_SURFACE)
        text_col.pack(side="left", fill="both", expand=True)

        bottom = tk.Frame(dlg, bg=BG_CARD)
        bottom.pack(fill="x", side="bottom")
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x", side="top")

        tk.Label(
            text_col,
            text="Сначала выполните вход в Kino.pub",
            bg=BG_SURFACE,
            fg=TEXT,
            font=("Segoe UI Semibold", 16),
        ).pack(anchor="w", pady=(0, 6))

        msg = tk.Label(
            text_col,
            text="Нажмите кнопку «Войти в Kino.pub» в верхней панели,\n"
                 "авторизуйтесь, и после этого функция станет доступна.",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=max(360, w - 160),
        )
        msg.pack(anchor="w", fill="x")

        def _sync_wrap(_e=None):
            try:
                wrap = max(360, int(text_col.winfo_width()) - 10)
                msg.configure(wraplength=wrap)
            except Exception:
                pass

        dlg.bind("<Configure>", _sync_wrap)
        dlg.after(0, _sync_wrap)

        btn_row = tk.Frame(bottom, bg=BG_CARD)
        btn_row.pack(fill="x", padx=18, pady=14)
        btn_ok = tk.Button(btn_row, text="Понятно", command=dlg.destroy)
        style_primary(btn_ok)
        btn_ok.pack(side="right")

        dlg.bind("<Return>", lambda e: dlg.destroy())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def show_download_history():
        win = _history_ui.get("win")
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                try:
                    win.focus_force()
                except Exception:
                    pass
                try:
                    cb = _history_ui.get("refresh")
                    if callable(cb):
                        cb()
                except Exception:
                    pass
                return
        except Exception:
            pass

        win = tk.Toplevel(root)
        _history_ui["win"] = win
        try:
            win.iconbitmap(get_app_icon())
        except Exception:
            pass

        win.title("История загрузок")
        win.transient(root)
        win.resizable(True, True)
        win.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        try:
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        scale = max(1.0, min(3.0, scale))
        w, h = int(880 * scale), int(520 * scale)
        try:
            sw = int(root.winfo_screenwidth())
            sh = int(root.winfo_screenheight())
            w = min(w, max(520, sw - 120))
            h = min(h, max(360, sh - 160))
        except Exception:
            pass

        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        try:
            x = max(10, min(x, root.winfo_screenwidth() - w - 10))
            y = max(10, min(y, root.winfo_screenheight() - h - 10))
        except Exception:
            pass
        win.geometry(f"{w}x{h}+{x}+{y}")
        try:
            win.minsize(520, 360)
        except Exception:
            pass

        def _on_close():
            try:
                _history_ui["win"] = None
                _history_ui["refresh"] = None
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)

        tk.Frame(win, bg=ACCENT, height=3).pack(fill="x")
        body = tk.Frame(win, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        head = tk.Frame(body, bg=BG_SURFACE)
        head.pack(fill="x")

        tk.Label(
            head,
            text="История загрузок",
            bg=BG_SURFACE,
            fg=TEXT,
            font=("Segoe UI Semibold", 14),
        ).pack(side="left")

        counter_lbl = tk.Label(head, text="", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))
        counter_lbl.pack(side="left", padx=(10, 0))

        filter_row = tk.Frame(body, bg=BG_SURFACE)
        filter_row.pack(fill="x", pady=(10, 8))

        tk.Label(filter_row, text="Поиск:", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10)).pack(side="left")
        filter_var = tk.StringVar(value="")
        filter_entry = tk.Entry(filter_row, textvariable=filter_var, font=("Segoe UI", 10))
        try:
            style_entry(filter_entry)
        except Exception:
            pass
        filter_entry.pack(side="left", fill="x", expand=True, padx=(8, 10), ipady=3)

        btn_clear = tk.Button(filter_row, text="Очистить")
        style_secondary(btn_clear)
        btn_clear.pack(side="right")

        btn_refresh = tk.Button(filter_row, text="Обновить")
        style_secondary(btn_refresh)
        btn_refresh.pack(side="right", padx=(0, 8))

        table_wrap = tk.Frame(body, bg=BG_SURFACE)
        table_wrap.pack(fill="both", expand=True)

        xscroll = ttk.Scrollbar(table_wrap, orient="horizontal")
        xscroll.pack(side="bottom", fill="x")
        yscroll = ttk.Scrollbar(table_wrap, orient="vertical")
        yscroll.pack(side="right", fill="y")

        columns = ("ts", "result", "title", "path")
        hist_tree = ttk.Treeview(
            table_wrap,
            columns=columns,
            show="headings",
            yscrollcommand=yscroll.set,
            xscrollcommand=xscroll.set,
        )
        yscroll.config(command=hist_tree.yview)
        xscroll.config(command=hist_tree.xview)
        hist_tree.pack(side="left", fill="both", expand=True)

        hist_tree.heading("ts", text="Время", anchor="w")
        hist_tree.heading("result", text="Статус", anchor="center")
        hist_tree.heading("title", text="Название / URL", anchor="w")
        hist_tree.heading("path", text="Путь", anchor="w")

        hist_tree.column("ts", width=int(160 * scale), anchor="w", stretch=False)
        hist_tree.column("result", width=int(80 * scale), anchor="center", stretch=False)
        hist_tree.column("title", width=int(340 * scale), anchor="w", stretch=True)
        hist_tree.column("path", width=int(420 * scale), anchor="w", stretch=True)

        meta_by_iid: dict[str, dict] = {}

        def _badge(res: str) -> str:
            r = (res or "").strip().lower()
            if r in ("success", "ok", "done", "готово"):
                return "✅"
            if r in ("prepared", "ready", "pending_mux", "ready_to_mux", "готово к конвертации"):
                return "🎞"
            if r in ("paused", "paused_error", "pause", "пауза", "прервано"):
                return "⏸"
            if r in ("canceled", "cancelled", "отменено", "cancel"):
                return "⛔"
            if r in ("error", "ошибка", "fail", "failed"):
                return "❌"
            return res or ""

        def _render():
            for iid in hist_tree.get_children():
                hist_tree.delete(iid)
            meta_by_iid.clear()

            q = (filter_var.get() or "").strip().lower()
            items = get_download_history()

            shown = 0
            for ev in items:
                ts = str(ev.get("ts") or "")
                res = str(ev.get("result") or ev.get("status") or "")
                title = str(ev.get("title") or ev.get("display") or ev.get("url") or "")
                url = str(ev.get("url") or "")
                out_path = ev.get("out_path") or ""
                out_dir = ev.get("out_dir") or ""
                err = str(ev.get("error") or "")
                path = str(out_path or out_dir or "")

                hay = f"{ts} {title} {url} {path} {err}".lower()
                if q and q not in hay:
                    continue

                iid = hist_tree.insert("", "end", values=(ts, _badge(res), title, path))
                meta_by_iid[iid] = ev
                shown += 1

            counter_lbl.config(text=f"{shown} / {len(items)}")

        _history_ui["refresh"] = _render

        def _selected_event() -> dict | None:
            sel = hist_tree.selection()
            if not sel:
                return None
            return meta_by_iid.get(sel[0])

        def _open_file_or_select():
            ev = _selected_event()
            if not ev:
                return
            p = ev.get("out_path") or ev.get("out_dir")
            if not p:
                return
            open_in_explorer(str(p))

        def _open_folder_only():
            ev = _selected_event()
            if not ev:
                return
            p = ev.get("out_path") or ev.get("out_dir")
            if not p:
                return
            p = str(p)
            try:
                if os.path.isfile(p):
                    p = os.path.dirname(p) or p
            except Exception:
                pass
            open_in_explorer(p)

        def _copy_url():
            ev = _selected_event()
            if not ev:
                return
            url = str(ev.get("url") or "").strip()
            if not url:
                return
            try:
                root.clipboard_clear()
                root.clipboard_append(url)
                push_notification("История", "URL скопирован в буфер обмена", unread=False)
            except Exception:
                pass

        def _retry():
            ev = _selected_event()
            if not ev:
                return
            url = str(ev.get("url") or "").strip()
            if not url:
                return
            title = str(ev.get("title") or url)

            out_dir = _get_out_dir(create=True)
            if not out_dir:
                return

            row_id = add_row(title, status="🟡 Ожидает...")
            try:
                manager.url_by_item[row_id] = url
            except Exception:
                pass

            if kino_logged_in:
                try:
                    manager.start_item(row_id, url, out_dir)
                except Exception:
                    pass
            else:
                try:
                    tree.set(row_id, "status", "⏸ Ожидает входа")
                except Exception:
                    pass
                try:
                    show_login_required()
                except Exception:
                    pass

            try:
                reindex_rows()
            except Exception:
                pass
            try:
                _schedule_kino_queue_save()
            except Exception:
                pass
            try:
                show_screen(screens, "kino")
            except Exception:
                pass

        def _delete_record():
            ev = _selected_event()
            if not ev:
                return
            hist = get_download_history()

            def _match(h: dict) -> bool:
                keys = ("ts", "result", "title", "url", "out_path", "out_dir", "error")
                try:
                    return all((h.get(k) or "") == (ev.get(k) or "") for k in keys)
                except Exception:
                    return False

            removed = False
            for i, h in enumerate(hist):
                if _match(h):
                    del hist[i]
                    removed = True
                    break
            if removed:
                set_download_history(hist)
                _render()

        def _clear_all():
            if not messagebox.askyesno("История", "Очистить всю историю загрузок?"):
                return
            clear_download_history()
            _render()

        btn_refresh.config(command=_render)
        btn_clear.config(command=_clear_all)

        filter_var.trace_add("write", lambda *_: _render())

        hist_tree.bind("<Double-1>", lambda e: _open_file_or_select())

        menu = tk.Menu(win, tearoff=0)
        menu.add_command(label="Открыть файл / выделить", command=_open_file_or_select)
        menu.add_command(label="Открыть папку", command=_open_folder_only)
        menu.add_separator()
        menu.add_command(label="Повторить загрузку", command=_retry)
        menu.add_command(label="Копировать URL", command=_copy_url)
        menu.add_separator()
        menu.add_command(label="Удалить запись", command=_delete_record)
        menu.add_command(label="Очистить историю", command=_clear_all)

        def _on_right_click(event):
            try:
                row = hist_tree.identify_row(event.y)
                if row:
                    hist_tree.selection_set(row)
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    menu.grab_release()
                except Exception:
                    pass

        hist_tree.bind("<Button-3>", _on_right_click)

        _render()
        try:
            filter_entry.focus_set()
        except Exception:
            pass

    try:
        btn_kino_history.config(command=show_download_history)
    except Exception:
        pass

    def show_notifications():
        pop = tk.Toplevel(root)
        try:
            pop.iconbitmap(get_app_icon())
        except Exception:
            pass

        pop.title("Уведомления")
        pop.transient(root)
        pop.resizable(True, True)
        pop.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        try:
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        scale = max(1.0, min(3.0, scale))
        w, h = int(420 * scale), int(320 * scale)
        try:
            sw = int(root.winfo_screenwidth())
            sh = int(root.winfo_screenheight())
            w = min(w, max(360, sw - 80))
            h = min(h, max(260, sh - 120))
        except Exception:
            pass

        x = root.winfo_rootx() + root.winfo_width() - w - 40
        y = root.winfo_rooty() + 60
        try:
            x = max(10, min(x, root.winfo_screenwidth() - w - 10))
            y = max(10, min(y, root.winfo_screenheight() - h - 10))
        except Exception:
            pass
        pop.geometry(f"{w}x{h}+{x}+{y}")
        try:
            pop.minsize(360, 260)
        except Exception:
            pass

        tk.Frame(pop, bg=ACCENT, height=3).pack(fill="x")
        body = tk.Frame(pop, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        list_wrap = tk.Frame(body, bg=BG_SURFACE)
        list_wrap.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_wrap, bg=BG_SURFACE, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG_SURFACE)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_scrollregion(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(win_id, width=canvas.winfo_width())
            except Exception:
                pass

            # подгон wraplength под текущую ширину
            try:
                wrap = max(260, int(canvas.winfo_width()) - 40)
                for card in inner.winfo_children():
                    try:
                        for ch in card.winfo_children():
                            if isinstance(ch, tk.Label) and getattr(ch, "_notif_msg", False):
                                ch.configure(wraplength=wrap)
                    except Exception:
                        pass
            except Exception:
                pass

        inner.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_scrollregion)

        def render():
            for c in inner.winfo_children():
                try:
                    c.destroy()
                except Exception:
                    pass

            if (not NOTIFICATIONS_ENABLED) or (not notifications):
                tk.Label(
                    inner,
                    text="Нет уведомлений",
                    bg=BG_SURFACE,
                    fg=SUBTEXT,
                    font=("Segoe UI", 10),
                ).pack(pady=40)
                _sync_scrollregion()
                return

            for n in notifications[:50]:
                title = n.get("title") or ""
                msg = n.get("message") or ""
                ts = n.get("ts") or ""
                unread = bool(n.get("unread"))

                item_bg = BG_CARD
                border = ACCENT if unread else BORDER

                card = tk.Frame(inner, bg=item_bg, highlightthickness=1, highlightbackground=border)
                card.pack(fill="x", pady=6)

                head = tk.Frame(card, bg=item_bg)
                head.pack(fill="x", padx=10, pady=(8, 0))
                tk.Label(head, text=title, bg=item_bg, fg=TEXT,
                         font=("Segoe UI Semibold", 10)).pack(side="left", anchor="w")
                if ts:
                    tk.Label(head, text=ts, bg=item_bg, fg=SUBTEXT,
                             font=("Segoe UI", 8)).pack(side="right", anchor="e")

                msg_lbl = tk.Label(
                    card,
                    text=msg,
                    bg=item_bg,
                    fg=SUBTEXT,
                    font=("Segoe UI", 9),
                    wraplength=max(260, w - 80),
                    justify="left",
                )
                try:
                    msg_lbl._notif_msg = True
                except Exception:
                    pass
                msg_lbl.pack(anchor="w", padx=10, pady=(2, 8))

            _sync_scrollregion()

        render()

        # "невидимый" скролл: без полосы, но с прокруткой колёсиком
        def _on_wheel(event):
            try:
                delta = getattr(event, "delta", 0)
                if delta:
                    canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            except Exception:
                pass

        def _on_wheel_up(_e):
            try:
                canvas.yview_scroll(-1, "units")
            except Exception:
                pass

        def _on_wheel_down(_e):
            try:
                canvas.yview_scroll(1, "units")
            except Exception:
                pass

        def _focus_scroll(_e=None):
            try:
                canvas.focus_set()
            except Exception:
                pass

        canvas.bind("<Enter>", _focus_scroll)
        inner.bind("<Enter>", _focus_scroll)
        canvas.bind("<MouseWheel>", _on_wheel)
        canvas.bind("<Button-4>", _on_wheel_up)
        canvas.bind("<Button-5>", _on_wheel_down)
        inner.bind("<MouseWheel>", _on_wheel)
        inner.bind("<Button-4>", _on_wheel_up)
        inner.bind("<Button-5>", _on_wheel_down)

        btn_row = tk.Frame(body, bg=BG_SURFACE)
        btn_row.pack(fill="x", pady=(8, 0))

        def _clear():
            clear_notifications()
            render()

        def _mark_read():
            mark_all_notifications_read()
            render()

        btn_clear = tk.Button(btn_row, text="Очистить", command=_clear)
        style_secondary(btn_clear)
        btn_clear.pack(side="left")

        btn_mark = tk.Button(btn_row, text="Отметить как прочитанное", command=_mark_read)
        style_primary(btn_mark)
        btn_mark.pack(side="right")

        pop.bind("<Escape>", lambda e: pop.destroy())


    def login_to_kino():
        global kino_logged_in
        try:
            btn_kino.config(state="disabled")
        except Exception:
            pass

        try:
            kino_status.config(text="⏳ Инициализация входа.", fg=ACCENT_SECOND)
        except Exception:
            pass

        def _task(busy: BusyOverlay) -> bool:
            busy.set_message("⏳ Вход в Kino.pub…")

            def _cb(msg: str):
                msg = (msg or "")
                short = msg[-80:]
                busy.set_message(short or "⏳ Вход в Kino.pub…")
                try:
                    root.after(0, lambda: kino_status.config(text=short, fg=ACCENT_SECOND))
                except Exception:
                    pass

            return bool(real_login_to_kino(_cb))

        def _done(ok: bool, busy: BusyOverlay):
            global kino_logged_in
            global NOTIFICATIONS_ENABLED

            if ok:
                kino_logged_in = True
                NOTIFICATIONS_ENABLED = True
                try:
                    notify_count_var.set(0)
                except Exception:
                    pass

                update_sidebar_status()
                try:
                    kino_status.config(text="✅ Вход успешно выполнен", fg=ACCENT_SECOND)
                except Exception:
                    pass
                try:
                    _autostart_kino_queue_after_login()
                except Exception:
                    pass
                # Прогреваем пул браузеров заранее: меньше задержка перед стартом первых загрузок.
                try:
                    pool.warm_up_async(kino_max_parallel)
                except Exception:
                    pass
            else:
                kino_logged_in = False
                update_sidebar_status()
                try:
                    kino_status.config(text="❌ Не удалось войти", fg="red")
                except Exception:
                    pass
                messagebox.showerror("Ошибка", "❌ Не удалось войти в Kino.pub")

            try:
                btn_kino.config(state="normal")
            except Exception:
                pass

            busy.close()
            if ok:
                # Если логин шёл в браузере — возвращаем фокус на приложение
                # и переключаемся на раздел Kino.pub, чтобы было сразу понятно, что вход завершён.
                def _bring_to_front():
                    try:
                        show_screen(screens, "kino")
                        set_nav_active(nav_items, "kino")
                    except Exception:
                        pass

                    # Используем общую логику разворота (учитывает fullscreen/zoomed и tray).
                    try:
                        _show_from_tray()
                    except Exception:
                        try:
                            root.deiconify()
                        except Exception:
                            pass
                        try:
                            root.state("zoomed" if START_MAXIMIZED else "normal")
                        except Exception:
                            pass
                        try:
                            root.lift()
                            root.focus_force()
                        except Exception:
                            pass

                    # Windows иногда игнорирует focus_force(), поэтому дублируем на уровне WinAPI.
                    if os.name == "nt":
                        try:
                            import win32con
                            import win32gui

                            hwnd = int(root.winfo_id())
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                            win32gui.SetWindowPos(
                                hwnd,
                                win32con.HWND_TOPMOST,
                                0,
                                0,
                                0,
                                0,
                                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                            )
                            win32gui.SetWindowPos(
                                hwnd,
                                win32con.HWND_NOTOPMOST,
                                0,
                                0,
                                0,
                                0,
                                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                            )
                            win32gui.SetForegroundWindow(hwnd)
                        except Exception:
                            pass

                # Несколько попыток: после закрытия Chromium Windows не всегда отдаёт фокус сразу.
                for delay in (0, 220, 900):
                    try:
                        root.after(delay, _bring_to_front)
                    except Exception:
                        pass
                try:
                    _bring_to_front()
                except Exception:
                    pass

        def _err(e: Exception, _busy: BusyOverlay):
            global kino_logged_in
            kino_logged_in = False
            update_sidebar_status()
            try:
                kino_status.config(text=f"Ошибка: {e}", fg="red")
            except Exception:
                pass
            messagebox.showerror("Ошибка", f"Ошибка при авторизации: {e}")
            try:
                btn_kino.config(state="normal")
            except Exception:
                pass

        run_async(
            root,
            _task,
            title="Kino.pub",
            message="⏳ Вход в Kino.pub…",
            modal=False,
            on_done=_done,
            on_error=_err,
        )



    # после создания manager
    def _ui_set_title(item_id, text):
        def _do():
            try:
                if tree.exists(item_id):
                    tree.set(item_id, "title", text)
            except Exception:
                pass
        root.after(0, _do)

    manager.ui_set_title = _ui_set_title

    def reindex_rows():
        for i, item in enumerate(tree.get_children(), start=1):
            vals = list(tree.item(item, "values"))
            if len(vals) != 3: continue
            tree.item(item, values=(i, vals[1], vals[2]))

    def add_row(text, status="🟡 Подготовка..."):
        idx = len(tree.get_children()) + 1
        return tree.insert("", "end", values=(idx, text, status))

    def _restore_kino_queue_on_startup():
        try:
            s = load_settings()
            if not bool(s.get("kino_queue_persist", True)):
                return
            raw = s.get("kino_queue") or []
        except Exception:
            return

        if not isinstance(raw, list) or not raw:
            return

        for entry in raw:
            try:
                if isinstance(entry, str):
                    q = entry
                    display = entry
                    status = ""
                elif isinstance(entry, dict):
                    q = entry.get("q") or entry.get("url") or entry.get("query") or entry.get("text")
                    display = entry.get("display") or entry.get("title") or q
                    status = entry.get("status") or ""
                else:
                    continue

                q = (q or "").strip()
                display = (display or q or "").strip()
                if not q or not display:
                    continue

                status = (status or "").strip()
                if not status:
                    status = "🟡 Ожидает..."

                # если не залогинены — ставим "паузу" для всего НЕ финального
                if not kino_logged_in:
                    if not status.startswith(("✅", "❌", "⛔", "🎞")) and not status.startswith("⏸"):
                        status = "⏸ " + status
                row_id = add_row(display, status=status)
                try:
                    manager.url_by_item[row_id] = q
                except Exception:
                    pass
                try:
                    no = None
                    if isinstance(entry, dict):
                        no = entry.get("name_override") or entry.get("display_name_override")
                    if no:
                        if hasattr(manager, "name_override_by_item"):
                            manager.name_override_by_item[row_id] = str(no)
                except Exception:
                    pass
                try:
                    op = None
                    if isinstance(entry, dict):
                        op = entry.get("out_path") or entry.get("out") or entry.get("file")
                    if op:
                        manager.out_path_by_item[row_id] = os.path.normpath(str(op))
                except Exception:
                    pass
                try:
                    od = None
                    if isinstance(entry, dict):
                        od = entry.get("out_dir") or entry.get("dir")
                    if (not od) and isinstance(entry, dict):
                        op2 = entry.get("out_path") or entry.get("out") or entry.get("file")
                        if op2:
                            od = os.path.dirname(os.path.normpath(str(op2)))
                    if od and hasattr(manager, "out_dir_by_item"):
                        manager.out_dir_by_item[row_id] = os.path.normpath(str(od))
                except Exception:
                    pass
            except Exception:
                pass

        try:
            reindex_rows()
        except Exception:
            pass

        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    _restore_kino_queue_on_startup()

    def import_list():
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
        if not path: return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                q = line.strip()
                if not q: continue
                add_row(q, status="🟡 Подготовка...")
        reindex_rows()
        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    
    def start_kino_download():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return

        q = kino_input.get().strip()
        if not q:
            messagebox.showerror("Ошибка", "Введите запрос или URL карточки.")
            return

        item_id = add_row(q, status="🟡 Подготовка...")
        kino_input.delete(0, "end")
        out_dir = _get_out_dir()
        _start_kino_item(item_id, q, out_dir)
        try:
            _schedule_kino_queue_save()
        except Exception:
            pass

    def _center_toplevel(win: tk.Toplevel, *, w: int = 620, h: int = 260):
        try:
            win.update_idletasks()
        except Exception:
            pass
        try:
            root.update_idletasks()
        except Exception:
            pass

        try:
            rw = int(root.winfo_width())
            rh = int(root.winfo_height())
            rx = int(root.winfo_rootx())
            ry = int(root.winfo_rooty())
        except Exception:
            rw = rh = 0
            rx = ry = 0

        try:
            sw = int(win.winfo_screenwidth())
            sh = int(win.winfo_screenheight())
        except Exception:
            sw = sh = 0

        if rw >= 240 and rh >= 240:
            x = rx + (rw - w) // 2
            y = ry + (rh - h) // 2
        else:
            x = (sw - w) // 2
            y = (sh - h) // 2

        try:
            x = max(10, min(int(x), sw - w - 10))
            y = max(10, min(int(y), sh - h - 10))
        except Exception:
            pass
        try:
            win.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
        except Exception:
            pass

    def _ask_series_seasons(series_title: str, seasons_map: dict) -> list[int] | None:
        seasons = sorted(int(x) for x in (seasons_map or {}).keys())
        if not seasons:
            return None

        if len(seasons) == 1:
            try:
                total_eps = len(seasons_map.get(seasons[0]) or [])
            except Exception:
                total_eps = 0
            if messagebox.askyesno(
                "Скачать сериал",
                f"Сериал: {series_title}\n"
                f"Сезон: {seasons[0]}\n"
                f"Эпизодов: {total_eps}\n\n"
                "Добавить в очередь?",
            ):
                return seasons
            return None

        try:
            from tkinter import ttk
        except Exception:
            ttk = None

        res: dict = {"seasons": None}
        win = tk.Toplevel(root)
        win.title("Скачать сериал")
        try:
            win.iconbitmap(get_app_icon())
        except Exception:
            pass
        win.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
        win.resizable(False, False)
        tk.Frame(win, bg=ACCENT, height=3).pack(fill="x", side="top")

        body = tk.Frame(win, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(
            body,
            text=f"📺 {series_title}",
            bg=BG_SURFACE,
            fg=ACCENT,
            font=("Segoe UI Semibold", 14),
            anchor="w",
        ).pack(anchor="w")

        info_lines = []
        try:
            for s_num in seasons:
                cnt = len(seasons_map.get(s_num) or [])
                info_lines.append(f"Сезон {s_num}: {cnt} эп.")
        except Exception:
            pass

        tk.Label(
            body,
            text="\n".join(info_lines),
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
            justify="left",
            anchor="w",
        ).pack(anchor="w", pady=(4, 10))

        mode = tk.StringVar(value="all")
        season_var = tk.StringVar(value=str(seasons[0]))

        rb_all = tk.Radiobutton(
            body,
            text=f"Скачать все сезоны ({len(seasons)})",
            variable=mode,
            value="all",
            bg=BG_SURFACE,
            fg=TEXT,
            selectcolor=BG_CARD,
            activebackground=BG_SURFACE,
            activeforeground=TEXT,
        )
        rb_all.pack(anchor="w", pady=(2, 2))

        row = tk.Frame(body, bg=BG_SURFACE)
        row.pack(fill="x", pady=(2, 2))
        rb_one = tk.Radiobutton(
            row,
            text="Скачать только сезон:",
            variable=mode,
            value="one",
            bg=BG_SURFACE,
            fg=TEXT,
            selectcolor=BG_CARD,
            activebackground=BG_SURFACE,
            activeforeground=TEXT,
        )
        rb_one.pack(side="left")

        if ttk:
            cb = ttk.Combobox(row, values=[str(x) for x in seasons], textvariable=season_var, width=6, state="readonly")
            cb.pack(side="left", padx=(8, 0))
            try:
                cb.bind("<<ComboboxSelected>>", lambda e: mode.set("one"))
                cb.bind("<Button-1>", lambda e: mode.set("one"))
            except Exception:
                pass
        else:
            ent = tk.Entry(row, textvariable=season_var, width=6)
            style_entry(ent)
            ent.pack(side="left", padx=(8, 0))
            try:
                ent.bind("<FocusIn>", lambda e: mode.set("one"))
                ent.bind("<Button-1>", lambda e: mode.set("one"))
            except Exception:
                pass

        btns = tk.Frame(body, bg=BG_SURFACE)
        btns.pack(fill="x", pady=(14, 0))

        def _ok():
            try:
                if mode.get() == "all":
                    res["seasons"] = seasons
                else:
                    res["seasons"] = [int(season_var.get())]
            except Exception:
                res["seasons"] = None
            try:
                win.destroy()
            except Exception:
                pass

        def _cancel():
            res["seasons"] = None
            try:
                win.destroy()
            except Exception:
                pass

        btn_ok = tk.Button(btns, text="Добавить в очередь", command=_ok)
        style_secondary(btn_ok)
        btn_ok.pack(side="right")

        btn_cancel = tk.Button(btns, text="Отмена", command=_cancel)
        style_secondary(btn_cancel)
        btn_cancel.pack(side="right", padx=(0, 8))

        win.protocol("WM_DELETE_WINDOW", _cancel)
        _center_toplevel(win, w=640, h=300)
        try:
            win.transient(root)
            win.grab_set()
            win.lift()
            win.attributes("-topmost", True)
            win.after(200, lambda: win.attributes("-topmost", False))
        except Exception:
            pass

        try:
            win.wait_window()
        except Exception:
            pass
        return res.get("seasons")

    def start_kino_series_download():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return

        series_url = kino_input.get().strip()
        if not series_url:
            messagebox.showerror("Ошибка", "Вставьте ссылку на сериал.")
            return
        if not series_url.startswith("http"):
            messagebox.showerror("Ошибка", "Для «Скачать сериал» нужна ссылка (URL), а не текстовый запрос.")
            return

        out_dir = _get_out_dir()
        if not out_dir:
            return

        def _task(busy):
            from kino_pub_downloader import parse_series_episodes

            drv = get_search_driver()

            def _st(m: str):
                try:
                    busy.set_message(str(m))
                except Exception:
                    pass

            _st("⏳ Анализ сериала…")
            data = parse_series_episodes(series_url, driver=drv, status_cb=_st)
            return data

        def _done(data, busy):
            try:
                busy.close()
            except Exception:
                pass

            try:
                title = str((data or {}).get("title") or "").strip()
            except Exception:
                title = ""
            seasons_map = (data or {}).get("seasons") if isinstance(data, dict) else None
            if not title or not isinstance(seasons_map, dict) or not seasons_map:
                messagebox.showerror("Сериал", "Не удалось разобрать сезоны/эпизоды по ссылке.")
                return

            chosen = _ask_series_seasons(title, seasons_map)
            if not chosen:
                return

            # собираем список эпизодов
            series_root = os.path.join(out_dir, title)
            try:
                os.makedirs(series_root, exist_ok=True)
            except Exception:
                series_root = out_dir

            to_add: list[tuple[str, str, str]] = []
            seen_urls: set[str] = set()
            seen_titles: set[str] = set()
            for iid in tree.get_children():
                try:
                    u = manager.url_by_item.get(iid)
                    if u:
                        seen_urls.add(str(u))
                except Exception:
                    pass
                try:
                    t0 = str(tree.set(iid, "title") or "").strip()
                    if t0:
                        seen_titles.add(t0)
                except Exception:
                    pass

            for s_num in chosen:
                season_dir = os.path.join(series_root, f"Season {int(s_num)}")
                try:
                    os.makedirs(season_dir, exist_ok=True)
                except Exception:
                    season_dir = series_root

                eps = seasons_map.get(int(s_num)) or []
                for e in eps:
                    try:
                        ep = int((e or {}).get("episode") or 0)
                    except Exception:
                        ep = 0
                    try:
                        url = str((e or {}).get("url") or "").strip()
                    except Exception:
                        url = ""
                    if not url or ep <= 0:
                        continue
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    display = f"{title} S{int(s_num):02d}E{int(ep):02d}"
                    if display in seen_titles:
                        continue
                    seen_titles.add(display)
                    to_add.append((display, url, season_dir))

            if not to_add:
                messagebox.showinfo("Сериал", "Все серии уже в очереди (или не удалось найти ссылки эпизодов).")
                return

            # добавляем в очередь и запускаем
            for display, url, item_out_dir in to_add:
                item_id = add_row(display, status="🟡 Подготовка...")
                try:
                    _start_kino_item(item_id, url, item_out_dir, name_override=display)
                except Exception:
                    try:
                        manager.url_by_item[item_id] = url
                    except Exception:
                        pass
            try:
                _schedule_kino_queue_save()
            except Exception:
                pass

            try:
                kino_input.delete(0, "end")
            except Exception:
                pass

            try:
                messagebox.showinfo("Сериал", f"Добавлено в очередь: {len(to_add)}")
            except Exception:
                pass

        def _err(e, busy):
            try:
                busy.close()
            except Exception:
                pass
            messagebox.showerror("Сериал", str(e))

        run_async(
            root,
            _task,
            title="Kino.pub",
            message="⏳ Анализ сериала…",
            modal=False,
            on_done=_done,
            on_error=_err,
        )

    def on_kino_input_click(event):
        if not kino_logged_in:
            show_login_required()
            return "break"  # не даём поставить курсор

    kino_input.bind("<Button-1>", on_kino_input_click)

    def run_queue():
        out_dir = _get_out_dir()
        manager.start_all(out_dir)

    def stop_queue():
        manager.stop_all()
    def remove_selected():
        cancel_selected()
    if SHOW_QUEUE_CONTROLS:
        btn_import.config(command=import_list)
        btn_delete.config(command=remove_selected)
        btn_run.config(command=run_queue)
        btn_stop.config(command=stop_queue)

    
    btn_download.config(command=start_kino_download)
    btn_download_series.config(command=start_kino_series_download)

        # ========== Экран поиска Kino.pub (kino_search) ==========
    from tkinter import ttk  # на всякий случай, если выше не импортнулся

    search_top = tk.Frame(kino_search, bg=BG_SURFACE,
                          highlightbackground=BORDER, highlightthickness=1)
    search_top.pack(side="top", fill="x")

    tk.Label(
        search_top,
        text="Kino.pub Search",
        bg=BG_SURFACE,
        fg=ACCENT_SECOND,
        font=("Segoe UI Semibold", 16),
    ).pack(side="left", padx=12, pady=10)
    btn_back_dl = tk.Button(search_top, text="Загрузчик")
    style_secondary(btn_back_dl)
    btn_back_dl.config(command=lambda: show_screen(screens, "kino"))
    btn_back_dl.pack(side="left", padx=6)

    # Карточка поиска
    card_search = tk.Frame(
        kino_search,
        bg=BG_SURFACE,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    card_search.pack(fill="both", expand=True, padx=18, pady=(14, 18))
    tk.Frame(card_search, bg=ACCENT, height=3).pack(fill="x", side="top")

    top_s = tk.Frame(card_search, bg=BG_SURFACE)
    top_s.pack(fill="x", pady=(18, 6))
    tk.Label(
        top_s,
        text="🔎 Поиск фильмов на Kino.pub",
        bg=BG_SURFACE,
        fg=ACCENT,
        font=("Segoe UI Semibold", 18),
    ).pack(pady=(0, 4))
    tk.Label(
        top_s,
        text="Новинки, поиск по названию или списком.\n"
             "Выбранные результаты можно добавить в очередь загрузки.",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 10),
        wraplength=620,
        justify="center",
    ).pack()

            # --- Поиск по одному названию ---
    search_pane = tk.PanedWindow(
        card_search,
        orient="horizontal",
        bg=BG_SURFACE,
        bd=0,
        relief="flat",
        sashwidth=10,
        sashrelief="flat",
    )
    search_pane.pack(fill="both", expand=True, padx=18, pady=(10, 14))

    search_left = tk.Frame(search_pane, bg=BG_SURFACE)
    search_right = tk.Frame(search_pane, bg=BG_SURFACE)
    search_pane.add(search_left, minsize=340, stretch="never")
    search_pane.add(search_right, minsize=420, stretch="always")

    one_frame = tk.Frame(search_left, bg=BG_SURFACE)
    one_frame.pack(fill="x", padx=40, pady=(12, 4))

    tk.Label(
        one_frame,
        text="Название фильма:",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 10),
    ).pack(anchor="w")

    one_row = tk.Frame(one_frame, bg=BG_SURFACE)
    one_row.pack(fill="x", pady=(4, 0))

    search_entry = tk.Entry(
        one_row,
        bg=FIELD_BG,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        font=("Segoe UI", 11),
    )
    # В светлой теме без рамки поле "теряется" на белом фоне — используем общий стиль (с границей).
    try:
        style_entry(search_entry)
    except Exception:
        pass
    search_entry.pack(side="left", fill="x", expand=True, ipady=4)

    

    # Enter в этом поле запускает поиск
    search_entry.bind("<Return>", lambda e: (search_one_title() or "break"))

    btn_search_one = tk.Button(one_row, text="Искать")
    style_secondary(btn_search_one)
    btn_search_one.pack(side="left", padx=(8, 0), ipady=2)

        # --- Поиск по списку ---
    list_frame = tk.Frame(search_left, bg=BG_SURFACE)
    list_frame.pack(fill="x", padx=40, pady=(10, 4))
    tk.Label(
        list_frame,
        text="Список названий (по одному в строке):",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 10),
    ).pack(anchor="w")
    list_text = tk.Text(
        list_frame,
        height=4,
        bg=FIELD_BG,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        font=("Segoe UI", 10),
        wrap="none",
    )
    try:
        style_text(list_text)
    except Exception:
        pass
    list_text.pack(fill="x", pady=(4, 0))

    # ряд кнопок: [Искать по списку] [TXT]
    list_buttons_row = tk.Frame(list_frame, bg=BG_SURFACE)
    list_buttons_row.pack(fill="x", pady=(4, 0))

    # TXT будет правее
    btn_search_txt = tk.Button(list_buttons_row, text="TXT")
    style_secondary(btn_search_txt)
    btn_search_txt.pack(side="right")

    btn_search_list = tk.Button(list_buttons_row, text="Искать по списку")
    style_secondary(btn_search_list)
    btn_search_list.pack(side="right", padx=(8, 0))


    # (опции перекодирования убраны отсюда — оставлены только в настройках)

    # --- Новинки ---
    news_frame = tk.Frame(search_left, bg=BG_SURFACE)
    news_frame.pack(fill="x", padx=40, pady=(6, 0))
    btn_news = tk.Button(news_frame, text="📅 Выгрузить новинки")
    style_secondary(btn_news)
    btn_news.pack(anchor="w")

    # --- Таблица результатов поиска ---
    results_container = tk.Frame(search_right, bg=BG_SURFACE)
    results_container.pack(fill="both", expand=True, padx=32, pady=(10, 6))
    res_scroll = ttk.Scrollbar(results_container)
    res_scroll.pack(side="right", fill="y")



    # БЫЛО: res_columns = ("query", "title", "year", "url")
    # СТАЛО: первая колонка — чекбокс
    res_columns = ("chk", "query", "title", "year", "url")
    tree_search = ttk.Treeview(
        results_container,
        columns=res_columns,
        show="headings",
        height=7,
        yscrollcommand=res_scroll.set,
    )
    res_scroll.config(command=tree_search.yview)

    tree_search.heading("chk",   text="☐",       anchor="center")
    tree_search.heading("query", text="Запрос",  anchor="w")
    tree_search.heading("title", text="Название", anchor="w")
    tree_search.heading("year",  text="Год",     anchor="center")
    tree_search.heading("url",   text="URL",     anchor="w")

    tree_search.column("chk",   width=30,  anchor="center")
    tree_search.column("query", width=150, anchor="w")
    tree_search.column("title", width=260, anchor="w")
    tree_search.column("year",  width=60,  anchor="center")
    tree_search.column("url",   width=260, anchor="w")

    tree_search.pack(fill="both", expand=True)

    # --- состояние чекбоксов ---
    checked_items: set[str] = set()
    header_checked = False  # состояние "выбраны все" для заголовка

    def set_all(checked: bool):
        """Отметить или снять все галочки в списке."""
        nonlocal header_checked
        header_checked = checked

        # обновляем иконку в заголовке
        tree_search.heading("chk", text="☑" if checked else "☐")

        for item_id in tree_search.get_children():
            vals = list(tree_search.item(item_id, "values"))
            if not vals:
                continue

            if checked:
                checked_items.add(item_id)
                vals[0] = "☑"
            else:
                checked_items.discard(item_id)
                vals[0] = "☐"

            tree_search.item(item_id, values=vals)


    def toggle_check(item_id: str):
        if not item_id:
            return
        vals = list(tree_search.item(item_id, "values"))
        if not vals:
            return

        if item_id in checked_items:
            checked_items.remove(item_id)
            vals[0] = "☐"
        else:
            checked_items.add(item_id)
            vals[0] = "☑"

        tree_search.item(item_id, values=vals)

    def on_tree_click(event):
        """
        Клик по первой колонке:
        - по заголовку — отметить/снять все;
        - по ячейке — переключить галочку у строки.
        """
        region = tree_search.identify("region", event.x, event.y)
        col = tree_search.identify_column(event.x)  # "#1", "#2", ...

        # Клик по заголовку первой колонки — "выделить всё"
        if region == "heading" and col == "#1":
            set_all(not header_checked)
            return "break"

        if region != "cell":
            return

        row = tree_search.identify_row(event.y)
        if not row:
            return

        if col == "#1":  # колонка chk
            toggle_check(row)
            return "break"  # не трогаем стандартный selection


    tree_search.bind("<Button-1>", on_tree_click)

    CARD_SELECTORS = [
        ".item .item-title a[href*='/item/']",
        "div.item-title a[href*='/item/']",
        "a[href*='/item/view/']",
    ]
    def parse_kino_cards_from_soup(soup, max_results: int = 50):
        """
        Разбор HTML-страницы Kino.pub:
        возвращает список (display_title, url, base_title, year).
        Используется и для поиска, и для новинок.
        """
        results: list[tuple[str, str, str, str | None]] = []
        seen_urls: set[str] = set()

        # каждая карточка фильма/сериала
        for card in soup.select("div.item-list div.item"):
            # ссылка с названием
            link = card.select_one("div.item-title a[href*='/item/']")
            if not link:
                continue

            href = (link.get("href") or "").strip()
            if not href:
                continue
            href = urljoin(KINOPUB_BASE, href)

            if href in seen_urls:
                continue
            seen_urls.add(href)

            # текст названия
            text = (link.get("title") or link.get_text(" ", strip=True) or "").strip()
            if not text:
                continue

            # --- ищем год: перебираем ВСЕ meta-блоки ---
            year = None
            for meta_div in card.select("div.item-author.text-ellipsis.text-muted"):
                meta_text = meta_div.get_text(" ", strip=True)
                m = re.search(r"\b(19|20)\d{2}\b", meta_text)
                if m:
                    year = m.group(0)
                    break

            # чистое название без (год) на всякий
            base_title = re.sub(r"\s*\(\d{4}\)\s*", "", text).strip()
            display_title = f"{base_title} ({year})" if year else base_title

            results.append((display_title, href, base_title, year))

            if len(results) >= max_results:
                break

        return results

    def menu_open_in_browser():
        sel = tree_search.selection()
        if not sel:
            return
        for item in sel:
            vals = tree_search.item(item, "values")
            if len(vals) >= 5:
                url = vals[4]
                if url:
                    try:
                        webbrowser.open(url)
                    except Exception as e:
                        logging.error("Не удалось открыть URL %s: %s", url, e)


    def on_search_row_double_click(event):
        """
        Двойной клик по строке:
        - если по первой колонке (чекбокс) — переключаем галочку
        - иначе открываем карточку в браузере
        """
        row = tree_search.identify_row(event.y)
        if not row:
            return

        col = tree_search.identify_column(event.x)  # "#1", "#2", ...
        if col == "#1":  # клик по чекбоксу
            toggle_check(row)
            return

        vals = tree_search.item(row, "values")
        # columns: (chk, query, title, year, url)
        if len(vals) >= 5:
            url = vals[4]
            if url:
                try:
                    webbrowser.open(url)
                except Exception as e:
                    logging.error("Не удалось открыть URL %s: %s", url, e)

    # --- ПКМ по результатам поиска ---
    search_menu = tk.Menu(tree_search, tearoff=0)
    register_menu(search_menu)
    def menu_add_to_queue():
        add_selected_from_search()

    search_menu.add_command(label="Открыть карточку в браузере",
                            command=menu_open_in_browser)
    search_menu.add_command(label="Скачать (добавить в очередь)",
                            command=menu_add_to_queue)

    def on_search_right_click(event):
        item = tree_search.identify_row(event.y)
        if not item:
            return
        # выделяем строку под курсором
        if item not in tree_search.selection():
            tree_search.selection_set(item)
        search_menu.tk_popup(event.x_root, event.y_root)

    tree_search.bind("<Button-3>", on_search_right_click)
    tree_search.bind("<Double-1>", on_search_row_double_click)


    def kino_search_real(title: str, max_results: int = 50):
        """
        Реальный поиск на Kino.pub через /item/search?query=...
        Возвращает список кортежей:
            (display_title, url, base_title_ru, year, eng_title)

        Результаты дополнительно сортируются так, чтобы
        лучше всего совпадающие с запросом (по рус/анг названию)
        были сверху.
        """
        drv = get_search_driver()

        q = quote_plus(title)
        search_url = f"{KINOPUB_BASE}/item/search?query={q}"
        logging.info(f"[SEARCH] GET {search_url}")

        # Ловим таймауты/глюки рендерера, чтобы не падало всё приложение
        try:
            drv.get(search_url)
        except Exception as e:
            logging.warning("SEARCH drv.get timeout/error for %r: %s", search_url, e)
            return []  # ничего не нашли, но GUI жив

        try:
            WebDriverWait(drv, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            logging.warning("Страница поиска долго не загружается для запроса %s", title)

        html = drv.page_source
        soup = BeautifulSoup(html, "html.parser")

        results = _parse_items_from_soup(soup, max_results=max_results)

        # --- добиваемся адекватного порядка: сравниваем с русским и англ. названием ---
        def _norm(s: str | None) -> str:
            # используем твою normalize_name — она уже умеет рубить спецсимволы и регистр
            return normalize_name((s or "").strip())

        q_norm = _norm(title)

        def _score(rec):
            display_title, url, base_ru, year, eng_title = rec
            ru = _norm(base_ru)
            en = _norm(eng_title)

            score = 0
            if q_norm and (q_norm == ru or q_norm == en):
                score += 100
            if q_norm and (q_norm in ru or q_norm in en):
                score += 50
            # лёгкий бонус, если первые слова совпадают
            q_first = q_norm.split()[0] if q_norm else ""
            if q_first and (ru.startswith(q_first) or en.startswith(q_first)):
                score += 10

            # сортируем по убыванию score
            return -score

        results.sort(key=_score)
        logging.info("[SEARCH] '%s' -> %d результатов (после сортировки)", title, len(results))
        return results

    def kino_fetch_news_page(page: int, max_results: int | None = None):
        """
        Вытаскивает список новинок с /new или /new?page=N.
        """
        drv = get_search_driver()

        if page <= 1:
            url = f"{KINOPUB_BASE}/new"
        else:
            url = f"{KINOPUB_BASE}/new?page={page}"

        logging.info(f"[NEWS] GET {url}")
        drv.get(url)

        try:
            WebDriverWait(drv, 10).until(
                lambda d: (
                    d.find_elements(By.CSS_SELECTOR, "div.item-title a[href*='/item/']")
                    or d.find_elements(By.CSS_SELECTOR, "div#items div.item-info")
                )
            )
        except Exception:
            logging.warning("Страница новинок долго не загружается для страницы %s", page)

        html = drv.page_source
        soup = BeautifulSoup(html, "html.parser")

        return _parse_items_from_soup(soup, max_results=max_results)


    def _parse_items_from_soup(soup, max_results: int | None = None):
        """
        Общий парсер списка карточек Kino.pub.
        Работает и для страницы поиска (/item/search),
        и для новинок (/new).

        Возвращает список кортежей:
            (display_title, url, base_title_ru, year, eng_title)
        """
        results: list[tuple[str, str, str, str | None, str | None]] = []
        seen_urls: set[str] = set()

        # Старый layout (поиск): div.item-list > div.item
        cards = soup.select("div.item-list div.item")

        # Новый layout (новинки): <div id="items"> ... <div class="item-info"> ... </div>
        if not cards:
            cards = list(soup.select("div#items div.item-info"))

        for card in cards:
            # ссылка с РУ названием
            link = card.select_one("div.item-title a[href*='/item/']")
            if not link:
                continue

            href = (link.get("href") or "").strip()
            if not href:
                continue
            href = urljoin(KINOPUB_BASE, href)

            if href in seen_urls:
                continue
            seen_urls.add(href)

            # русский текст названия
            text_ru = (link.get("title") or link.get_text(" ", strip=True) or "").strip()
            if not text_ru:
                continue

            # --- английское название (из блока item-author) ---
            eng_title: str | None = None
            for a in card.select("div.item-author a"):
                t = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
                if not t:
                    continue
                # простая эвристика: если есть латинские буквы — считаем, что это англ. название
                if re.search(r"[A-Za-z]", t):
                    eng_title = t
                    break

            # --- год (как и было) ---
            year: str | None = None
            for meta_div in card.select("div.item-author"):
                meta_text = meta_div.get_text(" ", strip=True)
                m = re.search(r"\b(19|20)\d{2}\b", meta_text)
                if m:
                    year = m.group(0)
                    break

            # Новый layout иногда не кладёт год в item-author — вытаскиваем из общего текста карточки.
            if not year:
                try:
                    cand = ""
                    a_year = card.select_one("a.text-success[href*='years='], a[href*='years=']")
                    if a_year is not None:
                        cand = (a_year.get_text(" ", strip=True) or "") + " " + (a_year.get("href") or "")

                    if not cand:
                        cand = card.get_text(" ", strip=True) or ""

                    if not cand:
                        cand = str(card)

                    m = re.search(r"\b(19|20)\d{2}\b", cand)
                    if m:
                        year = m.group(0)
                except Exception:
                    pass

            base_title_ru = re.sub(r"\s*\(\d{4}\)\s*", "", text_ru).strip()
            display_title = f"{base_title_ru} ({year})" if year else base_title_ru

            results.append((display_title, href, base_title_ru, year, eng_title))

            if max_results is not None and len(results) >= max_results:
                break

        logging.info("[PARSE] найдено %d карточек", len(results))
        return results


    def search_one_title():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return

        raw = search_entry.get().strip()

        # отбрасываем "(1997)" – ищем только по названию
        title, _ = split_title_year(raw)
        if not title:
            messagebox.showinfo("Поиск", "Введите название фильма.")
            return

        # очищаем старые результаты
        for item in tree_search.get_children():
            tree_search.delete(item)
        search_meta.clear()

        try:
            btn_search_one.config(state="disabled")
            search_entry.config(state="disabled")
        except Exception:
            pass
        try:
            btn_search_list.config(state="disabled")
            btn_search_txt.config(state="disabled")
        except Exception:
            pass

        def _task(busy: BusyOverlay):
            busy.set_message(f"⏳ Поиск: {title}")
            return kino_search_real(title, max_results=50)

        def _done(results, busy: BusyOverlay):
            try:
                btn_search_one.config(state="normal")
                search_entry.config(state="normal")
            except Exception:
                pass
            try:
                btn_search_list.config(state="normal")
                btn_search_txt.config(state="normal")
            except Exception:
                pass

            if not results:
                messagebox.showinfo("Поиск", f"По запросу '{raw}' ничего не найдено.")
                busy.close()
                return

            for display_title, url, base_title, y, eng_title in results:
                shown_title = display_title
                if eng_title:
                    shown_title = f"{display_title} / {eng_title}"

                item_id = tree_search.insert(
                    "",
                    "end",
                    values=("☐", raw, shown_title, y or "", url),
                )
                search_meta[item_id] = {
                    "query": raw,
                    "title": base_title,
                    "year":  y,
                    "url":   url,
                    "eng_title": eng_title,
                }

            busy.close()

        def _err(e: Exception, _busy: BusyOverlay):
            try:
                btn_search_one.config(state="normal")
                search_entry.config(state="normal")
            except Exception:
                pass
            try:
                btn_search_list.config(state="normal")
                btn_search_txt.config(state="normal")
            except Exception:
                pass
            messagebox.showerror("Ошибка", f"Ошибка поиска:\n{e}")

        run_async(
            root,
            _task,
            title="Поиск Kino.pub",
            message="⏳ Поиск на Kino.pub…",
            modal=False,
            on_done=_done,
            on_error=_err,
        )

    def search_by_list():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return

        raw_lines = list_text.get("1.0", "end").splitlines()

        # очищаем старые результаты
        for item in tree_search.get_children():
            tree_search.delete(item)
        search_meta.clear()
        checked_items.clear()

        try:
            btn_search_one.config(state="disabled")
            search_entry.config(state="disabled")
        except Exception:
            pass
        try:
            btn_search_list.config(state="disabled")
            btn_search_txt.config(state="disabled")
        except Exception:
            pass

        def _task(busy: BusyOverlay):
            rows = []
            lines = [ln.strip() for ln in raw_lines if ln.strip()]
            total = max(1, len(lines))

            for i, original in enumerate(lines, start=1):
                title, _ = split_title_year(original)
                if not title:
                    continue

                busy.set_message(f"⏳ Список: {i}/{total}: {title}")

                results = kino_search_real(title, max_results=50)
                if not results:
                    logging.info("Список: для '%s' ничего не найдено", original)
                    continue

                for display_title, url, base_title, y, eng_title in results:
                    rows.append((original, display_title, url, base_title, y, eng_title))

            return rows

        def _done(rows, busy: BusyOverlay):
            try:
                btn_search_one.config(state="normal")
                search_entry.config(state="normal")
            except Exception:
                pass
            try:
                btn_search_list.config(state="normal")
                btn_search_txt.config(state="normal")
            except Exception:
                pass

            if not rows:
                messagebox.showinfo("Поиск", "Список пустой или по нему ничего не найдено.")
                busy.close()
                return

            chunk = 80
            total = len(rows)

            def _insert(i=0):
                end = min(total, i + chunk)
                busy.set_message(f"⏳ Добавляю в таблицу: {end}/{total}")

                for original, display_title, url, base_title, y, eng_title in rows[i:end]:
                    shown_title = display_title
                    if eng_title:
                        shown_title = f"{display_title} / {eng_title}"

                    item_id = tree_search.insert(
                        "",
                        "end",
                        values=("☐", original, shown_title, y or "", url),
                    )
                    search_meta[item_id] = {
                        "query": original,
                        "title": base_title,
                        "year":  y,
                        "url":   url,
                        "eng_title": eng_title,
                    }

                if end < total:
                    root.after(0, lambda: _insert(end))
                    return

                busy.close()

            _insert(0)

        def _err(e: Exception, _busy: BusyOverlay):
            try:
                btn_search_one.config(state="normal")
                search_entry.config(state="normal")
            except Exception:
                pass
            try:
                btn_search_list.config(state="normal")
                btn_search_txt.config(state="normal")
            except Exception:
                pass
            messagebox.showerror("Ошибка", f"Ошибка поиска по списку:\n{e}")

        run_async(
            root,
            _task,
            title="Поиск по списку",
            message="⏳ Поиск по списку…",
            modal=False,
            on_done=_done,
            on_error=_err,
        )



    def search_from_txt():
        path = filedialog.askopenfilename(
            title="Выберите TXT со списком фильмов",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return

        # заливаем содержимое в текстовое поле
        list_text.delete("1.0", "end")
        list_text.insert("1.0", content)

        # и сразу ищем по списку
        search_by_list()

    btn_search_one.config(command=search_one_title)
    btn_search_list.config(command=search_by_list)
    btn_search_txt.config(command=search_from_txt)

    def ask_news_range(parent) -> tuple[int | None, int | None, bool]:
        """
        Красивый диалог 'Новинки Kino.pub': 
        'Начать с страницы __  по страницу __'.
        Возвращает (start_page, end_page, resolve_years) или (None, None, ...), если Cancel.
        """
        dlg = tk.Toplevel(parent)
        dlg.title("Новинки Kino.pub")
        try:
            dlg.iconbitmap(get_app_icon())
        except Exception:
            pass

        dlg.transient(parent)
        dlg.grab_set()
        dlg.resizable(True, True)

        dlg.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        # центрируем
        dlg.update_idletasks()
        try:
            scale = float(globals().get("UI_SCALE", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        scale = max(1.0, min(3.0, scale))
        w, h = int(520 * scale), int(300 * scale)
        sw = int(parent.winfo_screenwidth())
        sh = int(parent.winfo_screenheight())
        w = min(w, max(420, sw - 80))
        h = min(h, max(220, sh - 120))
        x = (sw - w) // 2
        y = (sh - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        try:
            dlg.minsize(420, 240)
        except Exception:
            pass

        # --- контент ---
        title_lbl = tk.Label(
            dlg,
            text="Новинки Kino.pub",
            bg=BG_SURFACE,
            fg=ACCENT,
            font=("Segoe UI Semibold", 14),
        )
        title_lbl.pack(pady=(10, 4))

        hint_lbl = tk.Label(
            dlg,
            text="Укажите диапазон страниц новинок:\n"
                "например, с 2 по 5",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 9),
            justify="center",
            wraplength=max(320, w - 60),
        )
        hint_lbl.pack(pady=(0, 8), fill="x")

        def _sync_wrap(_e=None):
            try:
                wrap = max(320, int(dlg.winfo_width()) - 60)
                hint_lbl.configure(wraplength=wrap)
            except Exception:
                pass

        dlg.bind("<Configure>", _sync_wrap)
        dlg.after(0, _sync_wrap)

        row = tk.Frame(dlg, bg=BG_SURFACE)
        row.pack(pady=(4, 4))

        tk.Label(
            row,
            text="Начать с",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 4))

        start_var = tk.StringVar(value="1")
        start_entry = tk.Entry(
            row,
            textvariable=start_var,
            width=4,
            bg=FIELD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 11),
            justify="center",
        )
        try:
            style_entry(start_entry)
        except Exception:
            pass
        start_entry.pack(side="left")

        tk.Label(
            row,
            text="по",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=4)

        end_var = tk.StringVar(value="1")
        end_entry = tk.Entry(
            row,
            textvariable=end_var,
            width=4,
            bg=FIELD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 11),
            justify="center",
        )
        try:
            style_entry(end_entry)
        except Exception:
            pass
        end_entry.pack(side="left")

        error_lbl = tk.Label(
            dlg,
            text="",
            bg=BG_SURFACE,
            fg="red",
            font=("Segoe UI", 9),
        )
        error_lbl.pack(pady=(2, 0))

        try:
            s0 = load_settings()
            default_resolve_years = bool(s0.get("kino_news_resolve_years", False))
        except Exception:
            default_resolve_years = False
        resolve_var = tk.BooleanVar(value=default_resolve_years)

        chk_years = tk.Checkbutton(
            dlg,
            text="Подтягивать год для каждой карточки (медленно)",
            variable=resolve_var,
            onvalue=True,
            offvalue=False,
            bg=BG_SURFACE,
            fg=TEXT,
            activebackground=BG_SURFACE,
            activeforeground=TEXT,
            selectcolor=BG_SURFACE,
            font=("Segoe UI", 9),
            anchor="w",
        )
        chk_years.pack(anchor="w", padx=18, pady=(6, 0))

        res = {"start": None, "end": None, "resolve_years": bool(resolve_var.get())}

        def on_ok():
            try:
                s = int(start_var.get().strip())
                e = int(end_var.get().strip())
            except ValueError:
                error_lbl.config(text="Страницы должны быть числами.")
                return

            if s < 1 or e < 1:
                error_lbl.config(text="Номера страниц должны быть ≥ 1.")
                return
            if e > 999:
                error_lbl.config(text="Максимум 999 страниц.")
                return
            if e < s:
                error_lbl.config(text="Страница 'по' не может быть меньше 'с'.")
                return

            res["start"] = s
            res["end"] = e
            res["resolve_years"] = bool(resolve_var.get())
            try:
                sset = load_settings()
                sset["kino_news_resolve_years"] = bool(resolve_var.get())
                save_settings(sset)
            except Exception:
                pass
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=BG_SURFACE)
        btn_row.pack(pady=(10, 8))

        btn_ok = tk.Button(btn_row, text="OK", width=10, command=on_ok)
        style_primary(btn_ok)
        btn_ok.pack(side="left", padx=6)

        btn_cancel = tk.Button(btn_row, text="Cancel", width=10, command=on_cancel)
        style_secondary(btn_cancel)
        btn_cancel.pack(side="left", padx=6)

        start_entry.focus_set()

        def on_enter(event):
            on_ok()

        dlg.bind("<Return>", on_enter)
        dlg.bind("<Escape>", lambda e: on_cancel())

        parent.wait_window(dlg)
        return res["start"], res["end"], bool(res.get("resolve_years", False))

    # новинки пока оставим заглушкой
    def load_news():
        # красивый диалог "с / по"
        start_page, end_page, resolve_years = ask_news_range(root)
        if start_page is None or end_page is None:
            return

        # очищаем старые результаты
        for item in tree_search.get_children():
            tree_search.delete(item)
        search_meta.clear()
        checked_items.clear()

        try:
            btn_news.config(state="disabled")
        except Exception:
            pass

        def _task(busy: BusyOverlay):
            rows = []
            total = max(1, (end_page - start_page + 1))
            for i, page in enumerate(range(start_page, end_page + 1), start=1):
                busy.set_message(f"⏳ Новинки: страница {page} ({i}/{total})")
                try:
                    page_results = kino_fetch_news_page(page, max_results=None)
                except Exception as e:
                    logging.error("Ошибка при загрузке новинок страницы %s: %s", page, e)
                    continue

                for _display_title, url, base_title, year, eng_title in page_results:
                    if resolve_years and (not year):
                        try:
                            year = fetch_year_from_card(url)
                        except Exception:
                            year = None

                    rows.append((page, url, base_title, year, eng_title))
            return rows

        def _done(rows, busy: BusyOverlay):
            chunk = 60
            total = len(rows)

            def _insert(i=0):
                end = min(total, i + chunk)
                if total:
                    busy.set_message(f"⏳ Добавляю в таблицу: {end}/{total}")

                for page, url, base_title, year, eng_title in rows[i:end]:
                    query_label = f"стр {page}"
                    title_for_grid = base_title

                    item_id = tree_search.insert(
                        "",
                        "end",
                        values=("☐", query_label, title_for_grid, year or "", url),
                    )
                    search_meta[item_id] = {
                        "query": query_label,
                        "title": base_title,
                        "year":  year,
                        "url":   url,
                        "eng_title": eng_title,
                    }

                if end < total:
                    root.after(0, lambda: _insert(end))
                    return

                try:
                    btn_news.config(state="normal")
                except Exception:
                    pass
                busy.close()

            _insert(0)

        def _err(e: Exception, _busy: BusyOverlay):
            try:
                btn_news.config(state="normal")
            except Exception:
                pass
            messagebox.showerror("Ошибка", f"Не удалось загрузить новинки:\n{e}")

        run_async(
            root,
            _task,
            title="Новинки Kino.pub",
            message="⏳ Загружаю новинки…",
            modal=False,
            on_done=_done,
            on_error=_err,
        )


    # привязываем кнопку
    btn_news.config(command=load_news)

    # --- Кнопка: отправить выбранные в очередь скачивания ---
    bottom_search = tk.Frame(search_right, bg=BG_SURFACE)
    bottom_search.pack(fill="x", padx=32, pady=(4, 8))
     # слева — отправка в медиатеку
    btn_to_requests = tk.Button(bottom_search, text="Проверить в медиатеке")
    style_secondary(btn_to_requests)
    btn_to_requests.pack(side="left")
    btn_add_to_queue = tk.Button(bottom_search, text="Добавить выбранные в очередь")
    style_primary(btn_add_to_queue)
    btn_add_to_queue.pack(side="right")

    def add_selected_from_search():
        # если есть отмеченные галочками — используем их
        if checked_items:
            items = list(checked_items)
        else:
            # иначе — fallback на выделение строк
            items = list(tree_search.selection())

        if not items:
            messagebox.showinfo(
                "Очередь",
                "Выберите хотя бы один результат поиска (поставьте галочки)."
            )
            return

        out_dir = _get_out_dir()
        for item in items:
            vals = tree_search.item(item, "values")
            # (chk, query, title, year, url)
            if len(vals) < 5:
                continue

            _, _, display_title, year, url = vals

            row_title = display_title
            row_id = add_row(row_title, status="🟡 Подготовка...")

            if hasattr(manager, "url_by_item"):
                manager.url_by_item[row_id] = url

            _start_kino_item(row_id, url, out_dir)
        try:
            _schedule_kino_queue_save()
        except Exception:
            pass
    def send_selected_to_requests():
            """
            Забрать текущие результаты поиска / новинок и
            отправить их в экран 'Работа с запросами'.

            Если есть галочки — используем только отмеченные.
            Если галочек нет — берём все строки таблицы.
            Формат строки:
            - если знаем год:  'Название (Год)'
            - если года нет:   'Название'
            """
            global kino_urls_for_requests  
            kino_urls_for_requests.clear()
            # 1) какие строки брать
            if checked_items:
                items = list(checked_items)
            else:
                items = list(tree_search.get_children())

            if not items:
                messagebox.showinfo(
                    "Медиатека",
                    "Нет результатов для передачи в список запросов."
                )
                return

            lines: list[str] = []
            used: set[str] = set()

            for item in items:
                meta = search_meta.get(item)

                if meta:
                    base_title = (meta.get("title") or "").strip()
                    year = (meta.get("year") or "") or ""
                else:
                    # запасной вариант — читаем прямо из таблицы
                    vals = tree_search.item(item, "values")
                    # (chk, query, title, year, url)
                    if len(vals) < 3:
                        continue
                    base_title = str(vals[2]).strip()
                    year = str(vals[3]).strip() if len(vals) >= 4 else ""

                if not base_title:
                    continue

                # ВАЖНО:
                # если года нет (новинки) — ищем только по названию
                if year:
                    line = f"{base_title} ({year})"
                else:
                    line = base_title

                if line not in used:
                    used.add(line)
                    lines.append(line)
                    # НОВОЕ: если у нас есть URL — запомнить его для этой строки
                    if meta:
                        url = meta.get("url")
                        if url:
                            kino_urls_for_requests[line] = url
            if not lines:
                messagebox.showinfo(
                    "Медиатека",
                    "Не удалось собрать названия для запросов."
                )
                return
            
            # 2) заливаем список в экран запросов
            clear_requests(reset_urls=False)    
            req_text.insert("1.0", "\n".join(lines))

            # 3) переключаем экран
            try:
                _active[0] = "requests"
                set_nav_active(nav_items, "requests")
                animate_nav_indicator(nav_items["requests"])
            except Exception:
                pass
            slide_switch(kino_search, requests, root, "right")   

    btn_add_to_queue.config(command=add_selected_from_search)
    btn_to_requests.config(command=send_selected_to_requests)

    def enable_clipboard_for_all(root, kino_input, btn_download):
        def on_ctrl_key(event, entry):
            if event.state & 0x4 and event.keycode in (67, 83): entry.event_generate("<<Copy>>")
            elif event.state & 0x4 and event.keycode in (86, 77): entry.event_generate("<<Paste>>")
            elif event.state & 0x4 and event.keycode in (65, 70):
                entry.select_range(0, "end"); return "break"

        def bind_entry(entry):
            entry.bind("<KeyPress>", lambda e, ent=entry: on_ctrl_key(e, ent))
            menu = tk.Menu(entry, tearoff=0, bg=ACTIVE_BG, fg=TEXT,
                           activebackground=ACCENT, activeforeground=TEXT_ON_ACCENT,
                           font=("Segoe UI", 9))
            menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
            menu.add_command(label="Вставить",   command=lambda: entry.event_generate("<<Paste>>"))
            menu.add_command(label="Выделить всё", command=lambda: entry.select_range(0, "end"))
            entry.bind("<Button-3>", lambda e: (entry.focus_force(), menu.tk_popup(e.x_root, e.y_root)))

        def recurse(widget):
            for child in widget.winfo_children():
                if isinstance(child, (tk.Entry, tk.Text)): bind_entry(child)
                recurse(child)

        recurse(root)
        kino_input.bind("<Return>", lambda e: btn_download.invoke())
        root.bind("<Control-Return>", lambda e: btn_download.invoke())

    enable_clipboard_for_all(root, kino_input, btn_download)

    root.mainloop()

if __name__ == "__main__":
    main()
