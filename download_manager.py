import os
import sys
import time
import re
import logging
import threading
from tkinter import messagebox
import queue

from uc_driver import _safe_get_driver
from kino_pub_downloader import download


class DownloadManager:
    def __init__(self, root, tree, counter_label, max_parallel=2, pool=None, notify_cb=None, history_cb=None):
        self.root = root
        self.tree = tree
        self.url_by_item = {}  # item_id -> original URL
        self.counter_label = counter_label
        self.MAX_PARALLEL = max_parallel
        self.pool = pool
        self.notify_cb = notify_cb
        self.history_cb = history_cb

        self.sema = threading.Semaphore(self.MAX_PARALLEL)
        self.lock = threading.Lock()
        self.active = 0
        self.stop_flag = False
        self.threads = {}  # item_id -> Thread
        self.cancel_events = {}  # item_id -> Event
        self.final_status = {}  # item_id -> "✅"/"❌"/"⛔" (для корректного сохранения очереди)

        # какие item_id уже "освободили слот" (и по счётчику, и по семафору)
        self._slot_released = set()

        # очередь задач + диспетчер
        self.task_queue = queue.Queue()
        self._shutdown = threading.Event()
        self._dispatcher_thread = threading.Thread(target=self._dispatcher, daemon=True)
        self._dispatcher_thread.start()

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

                item_id, url, out_dir = task
                logging.info("Dispatcher got task: %s %s", item_id, url)

                # ждём свободный слот
                if not self.can_start(item_id):
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

    def set_status(self, item_id, text):
        def _do():
            try:
                if hasattr(self.tree, "exists") and not self.tree.exists(item_id):
                    return
                self.tree.set(item_id, "status", text)
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

    def inc_active(self):
        with self.lock:
            self.active += 1
        self._ui(
            self.counter_label.config,
            text=f"Активно: {self.active} / {self.MAX_PARALLEL}",
        )

    def _release_slot(self, item_id):
        """
        Освобождает сетевой слот (и счётчик, и семафор) ОДИН РАЗ на item_id.
        Можно вызывать и при MUX, и в finally — повторные вызовы игнорируются.
        """
        with self.lock:
            if item_id in self._slot_released:
                return
            self._slot_released.add(item_id)

            if self.active > 0:
                self.active -= 1
            self._ui(
                self.counter_label.config,
                text=f"Активно: {self.active} / {self.MAX_PARALLEL}",
            )

            # освобождаем семафор — можно запускать следующую загрузку
            try:
                self.sema.release()
            except ValueError:
                # на всякий случай, если кто-то релизнул лишний раз
                logging.warning("sema.release() extra for %s", item_id)

    # ---------- публичный API ----------
    def can_start(self, item_id):
        try:
            status = self.tree.set(item_id, "status")
        except Exception:
            return False
        ev = self.cancel_events.get(item_id)
        if ev and ev.is_set():
            return False
        try:
            s = str(status or "")
        except Exception:
            s = ""
        return (not self.stop_flag) and (not s.startswith(("✅", "❌", "⛔")))

    def cancel_item(self, item_id):
        try:
            status = str(self.tree.set(item_id, "status"))
            if status.startswith("✅"):
                return
        except Exception:
            pass

        ev = self.cancel_events.get(item_id)
        if ev is None:
            ev = threading.Event()
            self.cancel_events[item_id] = ev
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

    def stop_all(self, show_message: bool = True):
        self.stop_flag = True
        if show_message:
            messagebox.showinfo(
                "Остановлено",
                "Новые загрузки не будут запускаться.\nТекущие завершатся.",
            )

    def shutdown(self, *, cancel_active: bool = False, timeout: float = 2.0):
        """
        Аккуратно останавливает dispatcher, чтобы приложение могло завершиться без падений.
        cancel_active=True -> выставит cancel_event всем активным item_id.
        """
        self.stop_flag = True
        self._shutdown.set()

        if cancel_active:
            try:
                for item_id in list(self.threads.keys()):
                    try:
                        self.cancel_item(item_id)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self.task_queue.put_nowait(None)
        except Exception:
            try:
                self.task_queue.put(None)
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

        if not self.can_start(item_id):
            # слот уже занят/статус финальный — релизнем семафор обратно
            self._release_slot(item_id)
            return

        self.set_status(item_id, "🟡 Подготовка...")
        time.sleep(0.25)

        if not self.can_start(item_id):
            self._release_slot(item_id)
            return

        # начинаем активную сетевую работу
        self.inc_active()
        self.set_status(item_id, "🔵 Загрузка...")

        drv = None
        detected = {"name": None}
        try:
            if self.pool:
                drv = self.pool.acquire()
            else:
                drv = _safe_get_driver(
                    status_cb=lambda m: print(m),
                    headless=False,
                    suppress=True,
                    need_login_hint=False,
                )

            from kino_parser import load_cookies
            # прогрев не нужен — downloader сам загрузит cookies и проверит сессию
            pass

            def _status_proxy(msg):
                try:
                    text = str(msg)

                    # ---- Заголовок файла ----
                    try:
                        m = re.search(
                            r'(?:🎬\s*)?(?:Файл|Название)\s*:\s*(.+)', text
                        )
                        if m and hasattr(self, "ui_set_title"):
                            raw = m.group(1).strip().strip('"\'')
                            detected["out_path"] = raw
                            nice = os.path.splitext(os.path.basename(raw))[0]
                            detected["name"] = nice
                            self.ui_set_title(item_id, nice)
                    except Exception:
                        pass

                    # ---- Фильтр UI статусов ----
                    m = re.search(
                        r"⬇️\s*(Видео|Аудио)(?:\s+(\d+)\s*/\s*(\d+))?.*?(\d{1,3})%\s*(?:\(([^)]+)\))?",
                        text,
                    )
                    if m:
                        kind = m.group(1)
                        a_i = m.group(2)
                        a_total = m.group(3)
                        speed = (m.group(5) or "").strip()
                        try:
                            pct = max(0, min(100, int(m.group(4))))
                        except Exception:
                            pct = None

                        if pct is not None:
                            if kind == "Видео":
                                status = f"🔵 Видео {pct}%"
                            else:
                                frac = f"{a_i}/{a_total}" if a_i and a_total else ""
                                status = f"🔵 Аудио {frac} {pct}%".replace("  ", " ").strip()
                            if speed:
                                status = f"{status} {speed}"
                            self.set_status(item_id, status)

                    elif text.startswith("⬇️"):
                        m0 = re.search(r"^⬇️\s*(Видео|Аудио)(?:\s+(\d+)\s*/\s*(\d+))?", text)
                        if m0:
                            kind = m0.group(1)
                            a_i = m0.group(2)
                            a_total = m0.group(3)
                            if kind == "Видео":
                                self.set_status(item_id, "🔵 Видео…")
                            else:
                                frac = f"{a_i}/{a_total}" if a_i and a_total else ""
                                if frac:
                                    self.set_status(item_id, f"🔵 Аудио {frac}…")
                                else:
                                    self.set_status(item_id, "🔵 Аудио…")

                    elif "Скачиваю видео" in text:
                        self.set_status(item_id, "🔵 Видео…")

                    elif "Скачиваю аудио" in text:
                        self.set_status(item_id, "🔵 Аудио…")

                    elif "Муксую" in text or "MUX…" in text:
                        # начался MUX — сетевой трафик уже не идёт,
                        # освобождаем слот для следующих загрузок
                        self.set_status(item_id, "🟣 MUX…")
                        self._release_slot(item_id)

                    elif "Ошибка MUX" in text:
                        self.set_status(item_id, "❌ Ошибка MUX")

                    elif text.startswith("✅ "):
                        self.set_status(item_id, "✅ Готово")

                    else:
                        # прочее только в лог
                        print(text)

                except Exception:
                    logging.exception("Ошибка в _status_proxy")

            cancel_event = self.cancel_events.get(item_id)

            ok = download(
                url,
                out_dir,
                status_cb=_status_proxy,
                driver=drv,
                cancel_event=cancel_event,
            )

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
                if not str(cur).startswith("❌"):
                    self.set_status(item_id, "❌ Ошибка загрузки")
                self.final_status[item_id] = "❌"
                self._notify("❌ Kino.pub", f"Ошибка: {name}")
                _emit_history("error")
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
    def start_item(self, item_id, url, out_dir):
        ev = self.cancel_events.get(item_id)
        if ev is None:
            ev = threading.Event()
            self.cancel_events[item_id] = ev
        else:
            ev.clear()

        if not self.can_start(item_id):
            return
        self.url_by_item[item_id] = url
        try:
            self.final_status.pop(item_id, None)
        except Exception:
            pass

        # сбрасываем возможный прошлый флаг
        with self.lock:
            self._slot_released.discard(item_id)

        self.task_queue.put((item_id, url, out_dir))
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
            if s.startswith(("✅", "❌", "⛔")):
                continue
            url = self.url_by_item.get(item) or self.tree.set(item, "title")
            self.set_status(item, "🟡 Подготовка...")
            self.start_item(item, url, out_dir)
