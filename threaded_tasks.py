import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from file_actions import copy_single_file, get_files_to_copy
import logging



def show_loading(root, title, message):
    """Небольшое модальное окно 'Подготовка...' (используется для отдельных задач)."""
    win = tk.Toplevel(root)
    win.title(title)
    win.geometry("300x100")
    win.resizable(False, False)

    try:
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(__file__)
        icon_path = os.path.join(base_dir, "icon.ico")
        if os.path.exists(icon_path):
            win.iconbitmap(icon_path)
    except Exception:
        pass

    # делаем модальным, но при желании можно убрать grab_set(),
    # если нужно позволить пользователю спокойно сворачивать и работать параллельно
    win.transient(root)
    win.grab_set()
    win.lift()
    win.attributes("-topmost", True)

    tk.Label(win, text=message, font=("Arial", 12)).pack(expand=True, pady=20)
    win.update()
    return win


def threaded_load_titles(root, load_titles_fn):
    """Пример запуска долгой операции в фоне."""
    def task():
        load_titles_fn()
    threading.Thread(target=task, daemon=True).start()


def threaded_save_checked(root, found_files, checked_vars, movie_index, include_related):
    """Асинхронное копирование выбранных фильмов с прогрессом и поддержкой отмены."""
    from progress_window import ProgressWindow  # избегаем циклический импорт
    import threading, logging

    dest = filedialog.askdirectory(title="Куда сохранить отмеченные фильмы")
    if not dest:
        return

    # --- формируем список файлов ---
    selected_sources = [path for i, (_, path) in enumerate(found_files) if checked_vars[i].get()]
    if not selected_sources:
        messagebox.showinfo("Копирование", "Не выбрано ни одного файла.")
        return

    files_to_copy, seen = [], set()
    for src in selected_sources:
        for p in get_files_to_copy(src, include_related, movie_index):
            if p not in seen:
                files_to_copy.append(p)
                seen.add(p)

    progress = ProgressWindow(root, title="Копирование", total=len(files_to_copy))
    progress.update_progress(0, current_file="Подготовка... пожалуйста, подождите")

    # --- внутренняя функция обновления GUI через after() ---
    def safe_update(step, *args, **kwargs):
        if root.winfo_exists():
            def _safe_ui_update():
                if getattr(progress, "alive", False):
                    try:
                        progress.update_progress(step, *args, **kwargs)
                    except Exception:
                        pass

            root.after(0, _safe_ui_update)

    # Подсчёт суммарного объёма и размеров каждого файла — в фоне
    total_bytes = 0
    sizes = []
    prefix_bytes = []  # кумулятивные суммы для быстрого расчёта «сколько уже скопировано до текущего файла»

    def calc_total_size():
        nonlocal total_bytes, sizes, prefix_bytes
        sizes = [os.path.getsize(p) if os.path.exists(p) else 0 for p in files_to_copy]
        prefix_bytes = []
        run = 0
        for s in sizes:
            prefix_bytes.append(run)
            run += s
        total_bytes = run
        progress.total_bytes = total_bytes

    threading.Thread(target=calc_total_size, daemon=True).start()

    # Обновление строки состояния (вызывается copy_single_file по завершению файла)
    def progress_callback(file_index, filename, copied, total, speed, total_files):
        # если кумулятивная сумма уже готова — берём быстро; если ещё нет — считаем на лету
        if prefix_bytes and file_index < len(prefix_bytes):
            already = prefix_bytes[file_index]
        else:
            already = sum(os.path.getsize(files_to_copy[j]) if os.path.exists(files_to_copy[j]) else 0
                          for j in range(file_index))

        current_copied = already + copied
        status = f"{copied / 1024 / 1024:.2f} MB из {total / 1024 / 1024:.2f} MB @ {speed:.2f} MB/s"

        safe_update(
            file_index + 1,
            current_file=filename,
            status_text=status,
            copied_bytes=current_copied,
            total_bytes=total_bytes,
            speed=speed,
        )


    # Основная фоновая задача копирования
    def task():
        for i, file_path in enumerate(files_to_copy):
            # если нажата «Отмена» — выходим из цикла
            if getattr(progress, "cancelled", False):
                safe_update(
                    i + 1,
                    current_file="Отменено пользователем",
                    status_text=""
                )
                break


            filename = os.path.basename(file_path)
            dst_path = os.path.join(dest, filename)

            try:
                copy_single_file(
                    file_path,
                    dst_path,
                    progress_callback=progress_callback,
                    file_index=i,
                    total_files=len(files_to_copy),
                )
            except Exception as e:
                safe_update(
                    i + 1,
                    current_file=filename,
                    status_text=f"Ошибка: {e}"
                )


        # Закрываем окно (ProgressWindow сам покажет «Готово»/анимацию, если так настроено)
                
        root.after(0, progress.close)


    threading.Thread(target=task, daemon=True).start()
