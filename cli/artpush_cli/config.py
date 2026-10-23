"""Конфигурация утилиты: адрес системы и ключ агента."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from artpush_cli.output import err

CONFIG_PATH = Path(os.environ.get("ARTPUSH_CONFIG", "~/.config/artpush/credentials")).expanduser()


DEFAULT_URL = "https://artpush.fammix.org"


def is_windows() -> bool:
    """Единственная проверка ОС в утилите (тесты подменяют её)."""
    return os.name == "nt"


class ConfigError(Exception):
    """Конфигурация неполна или не читается."""


@dataclass
class Config:
    url: str
    key: str


def _warn_if_open() -> None:
    """Файл с ключом читают не только владелец — предупреждение (ключ остаётся рабочим)."""
    if is_windows():  # права профиля пользователя, режимы POSIX не применимы
        return
    try:
        mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        err(
            f"Предупреждение: права {CONFIG_PATH} — {mode:04o}, шире 0600; "
            f"ключ могут прочитать другие: chmod 600 {CONFIG_PATH}"
        )


def _read_file() -> dict[str, str]:
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {CONFIG_PATH}: {exc.strerror}") from None
    _warn_if_open()
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def load(*, url: str | None = None, key: str | None = None) -> Config:
    """Собирает конфигурацию: флаги, затем окружение, затем файл."""
    url = url or os.environ.get("ARTPUSH_URL")
    key = key or os.environ.get("ARTPUSH_TOKEN")
    if not (url and key):
        stored = _read_file()
        url = url or stored.get("url")
        key = key or stored.get("key")
    if not key:
        raise ConfigError("Ключ не задан: выполните artpush init")
    if not url:
        raise ConfigError("Адрес системы не задан: выполните artpush init")
    return Config(url=url.rstrip("/"), key=key)


def save(config: Config) -> None:
    """Пишет файл конфигурации: каталог 0700, файл 0600 (на Windows — права профиля)."""
    try:
        CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not is_windows():
            CONFIG_PATH.parent.chmod(0o700)
        fd = os.open(CONFIG_PATH, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if not is_windows():
                os.fchmod(handle.fileno(), 0o600)
            handle.write(f"url={config.url}\nkey={config.key}\n")
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise ConfigError(f"Не удалось записать {CONFIG_PATH}: {reason}") from None
