"""Администрирование: справочники, журнал, целостность, ключи, пользователи.

Нужен ключ с пределом admin у администратора — иначе система ответит 403.
"""

from artpush_cli.commands import CommandError, Context, command, group, two_step
from artpush_cli.output import emit

DICTS = "/api/dictionaries"


def _entries(ctx: Context, kind: str) -> list[dict]:
    return ctx.get(f"{DICTS}/{kind}")


def _entry_id(ctx: Context, kind: str, value: str) -> int:
    """Запись справочника (в т.ч. скрытая) по имени или коду без учёта регистра, либо по номеру."""
    entries = _entries(ctx, kind)
    wanted = value.strip().casefold()
    for entry in entries:
        if wanted in (entry["name"].casefold(), (entry.get("code") or "").casefold()):
            return entry["id"]
    if wanted.isdigit() and any(entry["id"] == int(wanted) for entry in entries):
        return int(wanted)
    raise CommandError(
        f"Значение «{value}» не найдено в справочнике {kind}",
        "dictionary_value",
        choices=[entry["name"] for entry in entries],
    )


def _dict_list(args, ctx: Context) -> None:
    emit(_entries(ctx, args.kind), table=ctx.table)


def _dict_add(args, ctx: Context) -> None:
    body = {"name": args.name, "code": args.code}
    emit(ctx.send("POST", f"{DICTS}/{args.kind}", json=body), table=ctx.table)


def _dict_rename(args, ctx: Context) -> None:
    pk = _entry_id(ctx, args.kind, args.entry)
    body = {"name": args.name}
    emit(ctx.send("PATCH", f"{DICTS}/{args.kind}/{pk}", json=body), table=ctx.table)


def _replace_preview(args, ctx: Context) -> None:
    pk = _entry_id(ctx, args.kind, args.entry)
    target = _entry_id(ctx, args.kind, args.to)
    params = {"target_id": target}
    emit(ctx.get(f"{DICTS}/{args.kind}/{pk}/preview", params=params), table=ctx.table)


def _replace(args, ctx: Context, token: str) -> None:
    pk = _entry_id(ctx, args.kind, args.entry)
    body = {"target_id": _entry_id(ctx, args.kind, args.to), "preview_token": token}
    emit(ctx.send("POST", f"{DICTS}/{args.kind}/{pk}/replace", json=body), table=ctx.table)


def _delete_preview(args, ctx: Context) -> None:
    pk = _entry_id(ctx, args.kind, args.entry)
    emit(ctx.get(f"{DICTS}/{args.kind}/{pk}/preview"), table=ctx.table)


def _delete(args, ctx: Context, token: str) -> None:
    pk = _entry_id(ctx, args.kind, args.entry)
    params = {"preview_token": token}
    emit(ctx.send("DELETE", f"{DICTS}/{args.kind}/{pk}", params=params), table=ctx.table)


def _journal(args, ctx: Context) -> None:
    params = {"since": args.since, "page": args.page}
    if args.kind:
        params["kind"] = args.kind
    if args.via:
        params["via"] = args.via
    if args.errors:
        params["errors_only"] = "true"
    emit(ctx.get("/api/admin/events", params=params), table=ctx.table)


def _integrity(args, ctx: Context) -> None:
    emit(ctx.get("/api/admin/integrity"), table=ctx.table)


def _integrity_run(args, ctx: Context) -> None:
    emit(ctx.send("POST", "/api/admin/integrity/run"), table=ctx.table)


def _keys(args, ctx: Context) -> None:
    params = {name: getattr(args, name) for name in ("user", "state") if getattr(args, name, None)}
    emit(ctx.get("/api/admin/keys", params=params or None), table=ctx.table)


def _revoke(args, ctx: Context) -> None:
    emit(ctx.send("POST", f"/api/admin/keys/{args.id}/revoke"), table=ctx.table)


def _users(args, ctx: Context) -> None:
    emit(ctx.get("/api/admin/users"), table=ctx.table)


def _user_action(action: str):
    def handler(args, ctx: Context) -> None:
        path = f"/api/admin/users/{args.username}/{action}"
        emit(ctx.send("POST", path), table=ctx.table)

    return handler


def _actions(parser, name: str):
    """Необязательные действия под командой: без действия — список."""
    return parser.add_subparsers(dest=f"{name}_action", metavar="действие")


def _dicts(admin, parents: list) -> None:
    dicts = command(admin, "dicts", "Справочник: список, пополнение, замена, удаление", parents)
    dicts.add_argument("kind", help="вид справочника, например technique")
    dicts.set_defaults(handler=_dict_list, mutates=None)
    actions = _actions(dicts, "dicts")

    list_ = command(actions, "list", "Записи справочника с числом использований", parents)
    list_.set_defaults(handler=_dict_list, mutates=None)

    add = command(actions, "add", "Добавить запись", parents)
    add.add_argument("name")
    add.add_argument("--code", help="код (для языков и валют)")
    add.set_defaults(handler=_dict_add, mutates=lambda a: True)

    rename = command(actions, "rename", "Переименовать запись", parents)
    rename.add_argument("entry", help="имя или номер")
    rename.add_argument("name", help="новое имя")
    rename.set_defaults(handler=_dict_rename, mutates=lambda a: True)

    replace = command(actions, "replace", "Заменить запись другой во всех картинах", parents)
    replace.add_argument("entry", help="что заменить: имя или номер")
    replace.add_argument("--to", required=True, help="на что: имя или номер")
    two_step(replace, preview=_replace_preview, apply=_replace)

    delete = command(actions, "delete", "Удалить неиспользуемую запись", parents)
    delete.add_argument("entry", help="имя или номер")
    two_step(delete, preview=_delete_preview, apply=_delete)


def register(subparsers, parents: list) -> None:
    admin = group(subparsers, "admin", "Администрирование (ключ admin)")
    _dicts(admin, parents)

    journal = command(admin, "journal", "Журнал событий системы", parents)
    journal.add_argument("--since", choices=("day", "week", "all"), default="all")
    journal.add_argument("--kind", help="вид события")
    journal.add_argument("--via", choices=("web", "agent", "system"), help="источник")
    journal.add_argument("--errors", action="store_true", help="только ошибки")
    journal.add_argument("--page", type=int, default=1)
    journal.set_defaults(handler=_journal, mutates=None)

    integrity = command(admin, "integrity", "Проверка целостности: итог или запуск", parents)
    integrity.set_defaults(handler=_integrity, mutates=None)
    run = command(_actions(integrity, "integrity"), "run", "Запустить проверку", parents)
    run.set_defaults(handler=_integrity_run, mutates=lambda a: True)

    keys = command(admin, "keys", "Ключи агентов всех пользователей", parents)
    keys.add_argument("--user", help="только ключи пользователя")
    keys.add_argument("--state", help="состояние ключа")
    keys.set_defaults(handler=_keys, mutates=None)
    key_actions = _actions(keys, "keys")
    command(key_actions, "list", "Список ключей", parents).set_defaults(handler=_keys, mutates=None)
    revoke = command(key_actions, "revoke", "Отозвать ключ", parents)
    revoke.add_argument("id", type=int, help="номер ключа")
    revoke.set_defaults(handler=_revoke, mutates=lambda a: True)

    users = command(admin, "users", "Пользователи", parents)
    users.set_defaults(handler=_users, mutates=None)
    user_actions = _actions(users, "users")
    command(user_actions, "list", "Список", parents).set_defaults(handler=_users, mutates=None)
    for action, help in (("deactivate", "Отключить"), ("activate", "Включить")):
        parser = command(user_actions, action, f"{help} пользователя", parents)
        parser.add_argument("username")
        parser.set_defaults(handler=_user_action(action), mutates=lambda a: True)
