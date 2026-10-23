"""Версии карточек, которые агент видел, — основа проверки конфликта при правке.

У утилиты нет сеанса, поэтому она запоминает на диске, какую версию карточки агент прочитал
(`painting get`, ответ на свою правку). Правка шлёт её в X-Base-Version: если картину с тех пор
изменили в браузере или другим ключом, система отвечает 409, и агент показывает расхождение
человеку вместо того, чтобы молча затереть чужую правку. Запись живёт сутки; без записи берётся
текущая версия (первое касание картины). Файл — рядом с кешем контракта, отдельно на адрес
системы и префикс ключа."""

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

from artpush_cli import client as client_mod

TTL = 24 * 3600  # секунд
MAX_ENTRIES = 1000  # на один адрес и ключ


def path() -> Path:
    return client_mod.CACHE_PATH.with_name("versions.json")


def _load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    """Атомарно; сбой записи не мешает работе — без записи правка возьмёт текущую версию."""
    target = path()
    temp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".versions-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False))
        os.replace(temp, target)
    except OSError:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)


def _fresh(entries: dict, now: float) -> dict:
    """Записи не старше TTL, не больше MAX_ENTRIES самых свежих."""
    alive = {
        pk: entry
        for pk, entry in entries.items()
        if isinstance(entry, dict) and now - entry.get("seen_at", 0) <= TTL
    }
    newest = sorted(alive.items(), key=lambda item: item[1]["seen_at"], reverse=True)
    return dict(newest[:MAX_ENTRIES])


def remember(scope: str, painting: dict) -> None:
    """Запомнить версию карточки из ответа системы (id и version обязательны)."""
    pk, version = painting.get("id"), painting.get("version")
    if not isinstance(pk, int) or not isinstance(version, int):
        return
    data = _load()
    now = time.time()
    entries = dict(data.get(scope) or {})
    entries[str(pk)] = {"version": version, "seen_at": now}
    data[scope] = _fresh(entries, now)
    _save(data)


def seen(scope: str, pk: int) -> int | None:
    """Версия карточки, которую агент видел последней, или None."""
    entry = (_load().get(scope) or {}).get(str(pk))
    if not isinstance(entry, dict) or time.time() - entry.get("seen_at", 0) > TTL:
        return None
    version = entry.get("version")
    return version if isinstance(version, int) else None
