"""Сырой вызов API: artpush call METHOD path."""

import json
from contextlib import ExitStack
from pathlib import Path

from artpush_cli.commands import (
    EXIT_FORBIDDEN,
    MUTATING_METHODS,
    CommandError,
    Context,
    UsageError,
    command,
)
from artpush_cli.output import emit

# Операции, которые утилита отклоняет сама, не обращаясь к системе.
REFUSED = {
    ("POST", "/api/trash/purge"): "Очистка корзины агенту недоступна — сделайте это в браузере",
}


def _refused(method: str, path: str) -> str | None:
    normalized = "/" + path.split("?", 1)[0].strip("/")
    return REFUSED.get((method, normalized))


def _pairs(values: list[str], flag: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        name, sep, rest = value.partition("=")
        if not sep or not name:
            raise UsageError(f"{flag}: ожидается имя=значение, получено «{value}»")
        pairs.append((name, rest))
    return pairs


def _call(args, ctx: Context) -> None:
    if message := _refused(args.method, args.path):
        raise CommandError(message, "forbidden_operation", EXIT_FORBIDDEN)
    try:
        body = json.loads(args.json) if args.json is not None else None
    except ValueError as exc:
        raise UsageError(f"--json: неверный JSON ({exc})") from None
    params = _pairs(args.param, "--param") or None
    with ExitStack() as stack:
        files = []
        for field, path in _pairs(args.file, "--file"):
            try:
                handle = stack.enter_context(Path(path).open("rb"))
            except OSError as exc:
                raise UsageError(f"--file: не открыть {path}: {exc.strerror}") from None
            files.append((field, (Path(path).name, handle)))
        response = ctx.client().request(
            args.method, args.path, json=body, params=params, files=files or None
        )
    if not response.content:
        emit({"status": response.status_code}, table=ctx.table)
        return
    try:
        data = response.json()
    except ValueError:
        data = {"status": response.status_code, "text": response.text}
    emit(data, table=ctx.table)


def register(subparsers, parents: list) -> None:
    call = command(subparsers, "call", "Сырой вызов API системы", parents)
    call.add_argument("method", type=str.upper, help="GET, POST, PATCH, PUT, DELETE")
    call.add_argument("path", help="путь, например /api/works")
    call.add_argument("--json", help="тело запроса в JSON")
    call.add_argument("--param", action="append", default=[], help="параметр запроса k=v")
    call.add_argument("--file", action="append", default=[], help="файл поле=путь")
    # Отклоняемая операция не сверяет контракт: отказ — без единого запроса.
    call.set_defaults(
        handler=_call,
        mutates=lambda a: a.method in MUTATING_METHODS and not _refused(a.method, a.path),
    )
