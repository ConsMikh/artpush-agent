"""Команды утилиты. Модуль команды отдаёт register(subparsers) и вешает на разбор
set_defaults(handler=..., mutates=...[, contract_check=False]); handler(args, ctx) возвращает
код выхода или None. contract_check=False — команда не сверяет контракт (init, update)."""

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from artpush_cli import config
from artpush_cli.client import ApiClient, ApiError

MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
IMPORT_SESSIONS = "/api/import/sessions"
POLL_SECONDS = 2

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2
EXIT_FORBIDDEN = 3
EXIT_RATE = 4
EXIT_CONTRACT = 5

# Поля картины со значением из справочника: вид справочника совпадает с именем поля.
DICT_FIELDS = ("author", "style", "genre", "material", "technique", "status")


class UsageError(Exception):
    """Неверные аргументы командной строки."""


class CommandError(Exception):
    """Ошибка команды со своим кодом ошибки, кодом выхода и доп. полями в JSON."""

    def __init__(self, message: str, code: str, exit_code: int = EXIT_ERROR, **extra):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.extra = extra


def preview_required() -> CommandError:
    return CommandError("Нужен --preview, затем --apply <жетон>", "preview_required", EXIT_CONFLICT)


@dataclass
class Context:
    """Окружение запуска: флаги командной строки и клиент по требованию."""

    args: argparse.Namespace
    transport: httpx.BaseTransport | None = None
    _client: ApiClient | None = field(default=None, init=False, repr=False)
    _dictionaries: dict | None = field(default=None, init=False, repr=False)

    @property
    def table(self) -> bool:
        return getattr(self.args, "table", False)

    def make_client(self, url: str, key: str) -> ApiClient:
        return ApiClient(url, key, transport=self.transport)

    def client(self) -> ApiClient:
        if self._client is None:
            cfg = config.load(
                url=getattr(self.args, "url", None), key=getattr(self.args, "key", None)
            )
            self._client = self.make_client(cfg.url, cfg.key)
        return self._client

    @property
    def scope(self) -> str:
        """Область запомненных версий карточек: адрес системы и префикс ключа."""
        client = self.client()
        return f"{client.url}|{client.key_prefix}"

    def get(self, path: str, **kwargs):
        return self.client().request("GET", path, **kwargs).json()

    def send(self, method: str, path: str, **kwargs):
        """Изменяющий запрос; пустой ответ (204) — {"status": код}."""
        response = self.client().request(method, path, **kwargs)
        return response.json() if response.content else {"status": response.status_code}

    def painting(self, pk: int) -> dict:
        return self.get(f"/api/paintings/{pk}")

    def dictionary(self, kind: str) -> list[dict]:
        """Видимые записи справочника (GET /api/dictionaries доступен любой роли)."""
        if self._dictionaries is None:
            self._dictionaries = self.get("/api/dictionaries")
        return self._dictionaries.get(kind, [])

    def resolve(self, kind: str, value: str) -> int:
        """id записи справочника по имени (или коду) без учёта регистра, либо по номеру."""
        entries = self.dictionary(kind)
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

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def poll_session(
    ctx: Context, pk: int, ready: Callable[[dict], bool], *, timeout: float, waiting: str
) -> dict:
    """Опрос сеанса импорта раз в POLL_SECONDS, пока ready(сеанс) не вернёт True.

    ready может бросить CommandError (сбой, остановка). 404 — сеанс отменён (session_discarded);
    дедлайн — timeout с текстом waiting; Ctrl-C — interrupted. Во всех ошибках — номер сеанса.
    """
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                current = ctx.get(f"{IMPORT_SESSIONS}/{pk}")
            except ApiError as exc:
                if exc.status == 404:
                    raise CommandError(
                        f"Сеанс {pk} отменён или удалён", "session_discarded", session=pk
                    ) from None
                raise
            if ready(current):
                return current
            if time.monotonic() >= deadline:
                raise CommandError(waiting, "timeout", session=pk, state=current["state"])
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        raise CommandError(
            f"Прервано: сеанс {pk} продолжает работу на сервере", "interrupted", session=pk
        ) from None


def timeout_argument(parser, default: float, what: str) -> None:
    parser.add_argument(
        "--timeout", type=float, default=default, help=f"сколько секунд ждать {what}"
    )


def ids(value: str) -> list[int]:
    """Тип argparse: список номеров через запятую."""
    try:
        result = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидаются номера через запятую: «{value}»") from None
    if not result:
        raise argparse.ArgumentTypeError("пустой список номеров")
    return result


def split(value: str) -> list[str]:
    """Список строк через запятую, без пустых."""
    return [part.strip() for part in value.split(",") if part.strip()]


def two_step(parser: argparse.ArgumentParser, *, preview, apply) -> None:
    """Двухшаговая операция: --preview → preview(args, ctx) печатает план с жетоном,
    --apply ЖЕТОН → apply(args, ctx, token); без обоих — preview_required, код 2."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--preview", action="store_true", help="показать план и жетон")
    group.add_argument("--apply", metavar="ЖЕТОН", help="применить по жетону из --preview")

    def handler(args, ctx):
        if args.apply:
            return apply(args, ctx, args.apply)
        if args.preview:
            return preview(args, ctx)
        raise preview_required()

    parser.set_defaults(handler=handler, mutates=lambda a: bool(a.apply))


def group(subparsers, name: str, help: str):
    """Группа команд (artpush painting …): отдаёт подпарсеры для её действий."""
    parser = subparsers.add_parser(name, help=help, description=help)
    return parser.add_subparsers(dest=f"{name}_action", metavar="действие")


def command(subparsers, name: str, help: str, parents: list) -> argparse.ArgumentParser:
    """Подкоманда с общими флагами (--table, --url, --key)."""
    return subparsers.add_parser(name, help=help, description=help, parents=parents)
