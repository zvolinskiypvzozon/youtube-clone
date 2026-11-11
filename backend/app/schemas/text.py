"""Пользовательский текст: управляющие символы, bidi, «невидимый» текст (день 6, шаг 1).

Решения архитектора 2026-11-10 и 2026-11-11:
- NUL и прочие управляющие символы C0/C1, кроме \\t \\n \\r, — 422 (NUL Postgres в text не
  принимает — без проверки был бы 500).
- Управляющие символы направления (U+202A–U+202E, U+2066–U+2069) вырезаются: ими подменяют
  порядок отображения текста. LRM/RLM (U+200E/U+200F) — законные метки, не трогаем.
- Текст без единого видимого символа (только пробелы, U+200B, U+FEFF, U+3164 и т. п.):
  обязательное поле — 422, необязательное — пустое значение. Внутри обычного текста
  невидимые символы сохраняются: ZWJ нужен эмодзи-последовательностям, ZWNJ — персидскому.

Использование в Annotated: StringConstraints(...), BeforeValidator(clean_text),
AfterValidator(require_visible | blank_if_invisible). BeforeValidator стоит после
StringConstraints и поэтому оборачивает их: сначала чистка, потом обрезка пробелов и длина
(длина считается по сохраняемому тексту).
"""

import re
import unicodedata

BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")
# Категория Cc без \t \n \r: C0, DEL, C1. Регулярное выражение, а не цикл по символам, —
# дешевле на длинных строках (аудит шага 1).
FORBIDDEN_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Потолок до любой обработки: BeforeValidator работает раньше max_length схемы, и без потолка
# тело в 10 МБ стоило бы секунды CPU в event loop (аудит шага 1, Medium). Самый длинный
# текст — описание (5000) — с запасом на вырезаемые bidi-символы.
RAW_TEXT_MAX = 10_000

# Категории без видимого начертания: управляющие, форматирующие, пробелы и разделители,
# одиночные комбинирующие знаки (в том числе вариационные селекторы и U+034F).
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Mn", "Me"})
# Видимые по категории (Lo, So), но отображаются пустыми — «заполнители» для пустых ников.
BLANK_LOOKING = frozenset("\u115f\u1160\u3164\uffa0\u2800")
# Default_Ignorable_Code_Point, ещё не назначенные (категория Cn): браузеры их не рисуют
# (аудит шага 1, Low). Назначенные default-ignorable уже попадают в Cf/Mn.
DEFAULT_IGNORABLE_UNASSIGNED = ((0x2060, 0x206F), (0xFFF0, 0xFFF8), (0xE0000, 0xE0FFF))

# Названия однострочные: табуляция и переводы строк — пробел (решение архитектора 2026-11-11;
# в причинах модерации и описаниях — сохраняются).
LINE_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " "})

INVISIBLE_ONLY_MESSAGE = "Текст не может состоять только из невидимых символов"


def clean_text(value: object) -> object:
    """Слишком длинный текст и управляющие символы → ошибка, bidi-символы вырезаются.
    Не строку пропускает дальше — её отклонит проверка типа str."""
    if not isinstance(value, str):
        return value
    if len(value) > RAW_TEXT_MAX:
        raise ValueError("Текст слишком длинный")
    if "\x00" in value:
        raise ValueError("Текст не может содержать NUL-символ")
    if FORBIDDEN_CONTROLS.search(value):
        raise ValueError("Текст не может содержать управляющие символы")
    return BIDI_CONTROLS.sub("", value)


def single_line(value: object) -> object:
    """Для названий: \t \n \r → пробел (до обрезки пробелов по краям и проверки длины)."""
    return value.translate(LINE_BREAKS) if isinstance(value, str) else value


def _invisible(ch: str) -> bool:
    if ch in BLANK_LOOKING or unicodedata.category(ch) in INVISIBLE_CATEGORIES:
        return True
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in DEFAULT_IGNORABLE_UNASSIGNED)


def has_visible(value: str) -> bool:
    return not all(_invisible(ch) for ch in value)


def require_visible(value: str) -> str:
    if not has_visible(value):
        raise ValueError(INVISIBLE_ONLY_MESSAGE)
    return value


def blank_if_invisible(value: str) -> str:
    """Необязательное поле: текст без видимых символов — как пустой (решение 2026-11-11, Б)."""
    return value if has_visible(value) else ""


def none_if_invisible(value: str) -> str | None:
    """Необязательное поле с null (пояснение к жалобе): без видимых символов — отсутствие."""
    return value if has_visible(value) else None
