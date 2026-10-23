"""Массовое заполнение поля: предпросмотр с жетоном, применение, отмена."""

from artpush_cli.commands import DICT_FIELDS, Context, UsageError, command, group, ids, split
from artpush_cli.output import emit

LIST_FILTERS = ("status", "technique", "material", "style", "genre", "origin")
INT_FILTERS = ("catalog", "year_min", "year_max")
FILTERS = ("q", "incomplete", "sort", *LIST_FILTERS, *INT_FILTERS)
PREVIEW_KEYS = ("found", "changed", "skipped", "preview", "preview_token")


def _filter_value(ctx: Context, name: str, value: str):
    if name in DICT_FIELDS:
        return [ctx.resolve(name, item) for item in split(value)]
    if name == "origin":
        return split(value)
    if name in INT_FILTERS:
        try:
            return int(value)
        except ValueError:
            raise UsageError(f"--filter {name}: ожидается число, получено «{value}»") from None
    if name == "incomplete":
        return value.strip().lower() in ("true", "1", "да", "yes")
    return value


def _body(args, ctx: Context) -> dict:
    """Тело BulkFillIn: выборка (--ids или --filter), поле, значение (справочник — по имени)."""
    if args.ids is None and not args.filter:
        raise UsageError("Укажите выборку: --ids или --filter поле=значение")
    body: dict = {}
    if args.ids is not None:
        body["ids"] = args.ids
    else:
        filters = {}
        for pair in args.filter:
            name, sep, value = pair.partition("=")
            if not sep or name not in FILTERS:
                raise UsageError(
                    f"--filter: «{pair}» — ожидается поле=значение; поля: {', '.join(FILTERS)}"
                )
            filters[name] = _filter_value(ctx, name, value)
        body["filter"] = filters
    value = ctx.resolve(args.field, args.value) if args.field in DICT_FIELDS else args.value
    body.update(field=args.field, value=value, language=args.language, only_empty=args.only_empty)
    return body


def _preview(args, ctx: Context) -> None:
    out = ctx.send("POST", "/api/paintings/bulk/fill", json={**_body(args, ctx), "dry_run": True})
    emit({key: out.get(key) for key in PREVIEW_KEYS}, table=ctx.table)


def _apply(args, ctx: Context) -> None:
    body = {**_body(args, ctx), "dry_run": False, "preview_token": args.token}
    emit(ctx.send("POST", "/api/paintings/bulk/fill", json=body), table=ctx.table)


def _undo(args, ctx: Context) -> None:
    emit(ctx.send("POST", f"/api/paintings/bulk/{args.operation}/undo"), table=ctx.table)


def _selection(parser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--ids", type=ids, help="номера картин через запятую")
    selection.add_argument(
        "--filter",
        action="append",
        default=[],
        help=f"поле=значение (справочники — по имени, через запятую): {', '.join(FILTERS)}",
    )
    parser.add_argument("--field", required=True, help="какое поле заполнить")
    parser.add_argument("--value", required=True, help="значение (справочник — по имени)")
    parser.add_argument("--language", help="язык для description и keywords")
    parser.add_argument("--only-empty", action="store_true", help="только пустые")


def register(subparsers, parents: list) -> None:
    bulk = group(subparsers, "bulk", "Массовое заполнение поля")
    preview = command(bulk, "preview", "Что изменится, и жетон для apply", parents)
    _selection(preview)
    preview.set_defaults(handler=_preview, mutates=None)

    apply = command(bulk, "apply", "Применить по жетону из preview (та же выборка)", parents)
    apply.add_argument("token", help="preview_token из bulk preview")
    _selection(apply)
    apply.set_defaults(handler=_apply, mutates=lambda a: True)

    undo = command(bulk, "undo", "Отменить массовую операцию", parents)
    undo.add_argument("operation", type=int, help="номер операции")
    undo.set_defaults(handler=_undo, mutates=lambda a: True)
