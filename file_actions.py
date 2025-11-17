import ctypes
import os
import re
import shutil
import string
import time
import tkinter as tk
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from tkinter import Label, Toplevel, filedialog, messagebox, ttk
EFU_FILE = "all_movies.efu"

DEST_FOLDER = r"C:\MoviesFound"
VIDEO_EXTENSIONS = [".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv"]
RELATED_EXTENSIONS = [".jpg", ".jpeg", ".png", ".nfo"]
RELATED_SUFFIXES = ["", "-poster", "-fanart", "-clearlogo", "-landscape"]
DRIVE_LETTER_RE = re.compile(r"^([A-Za-z]):\\(.*)")


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_network_drive(drive_letter):
    DRIVE_REMOTE = 4
    return ctypes.windll.kernel32.GetDriveTypeW(f"{drive_letter}:\\") == DRIVE_REMOTE


@lru_cache(maxsize=26)
def _mapped_drive_unc(letter: str) -> str | None:
    r"""
    Возвращает UNC-рутиз для сопоставленной буквы диска:
    'X' -> r'\\server\share'
    """
    try:
        mpr = ctypes.WinDLL("mpr.dll")
        WNetGetConnectionW = mpr.WNetGetConnectionW
        WNetGetConnectionW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        WNetGetConnectionW.restype = wintypes.DWORD

        local = f"{letter}:"
        NO_ERROR = 0
        ERROR_MORE_DATA = 234
        ERROR_NOT_CONNECTED = 2250

        buf_len = wintypes.DWORD(1024)
        remote_buf = ctypes.create_unicode_buffer(buf_len.value)
        res = WNetGetConnectionW(local, remote_buf, ctypes.byref(buf_len))
        if res == NO_ERROR:
            return remote_buf.value or None
        if res == ERROR_MORE_DATA:
            remote_buf = ctypes.create_unicode_buffer(buf_len.value)
            res = WNetGetConnectionW(local, remote_buf, ctypes.byref(buf_len))
            if res == NO_ERROR:
                return remote_buf.value or None
        if res == ERROR_NOT_CONNECTED:
            return None
        return None
    except Exception:
        return None
        
def normalize_name(name: str) -> str:
    m = re.search(r"\(\d{4}\)", name)
    return name[: m.end()].lower().strip() if m else name.lower().strip()

def to_unc(path: str) -> str:
    # X:\dir\file -> \\server\share\dir\file (если это сопоставленный сетевой диск)
    m = re.match(r"^([a-zA-Z]):\\(.*)$", path)
    if not m:
        return path  # уже UNC или не буква-диск
    letter, tail = m.group(1).upper(), m.group(2)
    unc_root = _mapped_drive_unc(letter)
    if unc_root:
        return unc_root.rstrip("\\/") + "\\" + tail
    return path  # не сопоставленный диск — оставляем как есть


def is_network_path(path: str) -> bool:
    if not path:
        return False
    if path.startswith("\\\\") or path.lower().startswith("smb://"):
        return True
    m = DRIVE_LETTER_RE.match(path)
    if m:
        letter = m.group(1).upper()
        try:
            if is_network_drive(letter):
                return True
        except Exception:
            pass
        return _mapped_drive_unc(letter) is not None
    return False

def export_and_load_index(year_entry: tk.Entry):
    global movie_index, index_loaded
    try:
        cmd = ["es.exe", "ext:mp4", "-n", "9999999", "-export-efu", EFU_FILE]
        subprocess.run(cmd, check=True)
        movie_index = load_index_from_efu(EFU_FILE)
        index_loaded = True
        year_entry.config(state="normal")
        messagebox.showinfo("Индекс", f"Загружено {len(movie_index)} mp4 файлов")
    except Exception as e:
        messagebox.showerror("Ошибка", str(e))

def load_index_from_efu(file_path="all_movies.efu"):
    index = []
    exts = {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv", ".jpg", ".jpeg", ".png", ".nfo"}
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("filename"):
                    continue
                full_path = line.split('"')[1] if '"' in line else line
                # быстрый отсев по расширению
                ext = os.path.splitext(full_path)[1].lower()
                if ext not in exts:
                    continue
                # в UNC только если это буква-диск
                full_path_unc = full_path if full_path.startswith("\\\\") else to_unc(full_path)
                name = os.path.basename(full_path_unc).lower()
                index.append((name, full_path_unc))
    except Exception as e:
        print(f"[EFU] Ошибка чтения EFU: {e}")
    return index


def get_available_drives():
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def select_disks(root):
    def on_confirm():
        selected = [v.get() for v in vars_ if v.get()]
        if not selected:
            messagebox.showwarning("Выбор дисков", "Выберите хотя бы один диск.")
            return
        nonlocal selected_disks
        selected_disks = selected
        win.destroy()

    selected_disks = []
    win = Toplevel(root)
    win.title("Выбор дисков")
    Label(win, text="Выберите диски:", font=("Arial", 12)).pack(pady=10)
    vars_ = []
    for drive in get_available_drives():
        v = tk.StringVar()
        tk.Checkbutton(win, text=drive, variable=v, onvalue=drive, offvalue="").pack(
            anchor="w", padx=20
        )
        vars_.append(v)
    tk.Button(win, text="Сканировать", command=on_confirm).pack(pady=10)
    win.wait_window()
    return selected_disks


def scan_all_disks(disks, root):
    index = []
    skip_dirs = [
        "$recycle.bin",
        "system volume information",
        ".git",
        "windows",
        "program files",
        "appdata",
    ]

    progress = Toplevel(root)
    progress.title("Сканирование...")
    label = Label(progress, text="Подготовка...", font=("Arial", 12))
    label.pack(pady=30)
    progress.update()

    for disk in disks:
        label.config(text=f"Сканируем: {disk}")
        progress.update()
        for root_dir, dirs, files in os.walk(disk):
            dirs[:] = [
                d
                for d in dirs
                if not any(s in os.path.join(root_dir, d).lower() for s in skip_dirs)
            ]
            for f in files:
                if any(f.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                    index.append((f.lower(), os.path.join(root_dir, f)))

    label.config(text="Сканирование завершено")
    progress.after(1000, progress.destroy)
    return index


def get_files_to_copy(src_path, include_related=False, index=None):
    files_to_copy = [src_path]
    if include_related and index:
        base_title = normalize(os.path.splitext(os.path.basename(src_path))[0])
        seen_paths = set()
        for name, path in index:
            if not is_network_path(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in RELATED_EXTENSIONS:
                continue
            name_no_ext = normalize(os.path.splitext(name)[0])
            for suffix in RELATED_SUFFIXES:
                if name_no_ext == normalize(base_title + suffix) and path not in seen_paths:
                    files_to_copy.append(path)
                    seen_paths.add(path)
                    break
    return files_to_copy


import subprocess
import os
import time
from shutil import which
from pathlib import Path
CREATE_NO_WINDOW = 0x08000000  # ← подавляет открытие окна cmd
def _robocopy_single(src_path: str, dst_path: str) -> tuple[int, str]:
    """
    Копирует один файл robocopy'ем без появления окна консоли.
    Возвращает (код_возврата, stdout+stderr). Успешные коды: 0–7.
    """
    src = Path(src_path)
    dst = Path(dst_path)
    src_dir = str(src.parent)
    dst_dir = str(dst.parent)
    fname = src.name

    os.makedirs(dst_dir, exist_ok=True)

    cmd = [
        "robocopy",
        src_dir,
        dst_dir,
        fname,
        "/COPY:DAT",
        "/R:1",
        "/W:1",
        "/NP",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/MT:16",  # многопоточное копирование
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=False,
        creationflags=CREATE_NO_WINDOW  # 🔸 окно cmd не открывается
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def _cmd_copy_single(src_path: str, dst_path: str) -> tuple[int, str]:
    """
    Fallback на встроенный copy без появления окна консоли.
    """
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    cmd = ["cmd", "/c", "copy", "/Y", "/B", src_path, dst_path]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=False,
        creationflags=CREATE_NO_WINDOW  # 🔸 скрыть окно cmd.exe
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def copy_single_file(src_path, dst_path, progress_callback=None, file_index=0, total_files=1):
    """
    Быстрое копирование «как Проводник»:
      1) robocopy (если доступен) — оптимален для SMB/сетей и больших файлов
      2) fallback: cmd /c copy /B /Y
    Возвращает: (copied_bytes, speed_mb_s)
    """
    start = time.time()
    copied = 0

    # сначала пробуем robocopy
    used_robocopy = False
    if which("robocopy"):
        rc, out = _robocopy_single(src_path, dst_path)
        # успешные коды robocopy: 0–7
        if rc <= 7:
            used_robocopy = True
        else:
            # даём текст ошибки наверх — пусть отработает fallback и/или поднимем исключение ниже
            robocopy_error = f"robocopy failed (code {rc}). Output:\n{out}"
        # если robocopy не скопировал (например, файл уже идентичен), всё равно считаем успех
        # и считаем размер назначения, если он есть.
    if not used_robocopy:
        rc, out = _cmd_copy_single(src_path, dst_path)
        if rc != 0:
            raise RuntimeError(f"COPY failed (code {rc}). Output:\n{out}")

    # финальные метрики
    if os.path.exists(dst_path):
        copied = os.path.getsize(dst_path)
    elapsed = max(time.time() - start, 1e-6)
    speed = copied / 1024 / 1024 / elapsed

    if progress_callback:
        progress_callback(
            file_index=file_index,
            filename=os.path.basename(src_path),
            copied=copied,
            total=copied,
            speed=speed,
            total_files=total_files,
        )
    return copied, speed



def copy_file_to_custom_folder(src_path, include_related=False, index=None, dest_folder=None):
    if not dest_folder:
        dest_folder = filedialog.askdirectory(title="Выберите папку")
        if not dest_folder:
            return

    files_to_copy = get_files_to_copy(src_path, include_related, index)
    total_files = len(files_to_copy)

    win = Toplevel()
    win.title("Копирование файлов")
    label = Label(win, text="Копирование...", font=("Arial", 12))
    label.pack(pady=10)
    bar = ttk.Progressbar(win, length=300, mode="determinate", maximum=total_files)
    bar.pack(pady=10)
    win.update()

    for i, file_path in enumerate(files_to_copy, 1):
        try:
            dst_path = os.path.join(dest_folder, os.path.basename(file_path))
            copied, speed = copy_single_file(file_path, dst_path)
            label.config(
                text=f"Файл {i}/{total_files}: {copied / 1024 / 1024:.2f} MB @ {speed:.2f} MB/s"
            )
            bar["value"] = i
            win.update()
        except Exception as e:
            messagebox.showerror("Ошибка", f"{file_path}\n{e}")

    label.config(text="Копирование завершено")
    bar["value"] = total_files
    win.after(1500, win.destroy)


def move_file(src_path):
    Path(DEST_FOLDER).mkdir(parents=True, exist_ok=True)
    dst_path = os.path.join(DEST_FOLDER, os.path.basename(src_path))
    try:
        shutil.copy2(src_path, dst_path)
    except Exception as e:
        messagebox.showerror("Ошибка копирования", str(e))
