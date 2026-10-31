"""Справочники: значения с номерами для полей картины и их правка.

У каждой художницы свои справочники (`dicts`, ключ write); эталон для новых архивов правит
администратор (`admin dicts`, ключ admin). Языки и валюты общие — их правит только администратор.
"""

from artpush_cli.commands import CommandError, Context, command, two_step
from artpush_cli.output import emit

OWN = "/api/dictionaries"

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
    "artist_note_kind": "вид заметки",
    "artist_doc_kind": "вид документа",
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
    everything = ctx.get(OWN)
    if ctx.table:
        rows = [{"kind": k, **e} for k, entries in everything.items() for e in entries]
        emit(rows, table=True, columns=["kind", "id", "name", "code"])
    else:
        emit(everything)


class _Editor:
    """Правка набора справочников по адресу base: свои или эталон."""

    def __init__(self, base: str):
        self.base = base

    def path(self, args, *rest) -> str:
        return "/".join([self.base, _kind(args.kind), *map(str, rest)])

    def entry(self, ctx: Context, args, value: str) -> int:
        """Запись (в т.ч. скрытая) по имени или коду без учёта регистра, либо по номеру."""
        entries = ctx.get(self.path(args))
        wanted = value.strip().casefold()
        for entry in entries:
            if wanted in (entry["name"].casefold(), (entry.get("code") or "").casefold()):
                return entry["id"]
        if wanted.isdigit() and any(entry["id"] == int(wanted) for entry in entries):
            return int(wanted)
        raise CommandError(
            f"Значение «{value}» не найдено в справочнике {args.kind}",
            "dictionary_value",
            choices=[entry["name"] for entry in entries],
        )

    def list(self, args, ctx: Context) -> None:
        emit(ctx.get(self.path(args)), table=ctx.table)

    def add(self, args, ctx: Context) -> None:
        body = {"name": args.name, "code": args.code}
        emit(ctx.send("POST", self.path(args), json=body), table=ctx.table)

    def rename(self, args, ctx: Context) -> None:
        pk = self.entry(ctx, args, args.entry)
        emit(ctx.send("PATCH", self.path(args, pk), json={"name": args.name}), table=ctx.table)

    def replace_preview(self, args, ctx: Context) -> None:
        pk = self.entry(ctx, args, args.entry)
        params = {"target_id": self.entry(ctx, args, args.to)}
        emit(ctx.get(self.path(args, pk, "preview"), params=params), table=ctx.table)

    def replace(self, args, ctx: Context, token: str) -> None:
        pk = self.entry(ctx, args, args.entry)
        body = {"target_id": self.entry(ctx, args, args.to), "preview_token": token}
        emit(ctx.send("POST", self.path(args, pk, "replace"), json=body), table=ctx.table)

    def delete_preview(self, args, ctx: Context) -> None:
        pk = self.entry(ctx, args, args.entry)
        emit(ctx.get(self.path(args, pk, "preview")), table=ctx.table)

    def delete(self, args, ctx: Context, token: str) -> None:
        pk = self.entry(ctx, args, args.entry)
        params = {"preview_token": token}
        emit(ctx.send("DELETE", self.path(args, pk), params=params), table=ctx.table)


def editing(subparsers, name: str, help: str, see_also: str, base: str, parents: list) -> None:
    """Команда «<name> ВИД [list|add|rename|replace|delete]» над набором по адресу base;
    see_also — строка в описании о соседних командах справочников."""
    editor = _Editor(base)
    parser = command(subparsers, name, help, parents)
    parser.description = f"{help}. {see_also}"
    parser.add_argument("kind", help="вид: код (technique) или имя (техника)")
    parser.set_defaults(handler=editor.list, mutates=None)
    actions = parser.add_subparsers(dest=f"{name}_action", metavar="действие")

    list_ = command(
        actions, "list", "Записи справочника, включая скрытые, с числом использований", parents
    )
    list_.set_defaults(handler=editor.list, mutates=None)

    add = command(actions, "add", "Добавить запись", parents)
    add.add_argument("name")
    add.add_argument("--code", help="код (для языков и валют)")
    add.set_defaults(handler=editor.add, mutates=lambda a: True)

    rename = command(actions, "rename", "Переименовать запись", parents)
    rename.add_argument("entry", help="имя или номер")
    rename.add_argument("name", help="новое имя")
    rename.set_defaults(handler=editor.rename, mutates=lambda a: True)

    replace = command(actions, "replace", "Заменить запись другой во всех картинах", parents)
    replace.add_argument("entry", help="что заменить: имя или номер")
    replace.add_argument("--to", required=True, help="на что: имя или номер")
    two_step(replace, preview=editor.replace_preview, apply=editor.replace)

    delete = command(actions, "delete", "Удалить неиспользуемую запись", parents)
    delete.add_argument("entry", help="имя или номер")
    two_step(delete, preview=editor.delete_preview, apply=editor.delete)


def register(subparsers, parents: list) -> None:
    parser = command(subparsers, "dictionaries", "Справочники: значения и номера", parents)
    parser.description = (
        "Справочники: значения и номера (только чтение). Правка своих — artpush dicts;"
        " эталон, языки и валюты (администратор) — artpush admin dicts."
    )
    parser.add_argument("kind", nargs="?", help="вид: код (status) или имя (тип события)")
    parser.set_defaults(handler=_dictionaries, mutates=None)
    editing(
        subparsers,
        "dicts",
        "Свои справочники: список, пополнение, замена, удаление",
        "Значения и номера для полей картины — artpush dictionaries; эталон, языки и валюты"
        " (администратор) — artpush admin dicts.",
        OWN,
        parents,
    )
