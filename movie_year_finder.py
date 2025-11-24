import os
import time
import logging
import tkinter as tk
import shutil
import json
import subprocess
import re
import difflib
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
import sys 
# === НОВОЕ: Selenium для реального поиска ===
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import threading
META_EXTENSIONS = set(RELATED_EXTENSIONS) | {
    ".nfo", ".xml", ".jpg", ".jpeg", ".png", ".webp", ".tbn"
}
VIDEO_EXTENSIONS = set(VIDEO_EXTENSIONS)
# --- Настройки (последняя папка сохранения и т.п.) ---
SETTINGS_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "MediaSearch")
os.makedirs(SETTINGS_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

YEAR_RE = re.compile(r"^(.*?)[\s\u00A0]*\((\d{4})\)\s*$")

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

def split_title_year(line: str):
    """
    Принимает строку вроде:
      'Название (2025)'
      'Название (2025) [WEB-DL 1080p]'
    Возвращает (title, year), где
      title — всё, что ДО первой скобки с годом,
      year  — строка '2025'.

    Если года нет — (исходная строка, None).
    """
    line = line.strip()
    if not line:
        return "", None

    m = YEAR_RE.search(line)
    if not m:
        # года нет — возвращаем как есть
        return line, None

    year = m.group(1)
    # Берём всё, что ДО "(год)"
    title = line[:m.start()].strip()
    if not title:
        title = line  # запасной вариант, если вдруг всё вырезали

    return title, year
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


SHOW_QUEUE_CONTROLS = False  # скрыть блок: Импорт списка / Удалить / Запустить всё / Остановить
# --- Режим окна при старте ---
START_MAXIMIZED  = True   # развернуть на весь экран (обычный «максимизированный» режим)
START_FULLSCREEN = False  # полноэкранный режим без рамок (F11/ESC для выхода)
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

# --- Палитра (чистая тёмная, без кругов) ---
BRAND_SKY     = "#8AADD3"   # светло-голубой
BRAND_MAGENTA = "#A24BA1"   # маджента
BRAND_NAVY    = "#1C226B"   # глубокий синий

BG_WINDOW  = "#0B0F2A"      # общий фон
BG_SURFACE = "#13183A"      # панели/карточки
BORDER     = "#222A5A"      # границы
TEXT       = "#E9ECF7"
SUBTEXT    = "#A8B2D9"

ACCENT         = BRAND_MAGENTA
ACCENT_HOVER   = "#B866B7"
ACCENT_SECOND  = BRAND_SKY

# ---------- UI helpers ----------
def dpi_scaling(root: tk.Tk):
    try:
        px = root.winfo_fpixels("1i")
        factor = max(1.0, round(px / 96, 2))
        root.tk.call("tk", "scaling", factor)
        logging.info(f"UI scaling set to {factor}")
    except Exception as e:
        logging.warning(f"Scaling failed: {e}")

def fade_in(window, alpha=0.0):
    alpha += 0.05
    if alpha <= 1.0:
        window.attributes("-alpha", alpha)
        window.after(20, lambda: fade_in(window, alpha))

def slide_switch(frame_out: tk.Frame, frame_in: tk.Frame, root: tk.Tk, direction="right"):
    frame_out.place_forget()
    frame_in.place(relx=1.0 if direction == "right" else -1.0, rely=0, relwidth=1.0, relheight=1.0)
    steps = 16
    for i in range(steps):
        x = 1.0 - (i + 1) / steps if direction == "right" else -1.0 + (i + 1) / steps
        frame_in.place_configure(relx=x)
        root.update_idletasks()
        time.sleep(0.008)
    frame_in.place_configure(relx=0.0)

def style_primary(btn: tk.Button):
    btn.config(
        bg=ACCENT, fg="white",
        activebackground=ACCENT_HOVER, activeforeground="white",
        relief="flat", borderwidth=0, cursor="hand2",
        font=("Segoe UI Semibold", 13),
        padx=20, pady=12, height=2, highlightthickness=0,
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_HOVER))
    btn.bind("<Leave>", lambda e: btn.config(bg=ACCENT))

def style_secondary(btn: tk.Button):
    btn.config(
        bg="#18204C", fg=ACCENT_SECOND,
        activebackground="#1E275A", activeforeground=ACCENT_SECOND,
        relief="flat", borderwidth=0, cursor="hand2",
        font=("Segoe UI", 11), padx=16, pady=10,
        highlightbackground=ACCENT_SECOND, highlightthickness=1,
    )

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
    for w in frame.winfo_children(): w.destroy()
    if not found_files:
        page_label.config(text="")
        for w in nav_frame.winfo_children(): w.destroy()
        nav_frame.pack_forget()
        return

    start = (current_page - 1) * items_per_page
    end = min(len(found_files), start + items_per_page)
    page_items = list(zip(found_files[start:end], checked_vars[start:end]))

    for idx, ((name, path), var) in enumerate(page_items, start=start + 1):
        bg = BG_SURFACE if idx % 2 else "#0F1440"
        card = tk.Frame(frame, bg=bg, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=8, pady=6)

        def _apply_card_state():
            if var.get():
                # выделяем карточку и делаем фон чуть контрастнее
                card.config(highlightbackground=ACCENT, highlightcolor=ACCENT, highlightthickness=2, bg="#10163D")
            else:
                card.config(highlightbackground=BORDER, highlightcolor=BORDER, highlightthickness=1, bg=bg)

        def _on_toggle():
            _apply_card_state()
            update_copy_button_text()

        chk = tk.Checkbutton(
            card,
            variable=var,
            bg=bg,
            fg=TEXT,
            activebackground=bg,
            selectcolor=ACCENT,      # <<< яркая заливка индикатора при отметке
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            command=_on_toggle
        )
        chk.pack(side="left", padx=10, pady=10)

        _apply_card_state()  # первичная отрисовка рамки


        info = tk.Frame(card, bg=bg); info.pack(side="left", fill="both", expand=True, pady=8)
        tk.Label(info, text=f"{idx}. {name}", font=("Segoe UI", 11, "bold"),
                 fg=TEXT, bg=bg, anchor="w").pack(anchor="w", fill="x")
        tk.Label(info, text=path, font=("Segoe UI", 9),
                 fg=SUBTEXT, bg=bg, anchor="w", wraplength=760, justify="left").pack(anchor="w")

    frame.update_idletasks()
    bbox = canvas.bbox("all")
    if bbox: canvas.configure(scrollregion=bbox)

    total_pages = (len(found_files) + items_per_page - 1) // items_per_page
    page_label.config(text=f"Страница {current_page} из {total_pages}", fg=SUBTEXT, bg=BG_SURFACE)

    for w in nav_frame.winfo_children(): w.destroy()

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
    style_secondary(btn_prev); style_secondary(btn_next)
    btn_prev.pack(side="left", padx=6); btn_next.pack(side="left", padx=6)
    nav_frame.pack(side="right")

def search_by_year(year, frame, canvas, count_label, page_label, nav_frame, update_copy_button_text):
    global found_files, checked_vars, current_page
    found_files, checked_vars = [], []
    seen = set()
    for name, path in movie_index:
        if f"({year})" in name:
            base = normalize_name(name)
            if base in seen: continue
            seen.add(base)
            found_files.append((name, path))
            checked_vars.append(tk.BooleanVar(value=False))

    count_label.config(text=f"Найдено фильмов: {len(found_files)}", fg=ACCENT_SECOND, bg=BG_WINDOW)
    if not found_files:
        messagebox.showinfo("Результат", f"Фильмы за {year} не найдены"); return
    current_page = 1
    render_page(frame, canvas, page_label, nav_frame, update_copy_button_text)

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
def main():
    global current_page
    root = tk.Tk()
    root.title("Movie Tools")
    try: root.iconbitmap("icon.ico")
    except Exception: logging.info("icon.ico not found, using default icon")
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
    root.attributes("-alpha", 0.0); fade_in(root)
    # авто-проверка обновлений через 2 секунды после старта
    root.after(2000, lambda: check_for_updates_async(root, show_if_latest=False))

    # --- Шапка ---
    appbar = tk.Frame(root, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    appbar.pack(side="top", fill="x")
    tk.Label(appbar, text="🎬 Movie Tools", bg=BG_SURFACE, fg=ACCENT,
             font=("Segoe UI Semibold", 20)).pack(side="left", padx=16, pady=10)

    # --- Экраны ---
    main_menu = tk.Frame(root, bg=BG_WINDOW)
    finder = tk.Frame(root, bg=BG_WINDOW)
    kino = tk.Frame(root, bg=BG_WINDOW)
    kino_search = tk.Frame(root, bg=BG_WINDOW)  # новый экран поиска Kino.pub
    requests = tk.Frame(root, bg=BG_WINDOW)     # НОВЫЙ ЭКРАН «Работа с запросами»

    for f in (main_menu, finder, kino, kino_search, requests):
        f.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
        f.place_forget()


    main_menu.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)


    # ========== Главный экран (чистый тёмный) ==========
    card = tk.Frame(
    main_menu,
    bg=BG_SURFACE,
    highlightbackground=BORDER,
    highlightthickness=1,
)
    card.place(relx=0.5, rely=0.5, anchor="center", width=520, height=390)

    # тонкая акцентная полоса сверху карточки
    tk.Frame(card, bg=ACCENT, height=3).pack(fill="x", side="top")

    tk.Label(
        card,
        text="🎬 MOVIE TOOLS",
        bg=BG_SURFACE,
        fg=ACCENT,
        font=("Segoe UI Semibold", 22),
    ).pack(pady=(22, 6))

    tk.Label(
        card,
        text="Управляй своей медиатекой легко и красиво",
        bg=BG_SURFACE,
        fg=SUBTEXT,
        font=("Segoe UI", 11),
    ).pack(pady=(0, 20))


    def prepare_index():
        global movie_index, index_loaded
        try:
            res = None
            try: res = export_and_load_index()
            except TypeError:
                try: res = export_and_load_index(year_entry)
                except Exception: res = None
            if isinstance(res, list) and (not res or isinstance(res[0], tuple)):
                movie_index = res or []
            else:
                movie_index = load_index_from_efu(EFU_FILE) or []
            index_loaded = bool(movie_index)
            year_entry.config(state="normal" if index_loaded else "disabled")
            btn_find_year.config(state="normal" if index_loaded else "disabled")
            if index_loaded:
                count_label.config(text=f"Индекс загружен: {len(movie_index)} фильмов",
                                   fg=ACCENT_SECOND, bg=BG_WINDOW)
            else:
                messagebox.showerror("Ошибка", "Не удалось загрузить индекс (NAS/EFU).")
        except Exception as e:
            index_loaded = False
            year_entry.config(state="disabled"); btn_find_year.config(state="disabled")
            messagebox.showerror("Ошибка", f"Проверка NAS не удалась: {e}")

    def neon_button(parent, text, command):
        wrap = tk.Frame(parent, bg=BG_SURFACE)
        wrap.pack(fill="x", padx=60, pady=6)  # было 8
        btn = tk.Button(
            wrap,
            text=text,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI Semibold", 13),
            cursor="hand2",
            padx=18,
            pady=10,
            highlightthickness=0,
        )
        style_primary(btn)
        btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_HOVER))
        btn.bind("<Leave>", lambda e: btn.config(bg=ACCENT))
        btn.config(command=command)
        btn.pack(fill="x", ipady=3)
        return btn


    neon_button(card, "🔎 Поиск фильмов по году", lambda: slide_switch(main_menu, finder, root, "right"))
    neon_button(card, "🎞 Работа с Kino.pub",     lambda: slide_switch(main_menu, kino,   root, "right"))
    neon_button(card, "📝 Работа с запросами",    lambda: slide_switch(main_menu, requests, root, "right"))


    tk.Frame(main_menu, bg=BORDER, height=1).place(relx=0, rely=1.0, 
                                                   relwidth=1.0, y=-26, anchor="sw")
    footer_label = tk.Label(main_menu, text="Created by Ti0jei v1.0.5",
                            bg=BG_WINDOW, fg=ACCENT_SECOND, font=("Segoe UI Semibold", 9))
    footer_label.place(relx=1.0, rely=1.0, x=-12, y=-8, anchor="se")

    # ========== Finder ==========
    commandbar = tk.Frame(finder, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    commandbar.pack(side="top", fill="x", pady=(0, 6))
    tk.Label(commandbar, text="🎞 MOVIE YEAR FINDER", bg=BG_SURFACE, fg=ACCENT_SECOND,
             font=("Segoe UI Semibold", 16)).pack(side="left", padx=12, pady=8)

    right_controls = tk.Frame(commandbar, bg=BG_SURFACE); right_controls.pack(side="right", padx=12, pady=8)
    btn_export = tk.Button(right_controls, text="Проверить NAS")
    style_secondary(btn_export)
    btn_export.pack(side="left", padx=(0, 10))
    tk.Label(right_controls, text="Год:", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 11)).pack(side="left")
    year_entry = tk.Entry(right_controls, font=("Segoe UI", 11), width=8, state="disabled",
                          bg="#0D1138", fg="white", insertbackground="white", relief="flat")
    year_entry.pack(side="left", padx=(6, 8))
    btn_find_year = tk.Button(right_controls, text="Найти", state="disabled")
    style_secondary(btn_find_year)
    btn_find_year.pack(side="left")

    btn_back_mm = tk.Button(commandbar, text="← В меню"); style_secondary(btn_back_mm)
    btn_back_mm.config(command=lambda: slide_switch(finder, main_menu, root, "left"))
    btn_back_mm.pack(side="left", padx=10)

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

    btn_export.config(command=prepare_index)
    btn_find_year.config(command=on_search)
    btn_copy.config(command=lambda: copy_selected(root))

    def _on_mousewheel(event):
        if event.num == 5 or event.delta == -120: canvas.yview_scroll(1, "units")
        if event.num == 4 or event.delta == 120:  canvas.yview_scroll(-1, "units")

    canvas.bind_all("<MouseWheel>", _on_mousewheel)
    canvas.bind_all("<Button-4>", _on_mousewheel)
    canvas.bind_all("<Button-5>", _on_mousewheel)
    root.bind("<Control-a>", lambda e: toggle_select_all())
    def on_key_return(event):
        if finder.winfo_ismapped():
            on_search()
        elif kino_search.winfo_ismapped():
            search_one_title()
    root.bind("<Return>", on_key_return)

    if START_FULLSCREEN:
        # Esc — выйти из полноэкранного; F11 — вернуть
        root.bind("<Escape>", lambda e: root.attributes("-fullscreen", False))
        root.bind("<F11>",   lambda e: root.attributes("-fullscreen",
                                                    not bool(root.attributes("-fullscreen"))))
    else:
        root.bind("<Escape>", lambda e: root.iconify())

        # сброс профиля MediaSearch + UC-драйвера для Kino.pub
    def reset_kino_profile():
        local = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        media_profile = os.path.join(local, "MediaSearch")

        roaming = os.getenv("APPDATA") or os.path.expanduser("~")
        uc_profile = os.path.join(roaming, "undetected_chromedriver")

        msg = (
            "Будут удалены папки профиля:\n\n"
            f"{media_profile}\n"
            f"{uc_profile}\n\n"
            "Это сбросит кеш/профиль браузера и UC-драйвера.\n"
            "Продолжить?"
        )
        if not messagebox.askyesno("Обновить профиль", msg):
            return

        for path in (media_profile, uc_profile):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                    logging.info("Удалена папка профиля: %s", path)
            except Exception as e:
                logging.error("Ошибка удаления профиля %s: %s", path, e)

        messagebox.showinfo(
            "Обновить профиль",
            "Папки профиля удалены.\n\n"
            "Рекомендуется перезапустить программу перед\n"
            "повторной работой с Kino.pub."
        )
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

    btn_back_req = tk.Button(req_top, text="← В меню")
    style_secondary(btn_back_req)
    btn_back_req.config(
        command=lambda: slide_switch(requests, main_menu, root, "left")
    )
    btn_back_req.pack(side="left", padx=10)

    # дубль кнопки "Проверить NAS" для экрана запросов
    btn_req_nas = tk.Button(req_top, text="Проверить NAS")
    style_secondary(btn_req_nas)
    btn_req_nas.config(command=prepare_index)
    btn_req_nas.pack(side="right", padx=12)
    # кнопка "Войти в Kino.pub" на экране запросов
    btn_req_login = tk.Button(req_top, text="Войти в Kino.pub")
    style_secondary(btn_req_login)
    btn_req_login.pack(side="right", padx=8)

    # --- Тело экрана ---
    req_body = tk.Frame(requests, bg=BG_WINDOW)
    req_body.pack(fill="both", expand=True, padx=10, pady=8)

    # правая колонка в 2 раза шире левой
    req_body.columnconfigure(0, weight=1)
    req_body.columnconfigure(1, weight=2)
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
        bg="#0D1138",
        fg="white",
        insertbackground="white",
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

    req_tree.column("sel",         width=24,  anchor="center", stretch=False)
    req_tree.column("req_title",   width=150, anchor="w")
    req_tree.column("status",      width=120, anchor="center")
    req_tree.column("found_title", width=220, anchor="w")
    req_tree.column("path",        width=520, anchor="w")
    req_tree.column("paths_btn",   width=26,  anchor="center", stretch=False)

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
            popup.iconbitmap("icon.ico")
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
            bg="#0D1138",
            fg="white",
            selectbackground=ACCENT,
            selectforeground="white",
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
            bg="#0D1138",
            fg=SUBTEXT,
            selectbackground=ACCENT,
            selectforeground="white",
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
        style_primary(btn_ok)
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
    btn_req_dl_selected = tk.Button(req_footer, text="Скачать выбранные")
    style_secondary(btn_req_dl_selected)
    btn_req_dl_selected.pack(side="right", padx=8)

    btn_req_dl_missing = tk.Button(req_footer, text="Скачать не найденные")
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
                "Сначала проверь данные на NAS (кнопка «Проверить NAS»).",
            )
            return

        lines = req_text.get("1.0", "end").splitlines()

        for item in req_tree.get_children():
            req_tree.delete(item)
        request_rows_meta.clear()
        req_checked_items.clear()

        index_map = build_index_map()

        total = 0
        found_cnt = 0

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
                display_title = ""
            else:
                videos = [r for r in matches if r["is_video"]]
                metas  = [r for r in matches if r["is_meta"]]

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

        missing = max(0, total - found_cnt)
        req_summary.config(
            text=f"Всего запросов: {total} | найдено: {found_cnt} | нет в медиатеке: {missing}"
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
            progress_win.iconbitmap("icon.ico")
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

    def clear_requests():
        req_text.delete("1.0", "end")
        for item in req_tree.get_children():
            req_tree.delete(item)
        req_checked_items.clear()
        request_rows_meta.clear()
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

        req_text.delete("1.0", "end")
        req_text.insert("1.0", content)
        
    def download_requests(mode: str):
        """
        mode = 'selected'  -> использовать строки, отмеченные галочками
        mode = 'missing'   -> использовать строки со статусом 'Нет в медиатеке'
        """

        if not kino_logged_in:
            show_login_required()
            return

        if mode == "selected":
            items = list(req_checked_items)
            if not items:
                messagebox.showinfo("Загрузка",
                                    "Отметьте галочкой слева хотя бы один фильм.")
                return
        elif mode == "missing":
            items = []
            for item in req_tree.get_children():
                vals = req_tree.item(item, "values")
                if len(vals) >= 3 and "Нет в медиатеке" in str(vals[2]):
                    items.append(item)
            if not items:
                messagebox.showinfo("Загрузка",
                                    "Нет строк со статусом «Нет в медиатеке».")
                return
        else:
            return

        out_dir = out_dir_var.get().strip()
        if not out_dir:
            messagebox.showerror("Загрузка",
                                "Не указана папка сохранения в блоке Kino.pub.")
            return

        added = 0
        not_found_online = 0

        for item_id in items:
            meta = request_rows_meta.get(item_id) or {}

            original = (meta.get("original") or "").strip()
            if not original:
                vals = req_tree.item(item_id, "values")
                if len(vals) >= 2:
                    original = str(vals[1]).strip()
            if not original:
                continue

            title, _ = split_title_year(original)
            title = title or original
            if not title:
                continue

            url = None
            display_title = None
            base_title = None
            year = None
            eng_title = None

            pre_url = meta.get("kino_url")
            if pre_url:
                url = pre_url
                vals = req_tree.item(item_id, "values")
                if len(vals) >= 4:
                    display_title = str(vals[3]) or title
                else:
                    display_title = title
            else:
                try:
                    results = kino_search_real(title, max_results=1)
                except Exception as e:
                    logging.error("kino_search_real('%s') failed: %s", title, e)
                    continue

                if not results:
                    not_found_online += 1
                    continue

                display_title, url, base_title, year, eng_title = results[0]

            if not url:
                not_found_online += 1
                continue

            shown_title = display_title
            if eng_title:
                shown_title = f"{display_title} / {eng_title}"

            row_id = add_row(shown_title, status="🟡 Подготовка...")
            if hasattr(manager, "url_by_item"):
                manager.url_by_item[row_id] = url

            manager.start_item(row_id, url, out_dir)
            added += 1

        messagebox.showinfo(
            "Kino.pub",
            f"В очередь загрузки добавлено: {added}\n"
            f"Не найдено на Kino.pub: {not_found_online}"
        )


    btn_req_check.config(command=check_requests)
    btn_req_clear.config(command=clear_requests)
    btn_req_txt.config(command=load_requests_from_txt)
    btn_req_copy.config(command=copy_selected_requests)
    btn_req_dl_selected.config(
        command=lambda: download_requests("selected")
    )
    btn_req_dl_missing.config(
        command=lambda: download_requests("missing")
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

    # кнопка "В меню" (одна!)
    btn_back_kino = tk.Button(kino_top, text="← В меню")
    style_secondary(btn_back_kino)
    btn_back_kino.config(command=lambda: slide_switch(kino, main_menu, root, "left"))
    btn_back_kino.pack(side="left", padx=10)

    # кнопка "Поиск" — открывает отдельный экран поиска kino_search
    btn_kino_search = tk.Button(kino_top, text="Поиск")
    style_secondary(btn_kino_search)

    def open_kino_search():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return
        slide_switch(kino, kino_search, root, "right")

    btn_kino_search.config(command=open_kino_search)
    btn_kino_search.pack(side="left", padx=6)

    # кнопка "Обновить профиль" (тоже одна)
    btn_reset_profile = tk.Button(kino_top, text="Обновить профиль")
    style_secondary(btn_reset_profile)
    btn_reset_profile.config(command=reset_kino_profile)
    btn_reset_profile.pack(side="left", padx=6)

    # кнопка "Войти в Kino.pub" — справа вверху
    btn_login_uc = tk.Button(kino_top, text="Войти в Kino.pub")
    style_secondary(btn_login_uc)
    btn_login_uc.pack(side="right", padx=12)

    # карточка загрузчика
    card_kino = tk.Frame(kino, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    card_kino.place(relx=0.5, rely=0.555, anchor="center", width=680, height=640)
    tk.Frame(card_kino, bg=ACCENT, height=3).pack(fill="x", side="top")

    card_kino = tk.Frame(kino, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    card_kino.place(relx=0.5, rely=0.555, anchor="center", width=680, height=640)
    tk.Frame(card_kino, bg=ACCENT, height=3).pack(fill="x", side="top")

    top_part = tk.Frame(card_kino, bg=BG_SURFACE); top_part.pack(fill="x", pady=(20, 10))
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
        bg="#0D1138",
        fg="white",
        insertbackground="white",
        relief="flat",
        font=("Segoe UI", 11),
        
    )
    kino_input.pack(side="left", fill="x", expand=True, ipady=4)

    # кнопка "Скачать" такого же размера/стиля, как "Выбрать"
    btn_download = tk.Button(input_row, text="⬇️ Скачать")
    style_secondary(btn_download)          # тот же стиль, что и у "Выбрать"
    btn_download.pack(side="left", padx=(8, 0), ipady=2)


    path_frame = tk.Frame(top_part, bg=BG_SURFACE); path_frame.pack(fill="x", padx=40, pady=(10, 8))
    tk.Label(path_frame, text="📂 Папка сохранения:", bg=BG_SURFACE, fg=SUBTEXT,
             font=("Segoe UI", 10)).pack(anchor="w")
    settings = load_settings()
    default_dir = settings.get("last_download_dir") or os.path.join(os.getcwd(), "Downloads")
    out_dir_var = tk.StringVar(value=default_dir)
    path_entry = tk.Entry(path_frame, textvariable=out_dir_var, bg="#0D1138", fg="white",
                          insertbackground="white", relief="flat", font=("Segoe UI", 10), )
    path_entry.pack(side="left", fill="x", expand=True, ipady=4, pady=(4, 0))

    def choose_folder():
        global kino_logged_in
        if not kino_logged_in:
            show_login_required()
            return

        d = filedialog.askdirectory(title="Выберите папку сохранения")
        if d:
            out_dir_var.set(d)
            s = load_settings()
            s["last_download_dir"] = d
            save_settings(s)
    choose_btn = tk.Button(path_frame, text="Выбрать", command=choose_folder); style_secondary(choose_btn)
    choose_btn.pack(side="left", padx=(8, 0))
    kino_status = tk.Label(top_part, text="", bg=BG_SURFACE, fg=ACCENT_SECOND, font=("Segoe UI", 10))
    kino_status.pack(pady=(8, 4))
    queue_part = tk.Frame(card_kino, bg=BG_SURFACE); queue_part.pack(fill="both", expand=True, padx=36, pady=(8, 12))

    from tkinter import ttk
    table_frame = tk.Frame(queue_part, bg=BG_SURFACE); table_frame.pack(fill="both", expand=True, pady=(4, 6))
    scrollbar = tk.Scrollbar(table_frame); scrollbar.pack(side="right", fill="y")
    columns = ("#", "title", "status")
    tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=6, yscrollcommand=scrollbar.set)
    # ========== ПКМ МЕНЮ ДЛЯ ПОВТОРА ==========
        # ========== ПКМ МЕНЮ ДЛЯ ПОВТОРА / ПЕРЕЗАПУСКА ==========
    context_menu = tk.Menu(root, tearoff=0)

    def retry_selected():
        """Перезапустить выделенный элемент с самого начала, в любом состоянии."""
        try:
            item = tree.selection()[0]
        except Exception:
            return

        # Берём исходный текст/URL
        url = manager.url_by_item.get(item) or tree.set(item, "title")

        # Сбрасываем статус и запускаем заново
        tree.set(item, "status", "🟡 Подготовка...")
        out_dir = out_dir_var.get().strip()
        manager.start_item(item, url, out_dir)

    context_menu.add_command(label="Повторить / перезапустить загрузку",
                             command=retry_selected)

    def on_right_click(event):
        item = tree.identify_row(event.y)
        if not item:
            return
        tree.selection_set(item)
        # Раньше меню показывалось только при ошибке,
        # теперь — всегда, чтобы можно было перезапустить в любой момент.
        context_menu.tk_popup(event.x_root, event.y_root)

    tree.bind("<Button-3>", on_right_click)
    # ========================================================

    # ==========================================

    scrollbar.config(command=tree.yview)
    tree.bind("<Button-3>", on_right_click)
    tree.heading("#", text="№", anchor="center")
    tree.heading("title", text="Название / URL", anchor="w")
    tree.heading("status", text="Статус", anchor="center")
    tree.column("#", width=30, anchor="center")
    tree.column("title", width=400, anchor="w")
    tree.column("status", width=120, anchor="center")
    tree.pack(fill="both", expand=True)

    style = ttk.Style(); style.theme_use("clam")
    style.configure("Treeview",
                    background=BG_SURFACE, foreground=TEXT,
                    rowheight=26, fieldbackground=BG_SURFACE,
                    font=("Segoe UI", 10), borderwidth=0)
    style.configure("Treeview.Heading",
                    background="#1A214A", foreground=ACCENT_SECOND,
                    font=("Segoe UI Semibold", 10), relief="flat")
    style.map("Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", "white")])

    # --- Кнопки управления очередью (скрываем, если флаг False) ---
    if SHOW_QUEUE_CONTROLS:
        controls = tk.Frame(queue_part, bg=BG_SURFACE); controls.pack(fill="x", pady=(6, 2))

        def style_btn(b, accent=False):
            b.config(font=("Segoe UI", 10), padx=12, pady=6, borderwidth=0, relief="flat", cursor="hand2")
            if accent:
                b.config(bg=ACCENT, fg="white", activebackground=ACCENT_HOVER, activeforeground="white")
            else:
                b.config(bg="#18204C", fg=ACCENT_SECOND, activebackground="#1E275A", activeforeground=ACCENT_SECOND)

        btn_import = tk.Button(controls, text="📂 Импорт списка"); style_btn(btn_import, True);  btn_import.pack(side="left", padx=4)
        btn_delete = tk.Button(controls, text="🗑 Удалить");        style_btn(btn_delete);       btn_delete.pack(side="left", padx=4)
        btn_run    = tk.Button(controls, text="⏩ Запустить всё");  style_btn(btn_run, True);    btn_run.pack(side="left", padx=4)
        btn_stop   = tk.Button(controls, text="⏹ Остановить");     style_btn(btn_stop);         btn_stop.pack(side="left", padx=4)


    counter_bar = tk.Frame(queue_part, bg=BG_SURFACE); counter_bar.pack(fill="x", pady=(2, 0))
    active_counter = tk.Label(counter_bar, text="Активно: 0 / 2", bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 10))
    active_counter.pack(side="right", padx=6)

    # ========== DownloadManager ==========
    pool = DriverPool(max_drivers=2, status_cb=lambda m: kino_status.config(text=m[-80:], fg=ACCENT_SECOND))
    manager = DownloadManager(root, tree, active_counter, max_parallel=2, pool=pool)
        # --- Драйвер для поиска кино (отдельный от менеджера загрузок) ---
    
    


    def on_close():
        logging.info("Запрошено закрытие окна, останавливаем загрузки и драйверы")

        # Останавливаем новые загрузки
        try:
            manager.stop_all()
        except Exception as e:
            logging.error("Ошибка при stop_all(): %s", e)

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
            root.destroy()
        except Exception:
            pass


    root.protocol("WM_DELETE_WINDOW", on_close)
    def show_login_required():
        """Окно в нашем стиле: нужно сначала войти в Kino.pub."""
        dlg = tk.Toplevel(root)
        dlg.title("Kino.pub")
        try:
            dlg.iconbitmap("icon.ico")
        except Exception:
            pass

        dlg.transient(root)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)

        # центрируем
        dlg.update_idletasks()
        w, h = 420, 180
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        tk.Frame(dlg, bg=ACCENT, height=3).pack(fill="x", side="top")

        body = tk.Frame(dlg, bg=BG_SURFACE)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        tk.Label(
            body,
            text="Сначала выполните вход в Kino.pub",
            bg=BG_SURFACE,
            fg=ACCENT,
            font=("Segoe UI Semibold", 14),
        ).pack(anchor="w", pady=(0, 6))

        tk.Label(
            body,
            text="Нажмите кнопку «Войти в Kino.pub» в верхней панели,\n"
                 "авторизуйтесь, и после этого функция станет доступна.",
            bg=BG_SURFACE,
            fg=SUBTEXT,
            font=("Segoe UI", 10),
            justify="left",
        ).pack(anchor="w")

        btn_row = tk.Frame(body, bg=BG_SURFACE)
        btn_row.pack(fill="x", pady=(14, 0))
        btn_ok = tk.Button(btn_row, text="Понятно", command=dlg.destroy)
        style_primary(btn_ok)
        btn_ok.pack(side="right")

        dlg.bind("<Return>", lambda e: dlg.destroy())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def login_to_kino():
        global kino_logged_in
        try:
            kino_status.config(text="⏳ Инициализация входа.", fg=ACCENT_SECOND)

            ok = real_login_to_kino(
                lambda msg: kino_status.config(text=msg[-80:], fg=ACCENT_SECOND)
            )

            if ok:
                kino_logged_in = True
                kino_status.config(text="✅ Вход успешно выполнен", fg=ACCENT_SECOND)
            else:
                kino_logged_in = False
                kino_status.config(text="❌ Не удалось войти", fg="red")
                messagebox.showerror("Ошибка", "❌ Не удалось войти в Kino.pub")

        except Exception as e:
            kino_logged_in = False
            kino_status.config(text=f"Ошибка: {e}", fg="red")
            messagebox.showerror("Ошибка", f"Ошибка при авторизации: {e}")



    # после создания manager
    def _ui_set_title(item_id, text):
        tree.set(item_id, "title", text)

    manager.ui_set_title = _ui_set_title

    def reindex_rows():
        for i, item in enumerate(tree.get_children(), start=1):
            vals = list(tree.item(item, "values"))
            if len(vals) != 3: continue
            tree.item(item, values=(i, vals[1], vals[2]))

    def add_row(text, status="🟡 Подготовка..."):
        idx = len(tree.get_children()) + 1
        return tree.insert("", "end", values=(idx, text, status))

    def import_list():
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
        if not path: return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                q = line.strip()
                if not q: continue
                add_row(q, status="🟡 Подготовка...")
        reindex_rows()

    
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
        out_dir = out_dir_var.get().strip()
        manager.start_item(item_id, q, out_dir)

    def on_kino_input_click(event):
        if not kino_logged_in:
            show_login_required()
            return "break"  # не даём поставить курсор

    kino_input.bind("<Button-1>", on_kino_input_click)

    def run_queue():
        out_dir = out_dir_var.get().strip()
        manager.start_all(out_dir)

    def stop_queue():
        manager.stop_all()
    def remove_selected():
        pass
    if SHOW_QUEUE_CONTROLS:
        btn_import.config(command=import_list)
        btn_delete.config(command=remove_selected)
        btn_run.config(command=run_queue)
        btn_stop.config(command=stop_queue)

    btn_login_uc.config(command=login_to_kino)
    btn_req_login.config(command=login_to_kino)  # ← новая кнопка на экране запросов
    btn_download.config(command=start_kino_download)

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

    # ← назад к загрузчику
    btn_back_to_downloads = tk.Button(search_top, text="← К загрузкам")
    style_secondary(btn_back_to_downloads)
    btn_back_to_downloads.config(
        command=lambda: slide_switch(kino_search, kino, root, "left")
    )
    btn_back_to_downloads.pack(side="left", padx=10)

    # В главное меню
    btn_back_to_menu_from_search = tk.Button(search_top, text="В меню")
    style_secondary(btn_back_to_menu_from_search)
    btn_back_to_menu_from_search.config(
        command=lambda: slide_switch(kino_search, main_menu, root, "left")
    )
    btn_back_to_menu_from_search.pack(side="left", padx=6)
    # кнопка "Проверить NAS" на экране поиска
    btn_search_nas = tk.Button(search_top, text="Проверить NAS")
    style_secondary(btn_search_nas)
    btn_search_nas.config(command=prepare_index)
    btn_search_nas.pack(side="right", padx=12)


    # Карточка поиска
    card_search = tk.Frame(
        kino_search,
        bg=BG_SURFACE,
        highlightbackground=BORDER,
        highlightthickness=1,
    )
    card_search.place(relx=0.5, rely=0.54, anchor="center", width=780, height=640)
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
    one_frame = tk.Frame(card_search, bg=BG_SURFACE)
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
        bg="#0D1138",
        fg="white",
        insertbackground="white",
        relief="flat",
        font=("Segoe UI", 11),
    )
    search_entry.pack(side="left", fill="x", expand=True, ipady=4)

    

    # Enter в этом поле запускает поиск
    search_entry.bind("<Return>", lambda e: search_one_title())

    search_entry.pack(side="left", fill="x", expand=True, ipady=4)
    btn_search_one = tk.Button(one_row, text="Искать")
    style_secondary(btn_search_one)
    btn_search_one.pack(side="left", padx=(8, 0), ipady=2)

        # --- Поиск по списку ---
    list_frame = tk.Frame(card_search, bg=BG_SURFACE)
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
        bg="#0D1138",
        fg="white",
        insertbackground="white",
        relief="flat",
        font=("Segoe UI", 10),
        wrap="none",
    )
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


    # --- Новинки ---
    news_frame = tk.Frame(card_search, bg=BG_SURFACE)
    news_frame.pack(fill="x", padx=40, pady=(6, 0))
    btn_news = tk.Button(news_frame, text="📅 Выгрузить новинки")
    style_secondary(btn_news)
    btn_news.pack(anchor="w")

    # --- Таблица результатов поиска ---
    results_container = tk.Frame(card_search, bg=BG_SURFACE)
    results_container.pack(fill="both", expand=True, padx=32, pady=(10, 6))
    res_scroll = tk.Scrollbar(results_container)
    res_scroll.pack(side="right", fill="y")

    # БЫЛО: res_columns = ("query", "title", "year", "url")
    # СТАЛО: первая колонка — чекбокс
    res_columns = ("chk", "query", "title", "year", "url")
    tree_search = ttk.Treeview(
        results_container,
        columns=res_columns,
        show="headings",
        height=8,
        yscrollcommand=res_scroll.set,
    )
    res_scroll.config(command=tree_search.yview)

    tree_search.heading("chk",   text="",        anchor="center")
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
        """Клик по первой колонке — переключаем галочку."""
        region = tree_search.identify("region", event.x, event.y)
        if region != "cell":
            return

        col = tree_search.identify_column(event.x)  # "#1", "#2", ...
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

        drv.get(search_url)

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
                lambda d: d.execute_script("return document.readyState") == "complete"
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

            base_title_ru = re.sub(r"\s*\(\d{4}\)\s*", "", text_ru).strip()
            display_title = f"{base_title_ru} ({year})" if year else base_title_ru

            results.append((display_title, href, base_title_ru, year, eng_title))

            if max_results is not None and len(results) >= max_results:
                break

        logging.info("[PARSE] найдено %d карточек", len(results))
        return results


    def search_one_title():
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

        # ищем реальные карточки на сайте
        results = kino_search_real(title, max_results=50)

        if not results:
            messagebox.showinfo("Поиск", f"По запросу '{raw}' ничего не найдено.")
            return

        for display_title, url, base_title, y, eng_title in results:
            # В таблице можно показывать "Рус / Англ", чтобы было понятно, что это за релиз
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

    def search_by_list():
        raw_lines = list_text.get("1.0", "end").splitlines()

        # очищаем старые результаты
        for item in tree_search.get_children():
            tree_search.delete(item)
        search_meta.clear()
        checked_items.clear()

        anything = False

        for line in raw_lines:
            original = line.strip()
            if not original:
                continue

            # отбрасываем "(год)" из строки
            title, _ = split_title_year(original)
            if not title:
                continue

            # Для списка берём только лучший (первый) результат
            results = kino_search_real(title, max_results=1)
            if not results:
                logging.info("Список: для '%s' ничего не найдено", line)
                continue

            display_title, url, base_title, y, eng_title = results[0]

            shown_title = display_title
            if eng_title:
                shown_title = f"{display_title} / {eng_title}"

            item_id = tree_search.insert(
                "", "end",
                values=("☐", original, shown_title, y or "", url),
            )
            search_meta[item_id] = {
                "query": original,
                "title": base_title,
                "year":  y,
                "url":   url,
                "eng_title": eng_title,
            }

            anything = True

        if not anything:
            messagebox.showinfo("Поиск", "Список пустой или по нему ничего не найдено.")


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

    def ask_news_range(parent) -> tuple[int | None, int | None]:
        """
        Красивый диалог 'Новинки Kino.pub': 
        'Начать с страницы __  по страницу __'.
        Возвращает (start_page, end_page) или (None, None), если Cancel.
        """
        dlg = tk.Toplevel(parent)
        dlg.title("Новинки Kino.pub")
        try:
            dlg.iconbitmap("icon.ico")
        except Exception:
            pass

        dlg.transient(parent)
        dlg.grab_set()
        dlg.resizable(False, False)

        dlg.configure(bg=BG_SURFACE)

        # центрируем
        dlg.update_idletasks()
        w, h = 360, 190
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

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
        )
        hint_lbl.pack(pady=(0, 8))

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
            bg="#0D1138",
            fg="white",
            insertbackground="white",
            relief="flat",
            font=("Segoe UI", 11),
            justify="center",
        )
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
            bg="#0D1138",
            fg="white",
            insertbackground="white",
            relief="flat",
            font=("Segoe UI", 11),
            justify="center",
        )
        end_entry.pack(side="left")

        error_lbl = tk.Label(
            dlg,
            text="",
            bg=BG_SURFACE,
            fg="red",
            font=("Segoe UI", 9),
        )
        error_lbl.pack(pady=(2, 0))

        res = {"start": None, "end": None}

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
        return res["start"], res["end"]

    # новинки пока оставим заглушкой
    def load_news():
        # красивый диалог "с / по"
        start_page, end_page = ask_news_range(root)
        if start_page is None or end_page is None:
            return

        # очищаем старые результаты
        for item in tree_search.get_children():
            tree_search.delete(item)
        search_meta.clear()

        # тянем каждую страницу /new?page=N
        for page in range(start_page, end_page + 1):
            try:
                page_results = kino_fetch_news_page(page, max_results=None)
            except Exception as e:
                logging.error("Ошибка при загрузке новинок страницы %s: %s", page, e)
                continue

            for display_title, url, base_title, year, eng_title in page_results:
                if not year:
                    year = fetch_year_from_card(url)

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


        # привязываем кнопку
    btn_news.config(command=load_news)

    # --- Кнопка: отправить выбранные в очередь скачивания ---
    bottom_search = tk.Frame(card_search, bg=BG_SURFACE)
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

        out_dir = out_dir_var.get().strip()
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

            manager.start_item(row_id, url, out_dir)
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
            clear_requests()
            req_text.insert("1.0", "\n".join(lines))

            # 3) переключаем экран
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
            menu = tk.Menu(entry, tearoff=0, bg="#10163D", fg="white",
                           activebackground=ACCENT, activeforeground="white",
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
