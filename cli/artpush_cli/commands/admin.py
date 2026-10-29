"""Администрирование: эталон справочников, журнал, целостность, ключи, пользователи,
статистика, выгрузка и удаление архивов художниц.

Нужен ключ с пределом admin у администратора — иначе система ответит 403. Своего архива у
администратора нет: картины, корзина, публикации — только с ключом художницы.
"""

import sys

from artpush_cli import config
from artpush_cli.commands import (
    EXIT_FORBIDDEN,
    CommandError,
    Context,
    archive,
    command,
    confirm,
    dictionaries,
    group,
    wait_for,
)
from artpush_cli.output import emit, err
from artpush_cli.secret_input import masked, read_secret

REFERENCE = "/api/admin/reference"
EGRESS = "/api/admin/egress"
SECRETS_KEY = "/api/admin/secrets/key"
CHECK_POLL = 2  # секунд между опросами проверки выхода
CHECK_LIMIT = 60  # секунд: дольше проверка не ждётся
WORKER_TEXT = "проверка не завершилась — worker недоступен?"


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


USERS = "/api/admin/users"
USER_COLUMNS = ["username", "name", "deactivated", "deleting", "active_keys", "last_seen_at"]
STATS_COLUMNS = [
    "username",
    "name",
    "state",
    "last_seen",
    "paintings",
    "media",
    "disk",
    "accounts",
    "published",
    "errors_30d",
    "export",
]
UNITS = ("Б", "КБ", "МБ", "ГБ", "ТБ")


def _users(args, ctx: Context) -> None:
    out = ctx.get(USERS)
    if ctx.table:
        emit(out["items"], table=True, columns=USER_COLUMNS)
    else:
        emit(out)


def size(value: int | None) -> str:
    """Байты по-человечески: «0 Б», «512 Б», «3.0 ГБ» (основание 1024)."""
    amount, unit = float(value or 0), 0
    while amount >= 1024 and unit < len(UNITS) - 1:
        amount, unit = amount / 1024, unit + 1
    return f"{int(amount)} Б" if unit == 0 else f"{amount:.1f} {UNITS[unit]}"


def _accounts(accounts: dict) -> str:
    parts = []
    for platform, count in (accounts or {}).items():
        test = f" (тест {count['test']})" if count.get("test") else ""
        parts.append(f"{platform} {count['total']}{test}")
    return ", ".join(parts)


def _published(published: dict) -> str:
    """«площадка картин/файлов» через запятую."""
    items = (published or {}).items()
    return ", ".join(f"{where} {n['paintings']}/{n['files']}" for where, n in items)


def _export(export: dict | None) -> str:
    if not export:
        return ""
    state = export["state"]
    if state in ("queued", "running"):
        return f"{state} {export.get('progress', 0)}%"
    if state == "ready" and export.get("expires_at"):
        return f"ready до {export['expires_at'][:10]}"
    return state


def _state(row: dict) -> str:
    if row.get("deleting"):
        return "удаляется"
    return "отключена" if row.get("deactivated") else "активна"


def _row(row: dict) -> dict:
    return {
        "username": row["username"],
        "name": row.get("name") or row["username"],
        "state": _state(row),
        "last_seen": (row.get("last_seen_at") or "")[:10],
        "paintings": row["paintings"],
        "media": row["media"],
        "disk": size(row["disk_bytes"]),
        "accounts": _accounts(row.get("accounts")),
        "published": _published(row.get("published")),
        "errors_30d": row["errors_30d"],
        "export": _export(row.get("export")),
    }


def _vless(state: dict | None) -> str:
    state = state or {}
    if not state.get("link"):
        return "ссылки нет"
    if not state.get("running"):
        text = "ссылка есть, не запущен"
    elif state.get("ok") is None:
        text = "не проверялся"
    else:
        text = "работает" if state["ok"] else "сбой проверки"
    checked = (state.get("checked_at") or "").replace("T", " ")[:16]
    return f"{text}, проверено {checked}" if checked else text


def _totals(totals: dict) -> dict:
    return {
        "users": totals["users"],
        "deactivated": totals["deactivated"],
        "paintings": totals["paintings"],
        "media": totals["media"],
        "disk": size(totals["disk_bytes"]),
        "accounts": _accounts(totals.get("accounts")),
        "published": _published(totals.get("published")),
        "errors_30d": totals["errors_30d"],
        "free_disk": f"{size(totals['free_bytes'])} из {size(totals['total_bytes'])}",
        "vless": _vless(totals.get("egress_state")),
    }


def _stats(args, ctx: Context) -> None:
    """Сводка по художницам и системе: строка на художницу и итоги одной таблицей поле—значение."""
    out = ctx.get(USERS)
    rows, totals = [_row(row) for row in out["items"]], _totals(out["totals"])
    if not ctx.table:
        emit({"items": rows, "totals": totals})
        return
    if rows:
        emit(rows, table=True, columns=STATS_COLUMNS)
        print()
    emit(totals, table=True)


def _admin_export(args, ctx: Context) -> None:
    archive.run_export(args, ctx, archive.admin_export_path(args.username))


def open_tty():
    """Терминал человека напрямую, мимо stdin: ответ в конвейер или из файла не подставить.
    Нет терминала (агент, cron) — OSError."""
    return open("CONIN$" if config.is_windows() else "/dev/tty", encoding="utf-8")  # noqa: SIM115


def _delete_archive(args, ctx: Context) -> None:
    """Удаление архива необратимо: подтверждает человек вводом логина в своём терминале."""
    row = next((r for r in ctx.get(USERS)["items"] if r["username"] == args.username), None)
    if row is None:
        raise CommandError(f"Пользователь «{args.username}» не найден", "user_not_found")
    try:
        tty = open_tty()
    except OSError:
        raise CommandError(
            "Удалить архив может только человек в своём терминале: пусть выполнит"
            f" artpush admin delete-archive {args.username} и введёт логин",
            "terminal_required",
            EXIT_FORBIDDEN,
        ) from None
    with tty:
        typed = _confirm_login(args, row, tty)
    body = {"confirm": typed}
    emit(ctx.send("POST", f"{USERS}/{args.username}/delete", json=body), table=ctx.table)


def _confirm_login(args, row: dict, tty) -> str:
    """Показать, что удаляется, и прочитать логин из терминала; не совпал — not_confirmed."""
    err(
        f"Удаляется архив «{args.username}» целиком и навсегда: картин {row['paintings']},"
        f" файлов {row['media']}, {size(row['disk_bytes'])}. Вход и ключи художницы перестанут"
        " работать."
    )
    export = row.get("export") or {}
    if export.get("state") == "ready":
        err(f"Свежая выгрузка есть: {_export(export)}")
    else:
        err("Свежей выгрузки нет — после удаления данные не вернуть (admin export)")
    sys.stderr.write("Для подтверждения введите логин: ")
    sys.stderr.flush()
    typed = tty.readline().strip()
    if typed != args.username:
        raise CommandError("Логин не совпал — ничего не удалено", "not_confirmed", EXIT_FORBIDDEN)
    return typed


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


def register(subparsers, parents: list) -> None:
    admin = group(subparsers, "admin", "Администрирование (ключ admin)")
    dictionaries.editing(
        admin,
        "dicts",
        "Эталон справочников для новых архивов, языки и валюты",
        "Свои справочники художницы правит она сама: artpush dicts; значения и номера — artpush"
        " dictionaries.",
        REFERENCE,
        parents,
    )

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

    stats = command(admin, "stats", "Статистика: художницы, диск, выгрузки, VLESS", parents)
    stats.set_defaults(handler=_stats, mutates=None)

    export = command(
        admin, "export", "Выгрузка архива художницы: запустить, состояние, скачать", parents
    )
    export.add_argument("username", help="логин художницы")
    archive.export_arguments(export)
    export.set_defaults(handler=_admin_export, mutates=lambda a: not (a.status or a.download))

    delete = command(
        admin,
        "delete-archive",
        "Удалить архив художницы навсегда (ввод логина в терминале)",
        parents,
    )
    delete.add_argument("username", help="логин художницы")
    delete.set_defaults(handler=_delete_archive, mutates=lambda a: True)

    users = command(admin, "users", "Пользователи", parents)
    users.set_defaults(handler=_users, mutates=None)
    user_actions = _actions(users, "users")
    command(user_actions, "list", "Список", parents).set_defaults(handler=_users, mutates=None)
    for action, help in (("deactivate", "Отключить"), ("activate", "Включить")):
        parser = command(user_actions, action, f"{help} пользователя", parents)
        parser.add_argument("username")
        parser.set_defaults(handler=_user_action(action), mutates=lambda a: True)
