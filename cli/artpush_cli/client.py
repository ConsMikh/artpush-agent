"""HTTP-клиент агентского интерфейса ArtPush."""

import contextlib
import json
import os
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from artpush_cli import SUPPORTED_CONTRACT, __version__


def default_cache_path() -> Path:
    """Кеш контракта: ARTPUSH_CACHE, иначе $XDG_CACHE_HOME/artpush, иначе ~/.cache/artpush."""
    if explicit := os.environ.get("ARTPUSH_CACHE"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "artpush" / "contract.json"


CACHE_PATH = default_cache_path()
CONTRACT_TTL = 600  # секунд
TIMEOUT = 60.0
UPLOAD_TIMEOUT = 600.0
MAX_RETRIES = 3  # повторов после ответа 429 или обрыва соединения на чтении
IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}
MAX_WAIT = 65  # секунд ожидания по Retry-After


class ApiError(Exception):
    """Система ответила ошибкой: статус, текст и код из {"detail", "code"}."""

    def __init__(self, status: int, detail: str, code: str | None = None, body=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code
        self.body = body
        self.extra: dict = {}  # доп. поля для JSON ошибки, которые добавила команда


class ContractMismatch(Exception):
    """Версия контракта системы не совпадает с поддерживаемой утилитой."""


def _detail_text(detail) -> str:
    if isinstance(detail, list):  # 422: список ошибок проверки
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc", []))
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)


def _api_error(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        reason = response.reason_phrase or "ошибка"
        return ApiError(response.status_code, f"HTTP {response.status_code}: {reason}")
    detail = body.get("detail")
    text = _detail_text(detail) if detail is not None else f"HTTP {response.status_code}"
    return ApiError(response.status_code, text, body.get("code"), body)


def _retry_after(response: httpx.Response) -> float:
    """Ожидание по Retry-After: секунды или HTTP-дата, не больше MAX_WAIT."""
    value = response.headers.get("Retry-After", "1")
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            seconds = 1
    seconds = min(max(seconds, 0), MAX_WAIT)
    return int(seconds) if seconds == int(seconds) else seconds


def _rewind(files) -> None:
    """Перед повтором запроса возвращает файлы в начало."""
    values = files.values() if isinstance(files, dict) else (v for _, v in files)
    for value in values:
        handle = value[1] if isinstance(value, tuple) else value
        if hasattr(handle, "seek"):
            handle.seek(0)


class ApiClient:
    def __init__(self, url: str, key: str, *, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.key_prefix = key[:11]  # «ap_» и 8 символов префикса: область запомненных версий
        self._http = httpx.Client(
            base_url=self.url,
            transport=transport,
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": f"artpush-cli/{__version__}",
            },
        )
        self._contract: dict | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json=None,
        params=None,
        files=None,
        headers=None,
        stream: bool = False,
    ) -> httpx.Response:
        """Запрос к системе; 429 и обрыв на чтении — повтор, не-2xx — ApiError."""
        timeout = UPLOAD_TIMEOUT if files else TIMEOUT
        for attempt in range(MAX_RETRIES + 1):
            if files and attempt:
                _rewind(files)
            built = self._http.build_request(
                method.upper(),
                path,
                json=json,
                params=params,
                files=files,
                headers=headers,
                timeout=timeout,
            )
            try:
                response = self._http.send(built, stream=stream)
            except (httpx.RemoteProtocolError, httpx.ReadError):
                # Сервер закрыл простаивающее соединение к нашему запросу (keep-alive
                # gunicorn — 2 с, как и опрос сеанса): чтение повторяем, запись — нет.
                if method.upper() in IDEMPOTENT and attempt < MAX_RETRIES:
                    continue
                raise
            if response.status_code == 429 and attempt < MAX_RETRIES:
                wait = _retry_after(response)
                response.close()
                time.sleep(wait)
                continue
            if response.is_success:
                return response
            if stream:
                response.read()
                response.close()
            raise _api_error(response)
        raise AssertionError("недостижимо")  # pragma: no cover

    def _cached(self) -> dict | None:
        try:
            entry = json.loads(CACHE_PATH.read_text(encoding="utf-8")).get(self.url)
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(entry, dict) or time.time() - entry.get("fetched_at", 0) > CONTRACT_TTL:
            return None
        return entry.get("contract")

    def _store(self, contract: dict) -> None:
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
        cache[self.url] = {"fetched_at": time.time(), "contract": contract}
        temp = None
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Атомарно: параллельный запуск не прочтёт полузаписанный файл.
            fd, temp = tempfile.mkstemp(dir=CACHE_PATH.parent, prefix=".contract-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(cache, ensure_ascii=False))
            os.replace(temp, CACHE_PATH)
        except OSError:
            # Кеш — ускорение, без него утилита работает.
            if temp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp)

    def contract(self) -> dict:
        """Контракт системы: из памяти, из кеша на диске (10 минут) или с сервера."""
        if self._contract is None:
            self._contract = self._cached()
        if self._contract is None:
            self._contract = self.request("GET", "/api/agent/contract").json()
            self._store(self._contract)
        return self._contract

    def refresh_contract(self) -> dict:
        """Контракт прямо с сервера, мимо памяти и кеша; свежее значение попадает в кеш."""
        self._contract = self.request("GET", "/api/agent/contract").json()
        self._store(self._contract)
        return self._contract

    def check_contract(self) -> None:
        version = self.contract().get("contract_version")
        if version != SUPPORTED_CONTRACT:
            raise ContractMismatch(
                f"Контракт системы {version}, утилита поддерживает {SUPPORTED_CONTRACT}"
                " — выполните artpush update"
            )

    def op(self, id: str) -> dict:
        """Операция контракта по id; нет такой — KeyError."""
        for operation in self.contract().get("operations", []):
            if operation.get("id") == id:
                return operation
        raise KeyError(id)
