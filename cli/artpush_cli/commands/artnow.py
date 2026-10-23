"""Импорт с Artnow: проверка ссылки, чтение работ, разбор с жетоном, создание."""

from artpush_cli.commands import (
    CommandError,
    Context,
    command,
    group,
    imports,
    poll_session,
    timeout_argument,
)
from artpush_cli.output import emit, err

ARTNOW = "/api/import/artnow"
CRAWL_TIMEOUT = 3600  # секунд ожидания конца чтения


def _check(args, ctx: Context) -> None:
    emit(ctx.send("POST", f"{ARTNOW}/check", json={"url": args.link}), table=ctx.table)


def _read(current: dict) -> bool:
    """Чтение кончилось — разбор; остановка — crawl_stopped."""
    if current["state"] != "reading":
        return True
    stopped = (current.get("artnow") or {}).get("stopped")
    if stopped:
        pk = current["id"]
        raise CommandError(
            f"Чтение остановлено: {stopped.get('reason')} — продолжить: artnow resume {pk}",
            "crawl_stopped",
            session=pk,
            stopped=stopped,
        )
    return False


def _finish(args, ctx: Context, pk: int) -> None:
    waiting = f"Чтение ещё идёт — дождаться: artnow wait {pk}"
    poll_session(ctx, pk, _read, timeout=args.timeout, waiting=waiting)
    emit(imports.review_out(ctx, pk), table=ctx.table)


def _start(args, ctx: Context) -> None:
    checked = ctx.send("POST", f"{ARTNOW}/check", json={"url": args.link})
    body = {"url": checked["url"], "batch": imports.batch(args, ctx)}
    body["confirm_rerun"] = args.confirm_rerun
    session = ctx.send("POST", f"{ARTNOW}/sessions", json=body)
    err(f"Сеанс {session['id']}: чтение работ с Artnow началось")
    _finish(args, ctx, session["id"])


def _resume(args, ctx: Context) -> None:
    ctx.send("POST", f"{imports.SESSIONS}/{args.session}/crawl")
    _finish(args, ctx, args.session)


def _wait(args, ctx: Context) -> None:
    _finish(args, ctx, args.session)


def _stop(args, ctx: Context) -> None:
    out = ctx.send("POST", f"{imports.SESSIONS}/{args.session}/crawl/stop")
    emit({key: out.get(key) for key in ("id", "state", "artnow")}, table=ctx.table)


def _works(args, ctx: Context) -> None:
    emit(ctx.get(f"{imports.SESSIONS}/{args.session}/works"), table=ctx.table)


def _timeout(parser) -> None:
    timeout_argument(parser, CRAWL_TIMEOUT, "конца чтения")


def register(subparsers, parents: list) -> None:
    artnow = group(subparsers, "artnow", "Импорт работ с Artnow")

    check = command(artnow, "check", "Проверить ссылку на художника", parents)
    check.add_argument("link", help="ссылка на страницу художника")
    check.set_defaults(handler=_check, mutates=lambda a: True)

    start = command(
        artnow,
        "start",
        "Прочитать работы и показать разбор с жетоном (картины не создаются)",
        parents,
    )
    start.add_argument("link", help="ссылка на страницу художника")
    start.add_argument(
        "--confirm-rerun", action="store_true", help="повторный импорт того же аккаунта"
    )
    imports.batch_arguments(start)
    _timeout(start)
    start.set_defaults(handler=_start, mutates=lambda a: True)

    resume = command(artnow, "resume", "Возобновить остановленное чтение и дождаться", parents)
    resume.add_argument("session", type=int)
    _timeout(resume)
    resume.set_defaults(handler=_resume, mutates=lambda a: True)

    wait = command(artnow, "wait", "Дождаться конца чтения и показать разбор", parents)
    wait.add_argument("session", type=int)
    _timeout(wait)
    wait.set_defaults(handler=_wait, mutates=None)

    stop = command(artnow, "stop", "Остановить чтение", parents)
    stop.add_argument("session", type=int)
    stop.set_defaults(handler=_stop, mutates=lambda a: True)

    works = command(artnow, "works", "Прочитанные работы сеанса", parents)
    works.add_argument("session", type=int)
    works.set_defaults(handler=_works, mutates=None)

    imports.create_command(artnow, parents, "Создать картины по жетону разбора (ждёт окончания)")
