"""Отчёт агента о сеансе: утилита шлёт markdown, пункты разбирает сервер."""

import re
import sys
from pathlib import Path

from artpush_cli.commands import Context, UsageError, command, group
from artpush_cli.output import emit, err

# Те же правила, что у сервера (ops/reports.py: SECRET_RE, CLIENT_RE).
SECRET_RE = re.compile(r"ap_[a-z0-9]{8}_[A-Za-z0-9_-]{20,}")
CLIENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def _read(source: str) -> str:
    if source == "-":
        if sys.stdin.isatty():
            raise UsageError(
                "Отчёт не передан: укажите файл (artpush report send report.md) "
                "или передайте текст через stdin"
            )
        return sys.stdin.read()
    try:
        return Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"Файл отчёта не прочитан: {exc}") from None


def _send(args, ctx: Context) -> None:
    if args.client and not CLIENT_RE.fullmatch(args.client):
        raise UsageError("--client: строчные латинские буквы, цифры и дефис, до 40 знаков")
    text = _read(args.file)
    if not text.strip():
        raise UsageError("Отчёт пуст")
    if SECRET_RE.search(text):
        err("Предупреждение: в отчёте похоже на ключ агента — сервер его отклонит; уберите ключ")
    out = ctx.send("POST", "/api/agent/reports", json={"text": text, "client": args.client})
    result = {"id": out["id"], "items_count": out["items_count"]}
    if out.get("suspicious"):
        result["suspicious"] = out["suspicious"]
        err(
            "Предупреждение: сервер счёл отчёт подозрительным ("
            + ", ".join(out["suspicious"])
            + ") — администратор проверит его вручную"
        )
    emit(result, table=ctx.table)


def register(subparsers, parents: list) -> None:
    report = group(subparsers, "report", "Отчёт агента о сеансе работы с ArtPush")
    send = command(report, "send", "Отправить отчёт (markdown) администратору", parents)
    send.add_argument(
        "file", nargs="?", default="-", help="файл отчёта или - (stdin, по умолчанию)"
    )
    send.add_argument("--client", default="", help="клиент: claude-code, claude-desktop, codex…")
    send.set_defaults(handler=_send, mutates=lambda a: True)
