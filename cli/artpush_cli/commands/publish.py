"""Публикации: аккаунты площадок, профили назначений, готовность, предпросмотр со снимком,
запуск только после подтверждения человека, пакет, отметки размещения, журнал, метки по архиву.
Секреты и боты — в срезе 11."""

import contextlib
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from artpush_cli import client as client_mod
from artpush_cli.client import ApiError
from artpush_cli.commands import (
    POLL_SECONDS,
    CommandError,
    Context,
    UsageError,
    command,
    confirm,
    group,
    ids,
    timeout_argument,
)
from artpush_cli.output import emit, err

BASE = "/api/publishing"
PLATFORMS = {"telegram": "Telegram", "vk": "VK", "instagram": "Instagram"}
ALIASES = {"tg": "telegram", "ig": "instagram", "insta": "instagram", "вк": "vk"}
COUNT = {2: "два", 3: "три", 4: "четыре"}
SOURCES = ("initial_import", "published", "manual_package", "manual")
PREVIEW_TIMEOUT = 120
SNAPSHOT_TTL = 14 * 86400  # секунд: старше — удаляются при записи нового снимка
STALE_TEXT = "Карточка изменилась — повторите publish preview"
READ_KEY_HINT = (
    "Ключу только для чтения доступны publish readiness, publish candidates, publish package "
    "и publish log; предпросмотр готовит файлы, а выгрузка и отметки — правка: нужен ключ "
    "с пределом write"
)
NOT_PREPARED_HINT = (
    "Файлы под цель ещё не готовили: publish preview <картина> --target … ключом с пределом "
    "write, дождитесь подготовки и повторите publish package"
)
FORCE_HINT = (
    "Повторная выгрузка в тот же аккаунт — та же команда с --force, только после «да» человека"
)
ACCOUNT_COLUMNS = ["id", "platform", "label", "title", "language", "targets", "is_test"]


def _platform(value: str) -> str:
    key = value.strip().casefold()
    key = ALIASES.get(key, key)
    if key not in PLATFORMS:
        raise UsageError(f"Площадка «{value}»: одна из {', '.join(PLATFORMS)} (или tg, ig)")
    return key


def _scope_hint(exc: ApiError) -> None:
    if exc.status == 403 and exc.code == "scope_denied":
        exc.extra["hint"] = READ_KEY_HINT


# --- Аккаунты и цели ---


def _accounts(ctx: Context, archived: bool = False) -> list[dict]:
    params = {"archived": 1} if archived else None
    return ctx.get(f"{BASE}/accounts", params=params)["items"]


def _where(account: dict) -> str:
    """«Telegram ru», если аккаунтов площадки несколько, иначе «Telegram» (как в браузере)."""
    title = PLATFORMS.get(account["platform"], account["platform"])
    return f"{title} {account['label']}" if account.get("show_label") else title


def _pick_target(account: dict, wanted: str | None) -> str:
    targets = account.get("targets") or []
    if wanted:
        code = wanted if "." in wanted else f"{account['platform']}.{wanted}"
        if code not in targets:
            raise CommandError(
                f"У аккаунта {account['id']} нет назначения «{wanted}»",
                "target_not_found",
                choices=targets,
            )
        return code
    if len(targets) == 1:
        return targets[0]
    if not targets:
        raise CommandError(f"У аккаунта {account['id']} нет назначений", "target_not_found")
    raise CommandError(
        f"У аккаунта {account['id']} несколько назначений — укажите: {', '.join(targets)}",
        "target_ambiguous",
        choices=targets,
    )


def resolve_target(accounts: list[dict], spec: str) -> tuple[dict, str]:
    """Цель «аккаунт[:назначение]»: номер аккаунта (`12`, `12:telegram.post`) или площадка с
    меткой (`tg:ru`, `instagram:en`, `tg:ru:post`). Площадка без метки — если аккаунт на ней
    один, иначе target_ambiguous со списком меток. Архивные аккаунты не считаются."""
    parts = [part.strip() for part in spec.split(":")]
    head = parts[0]
    if head.isdigit():
        found = next((a for a in accounts if a["id"] == int(head)), None)
        if found is None:
            raise CommandError(
                f"Аккаунт {head} не найден или в архиве",
                "target_not_found",
                choices=[f"{a['id']} ({_where(a)})" for a in accounts],
            )
        return found, _pick_target(found, parts[1] if len(parts) > 1 else None)
    platform = _platform(head)
    title = PLATFORMS[platform]
    same = [a for a in accounts if a["platform"] == platform]
    labels = [a["label"] for a in same]
    if not same:
        raise CommandError(
            f"Аккаунтов {title} нет — заведите: publish account create", "target_not_found"
        )
    label = parts[1] if len(parts) > 1 else ""
    if label:
        found = next((a for a in same if a["label"].casefold() == label.casefold()), None)
        if found is None:
            raise CommandError(
                f"У {title} нет аккаунта с меткой «{label}»: {', '.join(labels)}",
                "target_not_found",
                choices=labels,
            )
    elif len(same) == 1:
        found = same[0]
    else:
        n = len(same)
        word = f"{COUNT.get(n, n)} аккаунт{'а' if n < 5 else 'ов'}"
        raise CommandError(
            f"У {title} {word}: {', '.join(labels)} — укажите метку",
            "target_ambiguous",
            choices=labels,
        )
    return found, _pick_target(found, parts[2] if len(parts) > 2 else None)


def _targets(ctx: Context, specs: list[str]) -> list[tuple[dict, str]]:
    if not specs:
        raise UsageError("Укажите цель: --target tg:ru, instagram или номер аккаунта")
    accounts = _accounts(ctx)
    return [resolve_target(accounts, spec) for spec in specs]


def _one_target(ctx: Context, spec: str) -> tuple[dict, str]:
    return _targets(ctx, [spec])[0]


def _account(ctx: Context, pk: int) -> dict:
    found = next((a for a in _accounts(ctx, archived=True) if a["id"] == pk), None)
    if found is None:
        raise CommandError(f"Аккаунт {pk} не найден", "not_found")
    return found


def _list_accounts(args, ctx: Context) -> None:
    items = _accounts(ctx, archived=args.archived)
    if ctx.table:
        emit(items, table=True, columns=[*ACCOUNT_COLUMNS, "archived_at"])
    else:
        emit({"items": items})


def _show(args, ctx: Context) -> None:
    emit(_account(ctx, args.id), table=ctx.table)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"Не прочитать файл {path}: {exc.strerror or exc}") from None


def _create(args, ctx: Context) -> None:
    platform = _platform(args.platform)
    label = args.label
    if not label:
        params = {"platform": platform, "language": args.language, "title": args.title}
        label = ctx.get(f"{BASE}/accounts/label-suggest", params=params)["label"]
    body: dict = {
        "platform": platform,
        "title": args.title,
        "label": label,
        "language": args.language,
        "currency": args.currency,
    }
    if args.target:
        body["targets"] = args.target
    if args.address:
        body["url"] = args.address
    if args.test:
        body["is_test"] = True
    emit(ctx.send("POST", f"{BASE}/accounts", json=body), table=ctx.table)


def _set(args, ctx: Context) -> None:
    body: dict = {}
    if args.title is not None:
        body["title"] = args.title
    if args.label is not None:
        body["label"] = args.label
    if args.template_file:
        templates = dict(_account(ctx, args.id).get("templates") or {})
        for target, path in args.template_file:
            templates[target] = _read_text(path)
        body["templates"] = templates
    if args.pace_minutes is not None:
        body["pace_minutes"] = args.pace_minutes
    if args.pace_daily is not None:
        body["pace_daily"] = args.pace_daily
    if args.test is not None:
        body["is_test"] = args.test
    if not body:
        raise UsageError(
            "Нечего менять: --title, --label, --template-file, --pace-minutes, --pace-daily, "
            "--test или --no-test"
        )
    emit(ctx.send("PATCH", f"{BASE}/accounts/{args.id}", json=body), table=ctx.table)


def _archive(action: str):
    def handler(args, ctx: Context) -> None:
        emit(ctx.send("POST", f"{BASE}/accounts/{args.id}/{action}"), table=ctx.table)

    return handler


def _delete(args, ctx: Context) -> None:
    confirm(
        f"Удалить аккаунт {args.id} навсегда? Аккаунт с публикациями удалить нельзя — "
        "только убрать в архив.",
        args.yes,
    )
    emit(ctx.send("DELETE", f"{BASE}/accounts/{args.id}"), table=ctx.table)


# --- Профили и метки ---


def _profiles(args, ctx: Context) -> None:
    items = ctx.get(f"{BASE}/profiles")
    if ctx.table:
        emit(items, table=True, columns=["target", "title", "media_label", "overrides"])
    else:
        emit(items)


def _impact_text(impact: dict) -> str:
    def names(rows):
        return ", ".join(f"№ {r['id']} «{r['title']}»" for r in rows[:10]) + (
            f" и ещё {len(rows) - 10}" if len(rows) > 10 else ""
        )

    ready, lost = impact.get("became_ready", []), impact.get("became_not_ready", [])
    lines = [
        f"Станут готовыми: {len(ready)}" + (f" — {names(ready)}" if ready else ""),
        f"Перестанут быть готовыми: {len(lost)}" + (f" — {names(lost)}" if lost else ""),
    ]
    return "\n".join(lines)


def _profile_change(ctx: Context, target: str, body: dict, yes: bool, question: str) -> dict:
    """Сначала dry_run — что станет готовым и неготовым, затем подтверждение."""
    path = f"{BASE}/profiles/{target}"
    impact = ctx.send("PUT", path, json={**body, "dry_run": True})
    err(_impact_text(impact))
    confirm(question, yes, impact=impact)
    return impact


def _profile_set(args, ctx: Context) -> None:
    try:
        data = json.loads(_read_text(args.json))
    except ValueError as exc:
        raise UsageError(f"{args.json}: не JSON ({exc})") from None
    if not isinstance(data, dict):
        raise UsageError(f"{args.json}: ожидается объект JSON")
    if "overrides" in data:
        body = {"overrides": data["overrides"]}
        if "media_label" in data:
            body["media_label"] = data["media_label"]
    else:
        body = {"overrides": data}
    _profile_change(ctx, args.target, body, args.yes, f"Изменить профиль {args.target}?")
    path = f"{BASE}/profiles/{args.target}"
    emit(ctx.send("PUT", path, json={**body, "dry_run": False}), table=ctx.table)


def _profile_reset(args, ctx: Context) -> None:
    body = {"overrides": {}, "media_label": ""}
    question = f"Вернуть умолчания профиля {args.target}?"
    _profile_change(ctx, args.target, body, args.yes, question)
    emit(ctx.send("POST", f"{BASE}/profiles/{args.target}/reset"), table=ctx.table)


def _labels_apply(args, ctx: Context) -> None:
    spec = args.scope
    if "." in spec and not spec.split(":")[0].isdigit():
        scope: dict = {"target": spec}
        where = f"назначения {spec}"
    else:
        parts = spec.split(":")
        if len(parts) > (1 if parts[0].strip().isdigit() else 2):
            raise UsageError(
                f"«{spec}»: метки проставляются на аккаунт целиком или на назначение — "
                "укажите аккаунт (12, tg:ru) или назначение (vk.wall)"
            )
        found, _ = _one_target(ctx, spec)
        scope = {"account": found["id"]}
        where = f"аккаунта {_where(found)}"
    dry = ctx.send("POST", f"{BASE}/labels/apply", json={**scope, "dry_run": True})
    skipped = dry.get("skipped_removed", 0)
    note = f"; снятых человеком не трогаю: {skipped}" if skipped else ""
    err(f"Метка «{dry['label']}» {where} добавится файлам: {dry['added']}{note}")
    if not dry["added"]:
        emit(dry, table=ctx.table)
        return
    confirm(f"Проставить метку «{dry['label']}» {dry['added']} файлам?", args.yes, plan=dry)
    done = ctx.send("POST", f"{BASE}/labels/apply", json={**scope, "dry_run": False})
    emit(done, table=ctx.table)


# --- Готовность, кандидаты, журнал ---


INFO_CODES = {"placed_other"}  # справка; остальные неблокирующие причины — предупреждения
METHOD_CODES = {"package_only"}  # способ выгрузки — строкой «способ: …» под картиной
PLACEMENT_WORDS = {
    "placed": "размещена",
    "outdated": "размещена, устарела",
    "waiting": "ждёт размещения",
    "failed": "ошибка",
}


def _spec(ref: dict) -> str:
    """Цель для --target: площадка, а при нескольких аккаунтах — площадка:метка."""
    return f"{ref['platform']}:{ref['label']}" if ref.get("label") else ref["platform"]


def _readiness_lines(out: dict) -> list[str]:
    """Вид как на экране 9: цель, готова ли, причины, предупреждения и справки, способ."""
    lines: list[str] = []
    for item in out["items"]:
        painting = item["painting"]
        lines.append(f"№ {painting['id']} «{painting['title']}»")
        width = max((len(a["where"]) for a in item["accounts"]), default=0)
        methods = []
        for row in item["accounts"]:
            notes = []
            if row.get("placement"):
                notes.append(
                    PLACEMENT_WORDS.get(row["placement"]["state"], row["placement"]["state"])
                )
            for reason in row["reasons"]:
                if reason["code"] in METHOD_CODES:
                    continue
                kind = (
                    "причина"
                    if reason["blocking"]
                    else "справка"
                    if reason["code"] in INFO_CODES
                    else "предупреждение"
                )
                link = f" — {reason['url']}" if reason.get("url") else ""
                notes.append(f"{kind}: {reason['text']}{link}")
            state = "готова" if row["ready"] else "не готова"
            lines.append(f"  {row['where'].ljust(width)}   {state:<9}  {'; '.join(notes)}".rstrip())
            if row.get("method_text") and row["method_text"] not in methods:
                methods.append(row["method_text"])
        lines += [f"способ: {m}" for m in methods]
    return lines


def _readiness(args, ctx: Context) -> None:
    if args.catalog is not None:
        if args.paintings:
            raise UsageError("Укажите картины или --catalog, не оба")
        accounts = [a["id"] for a, _ in _targets(ctx, args.target)] if args.target else []
        body = {"catalog": ctx.resolve_catalog(args.catalog), "accounts": accounts}
        emit(ctx.send("POST", f"{BASE}/readiness/catalog", json=body), table=ctx.table)
        return
    if not args.paintings:
        raise UsageError("Укажите номера картин или --catalog")
    accounts = [a["id"] for a, _ in _targets(ctx, args.target)] if args.target else []
    body = {"paintings": args.paintings, "accounts": accounts}
    out = ctx.send("POST", f"{BASE}/readiness/paintings", json=body)
    for item in out["items"]:
        for row in item["accounts"]:
            row["where"] = _spec(row["account"])
    if ctx.table:
        print("\n".join(_readiness_lines(out)))
    else:
        emit(out)


def _candidates(args, ctx: Context) -> None:
    params: dict = {"not_ready": 1 if args.not_ready else 0}
    if args.catalog is not None:
        params["catalog"] = ctx.resolve_catalog(args.catalog)
    if args.q:
        params["q"] = args.q
    out = ctx.get(f"{BASE}/candidates", params=params)
    out.pop("others", None)  # второй список нужен вкладке браузера, агенту — только выбранный
    if ctx.table:
        rows = [
            {
                "id": item["painting"]["id"],
                "title": item["painting"]["title"],
                "targets": ", ".join(
                    _badge_where(t) + ("" if t["ready"] else f" — {t['reason']}")
                    for t in item["targets"]
                ),
                "pending": ", ".join(item.get("pending", [])),
            }
            for item in out["items"]
        ]
        emit(rows, table=True)
    else:
        emit(out)


def _badge_where(row: dict) -> str:
    return f"{PLATFORMS.get(row['platform'], row['platform'])} {row['label']}".strip()


def _log(args, ctx: Context) -> None:
    params: dict = {"page": args.page, "page_size": args.page_size}
    if args.painting is not None:
        params["painting"] = args.painting
    if args.q:
        params["q"] = args.q
    if args.platform:
        key = args.platform.strip().casefold()
        params["platform"] = key if key == "artnow" else _platform(key)
    if args.account:
        params["account"] = _one_target(ctx, args.account)[0]["id"]
    for name in ("state", "source"):
        if getattr(args, name):
            params[name] = getattr(args, name)
    if args.since:
        params["date_from"] = args.since
    if args.until:
        params["date_to"] = args.until
    out = ctx.get(f"{BASE}/log", params=params)
    if ctx.table:
        rows = [
            {
                "at": row["at"],
                "painting": row["painting"]["id"],
                "where": _badge_where(row),
                "source": row["source"],
                "state": row["state"],
                "url": row["url"],
                "error": row.get("error", ""),
            }
            for row in out["items"]
        ]
        emit({"items": rows, "total": out["total"]}, table=True)
    else:
        emit(out)


# --- Предпросмотр, снимок, запуск ---


def snapshots_dir() -> Path:
    return client_mod.CACHE_PATH.parent / "snapshots"


def _target_body(args, account: dict, target: str) -> dict:
    body: dict = {"account": account["id"], "target": target}
    if getattr(args, "media", None):
        body["media"] = args.media
    if getattr(args, "caption_file", None):
        body["caption"] = _read_text(args.caption_file)
    if getattr(args, "alt", None) is not None:
        body["alt"] = args.alt
    return body


def _number(items: list[dict]) -> str:
    """Номер снимка: 8 знаков хэша предпросмотра цели; у нескольких целей — хэш их хэшей."""
    if len(items) == 1:
        return items[0]["snapshot"][:8]
    joined = "|".join(item["snapshot"] for item in items)
    return hashlib.sha256(joined.encode()).hexdigest()[:8]


def _summary(item: dict, account: dict) -> dict:
    files = [t for t in item["tiles"] if t.get("state") != "skipped"]
    blocking = [r["text"] for r in item["readiness"] if r.get("blocking")]
    return {
        "where": _where(account),
        "target": item["target"],
        "files": len(files),
        "caption_len": item["texts"]["caption_len"],
        "caption_max": item["texts"]["caption_max"],
        "price": item.get("price", ""),
        "blocking": blocking,
    }


def _prune(folder: Path) -> None:
    """Удаляет снимки старше SNAPSHOT_TTL: запускать по ним всё равно поздно."""
    edge = time.time() - SNAPSHOT_TTL
    for path in folder.glob("*.json"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < edge:
                path.unlink()


def _preview(args, ctx: Context) -> None:
    targets = _targets(ctx, args.target)
    body = {
        "painting": args.painting,
        "targets": [_target_body(args, account, target) for account, target in targets],
        "enqueue": True,
    }
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            out = ctx.send("POST", f"{BASE}/preview", json=body)
        except ApiError as exc:
            _scope_hint(exc)
            raise
        if not any(item["preparing"] for item in out["items"]):
            break
        if time.monotonic() >= deadline:
            raise CommandError(
                "Файлы ещё готовятся — повторите publish preview позже", "timeout", preview=out
            )
        time.sleep(POLL_SECONDS)
    number = _number(out["items"])
    summary = [
        _summary(item, account) for item, (account, _) in zip(out["items"], targets, strict=True)
    ]
    cached = {
        "url": ctx.client().url,
        "painting": args.painting,
        "title": out["painting"]["title"],
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "targets": [
            {**target, "snapshot": item["snapshot"]}
            for target, item in zip(body["targets"], out["items"], strict=True)
        ],
        "summary": summary,
    }
    folder = snapshots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _prune(folder)
    (folder / f"{number}.json").write_text(
        json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if any(s["blocking"] for s in summary):
        err("Есть блокирующие причины — запуск будет отклонён, пока их не исправить")
    err(f"Номер снимка: {number}. Выгрузка — после «да» человека: artpush publish run {number}")
    if ctx.table:
        emit([{**s, "snapshot": number} for s in summary], table=True)
    else:
        emit({**out, "snapshot": number})


def _load_snapshot(ctx: Context, number: str) -> dict:
    path = snapshots_dir() / f"{Path(number).name}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if not isinstance(cached, dict) or cached.get("url") != ctx.client().url:
        raise CommandError(
            f"Снимок {number} не найден — сначала publish preview", "snapshot_not_found"
        )
    return cached


def _plan_text(cached: dict) -> str:
    lines = [f"Картина № {cached['painting']} «{cached.get('title', '')}»:"]
    for s in cached["summary"]:
        lines.append(
            f"  → {s['where']} ({s['target']}): файлов {s['files']}, "
            f"подпись {s['caption_len']} из {s.get('caption_max', '?')} знаков"
            + (f", цена {s['price']}" if s.get("price") else "")
        )
    return "\n".join(lines)


def _run(args, ctx: Context) -> None:
    cached = _load_snapshot(ctx, args.snapshot)
    err(_plan_text(cached))
    confirm("Выгрузить?", args.yes, snapshot=args.snapshot)
    body = {
        "painting": cached["painting"],
        "targets": [{**t, "force": args.force} for t in cached["targets"]],
    }
    try:
        out = ctx.send("POST", f"{BASE}/runs", json=body)
    except ApiError as exc:
        _scope_hint(exc)
        if exc.code == "snapshot_stale":
            exc.detail = STALE_TEXT
        elif exc.code == "already_placed" and not args.force:
            exc.extra["hint"] = FORCE_HINT
        raise
    pubs = [p["id"] for p in out.get("publications", []) if p.get("state") == "package_ready"]
    if pubs:
        err(
            "Пакеты готовы: publish package --publication <номер>, после размещения — "
            f"publish placed <номер> --url …; номера: {', '.join(map(str, pubs))}"
        )
    emit(out, table=ctx.table)


# --- Пакет и отметки ---


def _filename(content_disposition: str) -> str:
    """Имя файла из Content-Disposition: filename* (RFC 5987) или filename; без каталогов."""
    name = ""
    extended = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", content_disposition, re.I)
    if extended:
        try:
            name = unquote(extended.group(2).strip(), encoding=extended.group(1) or "utf-8")
        except LookupError:  # неизвестная кодировка — как utf-8
            name = unquote(extended.group(2).strip())
    else:
        plain = re.search(r'filename\s*=\s*(?:"([^"]*)"|([^;]+))', content_disposition, re.I)
        if plain:
            name = (plain.group(1) or plain.group(2) or "").strip()
    name = _UNSAFE.sub("_", Path(name.replace("\\", "/")).name)
    if name in ("", ".", ".."):
        return "package.zip"
    return _fit(name)


# Управляющие знаки и знаки направления текста в имени файла не нужны (переставляют части имени).
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
NAME_MAX_BYTES = 200


def _fit(name: str) -> str:
    """Имя не длиннее 200 байт UTF-8 (предел файловых систем — 255): основа укорачивается."""
    if len(name.encode()) <= NAME_MAX_BYTES:
        return name
    path = Path(name)
    suffix = path.suffix if len(path.suffix.encode()) < 16 else ""
    room = NAME_MAX_BYTES - len(suffix.encode())
    stem = name[: len(name) - len(suffix)] if suffix else name
    return stem.encode()[:room].decode(errors="ignore") + suffix


def _free(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
    return candidate


def _package_requests(args, ctx: Context) -> list[tuple[str, str, dict | None]]:
    """Запросы пакетов: (метод, путь, тело) — по публикации, по снимку или по целям."""
    if args.publication is not None:
        if args.painting is not None or args.target or args.snapshot:
            raise UsageError("--publication — без номера картины, --target и --snapshot")
        return [("GET", f"{BASE}/publications/{args.publication}/package", None)]
    if args.snapshot:
        if args.media or args.caption_file or args.alt is not None:
            raise UsageError("--snapshot — свои файлы и тексты уже в снимке")
        cached = _load_snapshot(ctx, args.snapshot)
        if args.painting is not None and args.painting != cached["painting"]:
            raise UsageError(f"Снимок {args.snapshot} — картины {cached['painting']}")
        targets = [{k: v for k, v in t.items() if k != "snapshot"} for t in cached["targets"]]
        if args.target:  # только эти цели снимка, в порядке --target
            chosen = []
            for spec, (account, target) in zip(
                args.target, _targets(ctx, args.target), strict=True
            ):
                found = next(
                    (t for t in targets if (t["account"], t["target"]) == (account["id"], target)),
                    None,
                )
                if found is None:
                    raise CommandError(
                        f"В снимке {args.snapshot} нет цели «{spec}»",
                        "target_not_found",
                        choices=[f"{t['account']}:{t['target']}" for t in targets],
                    )
                chosen.append(found)
            targets = chosen
        painting = cached["painting"]
    else:
        if args.painting is None or not args.target:
            raise UsageError(
                "Укажите картину и --target, --snapshot <номер> или --publication <номер>"
            )
        painting = args.painting
        targets = [_target_body(args, a, t) for a, t in _targets(ctx, args.target)]
    return [("POST", f"{BASE}/package", {"painting": painting, "target": t}) for t in targets]


def _package(args, ctx: Context) -> None:
    requests = _package_requests(args, ctx)
    if args.output and len(requests) > 1:
        raise UsageError("-o — для одного пакета; для нескольких — --to <папка>")
    saved = []
    for method, path, body in requests:
        try:
            response = ctx.client().request(method, path, json=body)
        except ApiError as exc:
            _scope_hint(exc)
            if exc.code == "not_prepared":
                exc.extra["hint"] = NOT_PREPARED_HINT
            raise
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            folder = Path(args.to)
            folder.mkdir(parents=True, exist_ok=True)
            name = _filename(response.headers.get("content-disposition", ""))
            target = _free(folder / name)
        target.write_bytes(response.content)
        saved.append({"path": str(target), "bytes": len(response.content)})
    emit({"saved": saved}, table=ctx.table)


def _mark(args, ctx: Context) -> None:
    account, target = _one_target(ctx, args.target)
    body: dict = {"account": account["id"], "target": target, "url": args.link}
    if args.date:
        body["date"] = args.date
    try:
        out = ctx.send("POST", f"/api/paintings/{args.painting}/placements", json=body)
    except ApiError as exc:
        _scope_hint(exc)
        raise
    emit(out, table=ctx.table)


def _placed(args, ctx: Context) -> None:
    path = f"{BASE}/publications/{args.publication}/placed"
    try:
        out = ctx.send("POST", path, json={"url": args.link})
    except ApiError as exc:
        _scope_hint(exc)
        raise
    emit(out, table=ctx.table)


# --- Разбор ---


def _yes(parser) -> None:
    parser.add_argument(
        "--yes", action="store_true", help="подтверждение уже получено от человека в диалоге"
    )


def _with_link(pub, name: str, help: str, parents: list):
    """Подкоманда с --url — ссылкой на пост. Общий --url (адрес системы) здесь только до
    команды (artpush --url … publish mark …): общие флаги берутся из parents без него."""
    parser = pub.add_parser(name, help=help, description=help)
    for parent in parents:
        for action in parent._actions:
            if "--url" not in action.option_strings and "-h" not in action.option_strings:
                parser._add_action(action)
    parser.add_argument("--url", dest="link", required=True, help="ссылка на пост")
    return parser


def _target_arg(parser, *, many: bool, required: bool = True) -> None:
    help_ = "цель: tg:ru, instagram:en, номер аккаунта; назначение — tg:ru:post или 12:vk.wall"
    if many:
        parser.add_argument(
            "--target", action="append", default=[], help=f"{help_}; можно несколько"
        )
    else:
        parser.add_argument("--target", required=required, help=help_)


def _overrides(parser) -> None:
    parser.add_argument("--media", type=ids, help="свои файлы и порядок: номера медиа, 3,1,2")
    parser.add_argument("--caption-file", help="своя подпись из файла (UTF-8)")
    parser.add_argument("--alt", help="свой alt-текст")


def _register_accounts(pub, parents: list) -> None:
    accounts = command(pub, "accounts", "Аккаунты площадок", parents)
    accounts.add_argument("--archived", action="store_true", help="вместе с архивными")
    accounts.set_defaults(handler=_list_accounts, mutates=None)

    one = pub.add_parser("account", help="Один аккаунт площадки", description="Один аккаунт")
    actions = one.add_subparsers(dest="account_action", metavar="действие")

    show = command(actions, "show", "Карточка аккаунта", parents)
    show.add_argument("id", type=int)
    show.set_defaults(handler=_show, mutates=None)

    create = command(actions, "create", "Завести аккаунт площадки", parents)
    create.add_argument("--platform", required=True, help="telegram, vk, instagram (tg, ig)")
    create.add_argument("--title", required=True, help="название аккаунта")
    create.add_argument("--language", required=True, help="код языка подписи, например ru")
    create.add_argument("--currency", required=True, help="код валюты цены, например RUB")
    create.add_argument("--label", help="метка до 3 знаков (без флага — предложит система)")
    create.add_argument(
        "--target", action="append", default=[], help="назначение, например telegram.post"
    )
    create.add_argument("--address", help="адрес канала или страницы")
    create.add_argument("--test", action="store_true", help="тестовый (только администратор)")
    create.set_defaults(handler=_create, mutates=lambda a: True)

    set_ = command(actions, "set", "Изменить аккаунт", parents)
    set_.add_argument("id", type=int)
    set_.add_argument("--title")
    set_.add_argument("--label", help="метка до 3 знаков")
    set_.add_argument(
        "--template-file",
        nargs=2,
        action="append",
        metavar=("НАЗНАЧЕНИЕ", "ФАЙЛ"),
        help="шаблон подписи назначения из файла (UTF-8)",
    )
    set_.add_argument("--pace-minutes", type=int, help="минут между публикациями")
    set_.add_argument("--pace-daily", type=int, help="публикаций в сутки")
    test = set_.add_mutually_exclusive_group()
    test.add_argument(
        "--test", dest="test", action="store_const", const=True, help="пометить тестовым (админ)"
    )
    test.add_argument("--no-test", dest="test", action="store_const", const=False)
    set_.set_defaults(handler=_set, mutates=lambda a: True, test=None)

    for action, what in (("archive", "Убрать в архив"), ("unarchive", "Вернуть из архива")):
        parser = command(actions, action, what, parents)
        parser.add_argument("id", type=int)
        parser.set_defaults(handler=_archive(action), mutates=lambda a: True)

    delete = command(actions, "delete", "Удалить аккаунт (с подтверждением)", parents)
    delete.add_argument("id", type=int)
    _yes(delete)
    delete.set_defaults(handler=_delete, mutates=lambda a: True)


def _register_profiles(pub, parents: list) -> None:
    profiles = command(pub, "profiles", "Профили назначений: встроенные и свои значения", parents)
    profiles.set_defaults(handler=_profiles, mutates=None)

    one = pub.add_parser("profile", help="Изменить профиль назначения", description="Профиль")
    actions = one.add_subparsers(dest="profile_action", metavar="действие")
    set_ = command(actions, "set", "Свои значения профиля: сначала влияние на готовность", parents)
    set_.add_argument("target", help="назначение, например telegram.post")
    set_.add_argument(
        "--json", required=True, help="файл JSON: переопределения или {overrides, media_label}"
    )
    _yes(set_)
    set_.set_defaults(handler=_profile_set, mutates=lambda a: True)

    reset = command(actions, "reset", "Вернуть умолчания профиля", parents)
    reset.add_argument("target", help="назначение, например telegram.post")
    _yes(reset)
    reset.set_defaults(handler=_profile_reset, mutates=lambda a: True)

    labels = pub.add_parser("labels", help="Метки площадок у файлов", description="Метки")
    actions = labels.add_subparsers(dest="labels_action", metavar="действие")
    apply = command(actions, "apply", "Проставить метки по архиву: сначала сколько файлов", parents)
    apply.add_argument("scope", help="аккаунт (tg:ru, 12) или назначение (instagram.post)")
    _yes(apply)
    apply.set_defaults(handler=_labels_apply, mutates=lambda a: True)


def register(subparsers, parents: list) -> None:
    pub = group(subparsers, "publish", "Публикации на площадках: подготовка, пакет, журнал")
    _register_accounts(pub, parents)
    _register_profiles(pub, parents)

    readiness = command(pub, "readiness", "Готовность картин или каталога для аккаунтов", parents)
    readiness.add_argument("paintings", type=int, nargs="*", help="номера картин")
    readiness.add_argument("--catalog", help="каталог: имя, путь «А / Б» или номер")
    _target_arg(readiness, many=True)
    readiness.set_defaults(handler=_readiness, mutates=None)

    candidates = command(pub, "candidates", "«Можно опубликовать» или «Неготовые»", parents)
    candidates.add_argument("--catalog", help="каталог: имя, путь «А / Б» или номер")
    candidates.add_argument("--not-ready", action="store_true", help="неготовые с причинами")
    candidates.add_argument("--q", help="№ или слова названия")
    candidates.set_defaults(handler=_candidates, mutates=None)

    preview = command(
        pub, "preview", "Предпросмотр: тексты, файлы, готовность и номер снимка", parents
    )
    preview.add_argument("painting", type=int, help="номер картины")
    _target_arg(preview, many=True)
    _overrides(preview)
    timeout_argument(preview, PREVIEW_TIMEOUT, "подготовки файлов")
    preview.set_defaults(handler=_preview, mutates=lambda a: True)

    run = command(pub, "run", "Выгрузить по номеру снимка — только после «да» человека", parents)
    run.add_argument("snapshot", help="номер снимка из publish preview")
    run.add_argument(
        "--force", action="store_true", help="повторно в тот же аккаунт (после «да» человека)"
    )
    _yes(run)
    run.set_defaults(handler=_run, mutates=lambda a: True)

    package = command(pub, "package", "Пакет публикации: ZIP с файлами и текстами", parents)
    package.add_argument("painting", type=int, nargs="?", help="номер картины")
    _target_arg(package, many=True)
    package.add_argument("--snapshot", help="номер снимка из publish preview: его цели и тексты")
    package.add_argument("--publication", type=int, help="пакет публикации из запуска")
    _overrides(package)
    where = package.add_mutually_exclusive_group()
    where.add_argument("-o", "--output", help="путь файла ZIP")
    where.add_argument("--to", "--out", default=".", help="папка; имя файла — от системы")
    package.set_defaults(handler=_package, mutates=None)

    mark = _with_link(pub, "mark", "Отметить ручное размещение картины", parents)
    mark.add_argument("painting", type=int, help="номер картины")
    _target_arg(mark, many=False)
    mark.add_argument("--date", help="дата размещения ГГГГ-ММ-ДД (задним числом)")
    mark.set_defaults(handler=_mark, mutates=lambda a: True)

    placed = _with_link(pub, "placed", "Пакет публикации размещён вручную", parents)
    placed.add_argument("publication", type=int, help="номер публикации")
    placed.set_defaults(handler=_placed, mutates=lambda a: True)

    log = command(pub, "log", "Журнал выгрузок, новые сверху", parents)
    log.add_argument("--painting", type=int, help="номер картины")
    log.add_argument("--q", help="№ или слова названия")
    log.add_argument("--platform", help="telegram, vk, instagram, artnow")
    log.add_argument("--account", help="аккаунт: tg:ru или номер")
    log.add_argument("--state", help="состояние: placed, outdated, failed, package_ready…")
    log.add_argument("--source", choices=SOURCES)
    log.add_argument("--since", help="с даты ГГГГ-ММ-ДД")
    log.add_argument("--until", help="по дату ГГГГ-ММ-ДД")
    log.add_argument("--page", type=int, default=1)
    log.add_argument("--page-size", type=int, default=50)
    log.set_defaults(handler=_log, mutates=None)
