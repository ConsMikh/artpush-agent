"""Ввод секретов мимо модели: скрытый ввод в терминале или файл. Значение не попадает в argv,
окружение, stdout, stderr, отчёт сеанса и тексты ошибок."""

import contextlib
import getpass
import sys
import warnings
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx

from artpush_cli.client import ApiError
from artpush_cli.commands import CommandError, UsageError

MASK = "***"
NO_ECHO_CONTROL = "Терминал не скрывает ввод — используйте --from-file"
TAIL_MIN = 20  # знаков: часть ключа бота после «:» маскируется отдельно
NEEDS_TTY = "Секрет вводится только в терминале или из файла (--from-file)"


def read_secret(prompt: str, from_file: str | None) -> str:
    """Секрет из файла (--from-file) или скрытым вводом в терминале; stdin без терминала
    не читается никогда, чтобы значение не прошло через диалог с моделью."""
    if from_file:
        try:
            value = Path(from_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "strerror", None) or "не читается как UTF-8"
            raise UsageError(f"Файл секрета {from_file} не прочитан: {reason}") from None
    else:
        if not sys.stdin.isatty():
            raise CommandError(NEEDS_TTY, "secret_needs_tty")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(prompt, stream=sys.stderr).strip()
            except getpass.GetPassWarning:  # скрыть ввод нельзя: он остался бы на экране
                raise CommandError(NO_ECHO_CONTROL, "secret_needs_tty") from None
    if not value:
        raise CommandError("Секрет пуст — ничего не записано", "secret_empty")
    return value


def _forms(secret: str) -> list[str]:
    """Что маскировать: значение, его часть после «:» (ключ бота), uuid ссылки VLESS и
    URL-кодированные виды всего этого. Длинные идут первыми."""
    parts = [secret]
    if ":" in secret and not secret.startswith(("vless://", "http")):
        tail = secret.split(":", 1)[1]
        if len(tail) >= TAIL_MIN:
            parts.append(tail)
    if "://" in secret and "@" in secret:
        userinfo = secret.split("://", 1)[1].split("@", 1)[0]
        if userinfo:
            parts.append(userinfo)
    forms = set(parts)
    for part in parts:
        forms |= {quote(part, safe=""), quote(part), quote_plus(part)}
    return sorted((f for f in forms if f), key=len, reverse=True)


def scrub(value, secret: str):
    """Заменяет секрет в строках, списках и словарях (ответ сервера мог повторить тело)."""
    if isinstance(value, str):
        for form in _forms(secret):
            value = value.replace(form, MASK)
        return value
    if isinstance(value, list):
        return [scrub(item, secret) for item in value]
    if isinstance(value, dict):
        return {k: scrub(v, secret) for k, v in value.items()}
    return value


@contextlib.contextmanager
def masked(secret: str):
    """Запрос с секретом: в ошибке сервера и сети значение заменяется на ***."""
    try:
        yield
    except ApiError as exc:
        exc.detail = scrub(exc.detail, secret)
        exc.args = (exc.detail,)
        exc.body = scrub(exc.body, secret)
        exc.extra = scrub(exc.extra, secret)
        raise
    except httpx.HTTPError as exc:
        raise CommandError(f"Сеть: {scrub(str(exc), secret)}", "network") from None
