"""
Kino.pub downloader logic (поиск + скачивание + статусы) + ОЧЕРЕДЬ.
Работает без GUI, но отдаёт прогресс через callback.
Использует движок загрузки из kino_hls.py и UC-логику из uc_driver.py.
"""

import os
import re
import time
import threading
import queue
from typing import Callable, List, Optional, Tuple

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Наши модули
from kino_hls import download_by_item_url, get_hls_info, start_hls_download
from kino_parser import load_cookies
from uc_driver import (
    _safe_get_driver,
    _check_login,
    _check_login_on,
    login_to_kino,
    DriverPool,
)

KINOPUB_BASE = "https://kino.pub"


# -------------------------------------------------------
# Утилита логирования
# -------------------------------------------------------
def _log(status_cb: Optional[Callable[[str], None]], msg: str):
    print(msg)
    if status_cb:
        try:
            status_cb(msg)
        except Exception:
            pass


# -------------------------------------------------------
# Поиск по названию на сайте
# -------------------------------------------------------
def search_titles(query: str, limit=1, status_cb=None, driver=None) -> List[Tuple[str, str]]:
    if driver is None:
        raise RuntimeError("search_titles ожидает активный driver")

    from urllib.parse import quote_plus
    url = f"{KINOPUB_BASE}/item/search?query=" + quote_plus(query)
    _log(status_cb, f"🔍 Поиск: {url}")
    driver.get(url)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".item-title a"))
        )
    except Exception:
        _log(status_cb, "⚠️ Ничего не найдено или страница не загрузилась.")
        return []

    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a in soup.select(".item-title a[href*='/item/']")[:limit]:
        title = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = KINOPUB_BASE + href
        results.append((title, href))

    _log(status_cb, f"🔎 Найдено: {len(results)} результат(ов).")
    return results


# -------------------------------------------------------
# Извлечение “красивого” имени файла
# -------------------------------------------------------
def _extract_display_name(driver, item_url) -> str:
    """Возвращает 'Русское название (YYYY)' с чисткой служебных символов."""
    try:
        driver.get(item_url)
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "meta[property='og:title'], h1, .item-title"))
        )
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        title_h1 = soup.select_one("h1, .item-title")
        title_ru = title_h1.get_text(strip=True) if title_h1 else (driver.title or "").strip()

        title_ru = re.split(r"[_/]", title_ru)[0].strip()
        title_ru = re.sub(r'\s+\(\d{4}\)$', '', title_ru)

        year = None
        for tr in soup.select("table.table.table-striped tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                label = tds[0].get_text(" ", strip=True).lower()
                if any(k in label for k in ("год выхода", "год выпуска", "год")):
                    text = tds[1].get_text(" ", strip=True)
                    m = re.search(r"\b(19|20)\d{2}\b", text)
                    if m:
                        year = m.group(0)
                        break

        if not year:
            m = re.search(r"\b(19|20)\d{2}\b", html)
            year = m.group(0) if m else ""

        name = f"{title_ru} ({year})" if year else title_ru
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        return name or "video"

    except Exception:
        slug = re.sub(r"[#?].*$", "", item_url).rstrip("/").split("/")[-1]
        return (slug.replace("-", " ").strip() or "video")


# -------------------------------------------------------
# ОДНО скачивание (с возможностью передать внешний driver из пула)
# -------------------------------------------------------
def download(query_or_url: str, out_dir=".", status_cb=None, driver=None) -> bool:
    """
    Скачивание одного фильма.
    Если driver передан (из DriverPool) — используем его, иначе сами поднимем скрытый UC.
    """
    os.makedirs(out_dir, exist_ok=True)

    internal_driver = None
    try:
        # ======= ЕСЛИ ПЕРЕДАН driver (пул UC) =======
        if driver is not None:
            try:
                if not getattr(driver, "_kino_cookies_loaded", False):
                    driver.get(KINOPUB_BASE + "/")
                    load_cookies(driver)
                    driver.refresh()
                    setattr(driver, "_kino_cookies_loaded", True)
            except Exception as e:
                _log(status_cb, f"⚠️ Ошибка подгрузки cookies в драйвер пула: {e}")

            if not _check_login_on(driver, status_cb):
                _log(status_cb, "⚠️ Сессия неактивна — требуется вход.")
                return False

            use_driver = driver

        # ======= ЕСЛИ ДРАЙВЕР НЕ ПЕРЕДАН =======
        else:
            if not _check_login(status_cb):
                _log(status_cb, "⚠️ Сессия неактивна. Выполните вход.")
                return False

            internal_driver = _safe_get_driver(status_cb, headless=True, suppress=True)
            use_driver = internal_driver

            use_driver.get(KINOPUB_BASE + "/")
            load_cookies(use_driver)
            use_driver.refresh()

            if not _check_login_on(use_driver, status_cb):
                _log(status_cb, "⚠️ Cookies не помогли — требуется вход.")
                return False

        # ======= ДАЛЬШЕ ИСПОЛЬЗУЕМ use_driver =======

        # URL или поиск
        if not query_or_url.startswith("http"):
            results = search_titles(query_or_url, limit=1, status_cb=status_cb, driver=use_driver)
            if not results:
                _log(status_cb, "❌ Ничего не найдено.")
                return False
            _, item_url = results[0]
        else:
            item_url = query_or_url

        _log(status_cb, "📋 Извлекаю название...")
        display_name = _extract_display_name(use_driver, item_url)
        out_path = os.path.join(out_dir, display_name + ".mp4")
        _log(status_cb, f"🎬 Файл: {os.path.basename(out_path)}")

        _log(status_cb, f"🎬 Запуск загрузки через HLS...")
        ok = download_by_item_url(item_url, out_path, driver=use_driver)

        _log(status_cb, "✅ Готово!" if ok else "❌ Ошибка.")
        return ok

    except Exception as e:
        _log(status_cb, f"❌ Ошибка: {e}")
        return False

    finally:
        if internal_driver:
            try:
                internal_driver.quit()
            except Exception:
                pass


# -------------------------------------------------------
# Параллельная загрузка по уже известным URL (быстрый батч)
# -------------------------------------------------------
def download_multiple(urls, out_dir, status_cb=None):
    """
    Простой батч: последовательно берём драйвер из пула → достаём m3u8 → запускаем ffmpeg в отдельных потоках.
    """
    os.makedirs(out_dir, exist_ok=True)
    pool = DriverPool(max_drivers=2, status_cb=status_cb)
    threads = []
    try:
        for url in urls:
            drv = pool.acquire(timeout=10)
            try:
                video_m3u8, hdrs, audios = get_hls_info(url, driver=drv)
                if not video_m3u8:
                    _log(status_cb, f"⚠️ Пропущено: нет потоков для {url}")
                    continue

                # Красивое имя (не через поиск — быстро)
                safe_name = _extract_display_name(drv, url)
                out_path = os.path.join(out_dir, safe_name + ".mp4")

                t = start_hls_download(video_m3u8, audios, hdrs, out_path, status_cb)
                threads.append(t)

            finally:
                pool.release(drv)

        for t in threads:
            t.join()

    finally:
        pool.close_all()


# =======================================================
#                ОЧЕРЕДЬ ЗАГРУЗОК (онлайн докидка)
# =======================================================
class QueueDownloader:
    """
    Многопоточная очередь загрузок:
      - add(url_or_query): докидывает задачу на лету
      - concurrency: сколько параллельных загрузок
      - внутри — DriverPool, каждый воркер берёт драйвер, получает m3u8 и запускает ffmpeg
      - драйвер освобождается сразу после старта ffmpeg (чтобы не держать CDP-сессию)
    """

    def __init__(self, out_dir: str, concurrency: int = 2, status_cb: Optional[Callable[[str], None]] = None):
        self.out_dir = out_dir
        self.status_cb = status_cb
        os.makedirs(self.out_dir, exist_ok=True)

        self.q: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.pool = DriverPool(max_drivers=max(1, concurrency), status_cb=status_cb)

        self._active_ffmpeg_threads: set[threading.Thread] = set()
        self._ff_lock = threading.Lock()

        # поднимаем воркеры
        self.workers: list[threading.Thread] = []
        for i in range(max(1, concurrency)):
            t = threading.Thread(target=self._worker, name=f"dl-worker-{i+1}", daemon=True)
            t.start()
            self.workers.append(t)

        _log(self.status_cb, f"🧵 Очередь готова: параллельных загрузок = {concurrency}")

    def add(self, query_or_url: str):
        """Добавить задачу (URL или поисковый запрос)."""
        self.q.put(query_or_url)
        _log(self.status_cb, f"➕ В очередь: {query_or_url}")

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                task = self.q.get(timeout=0.2)
            except queue.Empty:
                continue

            drv = None
            try:
                drv = self.pool.acquire(timeout=15)

                # быстрый прогрев cookies (один раз на драйвер)
                try:
                    if not getattr(drv, "_kino_cookies_loaded", False):
                        drv.get(KINOPUB_BASE + "/")
                        load_cookies(drv)
                        drv.refresh()
                        setattr(drv, "_kino_cookies_loaded", True)
                except Exception as e:
                    _log(self.status_cb, f"⚠️ Ошибка подгрузки cookies: {e}")

                # если дали не URL — сначала ищем
                if not task.startswith("http"):
                    results = search_titles(task, limit=1, status_cb=self.status_cb, driver=drv)
                    if not results:
                        _log(self.status_cb, f"❌ Не найдено: {task}")
                        self.q.task_done()
                        self.pool.release(drv)
                        continue
                    _, item_url = results[0]
                else:
                    item_url = task

                # красивое имя файла (русское + год)
                try:
                    display_name = _extract_display_name(drv, item_url)
                except Exception:
                    # не критично — fallback
                    display_name = os.path.basename(item_url).split("?")[0]
                out_path = os.path.join(self.out_dir, display_name + ".mp4")
                _log(self.status_cb, f"🎬 [{threading.current_thread().name}] → {os.path.basename(out_path)}")

                # получаем m3u8/заголовки/аудио
                video_m3u8, hdrs, audios = get_hls_info(item_url, driver=drv)
                if not video_m3u8:
                    _log(self.status_cb, f"⚠️ Пропуск (нет HLS): {item_url}")
                    self.q.task_done()
                    self.pool.release(drv)
                    continue

                # запускаем ffmpeg-поток и больше драйвер не нужен
                ff_t = start_hls_download(video_m3u8, audios, hdrs, out_path, self.status_cb)
                with self._ff_lock:
                    self._active_ffmpeg_threads.add(ff_t)

                # отдельный наблюдатель за конкретной загрузкой
                threading.Thread(
                    target=self._wait_and_detach, args=(ff_t, out_path), daemon=True
                ).start()

            except Exception as e:
                _log(self.status_cb, f"❌ Ошибка воркера: {e}")

            finally:
                try:
                    if drv:
                        self.pool.release(drv)
                finally:
                    self.q.task_done()

    def _wait_and_detach(self, ff_thread: threading.Thread, out_path: str):
        try:
            ff_thread.join()
            _log(self.status_cb, f"✅ Скачано: {out_path}")
        finally:
            with self._ff_lock:
                self._active_ffmpeg_threads.discard(ff_thread)

    def wait_all(self):
        """Дождаться, когда очередь опустеет и завершатся все текущие ffmpeg-потоки."""
        self.q.join()
        while True:
            with self._ff_lock:
                alive = [t for t in self._active_ffmpeg_threads if t.is_alive()]
            if not alive:
                break
            time.sleep(0.2)

    def stop(self):
        """Остановить воркеров и закрыть драйверы после завершения текущих задач."""
        self.stop_event.set()
        # дождаться очереди и текущих ffmpeg
        self.wait_all()

        # остановить воркеров
        for _ in self.workers:
            self.q.put_nowait("")  # разбудить
        for t in self.workers:
            t.join(timeout=1.0)

        # закрыть все драйверы пула
        self.pool.close_all()
        _log(self.status_cb, "🧹 Очередь остановлена, драйверы закрыты.")


# -------------------------------------------------------
# Синглтон-очередь (удобно дергать из GUI)
# -------------------------------------------------------
_queue_singleton: Optional[QueueDownloader] = None

def get_queue(out_dir: str, concurrency: int = 2, status_cb=None) -> QueueDownloader:
    """
    Получить (или создать) глобальную очередь загрузок.
    Пример:
        q = get_queue("Downloads", concurrency=2, status_cb=print)
        q.add("https://kino.pub/item/view/12345/...")
        q.add("https://kino.pub/item/view/67890/...")
        # можно докидывать ещё — по ходу
    """
    global _queue_singleton
    if _queue_singleton is None:
        _queue_singleton = QueueDownloader(out_dir=out_dir, concurrency=concurrency, status_cb=status_cb)
    return _queue_singleton
