import time
import os
import tkinter as tk
from tkinter import ttk
import itertools

APP_VERSION = "1.1"

# --- Палитра как в основном приложении ---
BRAND_SKY     = "#8AADD3"   # светло-голубой
BRAND_MAGENTA = "#A24BA1"   # маджента
BRAND_NAVY    = "#1C226B"   # глубокий синий

BG_WINDOW  = "#0B0F2A"      # общий фон
BG_SURFACE = "#13183A"      # панели/карточки
BORDER     = "#222A5A"      # границы
TEXT       = "#E9ECF7"
SUBTEXT    = "#A8B2D9"

ACCENT        = BRAND_MAGENTA
ACCENT_HOVER  = "#B866B7"
ACCENT_SECOND = BRAND_SKY


def style_secondary(btn: tk.Button):
    btn.config(
        bg="#18204C", fg=ACCENT_SECOND,
        activebackground="#1E275A", activeforeground=ACCENT_SECOND,
        relief="flat", borderwidth=0, cursor="hand2",
        font=("Segoe UI", 10, "bold"),
        padx=14, pady=6,
        highlightbackground=ACCENT_SECOND, highlightthickness=1,
    )


class ProgressWindow:
    def __init__(self, root, title="Копирование", total=1):
        self.root = root
        self.total = total
        self.current = 0
        self.start_time = time.time()
        self.cancelled = False

        # --- окно ---
        self.win = tk.Toplevel(root)
        self.alive = True                               # ← окно «живое»
        self.win.bind("<Destroy>", lambda e: setattr(self, "alive", False))
        self.win.title(title)
        self.win.geometry("520x200")
        self.win.resizable(False, False)
        self.win.configure(bg=BG_WINDOW)

        try:
            self.win.iconbitmap("icon.ico")
        except Exception:
            pass

        # --- стиль прогрессбара под тёмную тему ---
        style = ttk.Style(self.win)
        style.theme_use("clam")
        style.configure(
            "Movie.Horizontal.TProgressbar",
            troughcolor=BG_WINDOW,
            background=ACCENT,
            bordercolor=BG_SURFACE,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

        # --- карточка внутри окна ---
        frame = tk.Frame(
            self.win,
            bg=BG_SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        frame.pack(fill="both", expand=True, padx=14, pady=12)

        # заголовок
        tk.Label(
            frame,
            text="Копирование файлов",
            font=("Segoe UI Semibold", 12),
            bg=BG_SURFACE,
            fg=ACCENT_SECOND,
            anchor="w",
        ).pack(anchor="w", padx=10, pady=(10, 2))

        # основная надпись (меняется по ходу копирования)
        self.label = tk.Label(
            frame,
            text="Подготовка...",
            font=("Segoe UI", 11, "bold"),
            bg=BG_SURFACE,
            fg=TEXT,
            anchor="w",
        )
        self.label.pack(anchor="w", padx=10, pady=(0, 4))

        # подзаголовок (скорость/объём)
        self.sub_label = tk.Label(
            frame,
            text="",
            font=("Segoe UI", 9),
            bg=BG_SURFACE,
            fg=SUBTEXT,
            anchor="w",
        )
        self.sub_label.pack(anchor="w", padx=10, pady=(0, 6))

        # прогрессбар + проценты
        self.progress = ttk.Progressbar(
            frame,
            orient="horizontal",
            length=360,
            mode="determinate",
            style="Movie.Horizontal.TProgressbar",
        )
        self.progress.pack(padx=10, pady=(0, 4), fill="x")
        self.percent_label = tk.Label(
            frame,
            text="0%",
            font=("Segoe UI", 9),
            bg=BG_SURFACE,
            fg=SUBTEXT,
            anchor="e",
        )
        self.percent_label.pack(anchor="e", padx=10)

        # нижняя панель: кнопка + версия
        bottom = tk.Frame(frame, bg=BG_SURFACE)
        bottom.pack(fill="x", pady=(6, 8), padx=10)

        self.version_label = tk.Label(
            bottom,
            text=f"v{APP_VERSION}",
            font=("Segoe UI", 8),
            fg=SUBTEXT,
            bg=BG_SURFACE,
            anchor="w",
        )
        self.version_label.pack(side="left")

        self.cancel_btn = tk.Button(
            bottom,
            text="Отмена",
            command=self._cancel,
        )
        style_secondary(self.cancel_btn)
        self.cancel_btn.pack(side="right")

        # базовые настройки окна — можно сворачивать и работать с другими окнами
        self.win.transient(self.root)
        self.win.attributes("-topmost", False)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        # автоцентрирование + плавное появление
        self._center_and_fade_in()

        # пульсирующие точки для "Подготовка..."
        self._pulse_cycle = itertools.cycle(["", ".", "..", "..."])
        self._pulse()

    # --------- служебные методы ---------
    def _cancel(self):
        """Отмена копирования — безопасный сигнал фоновому потоку."""
        self.cancelled = True
        self.label.config(text="Отмена... Дождитесь завершения текущего файла.")
        self.sub_label.config(text="")
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.win.update()

    def _center_and_fade_in(self):
        """Расположить окно по центру и сделать мягкое появление."""
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        x = (self.win.winfo_screenwidth() - w) // 2
        y = (self.win.winfo_screenheight() - h) // 2
        self.win.geometry(f"+{x}+{y}")

        self.win.attributes("-alpha", 0.0)
        for i in range(0, 11):
            self.win.attributes("-alpha", i / 10)
            self.win.update()
            self.win.after(20)

    def _pulse(self):
        """Добавляет пульсирующие точки к надписи, пока окно живо."""
        if not getattr(self, "alive", False) or not self.win.winfo_exists() or self.cancelled:
            return

        dots = next(self._pulse_cycle)
        base = self.label.cget("text").split("...")[0].rstrip(".")
        self.label.config(text=f"{base}{dots}")
        self.win.after(400, self._pulse)

    def _on_close(self):
        """Крестик сворачивает окно (не уничтожаем, чтобы не словить TclError)."""
        try:
            self.win.iconify()
        except Exception:
            pass


    # --------- публичный интерфейс ---------
    def update_progress(
        self,
        step,
        current_file=None,
        copied_bytes=None,
        total_bytes=None,
        speed=None,
        status_text=None,
        **_kwargs
    ):
        """Обновляет окно прогресса — с процентами и скоростью."""
        # окно закрыто/уничтожается — игнорируем обновление
        if not getattr(self, "alive", False):
            return
        if not self.win.winfo_exists():
            return
        if self.cancelled:
            return


        self.current = step

        try:
            if current_file:
                short_name = os.path.basename(current_file)
                self.label.config(text=f"Копируется: {short_name}")
            if status_text:
                self.sub_label.config(text=status_text)

            if total_bytes and total_bytes > 0 and copied_bytes is not None:
                percent = max(0, min(100, int((copied_bytes / total_bytes) * 100)))
                self.progress["value"] = percent

                txt = f"{percent}%"
                if speed is not None:
                    txt += f" ({speed:.2f} MB/s)"
                self.percent_label.config(text=txt)

                try:
                    mb_done = copied_bytes / (1024 * 1024)
                    mb_total = total_bytes / (1024 * 1024)
                    self.sub_label.config(text=f"{mb_done:.1f} MB из {mb_total:.1f} MB")
                except Exception:
                    pass

            self.win.update_idletasks()
        except tk.TclError:
            # виджет(ы) уже уничтожены — тихо выходим
            return


    def _shake(self):
        """Небольшая «вибрация» в конце — эффект завершения."""
        try:
            x, y = self.win.winfo_x(), self.win.winfo_y()
            for i in range(6):
                dx = (-5, 5)[i % 2]
                self.win.geometry(f"+{x+dx}+{y}")
                self.win.update()
                self.win.after(40)
            self.win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def close(self):
        """Аккуратно завершает окно без системного белого messagebox."""
        try:
            self._shake()

            if self.cancelled:
                end_text = "Копирование отменено пользователем"
            else:
                end_text = "Все файлы успешно скопированы"
                try:
                    self.progress["value"] = 100
                    self.percent_label.config(text="100%")
                except Exception:
                    pass

            self.label.config(text=end_text)
            self.sub_label.config(text="")
            self.win.update_idletasks()

            # даём пользователю секунду увидеть статус и закрываем
            def _safe_destroy():
                self.alive = False
                try:
                    self.win.destroy()
                except Exception:
                    pass

            # даём пользователю секунду увидеть статус и закрываем
            self.win.after(900, _safe_destroy)

        except Exception:
            self.root.after(0, self.win.destroy)
