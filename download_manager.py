import os
import sys
import time
import re
import logging
import threading
from tkinter import messagebox

# ❗ Правильные импорты
from uc_driver import _safe_get_driver
from kino_pub_downloader import download


# =============== Download Manager (3 параллельные загрузки) ===============
class DownloadManager:
    def __init__(self, root, tree, counter_label, max_parallel=3, pool=None):
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

    # --- UI-safe обновления ---
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

        drv = None
        used_pool = False

        try:
            if not self.can_start(item_id):
                return

            # начальный статус (дублирует add_row, но ну и ладно)
            self.set_status(item_id, "🟡 Подготовка...")
            time.sleep(0.25)

            if not self.can_start(item_id):
                return

            # ждём свободный слот
            self.set_status(item_id, "⏳ Ожидание очереди…")
            self.sema.acquire()
            self.inc_active()
            self.set_status(item_id, "🔧 Инициализация Chromium…")

            # --- получаем драйвер ---
            if self.pool:
                drv = self.pool.acquire(timeout=30)
                used_pool = True
            else:
                # на всякий случай — если будем отлаживать exe с видимым окном
                drv = _safe_get_driver(
                    status_cb=lambda m: print(m),
                    headless=False,
                    suppress=True,
                    need_login_hint=False
                )

            # драйвер готов — переходим к загрузке
            self.set_status(item_id, "🔵 Загрузка...")

            def _status_proxy(msg):
                # обновляем статус
                self.set_status(item_id, str(msg))
                # вытаскиваем имя файла и подменяем title
                try:
                    m = re.search(r'(?:🎬\s*)?(?:Файл|Название)\s*:\s*(.+)', str(msg))
                    if m and hasattr(self, "ui_set_title"):
                        raw = m.group(1).strip().strip('"\'')
                        nice = os.path.splitext(os.path.basename(raw))[0]
                        self.ui_set_title(item_id, nice)
                except Exception:
                    pass

            ok = download(
                url,
                out_dir,
                status_cb=_status_proxy,
                driver=drv,
            )

            self.set_status(item_id, "✅ Готово" if ok else "❌ Ошибка")

        except Exception as e:
            err = f"Ошибка скачивания: {e}"
            logging.error(err)
            logging.error(traceback.format_exc())
            # чтобы в UI было видно, что что-то сломалось
            self.set_status(item_id, f"❌ {e}")

        finally:
            # аккуратно отпускаем ресурсы
            try:
                if drv:
                    if used_pool and self.pool:
                        try:
                            self.pool.release(drv)
                        except Exception:
                            pass
                    else:
                        try:
                            drv.quit()
                        except Exception:
                            pass
            finally:
                # всегда уменьшаем счётчик и отпускаем семафор
                try:
                    self.dec_active()
                except Exception:
                    pass
                try:
                    self.sema.release()
                except Exception:
                    pass


    # --- Публичные методы запуска ---
    def start_item(self, item_id, url, out_dir):
        if not self.can_start(item_id):
            return
        self.url_by_item[item_id] = url  # <-- добавили
        t = threading.Thread(target=self._worker, args=(item_id, url, out_dir), daemon=True)
        self.threads[item_id] = t
        t.start()


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
