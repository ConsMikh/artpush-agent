"""Справочники: значения с номерами для полей картины."""

from artpush_cli.commands import CommandError, Context, command
from artpush_cli.output import emit

# Вид справочника (код в API) и его русское имя.
KINDS = {
    "author": "автор",
    "style": "стиль",
    "genre": "жанр",
    "material": "материал",
    "technique": "техника",
    "status": "статус",
    "event_type": "тип события",
    "language": "язык",
    "currency": "валюта",
    "price_purpose": "назначение цены",
}


def _kind(value: str) -> str:
    wanted = " ".join(value.casefold().replace("_", " ").split())
    for code, name in KINDS.items():
        if wanted in (code.replace("_", " "), name):
            return code
    raise CommandError(
        f"Справочника «{value}» нет",
        "dictionary_kind",
        choices=[f"{code} ({name})" for code, name in KINDS.items()],
    )


def _dictionaries(args, ctx: Context) -> None:
    if args.kind:
        emit(ctx.dictionary(_kind(args.kind)), table=ctx.table, columns=["id", "name", "code"])
        return
    everything = ctx.get("/api/dictionaries")
    if ctx.table:
        rows = [{"kind": k, **e} for k, entries in everything.items() for e in entries]
        emit(rows, table=True, columns=["kind", "id", "name", "code"])
    else:
        emit(everything)


def register(subparsers, parents: list) -> None:
    parser = command(subparsers, "dictionaries", "Справочники: значения и номера", parents)
    parser.add_argument("kind", nargs="?", help="вид: код (status) или имя (тип события)")
    parser.set_defaults(handler=_dictionaries, mutates=None)
