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
    def __init__(self, root, tree, counter_label, max_parallel=2, pool=None):
        self.root = root
        self.tree = tree
        self.url_by_item = {}  # item_id -> original URL
        self.counter_label = counter_label
        self.MAX_PARALLEL = max_parallel
        self.pool = pool

        self.sema = threading.Semaphore(self.MAX_PARALLEL)
        self.lock = threading.Lock()
        self.active = 0
        self.stop_flag = False
        self.threads = {}  # item_id -> Thread

        # какие item_id уже "освободили слот" (и по счётчику, и по семафору)
        self._slot_released = set()

        # очередь задач + диспетчер
        self.task_queue = queue.Queue()
        threading.Thread(target=self._dispatcher, daemon=True).start()

    # ---------- утилиты UI ----------
    def _dispatcher(self):
        logging.info("Dispatcher thread started")
        while True:
            try:
                item_id, url, out_dir = self.task_queue.get()
                logging.info("Dispatcher got task: %s %s", item_id, url)

                # ждём свободный слот
                self.sema.acquire()

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
        self.root.after(0, lambda: func(*args, **kwargs))

    def set_status(self, item_id, text):
        self._ui(self.tree.set, item_id, "status", text)

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
        status = self.tree.set(item_id, "status")
        return (not self.stop_flag) and (status not in ("✅ Готово", "❌ Ошибка"))

    def stop_all(self):
        self.stop_flag = True
        messagebox.showinfo(
            "Остановлено",
            "Новые загрузки не будут запускаться.\nТекущие завершатся.",
        )

    # ---------- worker ----------
    def _worker(self, item_id, url, out_dir):
        import traceback

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
                            nice = os.path.splitext(os.path.basename(raw))[0]
                            self.ui_set_title(item_id, nice)
                    except Exception:
                        pass

                    # ---- Фильтр UI статусов ----
                    if "⬇️ Видео" in text or "Скачиваю видео" in text:
                        self.set_status(item_id, "🔵 Видео…")

                    elif text.startswith("⬇️ Аудио") or "Скачиваю аудио" in text:
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

            ok = download(
                url,
                out_dir,
                status_cb=_status_proxy,
                driver=drv,
            )

            # Если download() отработал, но не было "Муксую" (ошибка раньше) —
            # слот всё ещё не освобождён, сделаем это здесь.
            if not ok:
                cur = self.tree.set(item_id, "status")
                if not str(cur).startswith("❌"):
                    self.set_status(item_id, "❌ Ошибка загрузки")

        except Exception as e:
            err_text = f"Ошибка скачивания: {e}\n{traceback.format_exc()}"
            logging.error(err_text)
            self.set_status(item_id, f"❌ {e}")
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
        if not self.can_start(item_id):
            return
        self.url_by_item[item_id] = url

        # сбрасываем возможный прошлый флаг
        with self.lock:
            self._slot_released.discard(item_id)

        self.task_queue.put((item_id, url, out_dir))
        self.set_status(item_id, "🟡 Ожидает...")

    def start_all(self, out_dir):
        self.stop_flag = False
        items = list(self.tree.get_children())
        for item in items:
            status = self.tree.set(item, "status")
            if status in ("✅ Готово", "❌ Ошибка"):
                continue
            url = self.url_by_item.get(item) or self.tree.set(item, "title")
            self.set_status(item, "🟡 Подготовка...")
            self.start_item(item, url, out_dir)
