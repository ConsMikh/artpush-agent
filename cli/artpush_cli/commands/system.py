"""Служебные команды: init, whoami, contract, update."""

import getpass
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from artpush_cli import config
from artpush_cli.commands import CommandError, Context, command
from artpush_cli.output import emit, err

MIRROR_RAW = "https://raw.githubusercontent.com/ConsMikh/artpush-agent"
UTF8_BOM = b"\xef\xbb\xbf"
# Новое окно консоли для установщика на Windows (константа есть только в Windows-сборке Python).
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)


def _ask(prompt: str) -> str:
    """Вопрос человеку через stderr, чтобы stdout оставался чистым."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _init(args, ctx: Context) -> None:
    url = getattr(args, "url", None) or (
        _ask(f"Адрес ArtPush [{config.DEFAULT_URL}]: ") or config.DEFAULT_URL
    )
    # Ключ: флаг, затем ARTPUSH_TOKEN (не в argv: так ключ не виден в списке процессов),
    # затем скрытый ввод.
    key = (
        getattr(args, "key", None)
        or os.environ.get("ARTPUSH_TOKEN")
        or getpass.getpass("Ключ агента: ")
    )
    if not url or not key:
        raise config.ConfigError("Нужны адрес и ключ")
    url = url.strip().rstrip("/")
    with ctx.make_client(url, key.strip()) as client:
        me = client.request("GET", "/api/me").json()
    config.save(config.Config(url=url, key=key.strip()))
    err(f"Ключ проверен, конфигурация записана в {config.CONFIG_PATH}")
    emit({"user": me.get("username"), "role": me.get("role")}, table=ctx.table)


def _whoami(args, ctx: Context) -> None:
    me = ctx.client().request("GET", "/api/me").json()
    result = {key: me.get(key) for key in ("username", "display_name", "role")}
    if me.get("agent_scope") is not None:
        result["key_scope"] = me["agent_scope"]
    emit(result, table=ctx.table)


def _contract(args, ctx: Context) -> None:
    contract = ctx.client().contract()
    if args.op:
        found = next((op for op in contract.get("operations", []) if op["id"] == args.op), None)
        if found is None:
            raise CommandError(f"Операции «{args.op}» нет в контракте", "operation_not_found")
        if ctx.table:
            emit(found.get("body") or [], table=True, columns=["name", "type", "required"])
        else:
            emit(found)
    elif args.fields:
        labels = contract.get("field_labels", {})
        if ctx.table:
            rows = [{"поле": k, "подпись": v} for k, v in labels.items()]
            emit(rows, table=True, columns=["поле", "подпись"])
        else:
            emit(labels)
    else:
        emit(contract, table=ctx.table)


def version_key(version: str) -> tuple[int, ...]:
    """Числа версии для сравнения: «v0.8.0-dev» → (0, 8, 0)."""
    numbers = re.match(r"\d+(?:\.\d+)*", release_version(version))
    return tuple(int(part) for part in numbers.group().split(".")) if numbers else ()


def release_version(system_version: str) -> str:
    """Версия выпуска без ведущей «v»: система отдаёт «v0.7.0», набор и теги зеркала — «0.7.0»."""
    return system_version.removeprefix("v")


def _fetch_installer(version: str, root: Path, ctx: Context) -> Path:
    """Скачивает установщик выпуска из зеркала. Отдельный клиент, не ApiClient:
    ключ агента на GitHub не отправляется."""
    name = "install.ps1" if config.is_windows() else "install.sh"
    try:
        with httpx.Client(transport=ctx.transport, follow_redirects=True, timeout=60) as http:
            response = http.get(f"{MIRROR_RAW}/v{version}/{name}")
    except httpx.HTTPError as exc:
        raise CommandError(
            f"Зеркало с установщиком недоступно: {exc}", "release_unreachable"
        ) from None
    if response.status_code == 404:
        raise CommandError(
            f"В зеркале выпуска {version} нет — обратитесь к администратору системы",
            "release_missing",
        )
    if response.status_code != 200:
        raise CommandError(f"Зеркало ответило кодом {response.status_code}", "release_unreachable")
    content = response.content
    # В зеркале install.ps1 без BOM: иначе «irm | iex» получает U+FEFF первой командой.
    # Для -File BOM нужен: без него Windows PowerShell 5.1 читает файл как ANSI.
    if name == "install.ps1" and not content.startswith(UTF8_BOM):
        content = UTF8_BOM + content
    script = root / name
    script.write_bytes(content)
    return script


def _update(args, ctx: Context) -> None:
    cfg = config.load(url=getattr(args, "url", None), key=getattr(args, "key", None))
    with ctx.make_client(cfg.url, cfg.key) as client:
        # Свежий контракт: кеш на 10 минут после обновления сервера назвал бы прежнюю версию.
        system_version = client.refresh_contract().get("system_version")
    if not system_version:
        raise CommandError(
            "Система не назвала свою версию — обратитесь к администратору системы",
            "system_version_missing",
        )
    version = release_version(system_version)
    # Ключ установщику не нужен: он ставит набор, конфигурация утилиты остаётся.
    env = {k: v for k, v in os.environ.items() if k != "ARTPUSH_TOKEN"}
    if config.is_windows():
        _update_windows(version, env, ctx)
        return
    with tempfile.TemporaryDirectory(prefix="artpush-update-") as temp:
        script = _fetch_installer(version, Path(temp), ctx)
        cmd = ["sh", str(script), "--version", version]
        # stdout установки — в stderr: stdout утилиты остаётся для JSON.
        result = subprocess.run(cmd, env=env, stdout=sys.stderr, check=False)
    if result.returncode != 0:
        raise CommandError(
            f"Установка набора завершилась с кодом {result.returncode}", "update_failed"
        )
    emit({"updated": True, "version": version}, table=ctx.table)


def _update_windows(version: str, env: dict, ctx: Context) -> None:
    """На Windows работающий artpush.exe держит файлы своей среды uv, и переустановка их не
    заменит. Установщик идёт в отдельном окне и ждёт, пока этот процесс завершится.
    Папку со скриптом удалять некому: она остаётся во временном каталоге."""
    script = _fetch_installer(version, Path(tempfile.mkdtemp(prefix="artpush-update-")), ctx)
    env["ARTPUSH_VERSION"] = version
    env["ARTPUSH_WAIT_PID"] = str(os.getpid())
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    try:
        subprocess.Popen(cmd, env=env, creationflags=CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise CommandError(f"Не удалось запустить установку: {exc}", "update_failed") from None
    err("Обновление идёт в отдельном окне — дождитесь «Готово» и откройте агента заново")
    emit({"updated": "started", "version": version}, table=ctx.table)


def register(subparsers, parents: list) -> None:
    init = command(
        subparsers, "init", "Сохранить адрес системы и ключ агента (проверяет ключ)", parents
    )
    # init и update контракт не сверяют: первая сохраняет ключ, вторая и есть лекарство.
    init.set_defaults(handler=_init, mutates=None, contract_check=False)

    whoami = command(subparsers, "whoami", "Кто я: пользователь, роль, предел ключа", parents)
    whoami.set_defaults(handler=_whoami, mutates=None)

    contract = command(subparsers, "contract", "Показать контракт системы", parents)
    contract.add_argument("--op", help="одна операция с полями тела запроса (по id)")
    contract.add_argument("--fields", action="store_true", help="подписи полей картины")
    contract.set_defaults(handler=_contract, mutates=None)

    update = command(
        subparsers,
        "update",
        "Обновить утилиту и скиллы до версии системы (по сохранённому ключу)",
        parents,
    )
    update.set_defaults(handler=_update, mutates=None, contract_check=False)
