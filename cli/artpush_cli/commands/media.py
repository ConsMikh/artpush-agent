"""Медиа картины: загрузка, скачивание, порядок, главное, перенос, обработка, карточка файла."""

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

from artpush_cli.client import ApiError
from artpush_cli.commands import (
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_RATE,
    POLL_SECONDS,
    CommandError,
    Context,
    UsageError,
    command,
    group,
    ids,
    timeout_argument,
    two_step,
)
from artpush_cli.output import emit, err

UPLOAD_ATTEMPTS = 3  # попыток на файл при сбое сети или 5xx
CHUNK = 1 << 16
# Ответы, после которых остальные файлы не грузятся, и код выхода.
STOP_UPLOADS = {401: EXIT_FORBIDDEN, 403: EXIT_FORBIDDEN, 429: EXIT_RATE}
PROBE_READ_ONLY = "объём — оценка: проба сжатия недоступна ключу только для чтения"


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
            "format": m.get("format_label") or m.get("format") or "",
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
        size = _save(ctx, f"/api/paintings/{args.id}/media/{item['id']}/{kind}", target)
        saved.append({"media_id": item["id"], "path": str(target), "bytes": size})
    emit({"saved": saved}, table=ctx.table)


def _save(ctx: Context, path: str, target: Path) -> int:
    """Скачивает файл потоком в target; при сбое недокачанный файл удаляется. Отдаёт байты."""
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
    return size


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


# --- Обработка: media edit ---

MB = 1024 * 1024  # «МБ» — как в браузере: 1 МБ = 1 048 576 байт
EDIT_TIMEOUT = 600  # секунд на задание (проба, новый файл, замена)
NOTHING_CHANGED = "Ничего не изменено"
YES = {"да", "д", "yes", "y"}
# Расширение и подпись формата результата — как у сервера и редактора (client/.../editor/recipe.ts).
EXTS = {
    "jpeg": "jpg",
    "png": "png",
    "tiff": "tif",
    "webp": "webp",
    "avif": "avif",
    "heic": "heic",
    "pdf": "pdf",
}
FORMAT_LABELS = {
    "jpeg": "JPEG",
    "png": "PNG",
    "tiff": "TIFF",
    "webp": "WebP",
    "avif": "AVIF",
    "heic": "HEIC",
    "pdf": "PDF",
}
# Формат результата по коду формата исходного (MediaOut.format); RAW, PSD и прочих нет — у них JPEG.
SOURCE_FORMAT = {
    "JPEG": "jpeg",
    "PNG": "png",
    "TIFF": "tiff",
    "WEBP": "webp",
    "AVIF": "avif",
    "HEIF": "heic",
    "PDF": "pdf",
}
# Качество по умолчанию, если оно не взято у исходного того же формата (6а.1).
DEFAULT_QUALITY = {"jpeg": 90, "webp": 90, "avif": 75, "heic": 80, "pdf": 90}
# Предел кодера HEIC и AVIF в пикселях — как ENCODE_MAX_PIXELS в
# server/artpush/catalog/format_limits.py: исходный больше него по умолчанию идёт в JPEG.
ENCODE_MAX_PIXELS = {"avif": 40_000_000, "heic": 40_000_000}
QUALITY_WITH_MAX_MB = (
    "--quality не сочетается с --max-mb: при пределе объёма качество подбирает проба — "
    "оставьте один из флагов"
)
CMYK_FORMATS = ("jpeg", "tiff", "pdf")
CMYK_ONLY = "Печать CMYK — только для JPEG, TIFF и PDF"
# Флаг --profile → color.profile рецепта.
PROFILES = {"screen": "screen", "print": "print", "cmyk": "print_cmyk", "keep": "keep"}
# Настройка формата словами — как во вкладке «Формат» редактора.
TIFF_COMPRESSION = {"lzw": "LZW", "zip": "ZIP", "none": "без сжатия"}
PDF_IMAGE = {"jpeg": "JPEG", "lossless": "без потерь"}
SOFT_PROOF = "Цвета CMYK на экране приблизительные; профиль уточните у типографии"  # как ru.ts
SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*[xхX×]\s*(\d+(?:[.,]\d+)?)\s*$")


def _ask(prompt: str) -> bool:
    """Вопрос человеку в stderr, ответ — строка stdin; пусто или нет stdin — «нет»."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError, AttributeError):
        return False
    return line.strip().casefold() in YES


def _pair(kind):
    def parse(value: str):
        match = SIZE_RE.match(value)
        if not match:
            raise argparse.ArgumentTypeError(f"ожидается ШxВ, например 15x10: «{value}»")
        w, h = (float(x.replace(",", ".")) for x in match.groups())
        return (int(w), int(h)) if kind is int else (w, h)

    return parse


def _megabytes(value: str) -> float:
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается число мегабайт: «{value}»") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("предел объёма — больше нуля")
    return number


def _volume(size: int | None, estimate: bool = False) -> str:
    """«1,86 МБ» — два знака, как в редакторе; оценка — с «≈»."""
    if size is None:
        return ""
    text = f"{size / MB:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{'≈' if estimate else ''}{text} МБ"


def _num(value) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def _lossy(fmt: str, pdf_image: str | None) -> bool:
    """Сжатие с потерями: есть качество и его подбор под объём (PDF — по изображению внутри)."""
    return fmt in ("jpeg", "webp", "avif", "heic") or (fmt == "pdf" and pdf_image != "lossless")


def _combo_error(args, fmt: str) -> str:
    """Флаги, которые не подходят к формату результата; пусто — подходят."""
    if args.tiff_compression and fmt != "tiff":
        return (
            f"--tiff-compression — только у TIFF, а результат — {FORMAT_LABELS[fmt]}; "
            "добавьте --format tiff"
        )
    if args.pdf_image and fmt != "pdf":
        return (
            f"--pdf-image — только у PDF, а результат — {FORMAT_LABELS[fmt]}; добавьте --format pdf"
        )
    if args.profile == "cmyk" and fmt not in CMYK_FORMATS:
        return f"{CMYK_ONLY} (формат результата — {FORMAT_LABELS[fmt]})"
    if args.quality is not None and not _lossy(fmt, args.pdf_image):
        return (
            f"--quality — только у jpeg, webp, avif, heic и pdf с JPEG, не у {FORMAT_LABELS[fmt]}"
        )
    return ""


def _check_flags(args) -> None:
    """Проверка флагов до запросов к серверу при явном --format.

    Без --format формат — у исходного каждого файла: сочетание проверяет _recipe, и неподходящий
    файл не делается, а остальные идут.
    """
    if args.quality is not None and args.max_mb:
        raise UsageError(QUALITY_WITH_MAX_MB)
    if args.quality is not None and not 1 <= args.quality <= 100:
        raise UsageError("--quality — от 1 до 100")
    if args.format and (error := _combo_error(args, args.format)):
        raise UsageError(error)


def _source_format(card: dict) -> str:
    return SOURCE_FORMAT.get(card.get("format") or "JPEG", "jpeg")


def _default_format(card: dict) -> str:
    """Формат исходного, если его кодер берёт такой размер (HEIC 48 Мпикс — нет), иначе JPEG."""
    fmt = _source_format(card)
    pixels = (card.get("width") or 0) * (card.get("height") or 0)
    return "jpeg" if pixels > ENCODE_MAX_PIXELS.get(fmt, pixels) else fmt


def _recipe(args, card: dict) -> dict:
    """Рецепт обработки файла из флагов и его карточки; не заданное — по умолчанию сервера.

    Формат и качество по умолчанию — по исходному, как в редакторе: формат исходного, если система
    его пишет (RAW, PSD, HEIC и AVIF больше 40 Мпикс — JPEG); качество исходного того же формата,
    если известно, иначе DEFAULT_QUALITY.
    Увеличение в рецепте всегда «спросить»: так предпросмотр честно показывает, что оно нужно,
    а решение человека (--allow-upscale) уходит флагом allow_upscale задания.
    """
    fmt = args.format or _default_format(card)
    if error := _combo_error(args, fmt):
        raise CommandError(error, "bad_recipe")
    geometry = {}
    if args.rotate:
        geometry["rotate"] = args.rotate
    for side in args.flip or ():
        geometry[f"flip_{side}"] = True
    size: dict = {"mode": "keep"}
    if args.long_side:
        size = {"mode": "long", "long_px": args.long_side}
    elif args.size_px:
        size = {"mode": "px", "w_px": args.size_px[0], "h_px": args.size_px[1]}
    elif args.size_cm:
        size = {"mode": "cm", "w_cm": args.size_cm[0], "h_cm": args.size_cm[1]}
    if args.dpi:
        size["dpi"] = args.dpi
    if args.fit:
        size["fit"] = args.fit
    if args.pad_color:
        size["pad_color"] = args.pad_color
    size["upscale"] = "ask"
    encode: dict = {"format": fmt}
    if args.max_mb:
        encode.update(mode="max_bytes", max_bytes=round(args.max_mb * MB))
    else:
        encode["mode"] = "quality"
        if _lossy(fmt, args.pdf_image):
            known = card.get("quality") if _source_format(card) == fmt else None
            encode["quality"] = args.quality or known or DEFAULT_QUALITY[fmt]
    if args.tiff_compression:
        encode["tiff_compression"] = args.tiff_compression
    if args.pdf_image:
        encode["pdf_image"] = args.pdf_image
    recipe: dict = {"size": size, "encode": encode}
    if geometry:
        recipe["geometry"] = geometry
    if args.profile:
        recipe["color"] = {"profile": PROFILES[args.profile]}
    if args.keep_exif:
        recipe["meta"] = {"keep_shooting": True}
    return recipe


def _wait_edit(ctx: Context, edit: dict, timeout: float) -> dict:
    """Опрос задания до done/failed; дедлайн — CommandError timeout с номером задания."""
    deadline = time.monotonic() + timeout
    while edit["state"] not in ("done", "failed"):
        if time.monotonic() >= deadline:
            raise CommandError(
                f"Задание {edit['id']} не завершилось за {timeout:g} с — оно идёт на сервере",
                "timeout",
                edit=edit["id"],
            )
        time.sleep(POLL_SECONDS)
        edit = ctx.get(f"/api/media-edits/{edit['id']}")
    return edit


def _planned_name(args, file: str, fmt: str) -> str:
    """Имя результата, как его даст сервер; номер <n> по умолчанию сервер выберет сам."""
    stem = Path(file).stem
    if args.replace:
        base = stem
    else:
        base = _name(args.name, file) if args.name else f"{stem}-<n>"
    return f"{base}.{EXTS[fmt]}"


def _plan_row(args, item: dict) -> dict:
    report = item["report"]
    fmt = item["recipe"]["encode"]["format"]
    label = report.get("format") or FORMAT_LABELS[fmt]  # у сервера — подпись: «HEIC», «WebP»
    w, h = report["width"], report["height"]
    cm = report.get("cm") or [None, None]
    exact = "size" in report  # у пробы — точный объём, у предпросмотра — оценка
    warnings = list(report.get("warnings", []))
    if report.get("soft_proof") and SOFT_PROOF not in warnings:
        warnings.append(SOFT_PROOF)
    setting = {k: report[k] for k in ("tiff_compression", "pdf_image") if report.get(k)}
    return {
        "media_id": item["media_id"],
        "file": item["file"],
        "painting_id": item["painting_id"],
        "name": _planned_name(args, item["file"], fmt),
        "result": f"{w}×{h} px, {_num(cm[0])}×{_num(cm[1])} см, {report.get('dpi')} dpi",
        "encode": f"{label} {report.get('quality') or ''}".strip(),
        **setting,
        "profile": report.get("profile", ""),
        "soft_proof": bool(report.get("soft_proof")),
        "volume": _volume(report.get("size", report.get("size_estimate")), not exact),
        "warnings": warnings,
        "unmet": bool(report.get("unmet")),
        "upscale_needed": bool(report.get("upscale_needed")),
        "blocked": item.get("blocked", ""),
    }


def _print_plan(rows: list[dict]) -> None:
    """План для человека — в stderr, до вопроса."""
    for row in rows:
        warnings = "; ".join(row["warnings"])
        encode = row["encode"]
        if "tiff_compression" in row:
            encode += f" ({TIFF_COMPRESSION.get(row['tiff_compression'], row['tiff_compression'])})"
        if "pdf_image" in row:
            encode += f" ({PDF_IMAGE.get(row['pdf_image'], row['pdf_image'])})"
        if row["profile"]:
            encode += f", {row['profile']}"
        line = (
            f"{row['file']} (медиа {row['media_id']}, картина {row['painting_id']}): "
            f"{row['result']}, {encode}, {row['volume']} → {row['name']}"
        )
        err(line + (f" — {warnings}" if warnings else ""))
        if row["blocked"]:
            err(f"  нельзя без решения человека: {row['blocked']}")


def _prepare(args, ctx: Context, item: dict) -> None:
    """Дополняет item картой файла, рецептом, отчётом предпросмотра и (при --max-mb) пробой.

    Ошибка — исключением; имя файла к этому моменту уже в item, если карточка прочитана.
    """
    media_id = item["media_id"]
    card = ctx.get(f"/api/media/{media_id}")
    item.update(file=card["original_name"], painting_id=card["painting_id"])
    recipe = item["recipe"] = _recipe(args, card)
    report = ctx.send("POST", f"/api/media/{media_id}/preview", json=recipe)["report"]
    item["report"] = report
    if report.get("upscale_needed") and not args.allow_upscale:
        item["blocked"] = "нужно увеличение сверх исходного — повторите с --allow-upscale"
        return
    if args.max_mb:
        body = {"recipe": recipe, "mode": "probe", "allow_upscale": args.allow_upscale}
        try:
            started = ctx.send("POST", f"/api/media/{media_id}/edits", json=body)
        except ApiError as exc:
            # проба — запись: ключу «read» отчёт достаётся с оценкой «≈» вместо точного объёма
            if args.dry_run and exc.code == "scope_denied":
                report["warnings"] = [*report.get("warnings", []), PROBE_READ_ONLY]
                return
            raise
        probe = _wait_edit(ctx, started, args.timeout)
        if probe["state"] == "failed":
            raise CommandError(probe["error"] or "Сжатие не удалось", "edit_failed")
        item["probe"] = probe["id"]
        item["report"] = {**probe["report"], "upscale_needed": report.get("upscale_needed")}
        if probe["report"].get("unmet") and not args.accept_unmet:
            item["blocked"] = "объём больше заданного — повторите с --accept-unmet"


def _name(template: str, file: str) -> str:
    return template.replace("{name}", Path(file).stem) if template else ""


def _apply(args, ctx: Context, item: dict, folder: Path | None) -> dict:
    body = {
        "recipe": item["recipe"],
        "mode": "replace" if args.replace else "new",
        "name": "" if args.replace else _name(args.name, item["file"]),
        "label": args.label or "",
        "probe": item.get("probe"),
        "allow_upscale": args.allow_upscale,
        "accept_unmet": args.accept_unmet,
    }
    edit = ctx.send("POST", f"/api/media/{item['media_id']}/edits", json=body)
    edit = _wait_edit(ctx, edit, args.timeout)
    if edit["state"] == "failed":
        raise CommandError(edit["error"] or "Обработка не удалась", "edit_failed")
    result = edit["result_id"]
    done = {"media_id": item["media_id"], "file": item["file"], "result_id": result}
    if folder is not None:
        name = edit.get("name") or ctx.get(f"/api/media/{result}")["original_name"]
        target = _free_name(folder, Path(name).name, result)
        _save(ctx, f"/api/paintings/{item['painting_id']}/media/{result}/original", target)
        done["path"] = str(target)
    return done


def _reason(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.detail
    if isinstance(exc, httpx.TransportError):
        return f"Сеть: {exc}"
    return str(exc)


def _summary(done: list, failed: list) -> str:
    text = f"сделано {len(done)}, не сделано {len(failed)}"
    if failed:
        text += ": " + "; ".join(f"{f['file'] or f['media_id']} — {f['error']}" for f in failed)
    return text


def _edit(args, ctx: Context) -> int:
    if args.name and len(args.media_ids) > 1 and "{name}" not in args.name:
        raise UsageError("Для нескольких файлов --name — шаблон с {name}, иначе имена совпадут")
    if args.name and args.replace:
        raise UsageError("--name не сочетается с --replace: заменённый файл сохраняет имя")
    _check_flags(args)
    items, failed = [], []
    for media_id in args.media_ids:
        item = {"media_id": media_id, "file": ""}
        try:
            _prepare(args, ctx, item)
        except ApiError as exc:
            if exc.status in STOP_UPLOADS:
                raise
            failed.append({"media_id": media_id, "file": item["file"], "error": _reason(exc)})
        except (CommandError, httpx.TransportError) as exc:
            failed.append({"media_id": media_id, "file": item["file"], "error": _reason(exc)})
        else:
            items.append(item)
    plan = [_plan_row(args, item) for item in items]
    _print_plan(plan)
    for f in failed:
        err(f"{f['file'] or f['media_id']}: {f['error']}")
    if not items:
        emit(
            {"plan": plan, "done": [], "failed": failed, "summary": _summary([], failed)},
            table=ctx.table,
        )
        return EXIT_ERROR
    if args.dry_run:
        emit(
            {"plan": plan, "done": [], "failed": failed, "summary": "только отчёт"}, table=ctx.table
        )
        return EXIT_ERROR if failed else 0
    if any(row["blocked"] for row in plan):
        raise CommandError(
            NOTHING_CHANGED, "decision_required", EXIT_FORBIDDEN, plan=plan, failed=failed
        )
    human = any(row["unmet"] or row["upscale_needed"] for row in plan)
    asks = len(args.media_ids) > 1 or args.replace or human
    if asks and (human or not args.yes):
        what = "Заменить содержимое" if args.replace else "Сохранить новыми файлами"
        if not _ask(f"{what} ({len(items)} шт.)? (да / нет): "):
            raise CommandError(
                NOTHING_CHANGED, "not_confirmed", EXIT_FORBIDDEN, plan=plan, failed=failed
            )
    folder = None
    if args.to:
        folder = Path(args.to)
        folder.mkdir(parents=True, exist_ok=True)
    done = []
    for index, item in enumerate(items):
        try:
            done.append(_apply(args, ctx, item, folder))
            err(f"{item['file']}: готово")
        except ApiError as exc:
            failed.append(
                {"media_id": item["media_id"], "file": item["file"], "error": _reason(exc)}
            )
            if exc.status in STOP_UPLOADS:  # ключ отозван или темп исчерпан: дальше не идём
                skipped = f"Не сделано: остановлено после ошибки ({exc.detail})"
                failed += [
                    {"media_id": i["media_id"], "file": i["file"], "error": skipped}
                    for i in items[index + 1 :]
                ]
                break
        except (CommandError, httpx.TransportError, OSError) as exc:
            failed.append(
                {"media_id": item["media_id"], "file": item["file"], "error": _reason(exc)}
            )
    summary = _summary(done, failed)
    err(summary)
    emit({"plan": plan, "done": done, "failed": failed, "summary": summary}, table=ctx.table)
    return EXIT_ERROR if failed else 0


# --- Карточка файла: метки, заметка, имя, версии ---


def _patch(ctx: Context, media_id: int, body: dict) -> None:
    emit(ctx.send("PATCH", f"/api/media/{media_id}", json=body), table=ctx.table)


def _label(args, ctx: Context) -> None:
    labels = ctx.get(f"/api/media/{args.media_id}").get("labels", [])
    if args.add:
        if args.add.casefold() not in {x.casefold() for x in labels}:
            labels = [*labels, args.add]
    else:
        labels = [x for x in labels if x.casefold() != args.remove.casefold()]
    _patch(ctx, args.media_id, {"labels": labels})


def _note(args, ctx: Context) -> None:
    _patch(ctx, args.media_id, {"note": args.text})


def _rename(args, ctx: Context) -> None:
    _patch(ctx, args.media_id, {"original_name": args.name})


def _versions(args, ctx: Context) -> None:
    emit(ctx.get(f"/api/media/{args.media_id}/versions"), table=ctx.table)


def _restore(args, ctx: Context) -> None:
    question = (
        f"Вернуть версию {args.version_id} файла {args.media_id}? "
        "Текущее содержимое станет версией. (да / нет): "
    )
    if not args.yes and not _ask(question):
        raise CommandError(NOTHING_CHANGED, "not_confirmed", EXIT_FORBIDDEN)
    path = f"/api/media/{args.media_id}/versions/{args.version_id}/restore"
    emit(ctx.send("POST", path), table=ctx.table)


def _register_edit(media, parents: list) -> None:
    edit = command(
        media,
        "edit",
        "Обработать изображения: формат, размер, объём, поворот; итог — новые файлы или замена",
        parents,
    )
    edit.epilog = (
        "Если хоть один файл требует решения (увеличение без --allow-upscale, объём больше "
        "предела без --accept-unmet), ничего не делается ни с одним файлом: код 3, "
        "«Ничего не изменено». Подтверждение — ответ «да» строкой в stdin."
    )
    edit.add_argument("media_ids", type=ids, help="номера медиафайлов через запятую")
    edit.add_argument(
        "--format",
        choices=tuple(EXTS),
        help="формат результата (по умолчанию — формат исходного; у RAW, PSD, HEIC и AVIF "
        "больше 40 Мпикс — jpeg)",
    )
    # --quality и --max-mb вместе отклоняет _check_flags: у группы argparse текст английский.
    edit.add_argument(
        "--quality",
        type=int,
        help="качество 1–100 у jpeg, webp, avif, heic и pdf с JPEG (по умолчанию — исходного "
        "того же формата, иначе jpeg/webp/pdf 90, avif 75, heic 80)",
    )
    edit.add_argument(
        "--max-mb",
        type=_megabytes,
        help="предел объёма в МБ (1 МБ = 1024×1024 байт, как в браузере); "
        "сначала проба сжатия с точным объёмом; без флага объём не ограничен",
    )
    edit.add_argument(
        "--tiff-compression",
        choices=("lzw", "zip", "none"),
        help="сжатие TIFF без потерь (по умолчанию lzw); none — без сжатия; только у TIFF",
    )
    edit.add_argument(
        "--pdf-image",
        choices=("jpeg", "lossless"),
        help="изображение в PDF: jpeg (по умолчанию) или lossless — без потерь; только у PDF",
    )
    size = edit.add_mutually_exclusive_group()
    size.add_argument("--long-side", type=int, metavar="PX", help="длинная сторона в пикселях")
    size.add_argument("--size-px", type=_pair(int), metavar="WxH", help="ширина×высота в пикселях")
    size.add_argument(
        "--size-cm", type=_pair(float), metavar="WxH", help="ширина×высота в сантиметрах при --dpi"
    )
    edit.add_argument("--dpi", type=int, help="разрешение для --size-cm (по умолчанию 300)")
    edit.add_argument(
        "--fit",
        choices=("pad", "crop"),
        help="другие пропорции: pad — поля, crop — обрезать (по умолчанию pad)",
    )
    edit.add_argument("--pad-color", help="цвет полей, например '#ffffff'")
    edit.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        help="цвет: screen — sRGB (по умолчанию), print — печать RGB (ClayRGB), "
        "cmyk — печать CMYK (ISO Coated v2; только jpeg, tiff, pdf), keep — как у исходного",
    )
    edit.add_argument("--rotate", type=int, choices=(90, 180, 270), help="поворот по часовой")
    edit.add_argument(
        "--flip",
        choices=("h", "v"),
        action="append",
        help="отразить: h — по горизонтали, v — по вертикали",
    )
    edit.add_argument("--keep-exif", action="store_true", help="сохранить данные съёмки (EXIF)")
    edit.add_argument(
        "--name",
        default="",
        help="имя нового файла без расширения; шаблон с {name} — имя исходного "
        "(по умолчанию «{name}-<n>»)",
    )
    edit.add_argument("--label", help="метка нового файла")
    edit.add_argument(
        "--replace",
        action="store_true",
        help="заменить содержимое (прежнее станет версией); только по поручению",
    )
    edit.add_argument(
        "--allow-upscale",
        action="store_true",
        help="разрешить увеличение сверх исходного — только после ответа человека",
    )
    edit.add_argument(
        "--accept-unmet",
        action="store_true",
        help="принять объём больше предела — только после ответа человека",
    )
    edit.add_argument(
        "--yes",
        action="store_true",
        help="не спрашивать подтверждения (кроме увеличения и недостижимого объёма)",
    )
    edit.add_argument("--to", metavar="DIR", help="скачать результаты в папку")
    edit.add_argument(
        "--dry-run",
        action="store_true",
        help="только отчёт (предпросмотр и проба сжатия), ничего не сохранять",
    )
    timeout_argument(edit, EDIT_TIMEOUT, "каждого задания")
    # Отчёт без пробы только читает (предпросмотр — операция чтения): хватает ключа read.
    edit.set_defaults(handler=_edit, mutates=lambda a: not a.dry_run or bool(a.max_mb))


def _register_card(media, parents: list) -> None:
    label = command(media, "label", "Добавить или убрать метку файла", parents)
    label.add_argument("media_id", type=int, help="номер медиа")
    which = label.add_mutually_exclusive_group(required=True)
    which.add_argument("--add", metavar="T", help="добавить метку")
    which.add_argument("--remove", metavar="T", help="убрать метку")
    label.set_defaults(handler=_label, mutates=lambda a: True)

    note = command(media, "note", "Заметка к файлу (заменяет прежнюю; пусто — убрать)", parents)
    note.add_argument("media_id", type=int, help="номер медиа")
    note.add_argument("text", help="текст заметки")
    note.set_defaults(handler=_note, mutates=lambda a: True)

    rename = command(media, "rename", "Переименовать файл (расширение остаётся прежним)", parents)
    rename.add_argument("media_id", type=int, help="номер медиа")
    rename.add_argument("name", help="новое имя")
    rename.set_defaults(handler=_rename, mutates=lambda a: True)

    versions = command(media, "versions", "Прежние версии файла", parents)
    versions.add_argument("media_id", type=int, help="номер медиа")
    versions.set_defaults(handler=_versions, mutates=None)

    restore = command(media, "restore", "Вернуть прежнюю версию файла (с подтверждением)", parents)
    restore.add_argument("media_id", type=int, help="номер медиа")
    restore.add_argument("version_id", type=int, help="номер версии из media versions")
    restore.add_argument("--yes", action="store_true", help="человек уже ответил «да»")
    restore.set_defaults(handler=_restore, mutates=lambda a: True)


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

    _register_edit(media, parents)
    _register_card(media, parents)
