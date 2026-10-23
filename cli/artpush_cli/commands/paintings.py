"""Картины: поиск, карточка, правка полей, тексты, цены, удаление, производная."""

from artpush_cli import versions
from artpush_cli.client import ApiError
from artpush_cli.commands import DICT_FIELDS, Context, UsageError, command, group, split
from artpush_cli.output import emit

OVERWRITE_HELP = (
    "после конфликта (409): записать поверх чужой правки с текущей версией, "
    "а не с той, что читали; только после «да» человека"
)
OVERWRITE_HINT = (
    "Спросите человека: записать поверх чужой правки — та же команда с --overwrite; "
    "оставить чужую — ничего не делать"
)

TEXT_FIELDS = ("location", "painted_place", "date_text")
SIZE_FIELDS = ("height_cm", "width_cm", "depth_cm")
CHOICE_FIELDS = {
    "origin_category": ("personal", "student", "acquired", "gift", "study"),
    "framing": ("none", "passepartout", "frame"),
}
PATCH_FIELDS = (
    *DICT_FIELDS,
    *TEXT_FIELDS,
    *SIZE_FIELDS,
    *CHOICE_FIELDS,
    "publication_consent",
    "tags",
)
TRUE = {"true", "1", "yes", "да"}
FALSE = {"false", "0", "no", "нет"}
FILTER_DICTS = ("status", "technique", "material", "style", "genre")
# Ключи незаполненного из missing строки списка (completeness.REQUIRED на сервере).
MISSING_FIELDS = (
    "title",
    "author",
    "date",
    "size",
    "technique",
    "material",
    "status",
    "origin_category",
    "media",
)
WITH_PARTS = ("media", "notes", "history", "placements")
DERIVE_GROUPS = (
    "author",
    "style_genre",
    "material_technique",
    "date",
    "size",
    "status_location",
    "origin_category",
    "framing",
    "tags",
    "keywords",
    "prices",
    "description",
)


def _bool(name: str, value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered == "":
        return None
    if lowered in TRUE:
        return True
    if lowered in FALSE:
        return False
    raise UsageError(f"{name}: ожидается да/нет, получено «{value}»")


def _field_value(ctx: Context, name: str, value: str):
    if name in DICT_FIELDS:
        return ctx.resolve(name, value) if value.strip() else None
    if name == "tags":
        return split(value)
    if name == "publication_consent":
        return _bool(name, value)
    if name in SIZE_FIELDS:
        return value.strip().replace(",", ".") or None
    if name in CHOICE_FIELDS:
        if value not in CHOICE_FIELDS[name]:
            raise UsageError(f"{name}: одно из {', '.join(CHOICE_FIELDS[name])}")
        return value
    return value


def patch_body(ctx: Context, pairs: list[str]) -> dict:
    """Тело PaintingPatch из --field k=v; справочные значения — по имени."""
    raw = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        name = name.strip()
        if not sep or not name:
            raise UsageError(f"--field: ожидается поле=значение, получено «{pair}»")
        if name not in PATCH_FIELDS:
            raise UsageError(f"--field: поля «{name}» нет; доступны: {', '.join(PATCH_FIELDS)}")
        raw[name] = value
    return {name: _field_value(ctx, name, value) for name, value in raw.items()}


def versioned(
    ctx: Context,
    method: str,
    path: str,
    pk: int,
    body,
    *,
    current: dict | None = None,
    overwrite: bool = False,
) -> dict:
    """Изменение карточки с базовой версией — той, что агент видел (`painting get`, своя
    правка), чтобы правка в браузере с тех пор дала 409, а не затёрлась. Нет запомненной
    или --overwrite — текущая (current, иначе читается). Без заголовка агент получает 428."""
    version = None if overwrite else versions.seen(ctx.scope, pk)
    if version is None:
        version = (current or ctx.painting(pk))["version"]
    headers = {"X-Base-Version": str(version)}
    try:
        result = ctx.send(method, path, json=body, headers=headers)
    except ApiError as exc:
        if exc.code == "conflict" and not overwrite:
            exc.extra["hint"] = OVERWRITE_HINT
        raise
    versions.remember(ctx.scope, result)
    return result


def _search(args, ctx: Context) -> None:
    params: list[tuple[str, str]] = []
    if args.q:
        params.append(("q", args.q))
    for kind in FILTER_DICTS:
        for value in getattr(args, kind):
            params += [(kind, str(ctx.resolve(kind, item))) for item in split(value)]
    params += [("origin", value) for value in args.origin]
    year_min = args.year if args.year is not None else args.year_min
    year_max = args.year if args.year is not None else args.year_max
    if year_min is not None:
        params.append(("year_min", str(year_min)))
    if year_max is not None:
        params.append(("year_max", str(year_max)))
    if args.catalog is not None:
        params.append(("catalog", str(args.catalog)))
    text_lang = None
    if args.missing and args.missing.startswith("text:"):
        text_lang = args.missing.removeprefix("text:")
        # «en» — нет названия; «en:description» — нет описания
    elif args.missing and args.missing not in MISSING_FIELDS:
        raise UsageError(f"--missing: одно из {', '.join(MISSING_FIELDS)} или text:<язык>")
    if args.incomplete or (args.missing and not text_lang):
        params.append(("incomplete", "true"))
    if args.sort:
        params.append(("sort", args.sort))
    listing = ctx.get("/api/paintings", params=params)

    wanted = {tag.casefold() for tag in args.tag}
    rows = [
        row
        for row in listing["items"]
        if wanted <= {tag.casefold() for tag in row.get("tags", [])}
        and (not args.missing or text_lang or args.missing in row.get("missing", []))
    ]
    total = len(rows) if (args.tag or args.missing) else listing["total"]
    if text_lang:
        rows, total = _missing_text(ctx, rows, text_lang, args.offset + args.limit)
    if (args.tag or args.missing) and listing["total"] > len(listing["items"]):
        total = None  # сервер обрезал выдачу: сколько всего подходит, неизвестно
    items = rows[args.offset : args.offset + args.limit]
    if ctx.table:
        emit(items, table=True, columns=["id", "title", "year", "technique", "status", "missing"])
    else:
        emit({"items": items, "total": total})


def _missing_text(ctx: Context, rows: list[dict], spec: str, enough: int):
    """Картины без названия (spec = «en») или без описания (spec = «en:description») на языке.

    Читает карточки, пока не наберёт enough.

    Остановка раньше конца — total неизвестен (null)."""
    lang, _, field = spec.partition(":")
    field = field or "title"
    if field not in ("title", "description"):
        raise UsageError("--missing text:<язык>[:description]")
    found = []
    for index, row in enumerate(rows):
        texts = ctx.painting(row["id"]).get("texts", [])
        if not any(t.get("language") == lang and (t.get(field) or "").strip() for t in texts):
            found.append(row)
            if len(found) >= enough and index + 1 < len(rows):
                return found, None
    return found, len(found)


def _get(args, ctx: Context) -> None:
    painting = ctx.painting(args.id)
    versions.remember(ctx.scope, painting)
    for part in split(args.with_ or ""):
        if part not in WITH_PARTS:
            raise UsageError(f"--with: одно из {', '.join(WITH_PARTS)}")
        if part != "media":  # медиа уже в карточке
            painting[part] = ctx.get(f"/api/paintings/{args.id}/{part}")["items"]
    emit(painting, table=ctx.table)


def _create(args, ctx: Context) -> None:
    body = patch_body(ctx, args.field)
    painting = ctx.send("POST", "/api/paintings", json={"catalog_id": args.catalog})
    versions.remember(ctx.scope, painting)
    if body:
        pk = painting["id"]
        try:
            painting = versioned(ctx, "PATCH", f"/api/paintings/{pk}", pk, body, current=painting)
        except ApiError as exc:
            # Картина уже создана: агент правит её, а не заводит новый черновик.
            exc.detail = f"{exc.detail}. Картина {pk} создана — правьте её: painting set {pk}"
            exc.extra["painting_id"] = pk
            raise
    emit(painting, table=ctx.table)


def _set(args, ctx: Context) -> None:
    if not args.field:
        raise UsageError("Укажите хотя бы одно --field поле=значение")
    body = patch_body(ctx, args.field)
    result = versioned(
        ctx, "PATCH", f"/api/paintings/{args.id}", args.id, body, overwrite=args.overwrite
    )
    emit(result, table=ctx.table)


def _text(args, ctx: Context) -> None:
    if args.title is None and args.description is None and args.keywords is None:
        raise UsageError("Укажите --title, --description или --keywords")
    painting = ctx.painting(args.id)
    current = next((t for t in painting.get("texts", []) if t.get("language") == args.lang), {})
    body = {
        "title": current.get("title", "") if args.title is None else args.title,
        "description": current.get("description", "")
        if args.description is None
        else args.description,
        "keywords": current.get("keywords", []) if args.keywords is None else split(args.keywords),
    }
    path = f"/api/paintings/{args.id}/texts/{args.lang}"
    result = versioned(ctx, "PUT", path, args.id, body, current=painting, overwrite=args.overwrite)
    emit(result, table=ctx.table)


def _price(args, ctx: Context) -> None:
    currency_id = ctx.resolve("currency", args.currency)
    currency = next(e for e in ctx.dictionary("currency") if e["id"] == currency_id)
    code = currency.get("code") or currency["name"]
    purpose = ctx.resolve("price_purpose", args.purpose)
    painting = ctx.painting(args.id)
    new = {"amount": args.amount.strip().replace(",", "."), "currency": code, "purpose_id": purpose}
    prices = [
        {key: p[key] for key in ("amount", "currency", "purpose_id")}
        for p in painting.get("prices", [])
    ]
    # Цена с той же валютой и назначением меняется на месте, иначе добавляется в конец.
    index = next(
        (i for i, p in enumerate(prices) if (p["currency"], p["purpose_id"]) == (code, purpose)),
        None,
    )
    if index is None:
        prices.append(new)
    else:
        prices[index] = new
    path = f"/api/paintings/{args.id}/prices"
    result = versioned(
        ctx, "PUT", path, args.id, prices, current=painting, overwrite=args.overwrite
    )
    emit(result, table=ctx.table)


def _delete(args, ctx: Context) -> None:
    if not args.yes:
        emit({"would_trash": ctx.painting(args.id)}, table=ctx.table)
        return
    emit(ctx.send("DELETE", f"/api/paintings/{args.id}"), table=ctx.table)


def _derive(args, ctx: Context) -> None:
    groups = split(args.groups) if args.groups else list(DERIVE_GROUPS)
    body = {"groups": groups, "catalog_id": args.catalog}
    emit(ctx.send("POST", f"/api/paintings/{args.id}/derive", json=body), table=ctx.table)


def register(subparsers, parents: list) -> None:
    listing = group(subparsers, "paintings", "Поиск картин")
    search = command(listing, "search", "Найти картины по фильтрам", parents)
    search.add_argument("--q", help="поиск по тексту")
    for kind in FILTER_DICTS:
        search.add_argument(
            f"--{kind}", action="append", default=[], help="имя или номер, можно через запятую"
        )
    search.add_argument(
        "--origin", action="append", default=[], choices=CHOICE_FIELDS["origin_category"]
    )
    search.add_argument("--year", type=int, help="год (точно)")
    search.add_argument("--year-min", type=int)
    search.add_argument("--year-max", type=int)
    search.add_argument("--catalog", type=int, help="номер каталога")
    search.add_argument("--tag", action="append", default=[], help="тег (все указанные)")
    search.add_argument("--incomplete", action="store_true", help="только незаполненные")
    search.add_argument(
        "--missing",
        help=f"не заполнено: {', '.join(MISSING_FIELDS)} или text:<язык> (нет названия) "
        "или text:<язык>:description (нет описания) "
        "(text: читает карточки по одной; total = null, если просмотрены не все)",
    )
    search.add_argument("--sort", help="порядок, например -id")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--offset", type=int, default=0)
    search.set_defaults(handler=_search, mutates=None)

    one = group(subparsers, "painting", "Одна картина: карточка и правка")
    get = command(one, "get", "Карточка картины", parents)
    get.add_argument("id", type=int)
    get.add_argument("--with", dest="with_", help=f"добавить: {','.join(WITH_PARTS)}")
    get.set_defaults(handler=_get, mutates=None)

    fields_help = f"поле=значение; поля: {', '.join(PATCH_FIELDS)}"
    create = command(one, "create", "Новая картина", parents)
    create.add_argument("--catalog", type=int, help="положить в каталог")
    create.add_argument("--field", action="append", default=[], help=fields_help)
    create.set_defaults(handler=_create, mutates=lambda a: True)

    set_ = command(one, "set", "Изменить поля карточки", parents)
    set_.add_argument("id", type=int)
    set_.add_argument("--field", action="append", default=[], help=fields_help)
    set_.add_argument("--overwrite", action="store_true", help=OVERWRITE_HELP)
    set_.set_defaults(handler=_set, mutates=lambda a: True)

    text = command(one, "text", "Текст на языке (неуказанные части сохраняются)", parents)
    text.add_argument("id", type=int)
    text.add_argument("lang", help="код языка, например ru")
    text.add_argument("--title")
    text.add_argument("--description")
    text.add_argument("--keywords", help="через запятую")
    text.add_argument("--overwrite", action="store_true", help=OVERWRITE_HELP)
    text.set_defaults(handler=_text, mutates=lambda a: True)

    price = command(one, "price", "Цена (остальные цены сохраняются)", parents)
    price.add_argument("id", type=int)
    price.add_argument("--amount", required=True)
    price.add_argument("--currency", required=True, help="код или имя валюты")
    price.add_argument("--purpose", required=True, help="назначение цены: имя или номер")
    price.add_argument("--overwrite", action="store_true", help=OVERWRITE_HELP)
    price.set_defaults(handler=_price, mutates=lambda a: True)

    delete = command(one, "delete", "В корзину (без --yes — только показать)", parents)
    delete.add_argument("id", type=int)
    delete.add_argument("--yes", action="store_true", help="действительно удалить")
    delete.set_defaults(handler=_delete, mutates=lambda a: a.yes)

    derive = command(one, "derive", "Создать производную картину", parents)
    derive.add_argument("id", type=int)
    derive.add_argument(
        "--groups", help=f"группы полей через запятую (по умолчанию все): {','.join(DERIVE_GROUPS)}"
    )
    derive.add_argument("--catalog", type=int)
    derive.set_defaults(handler=_derive, mutates=lambda a: True)
