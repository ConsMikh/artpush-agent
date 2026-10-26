"""Администрирование: справочники, журнал, целостность, ключи, пользователи.

Нужен ключ с пределом admin у администратора — иначе система ответит 403.
"""

from artpush_cli.commands import (
    CommandError,
    Context,
    command,
    confirm,
    group,
    two_step,
    wait_for,
)
from artpush_cli.output import emit, err
from artpush_cli.secret_input import masked, read_secret

DICTS = "/api/dictionaries"
EGRESS = "/api/admin/egress"
SECRETS_KEY = "/api/admin/secrets/key"
CHECK_POLL = 2  # секунд между опросами проверки выхода
CHECK_LIMIT = 60  # секунд: дольше проверка не ждётся
WORKER_TEXT = "проверка не завершилась — worker недоступен?"


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


def _checked(egress: dict) -> dict:
    return {state["platform"]: state.get("checked_at") for state in egress.get("states", [])}


def _egress(args, ctx: Context) -> None:
    """Выход к площадкам. Ссылку сервер не отдаёт: только сервер, порт и имя профиля."""
    if not args.check:
        emit(ctx.get(EGRESS), table=ctx.table)
        return
    before = _checked(ctx.get(EGRESS)) if args.wait else {}
    ctx.send("POST", f"{EGRESS}/check")
    if not args.wait:
        emit({"status": "queued"}, table=ctx.table)
        return

    def every_platform_rechecked(egress: dict) -> bool:
        now = _checked(egress)
        return bool(now) and all(at is not None and at != before.get(p) for p, at in now.items())

    done = wait_for(
        lambda: ctx.get(EGRESS),
        every_platform_rechecked,
        interval=CHECK_POLL,
        timeout=CHECK_LIMIT,
        expired=WORKER_TEXT,
        code="check_timeout",
    )
    emit(done, table=ctx.table)


def _egress_set_link(args, ctx: Context) -> None:
    value = read_secret("Ссылка VLESS (ввод скрыт): ", args.from_file)
    with masked(value):
        out = ctx.send("PUT", f"{EGRESS}/link", json={"value": value})
    err("Ссылка записана; показать её нельзя, только сервер, порт и имя профиля")
    emit(out, table=ctx.table)


def _egress_delete_link(args, ctx: Context) -> None:
    confirm(
        "Стереть ссылку VLESS? Площадки с маршрутом «через VLESS» перестанут отправляться.",
        args.yes,
    )
    emit(ctx.send("DELETE", f"{EGRESS}/link"), table=ctx.table)


def _egress_route(args, ctx: Context) -> None:
    via = args.way == "vless"
    where = "через VLESS" if via else "напрямую (адрес сервера виден площадке)"
    confirm(f"Отправлять в {args.platform} {where}?", args.yes)
    body = {"via_vless": via}
    emit(ctx.send("PUT", f"{EGRESS}/routes/{args.platform}", json=body), table=ctx.table)


def _secrets(args, ctx: Context) -> None:
    """Ключ шифрования секретов и их список (только подписи). Значений сервер не отдаёт."""
    if args.confirm_copy:
        emit(ctx.send("POST", f"{SECRETS_KEY}/confirm-copy"), table=ctx.table)
    else:
        emit(ctx.get(SECRETS_KEY), table=ctx.table)


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

    egress = command(admin, "egress", "Выход к площадкам (VLESS): состояние и проверка", parents)
    egress.add_argument("--check", action="store_true", help="поставить проверку площадок")
    egress.add_argument("--wait", action="store_true", help="с --check: дождаться итога (до 60 с)")
    egress.set_defaults(handler=_egress, mutates=lambda a: a.check)
    egress_actions = _actions(egress, "egress")
    link = command(
        egress_actions, "set-link", "Записать ссылку VLESS: ввод скрыт или файл", parents
    )
    link.add_argument("--from-file", help="файл со ссылкой (UTF-8); без него — скрытый ввод")
    link.set_defaults(handler=_egress_set_link, mutates=lambda a: True)
    unlink = command(egress_actions, "delete-link", "Стереть ссылку VLESS", parents)
    unlink.add_argument("--yes", action="store_true", help="подтверждение получено от человека")
    unlink.set_defaults(handler=_egress_delete_link, mutates=lambda a: True)
    route = command(egress_actions, "route", "Маршрут площадки: через VLESS или напрямую", parents)
    route.add_argument("platform", help="площадка: telegram, vk, instagram")
    route.add_argument("way", choices=("vless", "direct"))
    route.add_argument("--yes", action="store_true", help="подтверждение получено от человека")
    route.set_defaults(handler=_egress_route, mutates=lambda a: True)

    secrets = command(admin, "secrets", "Ключ шифрования секретов и список записей", parents)
    secrets.add_argument(
        "--confirm-copy", action="store_true", help="отметить, что копия ключа сохранена"
    )
    secrets.set_defaults(handler=_secrets, mutates=lambda a: a.confirm_copy)

    users = command(admin, "users", "Пользователи", parents)
    users.set_defaults(handler=_users, mutates=None)
    user_actions = _actions(users, "users")
    command(user_actions, "list", "Список", parents).set_defaults(handler=_users, mutates=None)
    for action, help in (("deactivate", "Отключить"), ("activate", "Включить")):
        parser = command(user_actions, action, f"{help} пользователя", parents)
        parser.add_argument("username")
        parser.set_defaults(handler=_user_action(action), mutates=lambda a: True)
