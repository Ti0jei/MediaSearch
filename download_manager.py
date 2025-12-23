import os
import sys
import time
import re
import logging
import threading
import subprocess
import tempfile
import uuid
import shutil
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import queue

from uc_driver import _safe_get_driver
from kino_pub_downloader import download

_AUDIO_SELECT_LOCK = threading.Lock()


class DownloadManager:
    def __init__(
        self,
        root,
        tree,
        counter_label,
        max_parallel=2,
        pool=None,
        notify_cb=None,
        history_cb=None,
        audio_select_cb=None,
    ):
        self.root = root
        self.tree = tree
        self.url_by_item = {}  # item_id -> original URL
        self.out_path_by_item = {}  # item_id -> final output path (mp4)
        self.name_override_by_item = {}  # item_id -> display_name override (без .mp4)
        self.out_dir_by_item = {}  # item_id -> out_dir (для сериалов/разных папок)
        self.counter_label = counter_label
        self.MAX_PARALLEL = max_parallel
        self.pool = pool
        self.notify_cb = notify_cb
        self.history_cb = history_cb
        self.audio_select_cb = audio_select_cb or self._audio_select_dialog

        self.sema = threading.Semaphore(self.MAX_PARALLEL)
        self.lock = threading.Lock()
        self.active = 0
        self.stop_flag = False
        self.threads = {}  # item_id -> Thread
        self.cancel_events = {}  # item_id -> Event
        self.final_status = {}  # item_id -> "✅"/"❌"/"⛔" (для корректного сохранения очереди)

        # какие item_id уже "освободили слот" (и по счётчику, и по семафору)
        self._slot_released = set()

        # item_id, которые были остановлены как "пауза" (например, при закрытии приложения)
        self._paused_items = set()
        self._paused_status = {}  # item_id -> status string (с сохранением %)

        # прогресс параллельных аудиодорожек: item_id -> idx -> {"line": str, "ts": float}
        self._audio_progress_by_item = {}

        # чтобы не пересчитывать счётчик слишком часто (progress может спамить)
        self._counter_update_pending = False

        # очередь задач + диспетчер
        self.task_queue = queue.PriorityQueue()
        self._task_seq = 0
        self._task_token_by_item = {}
        self._pending_tasks = set()
        self._shutdown = threading.Event()
        self._dispatcher_thread = threading.Thread(target=self._dispatcher, daemon=True)
        self._dispatcher_thread.start()

    def _ui_call_sync(self, func, *, timeout: float = 0.8, default=None):
        if threading.current_thread() is threading.main_thread():
            try:
                return func()
            except Exception:
                return default

        done = threading.Event()
        box = {"value": default}

        def _do():
            try:
                box["value"] = func()
            except Exception:
                box["value"] = default
            finally:
                try:
                    done.set()
                except Exception:
                    pass

        try:
            self.root.after(0, _do)
        except Exception:
            return default

        try:
            done.wait(timeout=max(0.05, float(timeout)))
        except Exception:
            pass
        return box.get("value", default)

    def _tree_get_status(self, item_id) -> str:
        def _read():
            try:
                return str(self.tree.set(item_id, "status") or "")
            except Exception:
                return ""

        return self._ui_call_sync(_read, default="") or ""

    def _tree_parent(self, item_id) -> str:
        def _read():
            try:
                return str(self.tree.parent(item_id) or "")
            except Exception:
                return ""

        return self._ui_call_sync(_read, default="") or ""

    def _tree_index(self, item_id) -> int:
        def _read():
            try:
                return int(self.tree.index(item_id))
            except Exception:
                return 10**9

        try:
            return int(self._ui_call_sync(_read, default=10**9))
        except Exception:
            return 10**9

    def _find_ffmpeg_bins(self):
        ffmpeg_bin = None
        ffplay_bin = None

        try:
            from kino_hls import FFMPEG_BIN as _FFMPEG_BIN  # already bundled in app

            if isinstance(_FFMPEG_BIN, str) and os.path.isfile(_FFMPEG_BIN):
                ffmpeg_bin = _FFMPEG_BIN
        except Exception:
            ffmpeg_bin = None

        if not ffmpeg_bin:
            ffmpeg_bin = shutil.which("ffmpeg")

        if ffmpeg_bin:
            try:
                base = os.path.dirname(ffmpeg_bin)
                cand = os.path.join(base, "ffplay.exe" if os.name == "nt" else "ffplay")
                if os.path.isfile(cand):
                    ffplay_bin = cand
            except Exception:
                pass

        if not ffplay_bin:
            ffplay_bin = shutil.which("ffplay")

        return ffmpeg_bin, ffplay_bin

    def _start_ffplay_preview(self, *, video_file: str, audio_file: str, title: str, on_done=None):
        ffmpeg_bin, ffplay_bin = self._find_ffmpeg_bins()
        if not ffmpeg_bin or not ffplay_bin:
            try:
                messagebox.showerror("Предпросмотр", "Не найден ffmpeg/ffplay.")
            except Exception:
                pass
            if callable(on_done):
                try:
                    on_done(False)
                except Exception:
                    pass
            return

        if os.name == "nt":
            CREATE_NO_WINDOW = 0x08000000
        else:
            CREATE_NO_WINDOW = 0

        preview_path = os.path.join(
            tempfile.gettempdir(), f"kinopub_preview_{uuid.uuid4().hex}.mp4"
        )
        preview_duration = 1800  # 30 минут для более длинного прослушивания

        def _worker():
            ok = False
            try:
                # Предпросмотр (~30 минут), без перекодирования (copy).
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    video_file,
                    "-i",
                    audio_file,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c",
                    "copy",
                    "-t",
                    str(preview_duration),
                    "-shortest",
                    preview_path,
                ]
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                if res.returncode != 0 or (not os.path.isfile(preview_path)):
                    raise RuntimeError((res.stdout or "").strip()[-800:])

                play_cmd = [
                    ffplay_bin,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-autoexit",
                    "-window_title",
                    title,
                    preview_path,
                ]
                p = subprocess.Popen(play_cmd, creationflags=CREATE_NO_WINDOW)
                try:
                    p.wait()
                except Exception:
                    pass
                ok = True
            except Exception as e:
                try:
                    self._ui(messagebox.showerror, "Предпросмотр", f"Не удалось запустить предпросмотр:\n{e}")
                except Exception:
                    pass
            finally:
                try:
                    if os.path.exists(preview_path):
                        os.remove(preview_path)
                except Exception:
                    pass
                if callable(on_done):
                    try:
                        self._ui(on_done, ok)
                    except Exception:
                        try:
                            on_done(ok)
                        except Exception:
                            pass

        threading.Thread(target=_worker, daemon=True).start()

    def _audio_select_dialog(
        self,
        *,
        item_id=None,
        out_path: str | None = None,
        video_file: str,
        audio_files: list[str],
        audio_meta: list[tuple[str, str]],
        cancel_event=None,
        status_cb=None,
    ):
        # Called from a worker thread. Must show UI on the main thread and wait for the answer.
        if not audio_files or len(audio_files) < 2:
            return None
        if not self.root:
            return None

        # Не показываем несколько диалогов одновременно (иначе можно поймать несколько grab_set()).
        with _AUDIO_SELECT_LOCK:
            result = {"choice": None}
            done = threading.Event()
            win_ref = {"win": None}

            def _safe_close(choice):
                result["choice"] = choice
                done.set()
                try:
                    w = win_ref.get("win")
                    if w is not None and w.winfo_exists():
                        try:
                            w.grab_release()
                        except Exception:
                            pass
                        w.destroy()
                except Exception:
                    pass

            def _open():
                try:
                    title_text = ""
                    try:
                        if item_id is not None and hasattr(self.tree, "exists") and self.tree.exists(item_id):
                            title_text = str(self.tree.set(item_id, "title") or "")
                    except Exception:
                        title_text = ""
                    if not title_text and out_path:
                        title_text = os.path.basename(out_path)

                    w = tk.Toplevel(self.root)
                    win_ref["win"] = w
                    w.title("Выбор аудиодорожки")
                    w.transient(self.root)
                    w.grab_set()
                    w.resizable(True, True)

                    try:
                        if getattr(sys, "frozen", False):
                            base_dir = os.path.dirname(sys.executable)
                        else:
                            base_dir = os.path.dirname(__file__)
                        icon_path = os.path.join(base_dir, "icon.ico")
                        if os.path.exists(icon_path):
                            w.iconbitmap(icon_path)
                    except Exception:
                        pass

                    # Center
                    try:
                        sw = int(self.root.winfo_screenwidth())
                        sh = int(self.root.winfo_screenheight())
                        ww = min(980, max(720, int(sw * 0.55)))
                        hh = min(720, max(520, int(sh * 0.55)))
                        x = max(20, (sw - ww) // 2)
                        y = max(20, (sh - hh) // 2)
                        w.geometry(f"{ww}x{hh}+{x}+{y}")
                        w.minsize(720, 520)
                    except Exception:
                        pass

                    try:
                        w.update_idletasks()
                        w.lift()
                        w.focus_force()
                    except Exception:
                        pass

                    frame = ttk.Frame(w, padding=12)
                    frame.pack(fill="both", expand=True)

                    ttk.Label(
                        frame,
                        text="Выберите аудиодорожку перед MUX",
                        font=("Segoe UI Semibold", 12),
                    ).pack(anchor="w")
                    if title_text:
                        ttk.Label(frame, text=title_text, font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 10))

                    lb_frame = ttk.Frame(frame)
                    lb_frame.pack(fill="both", expand=True)

                    lb_scroll = ttk.Scrollbar(lb_frame, orient="vertical")
                    lb_scroll.pack(side="right", fill="y")

                    lb = tk.Listbox(lb_frame, exportselection=False, height=14)
                    lb.pack(side="left", fill="both", expand=True)
                    try:
                        lb.configure(yscrollcommand=lb_scroll.set)
                        lb_scroll.configure(command=lb.yview)
                    except Exception:
                        pass
                    for i, (name, lang) in enumerate(audio_meta[: len(audio_files)], start=1):
                        n = (name or f"Audio {i}").strip()
                        l = (lang or "und").strip()
                        lb.insert("end", f"{i}. {n} [{l}]")
                    if lb.size() > 0:
                        lb.selection_set(0)

                    info = ttk.Label(
                        frame,
                        text="Предпросмотр создаёт клип (~30 минут) и открывает ffplay. Перемотка: ←/→ (±10с), ↑/↓ (±1 мин).",
                    )
                    info.pack(anchor="w", pady=(8, 0))

                    btn_row = ttk.Frame(frame)
                    btn_row.pack(fill="x", pady=(10, 0))

                    preview_btn = ttk.Button(btn_row, text="Предпросмотр")
                    preview_btn.pack(side="left")

                    def _selected_index() -> int:
                        try:
                            sel = lb.curselection()
                            if sel:
                                return int(sel[0])
                        except Exception:
                            pass
                        return 0

                    try:
                        lb.bind("<Double-Button-1>", lambda _e: _safe_close(_selected_index()))
                        w.bind("<Return>", lambda _e: _safe_close(_selected_index()))
                        w.bind("<Escape>", lambda _e: _safe_close("cancel"))
                    except Exception:
                        pass

                    def _on_preview():
                        idx = _selected_index()
                        if idx < 0 or idx >= len(audio_files):
                            return
                        preview_btn.configure(state="disabled")
                        info.configure(text="Готовлю предпросмотр…")

                        def _done(_ok: bool):
                            try:
                                preview_btn.configure(state="normal")
                                info.configure(
                                    text="Предпросмотр создаёт клип (~30 минут) и открывает ffplay. Перемотка: ←/→ (±10с), ↑/↓ (±1 мин)."
                                )
                            except Exception:
                                pass

                        self._start_ffplay_preview(
                            video_file=video_file,
                            audio_file=audio_files[idx],
                            title=f"Kino.pub preview — audio {idx+1}",
                            on_done=_done,
                        )

                    preview_btn.configure(command=_on_preview)

                    ttk.Button(
                        btn_row,
                        text="Оставить выбранную",
                        command=lambda: _safe_close(_selected_index()),
                    ).pack(side="right")
                    ttk.Button(btn_row, text="Оставить все", command=lambda: _safe_close("all")).pack(
                        side="right", padx=(0, 8)
                    )
                    ttk.Button(btn_row, text="Отмена", command=lambda: _safe_close("cancel")).pack(
                        side="right", padx=(0, 8)
                    )

                    w.protocol("WM_DELETE_WINDOW", lambda: _safe_close("cancel"))
                except Exception:
                    _safe_close(None)

            try:
                self._ui(_open)
            except Exception:
                return None

            while not done.is_set():
                if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                    try:
                        self._ui(_safe_close, "cancel")
                    except Exception:
                        _safe_close("cancel")
                    break
                done.wait(timeout=0.2)

            choice = result.get("choice")
            if choice == "cancel":
                return "cancel"
            if choice == "all":
                return None
            return choice

    # ---------- утилиты UI ----------
    def _dispatcher(self):
        logging.info("Dispatcher thread started")
        while True:
            if self._shutdown.is_set():
                return
            try:
                try:
                    task = self.task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if task is None:
                    return

                try:
                    prio, seq, item_id, url, out_dir, token = task
                except Exception:
                    continue

                if item_id is None:
                    return

                try:
                    with self.lock:
                        cur = self._task_token_by_item.get(item_id)
                        if cur != token:
                            continue
                except Exception:
                    pass

                logging.info("Dispatcher got task: %s %s", item_id, url)

                # ждём свободный слот
                if not self.can_start(item_id):
                    # если пока нельзя запускать (например, ещё жив поток после паузы) —
                    # возвращаем задачу в очередь с небольшим "yield", чтобы не терять её.
                    st = self._tree_get_status(item_id)

                    drop = False
                    try:
                        ev = self.cancel_events.get(item_id)
                        if ev and ev.is_set():
                            drop = True
                    except Exception:
                        drop = False
                    if st.startswith(("✅", "❌", "⛔", "🎞")):
                        drop = True

                    if drop:
                        try:
                            with self.lock:
                                self._pending_tasks.discard(item_id)
                        except Exception:
                            pass
                        continue

                    try:
                        # слегка понижаем приоритет, чтобы не блокировать другие задачи
                        new_prio = int(self._task_priority(item_id)) + 1
                    except Exception:
                        new_prio = int(prio) + 1 if isinstance(prio, int) else 10**9

                    try:
                        with self.lock:
                            self._task_seq += 1
                            new_seq = int(self._task_seq)
                    except Exception:
                        new_seq = int(time.time() * 1000) % 1000000000

                    try:
                        self.task_queue.put((new_prio, new_seq, item_id, url, out_dir, token))
                    except Exception:
                        pass
                    time.sleep(0.15)
                    continue

                if self._shutdown.is_set():
                    return

                # Чтобы корректно выйти при закрытии приложения, не зависаем навсегда на sema.acquire().
                while True:
                    if self._shutdown.is_set():
                        return
                    if self.sema.acquire(timeout=0.5):
                        break
                if self._shutdown.is_set():
                    try:
                        self.sema.release()
                    except Exception:
                        pass
                    return

                # после ожидания слота проверяем, что задача ещё актуальна
                try:
                    with self.lock:
                        cur = self._task_token_by_item.get(item_id)
                    if cur != token or (not self.can_start(item_id)):
                        try:
                            self.sema.release()
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

                try:
                    with self.lock:
                        self._pending_tasks.discard(item_id)
                except Exception:
                    pass

                t = threading.Thread(
                    target=self._worker,
                    args=(item_id, url, out_dir),
                    daemon=True,
                    name=f"DLWorker-{item_id}",
                )
                self.threads[item_id] = t
                t.start()
            except Exception:
                logging.exception("Ошибка в dispatcher")

    def _ui(self, func, *args, **kwargs):
        try:
            self.root.after(0, lambda: func(*args, **kwargs))
        except Exception:
            pass

    def _task_priority(self, item_id) -> int:
        return self._tree_index(item_id)

    def _enqueue_task(self, item_id, url, out_dir, *, priority: int | None = None):
        try:
            prio = int(self._task_priority(item_id) if priority is None else priority)
        except Exception:
            prio = self._task_priority(item_id)

        try:
            with self.lock:
                self._task_seq += 1
                seq = int(self._task_seq)
                token = int(self._task_token_by_item.get(item_id, 0) or 0) + 1
                self._task_token_by_item[item_id] = token
                self._pending_tasks.add(item_id)
        except Exception:
            # best-effort fallback
            seq = int(time.time() * 1000) % 1000000000
            token = int(time.time() * 1000) % 1000000000
            try:
                self._pending_tasks.add(item_id)
            except Exception:
                pass

        try:
            self.task_queue.put((prio, seq, item_id, url, out_dir, token))
        except Exception:
            pass

    def reschedule_pending(self):
        """
        Перестраивает приоритет ожидающих задач по текущему порядку строк в Treeview.
        Работает только для НЕ запущенных элементов (ожидающих в очереди диспетчера).
        """
        try:
            with self.lock:
                pending = set(self._pending_tasks)
        except Exception:
            pending = set()

        if not pending:
            return

        for iid in list(self.tree.get_children("")):
            if iid not in pending:
                continue
            try:
                t = self.threads.get(iid)
                if t is not None and t.is_alive():
                    continue
            except Exception:
                pass
            try:
                ev = self.cancel_events.get(iid)
                if ev and ev.is_set():
                    continue
            except Exception:
                pass
            if not self.can_start(iid):
                continue

            url = self.url_by_item.get(iid) or self.tree.set(iid, "title")

            item_out_dir = None
            try:
                op = self.out_path_by_item.get(iid)
                if op:
                    item_out_dir = os.path.dirname(str(op))
            except Exception:
                item_out_dir = None
            try:
                if (not item_out_dir) and self.out_dir_by_item.get(iid):
                    item_out_dir = str(self.out_dir_by_item.get(iid))
            except Exception:
                pass
            if not item_out_dir:
                try:
                    item_out_dir = os.getcwd()
                except Exception:
                    item_out_dir = "."

            self._enqueue_task(iid, url, item_out_dir)

    def _schedule_counter_update(self):
        if self._counter_update_pending:
            return
        self._counter_update_pending = True

        def _run():
            self._counter_update_pending = False
            self._update_counter_label()

        try:
            self.root.after_idle(_run)
        except Exception:
            self._counter_update_pending = False
            self._update_counter_label()

    def set_status(self, item_id, text):
        def _do():
            try:
                if hasattr(self.tree, "exists") and not self.tree.exists(item_id):
                    return
                self.tree.set(item_id, "status", text)
                self._schedule_counter_update()
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._ui(_do)
        except Exception:
            pass

    def _clear_audio_progress(self, item_id):
        try:
            with self.lock:
                self._audio_progress_by_item.pop(item_id, None)
        except Exception:
            pass

        def _do():
            try:
                if hasattr(self.tree, "exists") and not self.tree.exists(item_id):
                    return
                for child in list(self.tree.get_children(item_id)):
                    try:
                        self.tree.delete(child)
                    except Exception:
                        pass
                try:
                    self.tree.item(item_id, open=False)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._ui(_do)
        except Exception:
            pass

    def _update_audio_progress(
        self,
        item_id,
        idx: int,
        *,
        pct: int | None = None,
        total: int | None = None,
        title: str = "",
        status: str = "",
        remove: bool = False,
    ):
        try:
            idx = int(idx)
        except Exception:
            return

        now = time.time()
        try:
            with self.lock:
                bucket = self._audio_progress_by_item.setdefault(item_id, {})
                if remove:
                    bucket.pop(idx, None)
                else:
                    bucket[idx] = {
                        "idx": idx,
                        "pct": pct,
                        "total": total,
                        "title": str(title or ""),
                        "status": str(status or ""),
                        "ts": float(now),
                    }

                if not bucket:
                    self._audio_progress_by_item.pop(item_id, None)
        except Exception:
            pass

    def _format_audio_progress_status(self, item_id, max_lines: int) -> str | None:
        try:
            max_lines = max(1, min(4, int(max_lines)))
        except Exception:
            max_lines = 1

        now = time.time()
        try:
            with self.lock:
                bucket = dict(self._audio_progress_by_item.get(item_id) or {})
        except Exception:
            bucket = {}

        items: list[tuple[float, int, str]] = []
        for idx, info in bucket.items():
            try:
                ts = float(info.get("ts", 0.0) or 0.0)
            except Exception:
                ts = 0.0
            if (now - ts) > 20.0:
                continue
            try:
                line = str(info.get("line") or "")
            except Exception:
                line = ""
            if not line:
                continue
            try:
                i = int(info.get("idx", idx))
            except Exception:
                try:
                    i = int(idx)
                except Exception:
                    i = 0
            items.append((ts, i, line))

        if not items:
            return None

        # выбираем самые "живые" дорожки, но отображаем в порядке индексов
        items.sort(key=lambda x: (-x[0], x[1]))
        selected = items[:max_lines]
        selected.sort(key=lambda x: x[1])
        return "\n".join([x[2] for x in selected if x[2]]) or None

    def _audio_summary_status(self, item_id) -> str | None:
        try:
            with self.lock:
                bucket = dict(self._audio_progress_by_item.get(item_id) or {})
        except Exception:
            bucket = {}

        if not bucket:
            return None

        active = len(bucket)
        total = 0
        pcts: list[int] = []
        for info in bucket.values():
            try:
                t = int(info.get("total") or 0)
            except Exception:
                t = 0
            total = max(total, t)

            pct = info.get("pct")
            if isinstance(pct, int):
                pcts.append(max(0, min(100, int(pct))))

        if total > 0:
            if pcts:
                return f"🔵 Аудио: {active} активных из {total} • {min(pcts)}%"
            return f"🔵 Аудио: {active} активных из {total}…"

        if pcts:
            return f"🔵 Аудио: {active} активных • {min(pcts)}%"
        return f"🔵 Аудио: {active} активных…"

    def _set_audio_child_row(self, item_id, idx: int, title: str, status: str):
        try:
            idx = int(idx)
        except Exception:
            return

        child_iid = f"{item_id}::audio{idx}"

        def _do():
            try:
                if hasattr(self.tree, "exists") and not self.tree.exists(item_id):
                    return

                # гарантируем, что child под правильным parent
                try:
                    if hasattr(self.tree, "exists") and self.tree.exists(child_iid):
                        if self.tree.parent(child_iid) != item_id:
                            try:
                                self.tree.delete(child_iid)
                            except Exception:
                                pass
                except Exception:
                    pass

                if hasattr(self.tree, "exists") and not self.tree.exists(child_iid):
                    self.tree.insert(item_id, "end", iid=child_iid, values=("", str(title or ""), str(status or "")))
                else:
                    try:
                        self.tree.set(child_iid, "title", str(title or ""))
                    except Exception:
                        pass
                    try:
                        self.tree.set(child_iid, "status", str(status or ""))
                    except Exception:
                        pass

                # держим порядок детей по idx
                try:
                    self.tree.move(child_iid, item_id, max(0, idx - 1))
                except Exception:
                    pass
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._ui(_do)
        except Exception:
            pass

    def _remove_audio_child_row(self, item_id, idx: int):
        try:
            idx = int(idx)
        except Exception:
            return

        child_iid = f"{item_id}::audio{idx}"

        def _do():
            try:
                if hasattr(self.tree, "exists") and self.tree.exists(child_iid):
                    self.tree.delete(child_iid)
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._ui(_do)
        except Exception:
            pass

    def _notify(self, title: str, message: str):
        if not self.notify_cb:
            return
        try:
            self._ui(self.notify_cb, title, message)
        except Exception:
            pass

    def _history(self, event: dict):
        if not self.history_cb:
            return
        try:
            self._ui(self.history_cb, event)
        except Exception:
            pass

    def _update_counter_label(self):
        def _do():
            try:
                with self.lock:
                    max_parallel = int(self.MAX_PARALLEL)

                downloading = 0
                try:
                    for iid in self.tree.get_children():
                        try:
                            s = str(self.tree.set(iid, "status") or "")
                        except Exception:
                            continue
                        if s.startswith(("🔵 Видео", "🔵 Аудио")):
                            downloading += 1
                except Exception:
                    pass

                text = f"Активно: {downloading} / {max_parallel}"
                self.counter_label.config(text=text)
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._ui(_do)
        except Exception:
            pass

    def inc_active(self):
        with self.lock:
            self.active += 1
        self._update_counter_label()

    def _release_slot(self, item_id):
        """
        Освобождает сетевой слот (и счётчик, и семафор) ОДИН РАЗ на item_id.
        Можно вызывать и при MUX, и в finally — повторные вызовы игнорируются.
        """
        with self.lock:
            already_released = item_id in self._slot_released
            if not already_released:
                self._slot_released.add(item_id)
                if self.active > 0:
                    self.active -= 1

        # даже если слот уже освобождён, обновим текст (например, когда MUX завершился)
        self._update_counter_label()

        if already_released:
            return

        # освобождаем семафор — можно запускать следующую загрузку
        try:
            self.sema.release()
        except ValueError:
            # на всякий случай, если кто-то релизнул лишний раз
            logging.warning("sema.release() extra for %s", item_id)

    # ---------- публичный API ----------
    def can_start(self, item_id):
        status = self._tree_get_status(item_id)
        if status is None:
            return False

        # не запускаем дочерние строки (аудио-детали)
        try:
            if self._tree_parent(item_id):
                return False
        except Exception:
            return False

        # не запускаем повторно, если уже есть живой поток
        try:
            t = self.threads.get(item_id)
            if (
                t is not None
                and getattr(t, "is_alive", lambda: False)()
                and (t is not threading.current_thread())
            ):
                return False
        except Exception:
            pass
        ev = self.cancel_events.get(item_id)
        if ev and ev.is_set():
            return False
        try:
            s = str(status or "")
        except Exception:
            s = ""
        return (not self.stop_flag) and (not s.startswith(("✅", "❌", "⛔", "🎞")))

    def cancel_item(self, item_id):
        try:
            status = str(self.tree.set(item_id, "status"))
            if status.startswith("✅"):
                return
        except Exception:
            pass

        # отмена пользователем — это НЕ пауза
        try:
            with self.lock:
                self._paused_items.discard(item_id)
                self._paused_status.pop(item_id, None)
        except Exception:
            pass

        ev = self.cancel_events.get(item_id)
        if ev is None:
            ev = threading.Event()
            self.cancel_events[item_id] = ev
        try:
            if hasattr(ev, "_keep_parts"):
                delattr(ev, "_keep_parts")
        except Exception:
            pass
        ev.set()
        try:
            t = self.threads.get(item_id)
            if t is not None and t.is_alive():
                self.set_status(item_id, "⏹ Отмена...")
            else:
                self.set_status(item_id, "⛔ Отменено")
                self.final_status[item_id] = "⛔"
        except Exception:
            pass

    def pause_item(self, item_id):
        """
        Мягкая остановка (пауза): сохраняем статус/процент и оставляем .parts,
        чтобы после перезапуска можно было продолжить.
        """
        try:
            status = str(self.tree.set(item_id, "status") or "")
        except Exception:
            status = ""

        # не трогаем финальные состояния / готово к конвертации
        if status.startswith(("✅", "❌", "⛔", "🎞")):
            return

        paused_status = (status or "").strip()
        if not paused_status:
            paused_status = "🟡 Подготовка..."
        if not paused_status.startswith("⏸"):
            paused_status = "⏸ " + paused_status

        try:
            with self.lock:
                self._paused_items.add(item_id)
                self._paused_status[item_id] = paused_status
        except Exception:
            pass

        ev = self.cancel_events.get(item_id)
        if ev is None:
            ev = threading.Event()
            self.cancel_events[item_id] = ev
        try:
            setattr(ev, "_keep_parts", True)
        except Exception:
            pass
        ev.set()

        try:
            # Сразу показываем сохранённый статус с %, чтобы при сохранении очереди
            # (и при перезапуске) не потерять прогресс.
            self.set_status(item_id, paused_status)
        except Exception:
            pass

    def stop_all(self, show_message: bool = True):
        self.stop_flag = True
        if show_message:
            messagebox.showinfo(
                "Остановлено",
                "Новые загрузки не будут запускаться.\nТекущие завершатся.",
            )

    def shutdown(self, *, cancel_active: bool = False, pause_active: bool = False, timeout: float = 2.0):
        """
        Аккуратно останавливает dispatcher, чтобы приложение могло завершиться без падений.
        cancel_active=True -> выставит cancel_event всем активным item_id.
        pause_active=True -> отмена как «пауза» (с сохранением прогресса и .parts).
        """
        self.stop_flag = True
        self._shutdown.set()

        if cancel_active:
            try:
                for item_id in list(self.threads.keys()):
                    try:
                        if pause_active:
                            self.pause_item(item_id)
                        else:
                            self.cancel_item(item_id)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self.task_queue.put_nowait((-1, -1, None, None, None, -1))
        except Exception:
            try:
                self.task_queue.put((-1, -1, None, None, None, -1))
            except Exception:
                pass

        try:
            t = getattr(self, "_dispatcher_thread", None)
            if t is not None and t.is_alive():
                t.join(timeout=timeout)
        except Exception:
            pass

        # best-effort: подождать рабочие потоки, чтобы не оставить их на финализацию интерпретатора
        deadline = time.time() + max(0.0, float(timeout))
        try:
            for wt in list(self.threads.values()):
                try:
                    if wt is None or not wt.is_alive():
                        continue
                    left = max(0.0, deadline - time.time())
                    if left <= 0:
                        break
                    wt.join(timeout=left)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- worker ----------
    def _worker(self, item_id, url, out_dir):
        import traceback
        from datetime import datetime
        import random

        if not self.can_start(item_id):
            # слот уже занят/статус финальный — релизнем семафор обратно
            self._release_slot(item_id)
            return

        self.set_status(item_id, "🟡 Подготовка...")
        time.sleep(0.25)

        if not self.can_start(item_id):
            self._release_slot(item_id)
            return

        detected = {"name": None}

        def _slot_already_released() -> bool:
            try:
                with self.lock:
                    return item_id in self._slot_released
            except Exception:
                return False

        def _is_retryable(err_text: str) -> bool:
            s = (err_text or "").lower()
            if not s:
                return True
            if "требуется вход" in s or "сессия неактивна" in s:
                return False
            if "ничего не найдено" in s:
                return False
            return True

        # 1-я попытка + ретраи для нестабильных сетевых падений
        max_attempts = 3
        active_started = False
        last_err_text = ""
        ok = False

        drv = None
        driver_returned = False
        try:
            for attempt in range(1, max_attempts + 1):
                cancel_event = self.cancel_events.get(item_id)
                if cancel_event and cancel_event.is_set():
                    ok = False
                    break

                # если уже ушли в MUX — сетевой слот освобождён, ретраить сетевую стадию нельзя
                if attempt > 1 and _slot_already_released():
                    break

                # межпопытка: backoff + jitter, чтобы не бомбить CDN в один момент
                if attempt > 1:
                    delay = min(10.0, 1.5 * attempt + random.uniform(0.4, 1.6))
                    self.set_status(item_id, f"♻️ Повтор {attempt}/{max_attempts} через {int(delay)}с…")
                    time.sleep(delay)

                # ждём браузер из пула (может занять время) — это ещё не «качает»
                self.set_status(item_id, "⏳ Ожидаю браузер…")

                drv = None
                driver_returned = False
                last_err_text = ""

                if self.pool:
                    drv = self.pool.acquire()
                else:
                    drv = _safe_get_driver(
                        status_cb=lambda m: print(m),
                        headless=False,
                        suppress=True,
                        need_login_hint=False,
                    )

                # начинаем активную сетевую работу (как только получили браузер)
                if not active_started:
                    self.inc_active()
                    active_started = True
                self.set_status(item_id, "🔵 Загрузка...")

                def _status_proxy(msg):
                    nonlocal drv, driver_returned, last_err_text
                    try:
                        text = str(msg)

                        # запоминаем последнюю «осмысленную» ошибку/предупреждение, чтобы решать о ретраях
                        if text.startswith(("⚠️", "❌")):
                            last_err_text = text
                        elif "SEGMENT FAIL" in text or "master.m3u8" in text:
                            last_err_text = text

                        def _return_driver_to_pool():
                            nonlocal drv, driver_returned
                            if driver_returned:
                                return
                            if self.pool and drv is not None:
                                try:
                                    self.pool.release(drv)
                                except Exception:
                                    return
                                driver_returned = True
                                drv = None

                        # ---- Заголовок файла ----
                        try:
                            m = re.search(r'(?:🎬\s*)?(?:Файл|Название)\s*:\s*(.+)', text)
                            if m:
                                raw = m.group(1).strip().strip('"\'')
                                out_path = raw
                                try:
                                    if out_path and not os.path.isabs(out_path):
                                        out_path = os.path.join(out_dir, out_path)
                                    out_path = os.path.normpath(out_path)
                                except Exception:
                                    out_path = raw

                                detected["out_path"] = out_path
                                try:
                                    self.out_path_by_item[item_id] = out_path
                                except Exception:
                                    pass

                                nice = os.path.splitext(os.path.basename(out_path))[0]
                                detected["name"] = nice
                                if hasattr(self, "ui_set_title"):
                                    self.ui_set_title(item_id, nice)
                        except Exception:
                            pass

                        # ---- Фильтр UI статусов ----
                        m = re.search(
                            r"⬇️\s*(Видео|Аудио)(?:\s+(\d+)\s*/\s*(\d+))?\s*(.*?)\s*(\d{1,3})%\s*(?:\(([^)]+)\))?",
                            text,
                        )
                        if m:
                            kind = m.group(1)
                            a_i = m.group(2)
                            a_total = m.group(3)
                            extra = (m.group(4) or "").strip()
                            speed = (m.group(6) or "").strip()
                            try:
                                pct = max(0, min(100, int(m.group(5))))
                            except Exception:
                                pct = None

                            if pct is not None:
                                # С этого момента идёт реальная загрузка сегментов — драйвер больше не нужен
                                _return_driver_to_pool()
                                if kind == "Видео":
                                    self._clear_audio_progress(item_id)
                                    status = f"🔵 Видео {pct}%"
                                    if speed:
                                        status = f"{status} {speed}"
                                    self.set_status(item_id, status)
                                else:
                                    idx_int = None
                                    total_int = None
                                    try:
                                        if a_i:
                                            idx_int = int(a_i)
                                    except Exception:
                                        idx_int = None
                                    try:
                                        if a_total:
                                            total_int = int(a_total)
                                    except Exception:
                                        total_int = None

                                    title = extra
                                    if title.startswith("(") and title.endswith(")"):
                                        title = title[1:-1].strip()
                                    if title and len(title) > 46:
                                        title = title[:45] + "…"

                                    child_title = ""
                                    if idx_int is not None and total_int:
                                        child_title = f"🎧 {idx_int}/{total_int}"
                                    elif idx_int is not None:
                                        child_title = f"🎧 {idx_int}"
                                    else:
                                        child_title = "🎧 Аудио"
                                    if title:
                                        child_title = f"{child_title} — {title}"

                                    child_status = f"🔵 {pct}%"
                                    if speed:
                                        child_status = f"{child_status} {speed}"

                                    if idx_int is not None:
                                        if pct >= 100:
                                            self._update_audio_progress(item_id, idx_int, remove=True)
                                            self._remove_audio_child_row(item_id, idx_int)
                                        else:
                                            self._update_audio_progress(
                                                item_id,
                                                idx_int,
                                                pct=pct,
                                                total=total_int,
                                                title=title,
                                                status=child_status,
                                            )
                                            self._set_audio_child_row(item_id, idx_int, child_title, child_status)

                                        summary = self._audio_summary_status(item_id)
                                        self.set_status(item_id, summary or "🔵 Аудио…")
                                    else:
                                        self.set_status(item_id, f"🔵 Аудио {pct}%")

                        elif text.startswith("⬇️"):
                            # Начало/промежуточные сообщения загрузки — драйвер уже не нужен
                            _return_driver_to_pool()
                            m0 = re.search(
                                r"^⬇️\s*(Видео|Аудио)(?:\s+(\d+)\s*/\s*(\d+))?\s*(.*)$", text
                            )
                            if m0:
                                kind = m0.group(1)
                                a_i = m0.group(2)
                                a_total = m0.group(3)
                                if kind == "Видео":
                                    self._clear_audio_progress(item_id)
                                    self.set_status(item_id, "🔵 Видео…")
                                else:
                                    extra = (m0.group(4) or "").strip()

                                    idx_int = None
                                    total_int = None
                                    try:
                                        if a_i:
                                            idx_int = int(a_i)
                                    except Exception:
                                        idx_int = None
                                    try:
                                        if a_total:
                                            total_int = int(a_total)
                                    except Exception:
                                        total_int = None

                                    title = extra
                                    if title.startswith("(") and title.endswith(")"):
                                        title = title[1:-1].strip()
                                    if title and len(title) > 46:
                                        title = title[:45] + "…"

                                    child_title = ""
                                    if idx_int is not None and total_int:
                                        child_title = f"🎧 {idx_int}/{total_int}"
                                    elif idx_int is not None:
                                        child_title = f"🎧 {idx_int}"
                                    else:
                                        child_title = "🎧 Аудио"
                                    if title:
                                        child_title = f"{child_title} — {title}"

                                    child_status = "🔵 …"

                                    if idx_int is not None:
                                        self._update_audio_progress(
                                            item_id,
                                            idx_int,
                                            pct=None,
                                            total=total_int,
                                            title=title,
                                            status=child_status,
                                        )
                                        self._set_audio_child_row(item_id, idx_int, child_title, child_status)
                                        summary = self._audio_summary_status(item_id)
                                        self.set_status(item_id, summary or "🔵 Аудио…")
                                    else:
                                        self.set_status(item_id, "🔵 Аудио…")

                        elif "Скачиваю видео" in text:
                            _return_driver_to_pool()
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, "🔵 Видео…")

                        elif "Скачиваю аудио" in text:
                            _return_driver_to_pool()
                            self.set_status(item_id, "🔵 Аудио…")

                        elif text.startswith("🔀 MUX"):
                            _return_driver_to_pool()
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, text)
                            self._release_slot(item_id)

                        elif "Муксую" in text or "MUX…" in text:
                            # начался MUX — сетевой трафик уже не идёт,
                            # освобождаем слот для следующих загрузок
                            _return_driver_to_pool()
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, "🟣 MUX…")
                            self._release_slot(item_id)

                        elif text.startswith("♻️"):
                            # кеш/повтор без докачки — не считаем как активную загрузку
                            handled = False
                            try:
                                m_cache = re.search(r"^♻️\s*Аудио(?:\s+(\d+)\s*/\s*(\d+))?", text)
                                if m_cache and m_cache.group(1):
                                    try:
                                        idx_int = int(m_cache.group(1))
                                    except Exception:
                                        idx_int = None
                                    if idx_int is not None:
                                        self._update_audio_progress(item_id, idx_int, remove=True)
                                        self._remove_audio_child_row(item_id, idx_int)
                                        summary = self._audio_summary_status(item_id)
                                        if summary:
                                            self.set_status(item_id, summary)
                                            handled = True
                            except Exception:
                                handled = False

                            if not handled:
                                self.set_status(item_id, text)

                        elif text.startswith("🎧"):
                            # выбор аудиодорожки перед MUX — сеть уже не используется
                            _return_driver_to_pool()
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, text)
                            self._release_slot(item_id)

                        elif text.startswith("🎞"):
                            # сегменты скачаны, но MUX запускается вручную
                            _return_driver_to_pool()
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, text)
                            self._release_slot(item_id)

                        elif "Ошибка MUX" in text:
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, "❌ Ошибка MUX")

                        elif text.startswith("✅ "):
                            self._clear_audio_progress(item_id)
                            self.set_status(item_id, "✅ Готово")

                        elif text.startswith(("🧩", "🌐")):
                            # Во время CF драйвер нужен, поэтому НЕ возвращаем его в пул.
                            self.set_status(item_id, "🧩 Cloudflare…")

                        else:
                            # прочее только в лог
                            print(text)

                    except Exception:
                        logging.exception("Ошибка в _status_proxy")

                def _audio_select_proxy(**kwargs):
                    cb = getattr(self, "audio_select_cb", None)
                    if not callable(cb):
                        return None
                    return cb(item_id=item_id, **kwargs)

                try:
                    auto_convert = bool(getattr(self.root, "_kino_auto_convert_all_audio", False))
                except Exception:
                    auto_convert = False

                try:
                    name_override = self.name_override_by_item.get(item_id)
                except Exception:
                    name_override = None

                try:
                    audio_parallel_tracks = int(getattr(self.root, "_kino_audio_parallel_tracks", 1) or 1)
                except Exception:
                    audio_parallel_tracks = 1
                try:
                    audio_parallel_tracks = max(1, min(4, int(audio_parallel_tracks)))
                except Exception:
                    audio_parallel_tracks = 1

                ok = download(
                    url,
                    out_dir,
                    status_cb=_status_proxy,
                    driver=drv,
                    cancel_event=cancel_event,
                    audio_select_cb=(_audio_select_proxy if not auto_convert else None),
                    defer_mux=(not auto_convert),
                    display_name_override=name_override,
                    audio_parallel_tracks=audio_parallel_tracks,
                )

                # если драйвер ещё у нас — вернём/закроем
                try:
                    if self.pool and drv is not None:
                        self.pool.release(drv)
                        drv = None
                    elif drv is not None:
                        drv.quit()
                        drv = None
                except Exception:
                    drv = None

                if ok:
                    break

                if cancel_event and cancel_event.is_set():
                    break

                # если это «не ретраится» — выходим сразу
                if attempt >= max_attempts or (not _is_retryable(last_err_text)):
                    break

            def _emit_history(result: str, err: str = ""):
                try:
                    title = detected.get("name") or str(url)
                except Exception:
                    title = str(url)

                out_path = detected.get("out_path")
                try:
                    if isinstance(out_path, str):
                        out_path = out_path.strip().strip('"\'')
                        if out_path and not os.path.isabs(out_path):
                            out_path = os.path.join(out_dir, out_path)
                except Exception:
                    pass

                if not out_path and detected.get("name"):
                    try:
                        out_path = os.path.join(out_dir, str(detected.get("name")) + ".mp4")
                    except Exception:
                        out_path = None

                event = {
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "result": result,
                    "title": title,
                    "url": str(url),
                    "out_dir": str(out_dir),
                    "out_path": out_path,
                }
                if err:
                    event["error"] = str(err)
                self._history(event)

            name = detected["name"] or str(url)
            if cancel_event and cancel_event.is_set() and not ok:
                paused_status = None
                is_paused = False
                try:
                    with self.lock:
                        is_paused = item_id in self._paused_items
                        paused_status = self._paused_status.get(item_id)
                except Exception:
                    is_paused = False
                    paused_status = None

                if is_paused:
                    if not paused_status:
                        try:
                            cur = str(self.tree.set(item_id, "status") or "")
                        except Exception:
                            cur = ""
                        paused_status = (cur or "").strip() or "⏸ Пауза"
                        if not paused_status.startswith("⏸"):
                            paused_status = "⏸ " + paused_status
                    self.set_status(item_id, paused_status)
                    _emit_history("paused")
                    return

                self.set_status(item_id, "⛔ Отменено")
                self.final_status[item_id] = "⛔"
                self._notify("⛔ Kino.pub", f"Отменено: {name}")
                _emit_history("canceled")
                return

            # Если download() отработал, но не было "Муксую" (ошибка раньше) —
            # слот всё ещё не освобождён, сделаем это здесь.
            if not ok:
                try:
                    cur = self.tree.set(item_id, "status")
                except Exception:
                    cur = ""

                # Для "перебойных" ошибок оставляем элемент в паузе, чтобы можно было продолжить
                # после перезапуска/логина, не теряя последний % в статусе.
                try:
                    retryable = _is_retryable(last_err_text)
                except Exception:
                    retryable = True

                if retryable:
                    paused = (str(cur or "")).strip() or "🟡 Подготовка..."
                    if not paused.startswith("⏸"):
                        paused = "⏸ " + paused
                    self.set_status(item_id, paused)
                    _emit_history("paused_error", err=last_err_text)
                else:
                    if not str(cur).startswith("❌"):
                        self.set_status(item_id, "❌ Ошибка загрузки")
                    self.final_status[item_id] = "❌"
                    self._notify("❌ Kino.pub", f"Ошибка: {name}")
                    _emit_history("error", err=last_err_text)
            else:
                try:
                    cur = str(self.tree.set(item_id, "status") or "")
                except Exception:
                    cur = ""

                # В режиме ручной конвертации это ещё не финал.
                if cur.startswith("🎞"):
                    if not cur:
                        cur = "🎞 Готово к конвертации"
                        self.set_status(item_id, cur)
                    self._notify("🎞 Kino.pub", f"Готово к конвертации: {name}")
                    _emit_history("prepared")
                else:
                    self.set_status(item_id, "✅ Готово")
                    self.final_status[item_id] = "✅"
                    self._notify("✅ Kino.pub", f"Готово: {name}")
                    _emit_history("success")

        except Exception as e:
            name = detected["name"] or str(url)
            err_text = f"Ошибка скачивания: {e}\n{traceback.format_exc()}"
            logging.error(err_text)
            self.set_status(item_id, f"❌ {e}")
            self.final_status[item_id] = "❌"
            self._notify("❌ Kino.pub", f"Ошибка: {name}")
            try:
                self._history(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "result": "error",
                        "title": name,
                        "url": str(url),
                        "out_dir": str(out_dir),
                        "out_path": detected.get("out_path"),
                        "error": str(e),
                    }
                )
            except Exception:
                pass
            print(err_text, flush=True)

        finally:
            try:
                if self.pool and drv is not None:
                    self.pool.release(drv)
                elif drv is not None:
                    drv.quit()
            except Exception:
                pass

            # гарантия: слот точно будет освобождён
            self._release_slot(item_id)
            # убрать из множества, чтобы при следующем запуске item_id начать с нуля
            with self.lock:
                self._slot_released.discard(item_id)

    # ---------- запуск задач ----------
    def start_item(self, item_id, url, out_dir, name_override=None):
        # если ранее нажимали "Остановить" или было stop_all() — новый запуск должен возобновлять очередь
        self.stop_flag = False

        ev = self.cancel_events.get(item_id)
        if ev is None:
            ev = threading.Event()
            self.cancel_events[item_id] = ev
        else:
            ev.clear()
        try:
            if hasattr(ev, "_keep_parts"):
                delattr(ev, "_keep_parts")
        except Exception:
            pass

        try:
            with self.lock:
                self._paused_items.discard(item_id)
                self._paused_status.pop(item_id, None)
        except Exception:
            pass

        self._clear_audio_progress(item_id)

        if not self.can_start(item_id):
            return
        self.url_by_item[item_id] = url
        try:
            if out_dir:
                self.out_dir_by_item[item_id] = str(out_dir)
        except Exception:
            pass
        try:
            if name_override is not None:
                self.name_override_by_item[item_id] = str(name_override)
        except Exception:
            pass
        try:
            self.final_status.pop(item_id, None)
        except Exception:
            pass

        # сбрасываем возможный прошлый флаг
        with self.lock:
            self._slot_released.discard(item_id)

        self._enqueue_task(item_id, url, out_dir)
        self.set_status(item_id, "🟡 Ожидает...")

    def start_all(self, out_dir):
        self.stop_flag = False
        items = list(self.tree.get_children())
        for item in items:
            ev = self.cancel_events.get(item)
            if ev and ev.is_set():
                continue
            status = self.tree.set(item, "status")
            try:
                s = str(status or "")
            except Exception:
                s = ""
            if s.startswith(("✅", "❌", "⛔", "🎞", "⏸")):
                continue
            url = self.url_by_item.get(item) or self.tree.set(item, "title")
            self.set_status(item, "🟡 Подготовка...")
            item_out_dir = None
            try:
                op = self.out_path_by_item.get(item)
                if op:
                    item_out_dir = os.path.dirname(str(op))
            except Exception:
                item_out_dir = None
            try:
                if (not item_out_dir) and self.out_dir_by_item.get(item):
                    item_out_dir = str(self.out_dir_by_item.get(item))
            except Exception:
                pass
            if not item_out_dir:
                item_out_dir = out_dir
            self.start_item(item, url, item_out_dir)
