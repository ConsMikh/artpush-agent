"""Импорт из папки: манифест, загрузка с возобновлением, разбор с жетоном, создание картин."""

import os
import re
import time
from pathlib import Path

import httpx

from artpush_cli.client import ApiError
from artpush_cli.commands import (
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_RATE,
    CommandError,
    Context,
    UsageError,
    command,
    group,
    ids,
    poll_session,
    timeout_argument,
    two_step,
)
from artpush_cli.output import emit, err

SESSIONS = "/api/import/sessions"
ORIGINS = ("personal", "student", "acquired", "gift", "study")
FRAMINGS = ("none", "passepartout", "frame")
UPLOAD_ATTEMPTS = 3  # попыток на файл при сбое сети или 5xx
CREATE_TIMEOUT = 3600  # секунд ожидания создания картин
# Ответы, после которых остальные файлы не грузятся, и код выхода.
STOP_UPLOADS = {401: EXIT_FORBIDDEN, 403: EXIT_FORBIDDEN, 409: EXIT_ERROR, 429: EXIT_RATE}
# Сеансы, которые можно продолжить тем же набором файлов.
RESUMABLE = ("uploading", "review")

# Служебные файлы — как в браузере (client/src/lib/folderFiles.ts, isJunk): скрытые (с точки)
# и служебные файлы ОС пропускаются молча на любом уровне пути.
JUNK_NAMES = re.compile(r"^(thumbs\.db|desktop\.ini)$", re.IGNORECASE)


def is_junk(name: str) -> bool:
    return name.startswith(".") or bool(JUNK_NAMES.match(name))


DIGITS = re.compile(r"(\d+)")


def _natural_key(text: str) -> list:
    """Естественный порядок, как natural_key сервера: числа — как числа, без регистра."""
    parts = DIGITS.split(text.casefold())
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts]


def scan(folder: Path) -> list[tuple[str, Path, int]]:
    """Файлы папки на всю глубину: (путь от папки через «/», файл, размер), естественный порядок."""
    found = []
    for top, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not is_junk(d)]
        for name in files:
            if is_junk(name):
                continue
            path = Path(top) / name
            if not path.is_file():
                continue
            rel = path.relative_to(folder).as_posix()
            found.append((rel, path, path.stat().st_size))
    return sorted(found, key=lambda item: _natural_key(item[0]))


# --- общие данные пачки -----------------------------------------------------------------------


def batch(args, ctx: Context) -> dict:
    """Общие данные пачки: флаги, иначе умолчания из настроек пользователя."""
    defaults = (ctx.get("/api/me").get("settings") or {}) if _needs_settings(args) else {}
    origin = args.origin or defaults.get("default_origin_category")
    if not origin:
        raise UsageError(
            f"Укажите --origin ({', '.join(ORIGINS)}): в настройках нет категории по умолчанию"
        )
    body: dict = {"origin_category": origin, "framing": args.framing, "location": args.location}
    for kind in ("author", "status", "technique", "material"):
        value = getattr(args, kind)
        body[f"{kind}_id"] = ctx.resolve(kind, value) if value else defaults.get(f"default_{kind}")
    body["tags"] = args.tag
    body["catalog_ids"] = [ctx.resolve_catalog(value) for value in args.catalog]
    body["new_catalog"] = args.new_catalog
    return body


# Флаги общих данных пачки и их значения по умолчанию.
BATCH_FLAGS = {
    "origin": None,
    "author": None,
    "status": None,
    "technique": None,
    "material": None,
    "framing": "none",
    "location": "",
    "tag": [],
    "catalog": [],
    "new_catalog": None,
}


def given_batch_flags(args) -> list[str]:
    """Флаги пачки, заданные явно (у продолжаемого сеанса они не применяются)."""
    return [
        "--" + name.replace("_", "-")
        for name, default in BATCH_FLAGS.items()
        if getattr(args, name) != default
    ]


def _needs_settings(args) -> bool:
    return not (args.origin and args.author and args.status and args.technique and args.material)


def batch_arguments(parser) -> None:
    parser.add_argument("--origin", choices=ORIGINS, help="категория происхождения")
    parser.add_argument("--author", help="автор: имя или номер")
    parser.add_argument("--status", help="статус: имя или номер")
    parser.add_argument("--technique", help="техника: имя или номер")
    parser.add_argument("--material", help="материал: имя или номер")
    parser.add_argument("--framing", choices=FRAMINGS, default="none")
    parser.add_argument("--location", default="", help="местонахождение")
    parser.add_argument("--tag", action="append", default=[], help="тег (можно несколько)")
    parser.add_argument(
        "--catalog", action="append", default=[], help="каталог: имя или номер (можно несколько)"
    )
    parser.add_argument("--new-catalog", help="создать каталог с этим именем для картин")


# --- разбор ------------------------------------------------------------------------------------


def _file_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "rel_path": row["rel_path"],
        "skipped": row.get("skipped", False),
        "exists_in": row.get("exists_in"),
    }


def review_out(ctx: Context, pk: int) -> dict:
    """Разбор сеанса со свежим жетоном для create."""
    review = ctx.get(f"{SESSIONS}/{pk}/review")
    groups = [
        {
            "id": g["id"],
            "name": g["name"],
            "main_file_id": g.get("main_file_id"),
            "skipped": g["skipped"],
            "matches_painting": g.get("matches_painting"),
            "files": [_file_out(f) for f in g.get("files", [])],
        }
        for g in review["groups"]
    ]
    return {
        "session": pk,
        "groups": groups,
        "singles": review.get("singles"),
        "duplicates": review.get("duplicates"),
        "warnings": review.get("warnings"),
        "preview_token": review["preview_token"],
    }


def _review(args, ctx: Context) -> None:
    emit(review_out(ctx, args.session), table=ctx.table)


# --- загрузка ----------------------------------------------------------------------------------


def _upload_one(ctx: Context, pk: int, file_id: int, path: Path) -> dict:
    """Загрузка файла сеанса; сеть и 5xx — повтор до UPLOAD_ATTEMPTS раз, 4xx — сразу."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle)}
                return ctx.send("POST", f"{SESSIONS}/{pk}/files/{file_id}", files=files)
        except (httpx.TransportError, ApiError) as exc:
            retryable = not isinstance(exc, ApiError) or exc.status >= 500
            if not retryable or attempt == UPLOAD_ATTEMPTS:
                raise
            err(f"{path.name}: попытка {attempt} не удалась ({exc}), повтор")
            time.sleep(attempt)
    raise AssertionError("недостижимо")  # pragma: no cover


def _find_session(ctx: Context, manifest: set[tuple[str, int]]) -> dict | None:
    """Незавершённый сеанс папки пользователя с тем же набором путей и размеров."""
    for item in ctx.get(SESSIONS)["items"]:
        if item["source"] != "folder" or item["state"] not in RESUMABLE:
            continue
        if item.get("counts", {}).get("files_total") != len(manifest):
            continue
        rows = ctx.get(f"{SESSIONS}/{item['id']}/files")["items"]
        if {(r["rel_path"], r["expected_size"]) for r in rows} == manifest:
            return item
    return None


def _upload_missing(ctx: Context, pk: int, paths: dict[str, Path]) -> tuple[int, list, int]:
    """Догружает ожидаемые файлы сеанса: (загружено, ошибки, код выхода при остановке)."""
    rows = ctx.get(f"{SESSIONS}/{pk}/files")["items"]
    pending = [r for r in rows if r["state"] == "expected"]
    done, failed = 0, []
    for index, row in enumerate(pending):
        rel = row["rel_path"]
        try:
            out = _upload_one(ctx, pk, row["id"], paths[rel])
        except ApiError as exc:
            failed.append({"file": rel, "error": exc.detail})
            if exc.status in STOP_UPLOADS:
                skipped = f"Не загружен: остановлено после ошибки ({exc.detail})"
                failed += [{"file": r["rel_path"], "error": skipped} for r in pending[index + 1 :]]
                return done, failed, STOP_UPLOADS[exc.status]
            continue
        except httpx.TransportError as exc:
            failed.append({"file": rel, "error": f"Сеть: {exc}"})
            continue
        done += 1
        state = "отклонён: " + out.get("reason", "") if out.get("state") == "rejected" else "ок"
        err(f"[{index + 1}/{len(pending)}] {rel}: {state}")
    return done, failed, EXIT_ERROR if failed else 0


def _folder(args, ctx: Context) -> int:
    folder = Path(args.path)
    if not folder.is_dir():
        raise UsageError(f"Папка не найдена: {args.path}")
    files = scan(folder)
    if not files:
        raise CommandError("В папке нет файлов для импорта", "empty_folder")
    manifest = {(rel, size) for rel, _, size in files}
    found = _find_session(ctx, manifest)
    resumed = found is not None
    ignored = given_batch_flags(args) if resumed else []
    if not resumed:
        body = {
            "files": [{"rel_path": rel, "size": size} for rel, _, size in files],
            "batch": batch(args, ctx),
            "source": "folder",
        }
        found = ctx.send("POST", SESSIONS, json=body)
        err(f"Сеанс импорта {found['id']}: файлов {len(files)}")
    else:
        err(f"Продолжаю сеанс импорта {found['id']} (общие данные — из сеанса)")
        if ignored:
            err(f"Не применены к продолжаемому сеансу: {', '.join(ignored)}")
    pk = found["id"]
    if found["state"] == "uploading":
        uploaded, failed, code = _upload_missing(ctx, pk, {rel: path for rel, path, _ in files})
        if failed:
            raise CommandError(
                f"Загружены не все файлы ({len(failed)} с ошибкой) — повторите import folder, "
                "сеанс продолжится",
                "upload_incomplete",
                code,
                session=pk,
                uploaded=uploaded,
                failed=failed,
            )
        ctx.send("POST", f"{SESSIONS}/{pk}/review")
    out = {**review_out(ctx, pk), "resumed": resumed}
    if ignored:
        out["ignored_flags"] = ignored
    emit(out, table=ctx.table)
    return 0


# --- перегруппировка ----------------------------------------------------------------------------


def _target(value: str) -> int | None:
    if value == "new":
        return None
    if value.isdigit():
        return int(value)
    raise UsageError(f"--to: номер группы или new, получено «{value}»")


def _regroup(args, ctx: Context) -> None:
    body = {"file_ids": args.move, "group_id": _target(args.to)}
    ctx.send("POST", f"{SESSIONS}/{args.session}/files/move", json=body)
    emit(review_out(ctx, args.session), table=ctx.table)


def _patch_group(ctx: Context, args, body: dict) -> None:
    ctx.send("PATCH", f"{SESSIONS}/{args.session}/groups/{args.group}", json=body)
    emit(review_out(ctx, args.session), table=ctx.table)


def _rename(args, ctx: Context) -> None:
    _patch_group(ctx, args, {"name": args.name})


def _skip(args, ctx: Context) -> None:
    _patch_group(ctx, args, {"skipped": not args.undo})


# --- создание и отказ ---------------------------------------------------------------------------


def _created(current: dict) -> bool:
    if current["state"] == "done":
        return True
    if current.get("error"):  # сбой задания: сеанс остаётся в «создании», повтор — create
        raise CommandError(
            f"Создание картин прервано: {current['error']} — повторите create",
            "import_failed",
            session=current["id"],
            state=current["state"],
        )
    return False


def _create_preview(args, ctx: Context) -> None:
    emit(review_out(ctx, args.session), table=ctx.table)


def create(args, ctx: Context, token: str) -> None:
    ctx.send("POST", f"{SESSIONS}/{args.session}/create", json={"preview_token": token})
    err(f"Сеанс {args.session}: создание картин запущено")
    done = poll_session(
        ctx,
        args.session,
        _created,
        timeout=args.timeout,
        waiting="Создание ещё идёт — проверьте позже: import sessions",
    )
    emit(
        {"session": done["id"], "state": done["state"], "result": done.get("result")},
        table=ctx.table,
    )


def _discard_preview(args, ctx: Context) -> None:
    current = ctx.get(f"{SESSIONS}/{args.session}")
    keys = ("id", "source", "state", "counts", "preview_token")
    emit({key: current.get(key) for key in keys}, table=ctx.table)


def _discard(args, ctx: Context, token: str) -> None:
    path = f"{SESSIONS}/{args.session}/discard"
    emit(ctx.send("POST", path, json={"preview_token": token}), table=ctx.table)


def _sessions(args, ctx: Context) -> None:
    keys = ("id", "source", "state", "counts", "created_at", "updated_at", "error")
    items = [{key: item.get(key) for key in keys} for item in ctx.get(SESSIONS)["items"]]
    emit(items, table=ctx.table)


def create_command(subparsers, parents: list, help: str) -> None:
    parser = command(subparsers, "create", help, parents)
    parser.add_argument("session", type=int, help="номер сеанса")
    timeout_argument(parser, CREATE_TIMEOUT, "создания картин")
    two_step(parser, preview=_create_preview, apply=create)


def register(subparsers, parents: list) -> None:
    imports = group(subparsers, "import", "Импорт из папки: загрузка, разбор, создание картин")

    folder = command(
        imports,
        "folder",
        "Загрузить папку и показать разбор с жетоном (картины не создаются)",
        parents,
    )
    folder.add_argument("path", help="папка; подпапки — отдельные картины")
    batch_arguments(folder)
    folder.set_defaults(handler=_folder, mutates=lambda a: True)

    review = command(imports, "review", "Разбор сеанса со свежим жетоном", parents)
    review.add_argument("session", type=int)
    review.set_defaults(handler=_review, mutates=None)

    regroup = command(imports, "regroup", "Перенести файлы в другую или новую группу", parents)
    regroup.add_argument("session", type=int)
    regroup.add_argument("--move", type=ids, required=True, help="номера файлов через запятую")
    regroup.add_argument("--to", required=True, help="номер группы или new")
    regroup.set_defaults(handler=_regroup, mutates=lambda a: True)

    rename = command(imports, "rename", "Переименовать группу (название картины)", parents)
    rename.add_argument("session", type=int)
    rename.add_argument("group", type=int)
    rename.add_argument("name")
    rename.set_defaults(handler=_rename, mutates=lambda a: True)

    skip = command(imports, "skip", "Пропустить группу (--undo — вернуть)", parents)
    skip.add_argument("session", type=int)
    skip.add_argument("group", type=int)
    skip.add_argument("--undo", action="store_true")
    skip.set_defaults(handler=_skip, mutates=lambda a: True)

    create_command(imports, parents, "Создать картины по жетону разбора (ждёт окончания)")

    sessions = command(imports, "sessions", "Незавершённые и завершённые сеансы", parents)
    sessions.set_defaults(handler=_sessions, mutates=None)

    discard = command(imports, "discard", "Отказаться от сеанса (в два шага)", parents)
    discard.add_argument("session", type=int)
    two_step(discard, preview=_discard_preview, apply=_discard)
