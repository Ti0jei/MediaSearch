import time
import os
import tkinter as tk
from tkinter import ttk, messagebox
import itertools
import logging

APP_VERSION = "1.1"



class ProgressWindow:
    def __init__(self, root, title="Копирование", total=1):
        self.root = root
        self.total = total
        self.current = 0
        self.start_time = time.time()
        self.cancelled = False  # ← флаг отмены, доступен другим потокам

        # окно
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.geometry("480x180")
        self.win.resizable(False, False)

        try:
            self.win.iconbitmap("icon.ico")
        except Exception:
            pass

        self.label = tk.Label(
            self.win,
            text="Подготовка...",
            font=("Segoe UI", 12, "bold"),
            bg="#F5F6F7"   # ← тот же фон, что и у окна
        )
        self.label.pack(pady=(20, 5))


        # прогрессбар + проценты
        self.progress = ttk.Progressbar(self.win, orient="horizontal", length=360, mode="determinate")
        self.progress.pack(pady=5)
        self.percent_label = tk.Label(self.win, text="0%", font=("Segoe UI", 10), fg="#555")
        self.percent_label.pack()

        # фон окна и базовые цвета
        self.win.configure(bg="#F5F6F7")  # мягкий серо-голубой фон
        accent_color = "#4A90E2"         # фирменный синий

        # кнопка отмены (в цвет общей темы)
        btn_cancel = tk.Button(
            self.win,
            text="Отмена",
            bg=accent_color,
            fg="white",
            activebackground="#357ABD",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            width=10,
            command=self._cancel
        )
        btn_cancel.pack(pady=(8, 12))


        try:
            self.win.iconbitmap("icon.ico")
        except Exception:
            pass

        # надписи
        self.label = tk.Label(self.win, text="Подготовка...", font=("Segoe UI", 12, "bold"))
        self.label.pack(pady=(20, 5))
                # ---- Версия программы ----
        self.version_label = tk.Label(
            self.win,
            text=f"v{APP_VERSION}",
            font=("Segoe UI", 8),
            fg="#6B6B6B",
            bg="#F5F6F7",
            anchor="se"
        )
        self.version_label.place(relx=1.0, rely=1.0, x=-10, y=-8, anchor="se")

        self.version_label.place(relx=1.0, rely=1.0, x=-8, y=-4, anchor="se")

        self.sub_label = tk.Label(self.win, text="", font=("Segoe UI", 10), fg="#444")
        self.sub_label.pack(pady=(0, 5))
                # кнопка отмены
        self.cancelled = False
        btn_cancel = tk.Button(
            self.win,
            text="Отмена",
            bg="#4A90E2",   # основной фирменный цвет
            fg="white",
            activebackground="#357ABD",  # слегка затемнённый оттенок при нажатии
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            width=10,
            command=self._cancel
)

        btn_cancel.pack(pady=(5, 8))


        # базовые настройки окна — можно сворачивать и работать с другими окнами
        self.win.transient(self.root)
        self.win.attributes("-topmost", False)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        # автоцентрирование + плавное появление
        self._center_and_fade_in()

        # включаем пульсацию
        self._pulse_cycle = itertools.cycle(["", ".", "..", "..."])
        self._pulse()
    def _cancel(self):
        """Отмена копирования — безопасно сигнализирует фоновому потоку."""
        self.cancelled = True
        self.label.config(text="Отмена... Дождитесь завершения текущего файла.")
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
        """Добавляет пульсирующие точки к надписи."""
        if not self.win.winfo_exists():
            return
        dots = next(self._pulse_cycle)
        base = self.label.cget("text").split("...")[0].rstrip(".")
        self.label.config(text=f"{base}{dots}")
        self.win.after(400, self._pulse)

    def _on_close(self):
        """Крестик сворачивает окно, не закрывая."""
        self.win.iconify()

    def update_progress(
        self,
        step,
        current_file=None,
        copied_bytes=None,
        total_bytes=None,
        speed=None,
    ):
        """Обновляет окно прогресса — с процентами и скоростью."""
        if self.cancelled:
            return

        self.current = step
        if current_file:
            short_name = os.path.basename(current_file)
            self.label.config(text=f"Копируется: {short_name}")

        if total_bytes and copied_bytes:
            percent = int((copied_bytes / total_bytes) * 100)
            self.progress["value"] = percent
            self.percent_label.config(text=f"{percent}% ({speed:.2f} MB/s)" if speed else f"{percent}%")

        self.win.update_idletasks()


    def _shake(self):
        """Небольшая вибрация в конце — как эффект завершения."""
        x, y = self.win.winfo_x(), self.win.winfo_y()
        for i in range(6):
            dx = (-5, 5)[i % 2]
            self.win.geometry(f"+{x+dx}+{y}")
            self.win.update()
            self.win.after(40)
        self.win.geometry(f"+{x}+{y}")

    def close(self):
        """Закрытие окна с эффектом и уведомлением."""
        try:
            self._shake()
            if not self.cancelled:
                messagebox.showinfo("Готово", "Все файлы успешно скопированы")
            else:
                messagebox.showinfo("Копирование", "Копирование отменено пользователем")
        except Exception:
            pass
        self.root.after(0, self.win.destroy)

