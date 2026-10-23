"""Медиа картины: загрузка, скачивание, порядок, главное, перенос."""

import time
from pathlib import Path

import httpx

from artpush_cli.client import ApiError
from artpush_cli.commands import (
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_RATE,
    Context,
    UsageError,
    command,
    group,
    ids,
    two_step,
)
from artpush_cli.output import emit, err

UPLOAD_ATTEMPTS = 3  # попыток на файл при сбое сети или 5xx
CHUNK = 1 << 16
# Ответы, после которых остальные файлы не грузятся, и код выхода.
STOP_UPLOADS = {401: EXIT_FORBIDDEN, 403: EXIT_FORBIDDEN, 429: EXIT_RATE}


def _upload_one(ctx: Context, painting: int, path: Path) -> dict:
    """Загрузка файла с повтором; ApiError 4xx — сразу, сеть и 5xx — до UPLOAD_ATTEMPTS раз."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle)}
                return ctx.send("POST", f"/api/paintings/{painting}/media", files=files)
        except (httpx.TransportError, ApiError) as exc:
            retryable = not isinstance(exc, ApiError) or exc.status >= 500
            if not retryable or attempt == UPLOAD_ATTEMPTS:
                raise
            err(f"{path.name}: попытка {attempt} не удалась ({exc}), повтор")
            time.sleep(attempt)
    raise AssertionError("недостижимо")  # pragma: no cover


def _upload(args, ctx: Context) -> int:
    result = {"uploaded": [], "duplicates": [], "failed": []}
    for index, name in enumerate(args.files):
        path = Path(name)
        if not path.is_file():
            result["failed"].append({"file": name, "error": "Файл не найден"})
            continue
        try:
            out = _upload_one(ctx, args.id, path)
        except ApiError as exc:
            result["failed"].append({"file": name, "error": exc.detail})
            if exc.status in STOP_UPLOADS:  # ключ отозван или темп исчерпан: дальше не идём
                skipped = f"Не загружен: остановлено после ошибки ({exc.detail})"
                result["failed"] += [{"file": f, "error": skipped} for f in args.files[index + 1 :]]
                emit(result, table=ctx.table)
                return STOP_UPLOADS[exc.status]
            continue
        except httpx.TransportError as exc:
            result["failed"].append({"file": name, "error": f"Сеть: {exc}"})
            continue
        media_id = out["media"]["id"]
        if out.get("duplicate"):
            result["duplicates"].append(
                {"file": name, "media_id": media_id, "also_in": out.get("also_in", [])}
            )
        else:
            result["uploaded"].append({"file": name, "media_id": media_id})
        err(f"{path.name}: загружен")
    emit(result, table=ctx.table)
    return EXIT_ERROR if result["failed"] else 0


def _free_name(folder: Path, name: str, media_id: int) -> Path:
    """Имя файла без перезаписи: занято — с номером медиа впереди, дальше -2, -3…"""
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate, number = folder / f"{media_id}-{name}", 1
    while candidate.exists():
        number += 1
        candidate = folder / f"{media_id}-{stem}-{number}{suffix}"
    return candidate


def _list(args, ctx: Context) -> None:
    rows = [
        {
            "id": m["id"],
            "kind": m.get("kind"),
            "is_main": m.get("is_main", False),
            "filename": m.get("original_name"),
            "status": m.get("processing_status"),
        }
        for m in ctx.painting(args.id).get("media", [])
    ]
    emit(rows, table=ctx.table)


def _download(args, ctx: Context) -> None:
    media = ctx.painting(args.id).get("media", [])
    if args.main:
        media = [m for m in media if m.get("is_main")]
        if not media:
            raise UsageError(f"У картины {args.id} нет главного медиа")
    elif args.media is not None:
        media = [m for m in media if m["id"] == args.media]
        if not media:
            raise UsageError(f"У картины {args.id} нет медиа {args.media}")
    folder = Path(args.to)
    folder.mkdir(parents=True, exist_ok=True)
    kind = "thumbnail" if args.thumbnail else "original"
    saved = []
    for item in media:
        name = Path(item.get("original_name") or str(item["id"])).name
        if args.thumbnail:
            name = f"{Path(name).stem}.thumb.webp"
        target = _free_name(folder, name, item["id"])
        path = f"/api/paintings/{args.id}/media/{item['id']}/{kind}"
        response = ctx.client().request("GET", path, stream=True)
        size = 0
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK):
                    handle.write(chunk)
                    size += len(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        saved.append({"media_id": item["id"], "path": str(target), "bytes": size})
    emit({"saved": saved}, table=ctx.table)


def _order(args, ctx: Context) -> None:
    body = {"ids": args.media_ids}
    emit(ctx.send("PUT", f"/api/paintings/{args.id}/media/order", json=body), table=ctx.table)


def _main(args, ctx: Context) -> None:
    media = ctx.painting(args.id).get("media", [])
    if not any(m["id"] == args.media_id for m in media):
        raise UsageError(f"У картины {args.id} нет медиа {args.media_id}")
    emit(ctx.send("POST", f"/api/media/{args.media_id}/main"), table=ctx.table)


def _transfer_body(args) -> dict:
    return {"ids": args.ids, "target_id": args.to, "catalog_id": args.catalog}


def _transfer_preview(args, ctx: Context) -> None:
    out = ctx.send("POST", "/api/media/transfer/preview", json=_transfer_body(args))
    emit(out, table=ctx.table)


def _transfer(args, ctx: Context, token: str) -> None:
    body = {**_transfer_body(args), "preview_token": token}
    emit(ctx.send("POST", "/api/media/transfer", json=body), table=ctx.table)


def register(subparsers, parents: list) -> None:
    media = group(subparsers, "media", "Медиафайлы картины")

    upload = command(media, "upload", "Загрузить файлы к картине (по одному)", parents)
    upload.add_argument("id", type=int, help="номер картины")
    upload.add_argument("files", nargs="+", help="пути к файлам")
    upload.set_defaults(handler=_upload, mutates=lambda a: True)

    download = command(media, "download", "Скачать оригиналы или миниатюры", parents)
    download.add_argument("id", type=int, help="номер картины")
    which = download.add_mutually_exclusive_group(required=True)
    which.add_argument("--main", action="store_true", help="главное медиа")
    which.add_argument("--media", type=int, help="номер медиа")
    which.add_argument("--all", action="store_true", help="все медиа картины")
    download.add_argument("--thumbnail", action="store_true", help="миниатюры вместо оригиналов")
    download.add_argument("--to", required=True, help="папка назначения")
    download.set_defaults(handler=_download, mutates=None)

    listing = command(media, "list", "Медиа картины: номер, вид, главное, файл, состояние", parents)
    listing.add_argument("id", type=int, help="номер картины")
    listing.set_defaults(handler=_list, mutates=None)

    order = command(media, "order", "Задать порядок медиа", parents)
    order.add_argument("id", type=int, help="номер картины")
    order.add_argument("media_ids", type=ids, help="номера медиа через запятую")
    order.set_defaults(handler=_order, mutates=lambda a: True)

    main = command(media, "main", "Сделать медиа главным", parents)
    main.add_argument("id", type=int, help="номер картины")
    main.add_argument("media_id", type=int, help="номер медиа")
    main.set_defaults(handler=_main, mutates=lambda a: True)

    transfer = command(media, "transfer", "Перенести медиа в другую или новую картину", parents)
    transfer.add_argument("--ids", type=ids, required=True, help="номера медиа через запятую")
    target = transfer.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", type=int, help="номер картины назначения")
    target.add_argument("--new", action="store_true", help="в новую картину")
    transfer.add_argument("--catalog", type=int, help="каталог новой картины")
    two_step(transfer, preview=_transfer_preview, apply=_transfer)
