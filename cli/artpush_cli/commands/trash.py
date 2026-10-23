"""Корзина: список и восстановление. Очистки нет — она только в браузере."""

from artpush_cli.commands import Context, command, group
from artpush_cli.output import emit

KINDS = ("painting", "media", "catalog")


def _list(args, ctx: Context) -> None:
    out = ctx.get("/api/trash")
    emit(out["items"] if ctx.table else out, table=ctx.table)


def _restore(args, ctx: Context) -> None:
    kind = next(k for k in KINDS if getattr(args, k) is not None)
    body = {"items": [{"kind": kind, "id": getattr(args, kind)}]}
    emit(ctx.send("POST", "/api/trash/restore", json=body), table=ctx.table)


def register(subparsers, parents: list) -> None:
    trash = group(subparsers, "trash", "Корзина (очистка — только в браузере)")
    list_ = command(trash, "list", "Что лежит в корзине", parents)
    list_.set_defaults(handler=_list, mutates=None)

    restore = command(trash, "restore", "Восстановить из корзины", parents)
    which = restore.add_mutually_exclusive_group(required=True)
    for kind, label in zip(KINDS, ("картины", "медиа", "каталога"), strict=True):
        which.add_argument(f"--{kind}", type=int, help=f"номер {label}")
    restore.set_defaults(handler=_restore, mutates=lambda a: True)
