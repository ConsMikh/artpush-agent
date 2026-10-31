"""Сведения о художнике («Обо мне»): заметки на языках и документы с файлами."""

import datetime
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

import httpx

from artpush_cli.client import ApiError
from artpush_cli.commands import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_RATE,
    CommandError,
    Context,
    UsageError,
    command,
    group,
    ids,
)
from artpush_cli.commands.media import CHUNK, _free_name, _primary_language
from artpush_cli.output import emit, err

NOTES = "/api/artist/notes"
DOCS = "/api/artist/documents"
UPLOAD_ATTEMPTS = 3  # попыток на файл при сбое сети или 5xx
# Ответы, после которых остальные файлы не грузятся, и код выхода.
STOP_UPLOADS = {401: EXIT_FORBIDDEN, 403: EXIT_FORBIDDEN, 429: EXIT_RATE}
FILENAME = re.compile(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?")


def _kind(value: str) -> int | str:
    """Вид — номер либо название (неизвестное название сервер заводит в справочнике)."""
    return int(value) if value.strip().isdigit() else value.strip()


def _kind_filter(ctx: Context, dictionary: str, value: str | None) -> int | None:
    return None if value is None else ctx.resolve(dictionary, value)


def _lang(args, ctx: Context) -> str:
    return args.lang or _primary_language(ctx)


def _date(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise UsageError(f"--date: ожидается ГГГГ-ММ-ДД, получено «{value}»") from None


def _items(out: dict, ctx: Context):
    return out["items"] if ctx.table else out


def _notes(args, ctx: Context) -> None:
    params = {"q": args.q, "kind": _kind_filter(ctx, "artist_note_kind", args.kind)}
    out = ctx.get(NOTES, params={k: v for k, v in params.items() if v})
    emit(_items(out, ctx), table=ctx.table)


def _note(args, ctx: Context) -> None:
    emit(ctx.get(f"{NOTES}/{args.id}"), table=ctx.table)


def _note_add(args, ctx: Context) -> None:
    body = {"title": args.title, "kind": _kind(args.kind), "texts": {_lang(args, ctx): args.text}}
    emit(ctx.send("POST", NOTES, json=body), table=ctx.table)


def _note_edit(args, ctx: Context) -> None:
    """Меняет текст одного языка; версия — из GET перед правкой, остальные языки не трогаются."""
    path = f"{NOTES}/{args.id}"
    lang = _lang(args, ctx)
    body = {"version": ctx.get(path)["version"], "texts": {lang: args.text}}
    try:
        emit(ctx.send("PATCH", path, json=body), table=ctx.table)
    except ApiError as exc:
        if exc.status != 409 or exc.code != "version_conflict":
            raise
        current = exc.body.get("note") if isinstance(exc.body, dict) else None
        raise CommandError(
            f"{exc.detail} (заметка № {args.id}, язык {lang}): сверьте текст и повторите правку",
            "version_conflict",
            EXIT_CONFLICT,
            note=current,
        ) from None


def _docs(args, ctx: Context) -> None:
    params = {
        "q": args.q,
        "year": args.year,
        "painting": args.painting,
        "kind": _kind_filter(ctx, "artist_doc_kind", args.kind),
    }
    out = ctx.get(DOCS, params={k: v for k, v in params.items() if v})
    emit(_items(out, ctx), table=ctx.table)


def _doc(args, ctx: Context) -> None:
    emit(ctx.get(f"{DOCS}/{args.id}"), table=ctx.table)


def _upload_file(ctx: Context, doc: int, path: Path) -> dict:
    """Загрузка файла с повтором; ApiError 4xx — сразу, сеть и 5xx — до UPLOAD_ATTEMPTS раз."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle)}
                return ctx.send("POST", f"{DOCS}/{doc}/files", files=files)
        except (httpx.TransportError, ApiError) as exc:
            retryable = not isinstance(exc, ApiError) or exc.status >= 500
            if not retryable or attempt == UPLOAD_ATTEMPTS:
                raise
            err(f"{path.name}: попытка {attempt} не удалась ({exc}), повтор")
            time.sleep(attempt)
    raise AssertionError("недостижимо")  # pragma: no cover


def _upload(ctx: Context, doc: int, names: list[str]) -> tuple[list, list, int]:
    """Грузит файлы по одному: ошибка одного печатается, остальные грузятся.
    Отдаёт (загружено, не загружено, код выхода)."""
    uploaded, failed, code = [], [], 0
    for name in names:
        path = Path(name)
        if not path.is_file():
            failed.append({"file": name, "error": "Файл не найден"})
            err(f"{name}: файл не найден")
            code = code or EXIT_ERROR
            continue
        try:
            uploaded.append(_upload_file(ctx, doc, path))
        except ApiError as exc:
            failed.append({"file": name, "error": exc.detail, "code": exc.code})
            err(f"{name}: {exc.detail}")
            code = code or STOP_UPLOADS.get(exc.status, EXIT_ERROR)
            if exc.status in STOP_UPLOADS:
                break
        except httpx.HTTPError as exc:
            failed.append({"file": name, "error": f"Сеть: {exc}"})
            err(f"{name}: сеть: {exc}")
            code = code or EXIT_ERROR
    return uploaded, failed, code


def _doc_add(args, ctx: Context) -> int:
    for name in args.files:
        if not Path(name).is_file():
            raise UsageError(f"Файл не найден: {name}")
    body = {
        "title": args.title,
        "kind": _kind(args.kind),
        "note": args.note,
        "painting_ids": args.painting or [],
        "event_ids": args.event or [],
        "media_ids": args.media or [],
    }
    if args.date:
        body["date"] = _date(args.date)
    doc = ctx.send("POST", DOCS, json=body)
    uploaded, failed, code = _upload(ctx, doc["id"], args.files)
    if uploaded or failed:
        doc = ctx.get(f"{DOCS}/{doc['id']}")
    result = {"document": doc, "uploaded": uploaded, "failed": failed}
    emit(result, table=ctx.table)
    return code


def _doc_file(args, ctx: Context) -> int:
    uploaded, failed, code = _upload(ctx, args.id, [args.file])
    emit({"uploaded": uploaded, "failed": failed}, table=ctx.table)
    return code


def _file_name(response: httpx.Response, pk: int) -> str:
    match = FILENAME.search(response.headers.get("content-disposition", ""))
    if match:
        return Path(unquote(match.group(1) or match.group(2))).name or f"file-{pk}"
    return f"file-{pk}"


def _doc_download(args, ctx: Context) -> None:
    """Скачивает файл документа потоком во временный файл и переименовывает по успеху.

    -o: путь с «/» на конце или существующий каталог — каталог (занятое имя не затирается);
    иное — файл, существующий не перезаписывается. Без -o — текущий каталог."""
    raw = args.output or "."
    folder = Path(raw)
    is_dir = raw.endswith(("/", "\\")) or folder.is_dir()
    if not is_dir and folder.exists():
        raise UsageError(f"Файл {raw} уже есть: укажите другое имя или каталог")
    response = ctx.client().request("GET", f"{DOCS}/files/{args.file_id}", stream=True)
    temp = None
    try:
        directory = folder if is_dir else folder.parent
        directory.mkdir(parents=True, exist_ok=True)
        target = (
            _free_name(folder, _file_name(response, args.file_id), args.file_id)
            if is_dir
            else folder
        )
        fd, name = tempfile.mkstemp(dir=directory, prefix=".artpush-", suffix=".part")
        temp = Path(name)
        size = 0
        with os.fdopen(fd, "wb") as handle:
            for chunk in response.iter_bytes(CHUNK):
                handle.write(chunk)
                size += len(chunk)
        os.replace(temp, target)
        temp = None
    except OSError as exc:
        raise CommandError(f"Не удалось сохранить файл: {exc}", "save_failed") from None
    finally:
        response.close()
        if temp is not None:
            temp.unlink(missing_ok=True)
    emit({"saved": {"file_id": args.file_id, "path": str(target), "bytes": size}}, table=ctx.table)


def register(subparsers, parents: list) -> None:
    artist = group(subparsers, "artist", "Сведения о художнике: заметки и документы")

    notes = command(artist, "notes", "Заметки о художнике", parents)
    notes.add_argument("--q", default="", help="слова из названия и текстов")
    notes.add_argument("--kind", help="вид заметки (название или номер)")
    notes.set_defaults(handler=_notes, mutates=None)
    note = command(artist, "note", "Заметка с текстами на языках", parents)
    note.add_argument("id", type=int, help="номер заметки")
    note.set_defaults(handler=_note, mutates=None)
    add = command(artist, "note-add", "Добавить заметку", parents)
    add.add_argument("--title", required=True)
    add.add_argument(
        "--kind", required=True, help="вид (название или номер; новое название заводится)"
    )
    add.add_argument("--text", required=True)
    add.add_argument("--lang", help="язык текста (по умолчанию — основной язык архива)")
    add.set_defaults(handler=_note_add, mutates=lambda a: True)
    edit = command(artist, "note-edit", "Изменить текст заметки на одном языке", parents)
    edit.add_argument("id", type=int, help="номер заметки")
    edit.add_argument("--text", required=True, help="новый текст этого языка")
    edit.add_argument("--lang", help="язык текста (по умолчанию — основной язык архива)")
    edit.set_defaults(handler=_note_edit, mutates=lambda a: True)

    docs = command(artist, "docs", "Документы художника", parents)
    docs.add_argument("--kind", help="вид документа (название или номер)")
    docs.add_argument("--year", type=int)
    docs.add_argument("--painting", type=int, help="номер картины")
    docs.add_argument("--q", default="", help="слова из названия и примечания")
    docs.set_defaults(handler=_docs, mutates=None)
    doc = command(artist, "doc", "Документ со связями и файлами", parents)
    doc.add_argument("id", type=int, help="номер документа")
    doc.set_defaults(handler=_doc, mutates=None)
    doc_add = command(artist, "doc-add", "Добавить документ (с файлами)", parents)
    doc_add.add_argument("--title", required=True)
    doc_add.add_argument(
        "--kind", required=True, help="вид (название или номер; новое название заводится)"
    )
    doc_add.add_argument("--date", help="ГГГГ-ММ-ДД")
    doc_add.add_argument("--note", default="", help="примечание")
    doc_add.add_argument("--painting", type=ids, help="номера картин через запятую")
    doc_add.add_argument("--event", type=ids, help="номера событий через запятую")
    doc_add.add_argument("--media", type=ids, help="номера медиафайлов через запятую")
    doc_add.add_argument("files", nargs="*", help="файлы документа")
    doc_add.set_defaults(handler=_doc_add, mutates=lambda a: True)
    doc_file = command(artist, "doc-file", "Добавить файл к документу", parents)
    doc_file.add_argument("id", type=int, help="номер документа")
    doc_file.add_argument("file", help="путь к файлу")
    doc_file.set_defaults(handler=_doc_file, mutates=lambda a: True)
    download = command(artist, "doc-download", "Скачать файл документа", parents)
    download.add_argument("file_id", type=int, help="номер файла (из artist doc)")
    download.add_argument(
        "-o",
        "--output",
        help="новый файл или каталог (путь с «/» на конце); по умолчанию — текущий каталог",
    )
    download.set_defaults(handler=_doc_download, mutates=None)
