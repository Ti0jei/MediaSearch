# ocr_tools.py
import os
import re
import zipfile
import tempfile
import logging

from tkinter import filedialog, messagebox

import cv2
import numpy as np
import pytesseract
from pytesseract import Output
import easyocr
import torch
BASE_DIR = os.path.dirname(__file__)
BELL_TEMPLATE_PATH = os.path.join(BASE_DIR, "res", "bell_icon.png")
# Насколько высока вертикальная полоса вокруг колокольчика (в "высотах" колокольчика)
HEADER_BAND_MULT = 2.0  # раньше было ~3.0, теперь уже чуть меньше

# --- DEBUG: сохранять кропы шапки в файлы ---
DEBUG_OCR = True # поставь True, когда хочешь посмотреть, как режется шапка
DEBUG_OCR_DIR = os.path.join(BASE_DIR, "ocr_debug")
if DEBUG_OCR:
    os.makedirs(DEBUG_OCR_DIR, exist_ok=True)


## Загружаем шаблон колокольчика один раз
if os.path.isfile(BELL_TEMPLATE_PATH):
    _bell_template = cv2.imread(BELL_TEMPLATE_PATH, cv2.IMREAD_UNCHANGED)
    if _bell_template is not None:
        # если PNG с альфой — конвертим в BGR
        if len(_bell_template.shape) == 3 and _bell_template.shape[2] == 4:
            _bell_template = cv2.cvtColor(_bell_template, cv2.COLOR_BGRA2BGR)
else:
    _bell_template = None

def _find_bell_by_template(img: np.ndarray):
    """
    Находит зелёный колокольчик по шаблону bell_icon.png.
    Возвращает (x, y, w, h) или None.
    """
    if _bell_template is None or img is None:
        return None

    img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bell_gray = cv2.cvtColor(_bell_template, cv2.COLOR_BGR2GRAY)

    h_t, w_t = bell_gray.shape[:2]

    best_val = 0.0
    best_rect = None

    # Несколько масштабов, чтобы пережить разные DPI / кропы
    for scale in (0.6, 0.75, 0.9, 1.0, 1.1, 1.25):
        templ = cv2.resize(bell_gray, None, fx=scale, fy=scale)
        th, tw = templ.shape[:2]

        if th >= img_gray.shape[0] or tw >= img_gray.shape[1]:
            continue

        res = cv2.matchTemplate(img_gray, templ, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

        if max_val > best_val:
            best_val = max_val
            best_rect = (max_loc[0], max_loc[1], tw, th)

    # Порог уверенности — подбираемый; 0.6–0.7 обычно норм
    if best_val < 0.6:
        return None

    return best_rect
def _debug_save_header_crop(image_path: str, header_img: np.ndarray) -> None:
    """
    Если включён DEBUG_OCR, сохраняет кроп шапки в папку ocr_debug.
    Имя файла: <basename>_header.png
    """
    if not DEBUG_OCR:
        return
    if header_img is None or header_img.size == 0:
        return

    base = os.path.splitext(os.path.basename(image_path))[0]
    out_path = os.path.join(DEBUG_OCR_DIR, f"{base}_header.png")
    try:
        cv2.imwrite(out_path, header_img)
    except Exception as e:
        logging.warning("DEBUG_OCR: не удалось сохранить кроп %s: %s", out_path, e)

# Насколько жёстко отбрасывать мусор OCR
STRICT_OCR_FILTER = True  # если что, можно ослабить до False


# --------------------------------------------------------------------------
# РУЧНЫЕ ПРАВКИ ОСОБО КРИВЫХ РЕЗУЛЬТАТОВ OCR
# --------------------------------------------------------------------------

MANUAL_OVERRIDES = {
    # старые кейсы
    "Champ: Omex (5) Erased": "Erased",
    "SHH UNIS IHOUCHT hours, nuts": "44 Minutes: The North Hollywood Shoot-Out",
    "IBSP6IBIBlact HOmEa": "Blast",
    "MpeANoKeHMe ThelPropesition UBD": "The Proposition",
    "MpeANoKeHMe ThelPropesition": "The Proposition",
}


def apply_manual_overrides(title: str | None) -> str | None:
    """Ручные фиксы для самых убитых строк OCR."""
    if not title:
        return title

    # 1) точные совпадения
    if title in MANUAL_OVERRIDES:
        return MANUAL_OVERRIDES[title]

    # 2) мягкие паттерны
    compact = re.sub(r"\s+", "", title).lower()

    if "hours,nuts" in compact:
        return "44 Minutes: The North Hollywood Shoot-Out"

    if "thelpropesit" in compact or "thelpropeesit" in compact:
        return "The Proposition"

    if "blacthomea" in compact:
        return "Blast"

    return title


# ============================================================================ #
# НАСТРОЙКА TESSERACT
# ============================================================================ #

TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

try:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
except Exception as e:
    logging.warning("Не удалось установить путь к Tesseract: %s", e)


# ============================================================================ #
# НАСТРОЙКА EasyOCR + CUDA
# ============================================================================ #

env_gpu = os.environ.get("MOVIETOOLS_EASYOCR_GPU", "").lower()

try:
    cuda_available = torch.cuda.is_available()
except Exception:
    cuda_available = False

if env_gpu in ("0", "false", "no", "off"):
    use_gpu = False
elif env_gpu in ("1", "true", "yes", "on"):
    # принудительно пробуем GPU, если реально есть CUDA
    use_gpu = cuda_available
else:
    # авто: если CUDA есть — используем GPU, иначе CPU
    use_gpu = cuda_available

try:
    _easyocr_reader = easyocr.Reader(["en", "ru"], gpu=use_gpu)
    HAS_EASYOCR = True
    logging.info(
        "EasyOCR инициализирован: gpu=%s, cuda_available=%s",
        use_gpu,
        cuda_available,
    )
except Exception as e:
    HAS_EASYOCR = False
    logging.warning("EasyOCR недоступен, работаем только с Tesseract: %s", e)


# ============================================================================ #

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

STOPWORDS = {
    "HD",
    "FULL",
    "FULLHD",
    "1080P",
    "720P",
    "2160P",
    "4K",
    "UHD",
    "AC3",
    "AAC",
    "DTS",
    "DDP",
    "ATMOS",
    "BDRIP",
    "BLURAY",
    "BRRIP",
    "WEB",
    "WEBRIP",
    "H264",
    "H.264",
    "X264",
    "X265",
    "HEVC",
}


# ============================================================================ #
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ВЫБОРА ФАЙЛОВ
# ============================================================================ #

def _collect_image_paths_from_zip(zip_path: str) -> list[str]:
    """Распаковать из ZIP только картинки во временную папку и вернуть их пути."""
    paths: list[str] = []
    tmp_dir = tempfile.mkdtemp(prefix="ms_ocr_")

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                ext = os.path.splitext(name)[1].lower()
                if ext not in IMG_EXT:
                    continue
                out_path = os.path.join(tmp_dir, os.path.basename(name))
                with zf.open(name) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
                paths.append(out_path)
    except Exception as e:
        logging.error("Ошибка распаковки ZIP %s: %s", zip_path, e)

    return paths


def _choose_images_for_ocr() -> list[str]:
    """
    Диалог выбора источника:
    - ZIP (распаковка всех картинок)
    - одиночная картинка (берём все картинки из этой же папки)
    """
    path = filedialog.askopenfilename(
        title="Выберите ZIP или картинку с постерами",
        filetypes=[
            ("Images or ZIP", "*.jpg;*.jpeg;*.png;*.webp;*.zip"),
            ("All files", "*.*"),
        ],
    )
    if not path:
        return []

    ext = os.path.splitext(path)[1].lower()

    if ext == ".zip":
        return _collect_image_paths_from_zip(path)

    if ext in IMG_EXT:
        folder = os.path.dirname(path)
        files = [
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in IMG_EXT
        ]
        files.sort()
        return files

    return []


# ============================================================================ #
# 1) ШАПКА (СТРОКА С НАЗВАНИЕМ НАД ПЛЕЕРОМ)
# ============================================================================ #

def _crop_header_around_bell(img: np.ndarray) -> np.ndarray | None:
    """
    Кроп шапки: берём вертикальную полосу по всей ширине,
    симметрично вокруг колокольчика (вверх и вниз).
    """
    if img is None:
        return None

    h, w = img.shape[:2]

    # 1. Пробуем найти по шаблону
    bell_box = _find_bell_by_template(img)

    if bell_box is None:
        # 2. HSV-фоллбэк
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower_green = np.array([40, 80, 80], dtype=np.uint8)
        upper_green = np.array([85, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_green, upper_green)

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return None

        best_box = None
        best_y = h + 1
        for c in contours:
            x, y, ww, hh = cv2.boundingRect(c)   # <-- тут просто '='
            area = ww * hh
            if area < 50:
                continue
            if y < best_y:
                best_y = y
                best_box = (x, y, ww, hh)

        if best_box is None:
            return None

        bell_box = best_box

    x, y, ww, hh = bell_box
    cy = y + hh // 2

    # стало уже: вместо каких-то фиксированных 90/60 берём k * hh
    band_half = int(hh * HEADER_BAND_MULT)

    top = max(0, cy - band_half)
    bottom = min(h, cy + band_half)

    header = img[top:bottom, 0:w]

    if header.size == 0 or header.shape[0] < 20:
        return None

    return header




def _get_header_region(img: np.ndarray) -> np.ndarray | None:
    """
    Возвращает фрагмент шапки:
    - сначала пробуем умную обрезку вокруг зелёного колокольчика;
    - если не получилось — берём универсальную полосу по высоте.
    """
    if img is None:
        return None

    h, w = img.shape[:2]

    header = _crop_header_around_bell(img)
    if header is None or header.size == 0 or header.shape[0] < 40:
        top = int(h * 0.14)  # зона заголовка между поиском и плеером
        bottom = int(h * 0.32)
        header = img[top:bottom, :]

    if header is None or header.size == 0:
        return None

    return header


def _extract_english_from_fragment(fragment: str) -> str | None:
    fragment = fragment.strip()
    if not fragment:
        return None

    low_full = fragment.lower()

    # --- спец-кейс: русское "13 дней 13 ночей" (любая вариация) -> "13 jours, 13 nuits"
    # ловим любые OCR-варианты: "дней/днеи", "ночей/ночи/ночь" и т.п.
    if fragment.count("13") >= 2 and ("дн" in low_full or "ноч" in low_full):
        return "13 jours, 13 nuits"


    def has_cyr(s: str) -> bool:
        return bool(re.search(r"[\u0400-\u04FF]", s))

    def has_lat(s: str) -> bool:
        return bool(re.search(r"[A-Za-z]", s))

    # 1. Если есть кириллица — режем по последней кириллической букве
    if has_cyr(fragment):
        last_cyr = None
        for m in re.finditer(r"[\u0400-\u04FF]", fragment):
            last_cyr = m
        if last_cyr is not None:
            eng_part = fragment[last_cyr.end():]
        else:
            eng_part = fragment
    else:
        # 2. Кириллицы нет: берём от первого латинского слова (француз/англ.)
        tokens = fragment.split()
        if not tokens:
            return None

        first_lat_idx = None
        for i, tok in enumerate(tokens):
            if has_lat(tok):
                first_lat_idx = i
                break

        if first_lat_idx is None:
            return None

        # захватываем предшествующее число (кейс '13 jours, 13 nuits')
        start_idx = first_lat_idx
        if first_lat_idx > 0 and tokens[first_lat_idx - 1].isdigit():
            start_idx = first_lat_idx - 1

        eng_tokens = tokens[start_idx:]
        eng_part = " ".join(eng_tokens)

    eng_part = eng_part.strip()
    if not eng_part:
        return None

    tokens = eng_part.split()
    if not tokens:
        return None

    # 3. Кейс '234 Unit 234' -> 'Unit 234'
    if (
        len(tokens) <= 3
        and tokens[0].isdigit()
        and any(re.search(r"[A-Za-z]", t) for t in tokens[1:])
    ):
        tokens = tokens[1:]
        if not tokens:
            return None

    # 4. Срезаем тех. хвост (UHD, 4K, HD, AC3 и т.п.)
    while tokens:
        clean = re.sub(r"[^A-Za-z0-9]+", "", tokens[-1]).upper()
        if clean and clean in STOPWORDS:
            tokens.pop()
            continue
        break

    # 5. Убираем мусор HO / A / HOA в конце
    while len(tokens) > 3:
        clean_last = re.sub(r"[^A-Za-z0-9]+", "", tokens[-1]).upper()
        if clean_last in {"A", "HO", "HOA"}:
            tokens.pop()
            continue
        break

    if not tokens:
        return None

    candidate = " ".join(tokens)
    candidate = candidate.strip(" -—:.,;|/\\")
    candidate = re.sub(r"\s+", " ", candidate)

    if len(candidate) < 2 or not re.search(r"[A-Za-z]", candidate):
        return None

    return candidate


def _ocr_title_from_header(img: np.ndarray) -> str | None:
    """
    OCR по шапке только (Tesseract).
    НИКАКОГО постера.
    """
    if img is None:
        return None

    header = _get_header_region(img)
    if header is None or header.size == 0:
        return None

    header = cv2.resize(header, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(header, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)

    texts: list[str] = []

    # a) OCR по серому
    try:
        t1 = pytesseract.image_to_string(
            gray,
            lang="eng+rus",
            config="--psm 6",
        )
        t1 = re.sub(r"[|«»]", " ", t1)
        t1 = re.sub(r"\s+", " ", t1).strip()
        if t1:
            texts.append(t1)
    except Exception:
        pass

    # b) OCR по бинарному
    try:
        _, gray_bin = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        t2 = pytesseract.image_to_string(
            gray_bin,
            lang="eng+rus",
            config="--psm 6",
        )
        t2 = re.sub(r"[|«»]", " ", t2)
        t2 = re.sub(r"\s+", " ", t2).strip()
        if t2:
            texts.append(t2)
    except Exception:
        pass

    if not texts:
        return None

    text = max(texts, key=len)
    logging.debug("OCR header raw (tesseract): %s", text)

    up = text.upper()
    pos_hd = up.rfind("HD")

    fragments: list[str] = []
    if pos_hd != -1:
        fragments.append(text[:pos_hd])
    fragments.append(text)

    for fragment in fragments:
        fragment = fragment.strip()
        if not fragment:
            continue
        title = _extract_english_from_fragment(fragment)
        if title:
            return title

    return None


# ============================================================================ #
# EasyOCR: только шапка
# ============================================================================ #

def _easyocr_read_words(bgr_img: np.ndarray, min_conf: float = 0.4) -> list[dict]:
    """
    Универсальный helper: прогоняет EasyOCR по BGR-изображению
    и возвращает список "слов" вида:
        {"text": str, "x": int, "y": int, "h": int, "conf": float}
    """
    if not HAS_EASYOCR or bgr_img is None or bgr_img.size == 0:
        return []

    rgb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)

    try:
        results = _easyocr_reader.readtext(rgb, detail=1, paragraph=False)
    except Exception as e:
        logging.warning("EasyOCR ошибка: %s", e)
        return []

    words: list[dict] = []
    for bbox, text, conf in results:
        try:
            conf = float(conf)
        except Exception:
            conf = 0.0

        text = (text or "").strip()
        if not text:
            continue
        if conf < min_conf:
            continue

        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        x = int(min(xs))
        y = int(min(ys))
        h = int(max(ys) - min(ys)) or 1

        words.append(
            {
                "text": text,
                "x": x,
                "y": y,
                "h": h,
                "conf": conf,
            }
        )

    return words


def _ocr_title_from_header_easyocr(img: np.ndarray) -> str | None:
    """
    OCR шапки через EasyOCR.
    """
    if not HAS_EASYOCR or img is None:
        return None

    header = _get_header_region(img)
    if header is None or header.size == 0:
        return None

    words = _easyocr_read_words(header, min_conf=0.4)
    if not words:
        return None

    words.sort(key=lambda w: (w["y"], w["x"]))
    lines: list[list[dict]] = []
    last_y = None

    for w_ in words:
        if last_y is None or abs(w_["y"] - last_y) > w_["h"] * 0.8:
            lines.append([])
            last_y = w_["y"]
        lines[-1].append(w_)
        last_y = (last_y * 0.7 + w_["y"] * 0.3) if last_y is not None else w_["y"]

    all_text_parts = []
    for line_words in lines:
        line_words.sort(key=lambda w_: w_["x"])
        all_text_parts.extend(w_["text"] for w_ in line_words)

    if not all_text_parts:
        return None

    text = " ".join(all_text_parts)
    text = re.sub(r"[|«»]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    logging.debug("OCR header raw (easyocr): %s", text)

    up = text.upper()
    pos_hd = up.rfind("HD")

    fragments: list[str] = []
    if pos_hd != -1:
        fragments.append(text[:pos_hd])
    fragments.append(text)

    for fragment in fragments:
        fragment = fragment.strip()
        if not fragment:
            continue
        title = _extract_english_from_fragment(fragment)
        if title:
            return title

    return None

def _ocr_russian_title_from_header(img: np.ndarray) -> str | None:
    """
    Фоллбэк: пытаемся вытащить РУССКОЕ название из шапки,
    НО только если в шапке почти нет латиницы (т.е. фильм на сайте без
    английского названия).
    """
    if img is None:
        return None

    header = _get_header_region(img)
    if header is None or header.size == 0:
        return None

    header = cv2.resize(header, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(header, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)

    texts: list[str] = []

    try:
        t1 = pytesseract.image_to_string(
            gray,
            lang="rus+eng",
            config="--psm 6",
        )
        t1 = re.sub(r"[|«»]", " ", t1)
        t1 = re.sub(r"\s+", " ", t1).strip()
        if t1:
            texts.append(t1)
    except Exception:
        pass

    try:
        _, gray_bin = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        t2 = pytesseract.image_to_string(
            gray_bin,
            lang="rus+eng",
            config="--psm 6",
        )
        t2 = re.sub(r"[|«»]", " ", t2)
        t2 = re.sub(r"\s+", " ", t2).strip()
        if t2:
            texts.append(t2)
    except Exception:
        pass

    if not texts:
        return None

    text = max(texts, key=len)

    # если латиницы хоть немного есть — считаем, что это «английский» кейс
    latin_letters = len(re.findall(r"[A-Za-z]", text))
    if latin_letters >= 5:
        return None

    # дальше вынимаем только слова с кириллицей
    tokens = text.split()
    rus_tokens: list[str] = []
    for tok in tokens:
        if re.search(r"[\u0400-\u04FF]", tok):
            # выкидываем совсем короткий мусор типа "да", "но", "и"
            if len(tok) <= 2:
                continue
            rus_tokens.append(tok)

    if not rus_tokens:
        return None

    rus_title = " ".join(rus_tokens)
    rus_title = re.sub(r"\s+", " ", rus_title).strip()

    if len(rus_title) < 4:
        return None

    return rus_title



# ============================================================================ #
# НОРМАЛИЗАЦИЯ + ФИЛЬТР
# ============================================================================ #

_SMALL_TITLE_WHITELIST = {"Up", "It", "Us", "Pi"}


def _normalize_title(title: str) -> str:
    """
    Лёгкая нормализация + фиксы спец-кейсов.
    Агрессивно выкидываем мусор (русские токены, очень короткие, тех.хвосты).
    Плюс спец-фиксы под часто встречающиеся фильмы.
    """
    t = re.sub(r"\s+", " ", (title or "")).strip()
    if not t:
        return ""

    # --- первичная чистка токенов ---
    tokens = t.split()
    cleaned_tokens: list[str] = []

    for tok in tokens:
        # выкидываем токены с нелатинскими буквами (русский и т.п.)
        if re.search(r"[^\x00-\x7F]", tok):
            continue

        letters = re.findall(r"[A-Za-z]", tok)
        digits = re.findall(r"\d", tok)
        if len(letters) + len(digits) == 0:
            continue

        # короткий мусор (1–2 символа), кроме whitelista
        if len(tok) < 3 and tok not in _SMALL_TITLE_WHITELIST:
            continue

        cleaned_tokens.append(tok)

    if not cleaned_tokens:
        return ""

    t = " ".join(cleaned_tokens)
    low = t.lower()
    letters_only = re.sub(r"[^a-z]", "", low)

    # --- ЯВНЫЕ НЕ-НАЗВАНИЯ / системный мусор ---
    if re.fullmatch(r"(?i)champ:?", t):
        return ""

    # --- СПЕЦ-КЕЙСЫ ПОД ТВОЙ НАБОР ФИЛЬМОВ ---

    # The One That Got Away — с мусором "regia", "jihat" и т.п.
    if "the one" in low and "got away" in low:
        return "The One That Got Away"

    # 13 jours, 13 nuits / 13 DAYS 13 NIGHTS
    if "13 jours" in low and "13 nuits" in low:
        return "13 jours, 13 nuits"
    if re.search(r"(?i)\b13\s+days?\b.*\b13\s+nights?\b", t):
        return "13 jours, 13 nuits"
    if re.fullmatch(r"(?i)jours\s+nuits", t):
        return "13 jours, 13 nuits"

    # Unit 234
    if re.fullmatch(r"(?i)unit\s*234", t):
        return "Unit 234"

    # An Innocent Man
    if re.fullmatch(r"(?i)innocent man", t):
        return "An Innocent Man"

    # The Art of War (часто "The Art War")
    if re.search(r"(?i)\bthe\s+art\b", t) and re.search(r"(?i)\bwar\b", t):
        return "The Art of War"

    # Любые OCR-варианты, где уцелели "enemy" и "lines"
    if "enemy" in low and "lines" in low:
        return "Behind Enemy Lines"

    # Collateral Damage
    if "coll" in low and "damage" in low and "teral" in low:
        return "Collateral Damage"
    if re.search(r"(?i)coll+ater?al\s+dam[a-z]ge", low.replace(" ", "")):
        return "Collateral Damage"
    if re.search(r"(?i)coll+teral\s+dar?nage", low.replace(" ", "")):
        return "Collateral Damage"

    # A Murder of Crows
    if re.search(r"(?i)murder\s+crows", t):
        return "A Murder of Crows"

    # Flight of the Phoenix
    if "phoenix" in low and "light" in low:
        return "Flight of the Phoenix"
    if "flightofthephoenix" in letters_only:
        return "Flight of the Phoenix"
    if re.search(r"(?i)\bflight\s+of\s+the\s+phoenix\b", t):
        return "Flight of the Phoenix"

    # 44 Minutes: The North Hollywood Shoot-Out
    if re.search(r"(?i)minutes:\s*the north hollywood shoot[- ]out", t):
        return "44 Minutes: The North Hollywood Shoot-Out"

    # Grupo7 -> Grupo 7
    t = re.sub(r"(?i)\bgrupo\s*7\b", "Grupo 7", t)
    t = re.sub(r"(?i)grupo7$", "Grupo 7", t)

    # --- срезаем короткие тех-хвосты / мусорные токены в конце ---
    tokens = t.split()
    while len(tokens) > 3:
        last = tokens[-1]
        clean = re.sub(r"[^A-Za-z0-9]", "", last)
        clean_up = clean.upper()

        if clean_up in STOPWORDS:
            tokens.pop()
            continue
        if re.fullmatch(r"[A-Z]{1,3}", clean_up):
            tokens.pop()
            continue
        if clean.isdigit() and not re.fullmatch(r"(19|20)\d{2}", clean):
            tokens.pop()
            continue
        if re.fullmatch(r"\d+[A-Z]{1,3}", clean_up):
            tokens.pop()
            continue
        break

    t = " ".join(tokens).strip()
    t = re.sub(r"(?<=\D)(\d)$", r" \1", t)

    return t



def _cleanup_final_title(title: str | None) -> str | None:
    """
    Финальная фильтрация строки, которая уже прошла через _normalize_title.

    Цель: выкинуть явный мусор вроде
      "IRYVCCKMIIKOHCYNlRoslikonzul gD!"
    и оставить только то, что похоже на человеческое название фильма.
    """
    if not title:
        return None

    t = re.sub(r"\s+", " ", title).strip()
    if not t:
        return None

    # --- базовые критерии --- #
    letters = re.findall(r"[A-Za-z]", t)
    if len(letters) < 4:
        # меньше 4 латинских букв — скорее всего мусор
        return None

    # хотя бы одна гласная
    if not re.search(r"[AEIOUYaeiouy]", t):
        return None

    # доля "нормальных" символов (буквы, цифры, пробел, базовая пунктуация)
    good_chars = len(re.findall(r"[A-Za-z0-9\s:,'\-]", t))
    if good_chars / max(1, len(t)) < 0.8:
        # больше 20% странных символов — мусор
        return None

    # --- анализ по словам --- #
    words = re.findall(r"[A-Za-z0-9']+", t)
    if not words:
        return None

    good_words: list[str] = []
    bad_words = 0
    weird_words = 0  # слова с странной капитализацией типа IMOFIBMA / aiMeaiculbaiiDa

    for w in words:
        # Оставляем чистые числа (годы, части) как есть
        if w.isdigit():
            if 1 <= len(w) <= 4:
                good_words.append(w)
                continue

        letters_w = re.findall(r"[A-Za-z]", w)
        if len(letters_w) == 0:
            bad_words += 1
            continue

        v = len(re.findall(r"[AEIOUYaeiouy]", "".join(letters_w)))
        c = len(letters_w) - v

        # Максимальная длина подряд идущих согласных
        cons_runs = re.findall(r"[BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz]+", w)
        max_run = max((len(run) for run in cons_runs), default=0)

        # В английских словах редко бывает >5 согласных подряд без гласных
        if max_run >= 6 and v == 0:
            bad_words += 1
            continue

        # Если слово длинное и почти без гласных — тоже мусор
        if len(w) >= 8 and v == 0:
            bad_words += 1
            continue

        # --- проверка "нормальности" формы слова по регистру --- #
        if not (
            re.fullmatch(r"[a-z]+", w)               # полностью нижний регистр
            or re.fullmatch(r"[A-Z][a-z]+", w)       # Title Case
            or re.fullmatch(r"[A-Z0-9]+", w)         # полностью капс/цифры (HD, USA)
            or re.fullmatch(r"[A-Z][a-z]+'[A-Za-z]+", w)  # типа "Dog's"
        ):
            weird_words += 1

        good_words.append(w)

    if not good_words:
        return None

    # --- строгие эвристики против «каши» --- #
    if STRICT_OCR_FILTER:
        total_letters = len("".join(good_words))

        # 1) если одно слово, оно "странное" и длинное — выкидываем (пример: RInNOpup)
        if len(good_words) == 1 and weird_words == 1 and total_letters >= 6:
            return None

        # 2) если слов >= 2 и есть хотя бы одно странное слово
        #    и общая длина большая — считаем всей строкой мусором
        if len(good_words) >= 2 and weird_words >= 1 and total_letters >= 15:
            return None

        # 3) если мусорных слов (без гласных и т.п.) больше нормальных — выкидываем
        if bad_words > len(good_words):
            return None

    candidate = " ".join(good_words).strip()
    if len(re.findall(r"[A-Za-z]", candidate)) < 4:
        return None

    return candidate



# ============================================================================ #
# Hелпер: прогнать весь pipeline по сырой строке
# ============================================================================ #

def _push_candidate_from_raw(raw: str | None,
                             candidates: list[str],
                             seen: set[str]) -> None:
    if not raw:
        return

    raw = apply_manual_overrides(raw)
    norm = _normalize_title(raw)
    fin = _cleanup_final_title(norm)

    if fin and fin not in seen:
        seen.add(fin)
        candidates.append(fin)


# ============================================================================ #
# ПУБЛИЧНАЯ ФУНКЦИЯ OCR ДЛЯ ОДНОГО ИЗОБРАЖЕНИЯ
# ============================================================================ #

def ocr_english_title(image_path: str) -> str | None:
    """
    Пытаемся вытащить главное АНГЛИЙСКОЕ название с скрина.

    ВАЖНО: используем ТОЛЬКО строку над плеером (шапку).
    Постер вообще игнорируем, чтобы не ловить лишний мусор.
    """
    if not os.path.isfile(image_path):
        return None

    img = cv2.imread(image_path)
    if img is None:
        return None
        # DEBUG: сохраним, что именно считаем "шапкой"
    if DEBUG_OCR:
        try:
            header_dbg = _get_header_region(img.copy())
            if header_dbg is not None and header_dbg.size > 0:
                _debug_save_header_crop(image_path, header_dbg)
        except Exception as e:
            logging.warning("DEBUG_OCR: ошибка при подготовке кропа для %s: %s", image_path, e)

    candidates: list[str] = []
    seen: set[str] = set()

    # Tesseract по шапке
    _push_candidate_from_raw(_ocr_title_from_header(img), candidates, seen)

    # EasyOCR по шапке
    if HAS_EASYOCR:
        _push_candidate_from_raw(_ocr_title_from_header_easyocr(img), candidates, seen)

    # Если английского не получилось – пробуем русский фоллбэк
    if not candidates:
        rus_title = _ocr_russian_title_from_header(img)
        if rus_title:
            # русское название возвращаем как есть
            return rus_title
        return None


    # Выбираем кандидата с максимальным "весом"
    def _score(s: str) -> int:
        letters = len(re.findall(r"[A-Za-z]", s))
        words = len(s.split())
        return letters + 2 * max(0, words - 1)

    best = max(candidates, key=_score)
    return best


# ============================================================================ #
# ГЛАВНАЯ ФУНКЦИЯ ДЛЯ Movie Tools
# ============================================================================ #

def import_requests_from_images(req_text_widget) -> None:
    image_paths = _choose_images_for_ocr()
    if not image_paths:
        return

    titles: list[str] = []
    seen: set[str] = set()
    failed: list[str] = []

    for img_path in image_paths:
        title = ocr_english_title(img_path)
        logging.info("OCR %s -> %s", img_path, title)

        if not title:
            failed.append(os.path.basename(img_path))
            continue

        if title not in seen:
            seen.add(title)
            titles.append(title)

    if not titles:
        messagebox.showinfo(
            "OCR",
            "Не удалось распознать ни одного английского названия.\n"
            "Попробуйте другие скриншоты или проверьте Tesseract / EasyOCR.",
        )
        return

    req_text_widget.delete("1.0", "end")
    req_text_widget.insert("1.0", "\n".join(titles))

    msg = f"Распознано названий: {len(titles)} из {len(image_paths)}."
    if failed:
        msg += "\n\nНе распознаны:\n  - " + "\n  - ".join(failed)

    messagebox.showinfo(
        "OCR",
        msg + "\n\nТеперь нажмите «Проверить в медиатеке», а затем, при желании,\n«Скачать не найденные».",
    )
