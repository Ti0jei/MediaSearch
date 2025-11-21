import os
import time
import logging
import tkinter as tk
import shutil
import json
import subprocess
from auto_update import check_for_updates_async
from download_manager import DownloadManager
from uc_driver import DriverPool
from tkinter import messagebox, filedialog
from pathlib import Path
from file_actions import export_and_load_index, normalize_name
from file_actions import load_index_from_efu
from threaded_tasks import threaded_save_checked
from kino_pub_downloader import login_to_kino as real_login_to_kino
from urllib.parse import urljoin
# --- Настройки (последняя папка сохранения и т.п.) ---
SETTINGS_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "MediaSearch")
os.makedirs(SETTINGS_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


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
    for f in (main_menu, finder, kino):
        f.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
        f.place_forget()
    main_menu.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)

    # ========== Главный экран (чистый тёмный) ==========
    card = tk.Frame(main_menu, bg=BG_SURFACE, highlightbackground=BORDER, highlightthickness=1)
    card.place(relx=0.5, rely=0.5, anchor="center", width=520, height=340)

    # тонкая акцентная полоса сверху карточки
    tk.Frame(card, bg=ACCENT, height=3).pack(fill="x", side="top")
    tk.Label(card, text="🎬 MOVIE TOOLS", bg=BG_SURFACE, fg=ACCENT,
             font=("Segoe UI Semibold", 22)).pack(pady=(26, 8))
    tk.Label(card, text="Управляй своей медиатекой легко и красиво",
             bg=BG_SURFACE, fg=SUBTEXT, font=("Segoe UI", 11)).pack(pady=(0, 26))

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
        wrap = tk.Frame(parent, bg=BG_SURFACE); wrap.pack(fill="x", padx=60, pady=8)
        btn = tk.Button(wrap, text=text, relief="flat", borderwidth=0,
                        font=("Segoe UI Semibold", 13), cursor="hand2",
                        padx=18, pady=10, highlightthickness=0)
        style_primary(btn)
        btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_HOVER))
        btn.bind("<Leave>", lambda e: btn.config(bg=ACCENT))
        btn.config(command=command); btn.pack(fill="x", ipady=3)
        return btn

    neon_button(card, "🔎 Поиск фильмов по году", lambda: slide_switch(main_menu, finder, root, "right"))
    neon_button(card, "🎞 Работа с Kino.pub",   lambda: slide_switch(main_menu, kino,   root, "right"))

    tk.Frame(main_menu, bg=BORDER, height=1).place(relx=0, rely=1.0, 
                                                   relwidth=1.0, y=-26, anchor="sw")
    footer_label = tk.Label(main_menu, text="Created by Ti0jei v1.0.3",
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
        if finder.winfo_ismapped(): on_search()
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

    # кнопка "В меню"
    btn_back_kino = tk.Button(kino_top, text="← В меню")
    style_secondary(btn_back_kino)
    btn_back_kino.config(command=lambda: slide_switch(kino, main_menu, root, "left"))
    btn_back_kino.pack(side="left", padx=10)

    # кнопка "Обновить профиль"
    btn_reset_profile = tk.Button(kino_top, text="Обновить профиль")
    style_secondary(btn_reset_profile)
    btn_reset_profile.config(command=reset_kino_profile)
    btn_reset_profile.pack(side="left", padx=6)

    # кнопка "Войти в Kino.pub" — справа вверху
    btn_login_uc = tk.Button(kino_top, text="Войти в Kino.pub")
    style_secondary(btn_login_uc)
    btn_login_uc.pack(side="right", padx=12)



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
        state="disabled",
    )
    kino_input.pack(side="left", fill="x", expand=True, ipady=4)

    # кнопка "Скачать" такого же размера/стиля, как "Выбрать"
    btn_download = tk.Button(input_row, text="⬇️ Скачать", state="disabled")
    style_secondary(btn_download)          # тот же стиль, что и у "Выбрать"
    btn_download.pack(side="left", padx=(8, 0), ipady=2)


    path_frame = tk.Frame(top_part, bg=BG_SURFACE); path_frame.pack(fill="x", padx=40, pady=(10, 8))
    tk.Label(path_frame, text="📂 Папка сохранения:", bg=BG_SURFACE, fg=SUBTEXT,
             font=("Segoe UI", 10)).pack(anchor="w")
    settings = load_settings()
    default_dir = settings.get("last_download_dir") or os.path.join(os.getcwd(), "Downloads")
    out_dir_var = tk.StringVar(value=default_dir)
    path_entry = tk.Entry(path_frame, textvariable=out_dir_var, bg="#0D1138", fg="white",
                          insertbackground="white", relief="flat", font=("Segoe UI", 10), state="disabled")
    path_entry.pack(side="left", fill="x", expand=True, ipady=4, pady=(4, 0))

    def choose_folder():
        d = filedialog.askdirectory(title="Выберите папку сохранения")
        if d:
            out_dir_var.set(d)
            s = load_settings()
            s["last_download_dir"] = d
            save_settings(s)


    choose_btn = tk.Button(path_frame, text="Выбрать", command=choose_folder); style_secondary(choose_btn)
    choose_btn.config(state="disabled"); choose_btn.pack(side="left", padx=(8, 0))

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

        # Добиваем процессы ffmpeg / Chromium (Windows)
        if os.name == "nt":
            for proc in ("ffmpeg.exe",
                         "chromium.exe",          # ← основной процесс Chromium
                         "chrome.exe",            # ← на всякий случай, если UC запустит так
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

    def login_to_kino():
        try:
            kino_status.config(text="⏳ Инициализация входа.", fg=ACCENT_SECOND)

            ok = real_login_to_kino(
                lambda msg: kino_status.config(text=msg[-80:], fg=ACCENT_SECOND)
            )

            if ok:
                kino_status.config(text="✅ Вход успешно выполнен", fg=ACCENT_SECOND)
                kino_input.config(state="normal")
                choose_btn.config(state="normal")
                path_entry.config(state="normal")
                btn_download.config(state="normal")
            else:
                kino_status.config(text="❌ Не удалось войти", fg="red")
                messagebox.showerror("Ошибка", "❌ Не удалось войти в Kino.pub")

        except Exception as e:
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
        q = kino_input.get().strip()
        if not q:
            messagebox.showerror("Ошибка", "Введите запрос или URL карточки."); return
        item_id = add_row(q, status="🟡 Подготовка...")
        kino_input.delete(0, "end")
        out_dir = out_dir_var.get().strip()
        manager.start_item(item_id, q, out_dir)

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
    btn_download.config(command=start_kino_download)


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
