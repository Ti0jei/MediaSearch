import os
import sys
import time
import re
import logging
import threading
from tkinter import messagebox
import queue
# ❗ Правильные импорты
from uc_driver import _safe_get_driver
from kino_pub_downloader import download


# =============== Download Manager (3 параллельные загрузки) ===============
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
        # 🔥 Новая очередь задач + диспетчер
        self.task_queue = queue.Queue()
        threading.Thread(target=self._dispatcher, daemon=True).start()
    # --- UI-safe обновления ---
    def _dispatcher(self):
        """Постоянно ждёт задач и запускает worker ТОЛЬКО когда есть место."""
        while True:
            item_id, url, out_dir = self.task_queue.get()   # ждём задачу

            # ЖДЁМ СВОБОДНЫЙ СЛОТ ❗
            self.sema.acquire()

            # Теперь запускаем worker
            t = threading.Thread(
                target=self._worker,
                args=(item_id, url, out_dir),
                daemon=True
            )
            self.threads[item_id] = t
            t.start()


    def _ui(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def set_status(self, item_id, text):
        self._ui(self.tree.set, item_id, "status", text)

    def inc_active(self):
        with self.lock:
            self.active += 1
        self._ui(self.counter_label.config, text=f"Активно: {self.active} / {self.MAX_PARALLEL}")

    def dec_active(self):
        with self.lock:
            self.active = max(0, self.active - 1)
        self._ui(self.counter_label.config, text=f"Активно: {self.active} / {self.MAX_PARALLEL}")

    def can_start(self, item_id):
        status = self.tree.set(item_id, "status")
        return (not self.stop_flag) and (status not in ("✅ Готово", "❌ Ошибка"))

    def stop_all(self):
        self.stop_flag = True
        messagebox.showinfo("Остановлено", "Новые загрузки не будут запускаться.\nТекущие завершатся.")
    
    # --- Обёртка вокруг фактической загрузки ---
    def _worker(self, item_id, url, out_dir):
        import traceback
        sys.stdout.flush()
        os.environ["PYTHONUNBUFFERED"] = "1"

        if not self.can_start(item_id):
            return

        self.set_status(item_id, "🟡 Подготовка...")
        time.sleep(0.25)

        if not self.can_start(item_id):
            return

        # ❗❗❗ УБИРАЕМ self.sema.acquire() — диспетчер уже сделал это!
        self.inc_active()
        self.set_status(item_id, "🔵 Загрузка...")


        drv = None
        try:
            # ❗ Теперь UC берём из uc_driver.py
            # 🔥 Берём драйвер из пула, а НЕ создаём новый UC каждый раз!
            if self.pool:
                drv = self.pool.acquire()
            else:
                drv = _safe_get_driver(
                    status_cb=lambda m: print(m),
                    headless=False,
                    suppress=True,
                    need_login_hint=False
                )

            from kino_parser import load_cookies

            # прогрев не нужен — downloader сам загрузит cookies и проверит сессию
            pass


            def _status_proxy(msg):
                text = str(msg)

                # ---- Заголовок файла ----
                try:
                    m = re.search(r'(?:🎬\s*)?(?:Файл|Название)\s*:\s*(.+)', text)
                    if m and hasattr(self, "ui_set_title"):
                        raw = m.group(1).strip().strip('"\'')
                        nice = os.path.splitext(os.path.basename(raw))[0]
                        self.ui_set_title(item_id, nice)
                except:
                    pass

                 # ---- Фильтр UI статусов ----
                # старт видео
                if "⬇️ Видео" in text or "Скачиваю видео" in text:
                    self.set_status(item_id, "🔵 Видео…")

                # старт аудио
                elif text.startswith("⬇️ Аудио") or "Скачиваю аудио" in text:
                    self.set_status(item_id, "🔵 Аудио…")

                # MUX идёт — счётчик НЕ трогаем
                elif "Муксую" in text or "MUX…" in text:
                    self.set_status(item_id, "🟣 MUX…")

                # Ошибка MUX — считаем как финал работы, уменьшаем active
                elif "Ошибка MUX" in text:
                    self.set_status(item_id, "❌ Ошибка MUX")
                    self.dec_active()

                # Успех — любое "✅ ..."
                elif text.startswith("✅ "):
                    self.set_status(item_id, "✅ Готово")
                    self.dec_active()

                # Остальное — только в лог
                else:
                    print(text)


                




            ok = download(
                url,
                out_dir,
                status_cb=_status_proxy,
                driver=drv
            )

            # Здесь НЕ ставим "Готово" — финальный статус приходит из HLS по msg "✅ ...".
            if not ok:
                cur = self.tree.set(item_id, "status")
                if not str(cur).startswith("❌"):
                    self.set_status(item_id, "❌ Ошибка загрузки")
                self.dec_active()


        

        except Exception as e:
            err_text = f"Ошибка скачивания: {e}\n{traceback.format_exc()}"
            logging.error(err_text)
            self.set_status(item_id, f"❌ {e}")
            print(err_text, flush=True)

        finally:
            try:
                if self.pool:
                    self.pool.release(drv)
                else:
                    drv.quit()
            except:
                pass


            self.sema.release()


    # --- Публичные методы запуска ---
    def start_item(self, item_id, url, out_dir):
        if not self.can_start(item_id):
            return
        self.url_by_item[item_id] = url

        # ❗ Вместо запуска worker — кладём задачу в очередь
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
