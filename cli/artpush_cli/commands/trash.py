"""Корзина: список и восстановление. Очистки нет — она только в браузере."""

from artpush_cli.client import ApiError
from artpush_cli.commands import CommandError, Context, command, group
from artpush_cli.output import emit, err

KINDS = ("painting", "media", "catalog")
LABELS = {"painting": "Картины", "media": "Медиафайла", "catalog": "Каталога"}


def _list(args, ctx: Context) -> None:
    out = ctx.get("/api/trash")
    emit(out["items"] if ctx.table else out, table=ctx.table)


def _restore(args, ctx: Context) -> None:
    kind = next(k for k in KINDS if getattr(args, k) is not None)
    pk = getattr(args, kind)
    body = {"items": [{"kind": kind, "id": pk}]}
    try:
        out = ctx.send("POST", "/api/trash/restore", json=body)
    except ApiError as exc:
        if exc.status != 404:
            raise
        label = LABELS[kind]
        raise CommandError(
            f"{label} № {pk} нет в корзине вашего архива — проверьте номер: artpush trash list",
            "not_found",
        ) from None
    if not out.get("restored"):
        err(f"{LABELS[kind]} № {pk} уже не в корзине — восстанавливать нечего")
    emit(out, table=ctx.table)


def register(subparsers, parents: list) -> None:
    trash = group(subparsers, "trash", "Корзина (очистка — только в браузере)")
    list_ = command(trash, "list", "Что лежит в корзине", parents)
    list_.set_defaults(handler=_list, mutates=None)

    restore = command(trash, "restore", "Восстановить из корзины", parents)
    which = restore.add_mutually_exclusive_group(required=True)
    for kind, label in zip(KINDS, ("картины", "медиа", "каталога"), strict=True):
        which.add_argument(f"--{kind}", type=int, help=f"номер {label}")
    restore.set_defaults(handler=_restore, mutates=lambda a: True)
