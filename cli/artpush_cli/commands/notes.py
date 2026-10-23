"""Заметки, история и офлайн-события картины."""

import datetime

from artpush_cli.commands import CommandError, Context, UsageError, command, group
from artpush_cli.output import emit


def _list(args, ctx: Context) -> None:
    out = ctx.get(f"/api/paintings/{args.id}/notes")
    emit(out["items"] if ctx.table else out, table=ctx.table)


def _add(args, ctx: Context) -> None:
    body = {"text": args.text}
    emit(ctx.send("POST", f"/api/paintings/{args.id}/notes", json=body), table=ctx.table)


def _history(args, ctx: Context) -> None:
    out = ctx.get(f"/api/paintings/{args.id}/history")
    emit(out["items"] if ctx.table else out, table=ctx.table)


def _event_type(ctx: Context, name: str) -> int:
    """Тип события по имени; нового нет — заводится, как из диалога браузера."""
    try:
        return ctx.resolve("event_type", name)
    except CommandError:
        created = ctx.send("POST", "/api/dictionaries/event_type", json={"name": name.strip()})
        return created["id"]


def _date(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise UsageError(f"--date: ожидается ГГГГ-ММ-ДД, получено «{value}»") from None


def _event(args, ctx: Context) -> None:
    date = _date(args.date)
    body = {"event_type_id": _event_type(ctx, args.type), "date": date, "comment": args.comment}
    emit(ctx.send("POST", f"/api/paintings/{args.id}/events", json=body), table=ctx.table)


def register(subparsers, parents: list) -> None:
    notes = group(subparsers, "notes", "Заметки картины")
    list_ = command(notes, "list", "Заметки картины", parents)
    list_.add_argument("id", type=int, help="номер картины")
    list_.set_defaults(handler=_list, mutates=None)
    add = command(notes, "add", "Добавить заметку", parents)
    add.add_argument("id", type=int, help="номер картины")
    add.add_argument("text", help="текст заметки")
    add.set_defaults(handler=_add, mutates=lambda a: True)

    history = command(subparsers, "history", "История картины", parents)
    history.add_argument("id", type=int, help="номер картины")
    history.set_defaults(handler=_history, mutates=None)

    events = group(subparsers, "events", "Офлайн-события картины")
    event = command(events, "add", "Добавить событие", parents)
    event.add_argument("id", type=int, help="номер картины")
    event.add_argument("--type", required=True, help="тип события по имени (новый — заводится)")
    event.add_argument("--date", required=True, help="ГГГГ-ММ-ДД")
    event.add_argument("--comment", default="")
    event.set_defaults(handler=_event, mutates=lambda a: True)
