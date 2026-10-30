"""--where: условие человеческими словами → JSON условия сервера.

Форма: «<поле>[:<язык>] [<способ>] [<значение>]». Поле — подпись из списка полей в браузере
в нижнем регистре или код сервера; способ — слова («содержит», «одно из», «есть», «было» …);
диапазон — «40..60», «40..», «..60», годы и даты ГГГГ-ММ-ДД. Значения справочников, типов
событий, назначений цены, валют и языков — по имени, как у остальных флагов поиска.
Контракт JSON — server/artpush/catalog/services/conditions/parse.py.
"""

import re
from dataclasses import dataclass
from datetime import date

from artpush_cli.commands import CommandError, Context, split

HELP_HINT = "Подробно: artpush paintings search --where help"
LANG_FIELDS = {"title", "description", "keywords", "has_description"}
_YEAR = re.compile(r"^\d{4}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD = re.compile(r"^[\d.\-–—]*\d[\d.\-–—]*$")  # похоже на год, дату или диапазон
_RANGE_SEP = re.compile(r"\.\.|–|—")
_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")
# Пробелы вокруг «..» у чисел: «2018 .. 2020» = «2018..2020»; «выставка ..2020» не склеивается.
_SPACE_BEFORE = re.compile(r"(?<=\d)\s+(?=\.\.|[–—])")
_SPACE_AFTER = re.compile(r"(?:(?<=\.\.)|(?<=[–—]))\s+(?=\d)")
_QUOTES = "«»\"'„“”"


@dataclass(frozen=True)
class Kind:
    ways: tuple[tuple[str, str], ...]  # показываемые способы: слова → op
    aliases: tuple[tuple[str, str], ...] = ()  # те же способы другими словами
    bare: str | None = None  # op, когда способ не назван
    example: str = ""


TEXT = Kind(
    (
        ("содержит", "contains"),
        ("не содержит", "not_contains"),
        ("не заполнено", "empty"),
        ("заполнено", "filled"),
    ),
    (("не заполнен", "empty"), ("заполнен", "filled"), ("пусто", "empty")),
    example="«содержит море», «не заполнено»; язык — через двоеточие: описание:en не заполнено",
)
DICT = Kind(
    (("одно из", "in"), ("не одно из", "not_in"), ("не указано", "unset")),
    (("не указан", "unset"), ("не указана", "unset")),
    bare="in",
    example="«одно из масло, акварель» или просто «масло»; «не одно из …»; «не указано»",
)
CHOICE = Kind((("одно из", "in"),), bare="in", example="«одно из …» или просто значение")
RANGE = Kind((("от..до", "range"),), bare="range", example="40..60, 40.. (от), ..60 (до), 50")
PRICE = Kind(
    (("от..до", "range"), ("есть", "range")),
    bare="range",
    example="«1000..5000 продажа RUB», «есть страховка»; назначение и валюта необязательны",
)
PERIOD = Kind(
    (("от..до", "overlaps"),),
    bare="overlaps",
    example="2018..2020, 2019, ..2015, 2019-05-01..2019-06-30; период картины пересекает этот",
)
STAMP = Kind((("от..до", "range"),), bare="range", example="2026-10-01.., 2025, ..2024-12-31")
TAGS = Kind(
    (("есть", "any"), ("нет", "none"), ("одно из", "in")),
    example="«есть», «нет», «одно из 3, 8» (номера; теги по имени — флаг --tag)",
)
CATALOG = Kind((("есть", "any"), ("нет", "none")), example="«есть» — в каком-нибудь каталоге")
FLAG = Kind((("есть", "yes"), ("нет", "no")), (("да", "yes"),), bare="yes", example="есть, нет")
DESCRIBED = Kind(
    (("есть", "yes"), ("нет", "no")),
    (("да", "yes"),),
    bare="yes",
    example="описание на языке:en нет — язык обязателен",
)
EVENT = Kind(
    (("было", "was"), ("не было", "was_not")),
    bare="was",
    example="«выставка 2023», «не было продажа», «было 2020..2021» (любой тип); тип и период "
    "необязательны",
)


@dataclass(frozen=True)
class Field:
    code: str
    names: tuple[str, ...]  # первое — как в списке полей браузера
    kind: Kind
    group: str


FIELDS = (
    Field("title", ("название",), TEXT, "текст"),
    Field("description", ("описание",), TEXT, "текст"),
    Field("keywords", ("ключевые слова",), TEXT, "текст"),
    Field("notes", ("заметки",), TEXT, "текст"),
    Field("painted_place", ("где нарисовано",), TEXT, "текст"),
    Field("location", ("местонахождение",), TEXT, "текст"),
    Field("author", ("автор",), DICT, "справочники и варианты"),
    Field("status", ("статус",), DICT, "справочники и варианты"),
    Field("technique", ("техника",), DICT, "справочники и варианты"),
    Field("material", ("материал",), DICT, "справочники и варианты"),
    Field("style", ("стиль",), DICT, "справочники и варианты"),
    Field("genre", ("жанр",), DICT, "справочники и варианты"),
    Field("origin", ("категория происхождения", "происхождение"), CHOICE, "справочники и варианты"),
    Field("framing", ("оформление",), CHOICE, "справочники и варианты"),
    Field("consent", ("согласие на публикацию", "согласие"), CHOICE, "справочники и варианты"),
    Field("painted", ("нарисована",), PERIOD, "даты и числа"),
    Field("height", ("высота",), RANGE, "даты и числа"),
    Field("width", ("ширина",), RANGE, "даты и числа"),
    Field("depth", ("глубина",), RANGE, "даты и числа"),
    Field("price", ("цена",), PRICE, "даты и числа"),
    Field("created", ("карточка создана",), STAMP, "даты и числа"),
    Field("updated", ("карточка изменена",), STAMP, "даты и числа"),
    Field("tags", ("теги",), TAGS, "есть или нет"),
    Field("catalog", ("каталог",), CATALOG, "есть или нет"),
    Field("photo", ("фото",), FLAG, "есть или нет"),
    Field("video", ("видео",), FLAG, "есть или нет"),
    Field("has_description", ("описание на языке",), DESCRIBED, "есть или нет"),
    Field("derived", ("производная",), FLAG, "есть или нет"),
    Field("date_unparsed", ("дата не распознана",), FLAG, "есть или нет"),
    Field("event", ("событие",), EVENT, "история"),
)
# Варианты: подпись → значение сервера (коды тоже принимаются).
CHOICES = {
    "origin": {
        "личная": "personal",
        "ученика": "student",
        "приобретённая": "acquired",
        "в дар": "gift",
        "для изучения": "study",
    },
    "framing": {"нет": "none", "паспарту": "passepartout", "рамка": "frame"},
    "consent": {"да": "yes", "нет": "no", "не указано": "unset"},
}


def _fold(text: str) -> str:
    return text.casefold().replace("ё", "е")


# Имена полей и коды, длинные первыми: «описание на языке» раньше «описания».
_NAMES = sorted(
    ((_fold(name), field) for field in FIELDS for name in (*field.names, field.code)),
    key=lambda item: -len(item[0]),
)


def bad(message: str, **extra) -> CommandError:
    return CommandError(message, "bad_where", **extra)


def help_data() -> dict:
    """Поля и способы для --where help."""
    return {
        "form": "<поле>[:<язык>] [<способ>] [<значение>]; несколько --where — через «и»",
        "fields": [
            {
                "name": field.names[0],
                "code": field.code,
                "group": field.group,
                "ways": [words for words, _ in field.kind.ways],
                "language": "обязателен"
                if field.kind is DESCRIBED
                else ("можно" if field.code in LANG_FIELDS else "нет"),
                "example": f"{field.names[0]} {field.kind.example}",
            }
            for field in FIELDS
        ],
    }


def parse_where(text: str, ctx: Context) -> dict:
    """Одно условие --where → JSON-объект условия сервера; ошибка — CommandError bad_where."""
    text = " ".join(text.split())
    if not text:
        raise bad(f"Пустое условие --where. {HELP_HINT}")
    field, rest = _field(text)
    label = field.names[0]
    lang = None
    if rest.startswith(":"):
        code, _, rest = rest[1:].partition(" ")
        if field.code not in LANG_FIELDS:
            raise bad(f"«{label}»: у поля «{label}» языка нет")
        lang = _language(ctx, code)
    rest = rest.strip()
    op, value = _way(field, rest)
    cond: dict = {"f": field.code, "op": op}
    kind = field.kind
    if kind is TEXT:
        if op in ("contains", "not_contains"):
            cond["v"] = _text(label, value)
        else:
            _nothing_left(field, value)
    elif kind is DICT:
        if op == "unset":
            _nothing_left(field, value)
        else:
            cond["v"] = [ctx.resolve(field.code, item) for item in _items(label, value)]
    elif kind is CHOICE:
        cond["v"] = [_choice(field, item) for item in _items(label, value)]
    elif kind is RANGE:
        cond["v"] = _bounds(label, value, _number, required=True)
    elif kind is PRICE:
        cond.update(_price(ctx, label, rest, value, op))
        cond["op"] = "range"
    elif kind in (PERIOD, STAMP):
        cond["v"] = _bounds(label, value, _date, required=True)
    elif kind is TAGS:
        if op == "in":
            cond["v"] = [_tag(item) for item in _items(label, value)]
        else:
            _nothing_left(field, value)
    elif kind is EVENT:
        cond["v"] = _event(ctx, label, value)
    else:  # CATALOG, FLAG, DESCRIBED
        _nothing_left(field, value)
    if kind is DESCRIBED and lang is None:
        raise bad(f"«{label}»: укажите язык — {label}:en есть")
    if lang is not None:
        cond["lang"] = lang
    return cond


def _field(text: str) -> tuple[Field, str]:
    for name, field in _NAMES:
        size = len(name)
        if _fold(text[:size]) == name and (len(text) == size or text[size] in " :"):
            return field, text[size:]
    word = re.split(r"[ :]", text, maxsplit=1)[0]
    names = ", ".join(field.names[0] for field in FIELDS)
    raise bad(f"Не знаю поле «{word}». Поля: {names}. {HELP_HINT}")


def _starts(text: str, words: str) -> bool:
    size = len(words)
    return _fold(text[:size]) == words and (len(text) == size or text[size] == " ")


def _way(field: Field, rest: str) -> tuple[str, str]:
    kind = field.kind
    # Длинные слова первыми: «не одно из» раньше «одно из», «не было» раньше «было».
    known = sorted((*kind.ways, *kind.aliases), key=lambda item: -len(item[0]))
    for words, op in known:
        if words != "от..до" and _starts(rest, words):
            return op, rest[len(words) :].strip()
    if not rest and kind not in (FLAG, DESCRIBED, EVENT):
        ways = [words for words, _ in kind.ways]
        raise bad(
            f"«{field.names[0]}»: после поля нужен способ: {', '.join(ways)} — "
            f"{field.names[0]} {kind.example}. {HELP_HINT}",
            choices=ways,
        )
    if kind.bare is not None:
        return kind.bare, rest
    raise _not_understood(field, rest)


def _not_understood(field: Field, text: str) -> CommandError:
    ways = [words for words, _ in field.kind.ways]
    return bad(
        f"«{field.names[0]}»: не понимаю «{text}». Способы: {', '.join(ways)}. {HELP_HINT}",
        choices=ways,
    )


def _nothing_left(field: Field, value: str) -> None:
    if value:
        raise _not_understood(field, value)


def _text(label: str, value: str) -> str:
    value = value.strip().strip(_QUOTES).strip()
    if not value:
        raise bad(f"«{label}»: после способа нужен текст — {label} содержит море")
    return value


def _items(label: str, value: str) -> list[str]:
    items = split(value)
    if not items:
        raise bad(f"«{label}»: укажите значение — через запятую, если их несколько")
    return items


def _language(ctx: Context, value: str) -> str:
    wanted = _fold(value.strip())
    entries = ctx.dictionary("language")
    for entry in entries:
        code = entry.get("code") or ""
        if wanted and wanted in (_fold(code), _fold(entry["name"])):
            return code
    codes = ", ".join(entry.get("code") or entry["name"] for entry in entries)
    raise bad(f"Не знаю язык «{value}». Языки: {codes}", choices=[e.get("code") for e in entries])


def _choice(field: Field, value: str) -> str:
    labels = CHOICES[field.code]
    wanted = _fold(value)
    for label, code in labels.items():
        if wanted in (_fold(label), code):
            return code
    raise bad(
        f"«{field.names[0]}»: «{value}» не подходит; {field.names[0]}: одно из {', '.join(labels)}",
        choices=list(labels),
    )


def _tag(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise bad(
            f"«теги»: в условии теги — номерами («теги одно из 3, 8»); по имени — флаг "
            f"--tag {value}"
        )
    return int(value)


def _number(label: str, value: str, _end: str):
    if not _NUMBER.match(value):  # без 1_0, 1e3, inf: только десятичная запись
        raise _range_error(label, value)
    number = float(value.replace(",", "."))
    return int(number) if number.is_integer() else number


def _date(label: str, value: str, end: str) -> str:
    if _YEAR.match(value) and int(value) > 0:
        return f"{value}-01-01" if end == "from" else f"{value}-12-31"
    if _DATE.match(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise bad(f"«{label}»: нет такой даты {value}") from None
    raise bad(f"«{label}»: ожидается год ГГГГ или дата ГГГГ-ММ-ДД, получено «{value}»")


def _range_error(label: str, value: str) -> CommandError:
    return bad(f"«{label}»: ожидается диапазон — 40..60, 40.., ..60 или число, получено «{value}»")


def _bounds(label: str, value: str, parse, *, required: bool) -> list:
    """«a..b», «a..», «..b» или одно значение (от и до — оно же)."""
    value = value.replace(" ", "")
    parts = _RANGE_SEP.split(value, maxsplit=1)
    if not value:
        if required:
            raise bad(f"«{label}»: укажите хотя бы одну границу — 40..60, 40.., ..60")
        return [None, None]
    if len(parts) == 1:
        low, high = value, value
    else:
        low, high = parts
    bounds = [
        parse(label, low, "from") if low else None,
        parse(label, high, "to") if high else None,
    ]
    if required and bounds == [None, None]:
        raise bad(f"«{label}»: укажите хотя бы одну границу — 40..60, 40.., ..60")
    if None not in bounds and bounds[0] > bounds[1]:  # ISO-даты сравниваются как строки
        raise bad(f"«{label}»: начало позже конца — {low}..{high}; поменяйте местами")
    return bounds


def _join_ranges(value: str) -> str:
    return _SPACE_AFTER.sub("", _SPACE_BEFORE.sub("", value))


def _lookup(ctx: Context, kind: str, value: str) -> dict | None:
    wanted = _fold(value.strip())
    for entry in ctx.dictionary(kind):
        if wanted in (_fold(entry["name"]), _fold(entry.get("code") or "")):
            return entry
    return None


def _price(ctx: Context, label: str, rest: str, value: str, op: str) -> dict:
    """«цена 1000..5000 [назначение] [валюта]» или «цена есть [назначение] [валюта]»."""
    words = _join_ranges(value).split()
    bounds = [None, None]
    if not _starts(rest, "есть"):
        if not words:
            raise bad(f"«{label}»: укажите сумму, назначение или валюту — {PRICE.example}")
        bounds = _bounds(label, words.pop(0), _number, required=True)
    out: dict = {"v": bounds}
    if words:
        currency = _lookup(ctx, "currency", words[-1])
        if currency is not None:
            out["currency"] = currency.get("code") or currency["name"]
            words.pop()
    if words:
        out["purpose"] = ctx.resolve("price_purpose", " ".join(words))
    if bounds == [None, None] and len(out) == 1:
        raise bad(f"«{label}»: укажите сумму, назначение или валюту — {PRICE.example}")
    return out


def _event(ctx: Context, label: str, value: str) -> dict:
    """«[тип[, тип]] [период]»: период — последнее слово, если похоже на год, дату, диапазон."""
    head, _, last = _join_ranges(value).rpartition(" ")
    if _PERIOD.match(last):
        low, high = _bounds(label, last, _date, required=False)
        value = head
    else:
        low = high = None
    types = [ctx.resolve("event_type", item) for item in split(value)]
    return {"types": types, "from": low, "to": high}
