"""Точка входа утилиты artpush."""

import argparse
import sys

import httpx

from artpush_cli import __version__
from artpush_cli.client import ApiError, ContractMismatch
from artpush_cli.commands import (
    EXIT_CONFLICT,
    EXIT_CONTRACT,
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_OK,
    EXIT_RATE,
    CommandError,
    Context,
    UsageError,
    admin,
    artnow,
    bulk,
    catalogs,
    dictionaries,
    imports,
    media,
    notes,
    paintings,
    raw,
    report,
    system,
    trash,
)
from artpush_cli.config import ConfigError
from artpush_cli.output import emit, err

__all__ = ["EXIT_CONFLICT", "EXIT_CONTRACT", "EXIT_ERROR", "EXIT_FORBIDDEN", "EXIT_OK", "EXIT_RATE"]

# Модули команд: каждый отдаёт register(subparsers, parents).
COMMAND_MODULES = [
    system,
    raw,
    paintings,
    media,
    catalogs,
    notes,
    dictionaries,
    trash,
    bulk,
    imports,
    artnow,
    admin,
    report,
]

# Поля ответа 409 о конфликте версий, которые попадают в JSON ошибки.
CONFLICT_FIELDS = ("field", "by", "at", "their_label", "mine_label")


class Parser(argparse.ArgumentParser):
    """Ошибка разбора — UsageError, чтобы ответить JSON, а не текстом с выходом 2."""

    def error(self, message: str):
        raise UsageError(message)


def _common() -> argparse.ArgumentParser:
    """Общие флаги; SUPPRESS — чтобы флаг подкоманды не затирал флаг до неё."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--table", action="store_true", default=argparse.SUPPRESS, help="таблица вместо JSON"
    )
    common.add_argument("--url", default=argparse.SUPPRESS, help="адрес системы")
    common.add_argument(
        "--key",
        default=argparse.SUPPRESS,
        help="ключ агента (виден в списке процессов; предпочтительнее ARTPUSH_TOKEN "
        "или artpush init)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = Parser(
        prog="artpush",
        description="Агентский интерфейс ArtPush. Вывод — JSON в stdout, сообщения — в stderr.",
        epilog="Коды выхода: 0 — успех, 1 — ошибка, 2 — конфликт версии или нужен предпросмотр, "
        "3 — нет прав или ключ недействителен, 4 — превышен темп, 5 — контракт не совпадает.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"artpush {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="команда")
    for module in COMMAND_MODULES:
        module.register(subparsers, [common])
    return parser


def _fail(code: int, message: str, error_code: str | None, status: int | None, **extra) -> int:
    emit({"error": message, "code": error_code, "status": status, **extra})
    err(message)
    return code


def _exit_for(status: int) -> int:
    if status in (409, 428):
        return EXIT_CONFLICT
    if status in (401, 403):
        return EXIT_FORBIDDEN
    if status == 429:
        return EXIT_RATE
    return EXIT_ERROR


def _check_contract(args, ctx: Context) -> None:
    """Контракт сверяется при каждом запуске (кеш 10 минут): изменяющая команда при
    несовпадении или сбое запроса контракта отказывает (код 5 или код ошибки), команда
    чтения предупреждает в stderr и выполняется."""
    try:
        client = ctx.client()
        client.check_contract()
        system_version = system.release_version(client.contract().get("system_version") or "")
        if system_version and system.version_key(system_version) < system.version_key(__version__):
            err(
                f"Предупреждение: набор {__version__}, система {system_version}"
                " — выполните artpush update"
            )
    except ContractMismatch as exc:
        if args.mutates and args.mutates(args):
            raise
        err(f"Предупреждение: {exc}")
    except (ApiError, httpx.HTTPError) as exc:
        if args.mutates and args.mutates(args):
            raise
        err(f"Предупреждение: контракт не получен: {getattr(exc, 'detail', exc)}")


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # кириллица в консоли Windows (cp1251/cp866)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help и --version
        return EXIT_OK if exc.code in (0, None) else EXIT_ERROR
    except UsageError as exc:
        return _fail(EXIT_ERROR, f"{exc} (см. artpush --help)", "usage", None)
    if not getattr(args, "handler", None):
        return _fail(EXIT_ERROR, "Не указана команда (см. artpush --help)", "usage", None)

    ctx = Context(args, transport=transport)
    try:
        if getattr(args, "contract_check", True):
            _check_contract(args, ctx)
        return args.handler(args, ctx) or EXIT_OK
    except ApiError as exc:
        extra = {}
        if exc.code == "conflict" and isinstance(exc.body, dict):
            extra = {name: exc.body[name] for name in CONFLICT_FIELDS if name in exc.body}
        extra.update(exc.extra)
        return _fail(_exit_for(exc.status), exc.detail, exc.code, exc.status, **extra)
    except CommandError as exc:
        return _fail(exc.exit_code, str(exc), exc.code, None, **exc.extra)
    except ContractMismatch as exc:
        return _fail(EXIT_CONTRACT, str(exc), "contract_mismatch", None)
    except ConfigError as exc:
        return _fail(EXIT_ERROR, str(exc), "config", None)
    except UsageError as exc:
        return _fail(EXIT_ERROR, str(exc), "usage", None)
    except httpx.HTTPError as exc:
        return _fail(EXIT_ERROR, f"Сеть: {exc}", "network", None)
    except KeyboardInterrupt:
        return _fail(EXIT_ERROR, "Прервано", "interrupted", None)
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
