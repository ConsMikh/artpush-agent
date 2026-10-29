"""Выгрузка архива одним ZIP: запуск, состояние, скачивание.

Своя — ключ художницы (`archive export`); чужая по логину — ключ администратора (`admin export`).
Сборка идёт на сервере в фоне, готовый файл хранится 7 дней.
"""

from pathlib import Path

from artpush_cli.client import ApiError
from artpush_cli.commands import CommandError, Context, command, group
from artpush_cli.commands.publish import _filename, _free
from artpush_cli.output import emit, err

MY_EXPORT = "/api/me/export"
CHUNK = 1 << 20


def admin_export_path(username: str) -> str:
    return f"/api/admin/users/{username}/export"


def _download(ctx: Context, base: str, folder: Path) -> None:
    """Готовый ZIP потоком в folder под именем от сервера; занятое имя не перезаписывается."""
    response = ctx.client().request("GET", f"{base}/download", stream=True)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        name = _filename(response.headers.get("content-disposition", ""))
        target = _free(folder / name)
        partial = target.with_name(target.name + ".part")
        size = 0
        try:
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK):
                    handle.write(chunk)
                    size += len(chunk)
            expected = response.headers.get("content-length")
            if expected is not None and expected.isdigit() and int(expected) != size:
                raise CommandError(
                    f"Скачивание оборвалось: получено {size} из {expected} байт — скачайте заново",
                    "download_incomplete",
                )
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    finally:
        response.close()
    emit({"saved": str(target), "bytes": size}, table=ctx.table)


def run_export(args, ctx: Context, base: str) -> None:
    """--status — состояние, --download ПАПКА — скачать, без флагов — запустить сборку."""
    if args.download:
        _download(ctx, base, Path(args.download))
        return
    if args.status:
        emit(ctx.get(base), table=ctx.table)
        return
    try:
        out = ctx.send("POST", base)
    except ApiError as exc:
        if exc.code != "export_running":
            raise
        err("Выгрузка уже идёт — показываю её состояние")
        out = ctx.get(base)
    emit(out, table=ctx.table)


def export_arguments(parser) -> None:
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--status", action="store_true", help="состояние последней выгрузки")
    which.add_argument("--download", metavar="ПАПКА", help="скачать готовый ZIP в папку")


def register(subparsers, parents: list) -> None:
    archive = group(subparsers, "archive", "Архив целиком (ключ художницы)")
    export = command(
        archive, "export", "Выгрузка архива в ZIP: запустить, состояние, скачать", parents
    )
    export_arguments(export)
    export.set_defaults(
        handler=lambda args, ctx: run_export(args, ctx, MY_EXPORT),
        mutates=lambda a: not (a.status or a.download),
    )
