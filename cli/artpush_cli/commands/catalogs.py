"""Каталоги: дерево, создание, состав, удаление ветки."""

from artpush_cli.commands import Context, command, group, ids, two_step
from artpush_cli.output import emit


def _tree(args, ctx: Context) -> None:
    emit(ctx.get("/api/catalogs"), table=ctx.table)


def _add(args, ctx: Context) -> None:
    parent = ctx.resolve_catalog(args.parent) if args.parent else None
    body = {"name": args.name, "parent_id": parent}
    emit(ctx.send("POST", "/api/catalogs", json=body), table=ctx.table)


def _move(args, ctx: Context) -> None:
    body: dict = {"parent_id": None if args.root else ctx.resolve_catalog(args.parent)}
    if args.position is not None:
        body["position"] = args.position
    emit(ctx.send("POST", f"/api/catalogs/{args.id}/move", json=body), table=ctx.table)


def _members(args, ctx: Context) -> None:
    if args.add:
        out = ctx.send("POST", f"/api/catalogs/{args.id}/members", json={"ids": args.add})
    else:
        path = f"/api/catalogs/{args.id}/members/remove"
        out = ctx.send("POST", path, json={"ids": args.remove})
    emit(out, table=ctx.table)


def _delete_preview(args, ctx: Context) -> None:
    emit(ctx.get(f"/api/catalogs/{args.id}/delete-preview"), table=ctx.table)


def _delete(args, ctx: Context, token: str) -> None:
    params = {"preview_token": token}
    emit(ctx.send("DELETE", f"/api/catalogs/{args.id}", params=params), table=ctx.table)


def register(subparsers, parents: list) -> None:
    catalogs = group(subparsers, "catalogs", "Каталоги")
    tree = command(catalogs, "tree", "Дерево каталогов", parents)
    tree.set_defaults(handler=_tree, mutates=None)

    catalog = group(subparsers, "catalog", "Один каталог")
    add = command(catalog, "add", "Создать каталог", parents)
    add.add_argument("name")
    add.add_argument("--parent", help="родитель: имя, путь «А / Б» или номер")
    add.set_defaults(handler=_add, mutates=lambda a: True)

    move = command(catalog, "move", "Перенести каталог с веткой", parents)
    move.add_argument("id", type=int, help="номер каталога")
    where = move.add_mutually_exclusive_group(required=True)
    where.add_argument("--parent", help="новый родитель: имя, путь «А / Б» или номер")
    where.add_argument("--root", action="store_true", help="в корень")
    move.add_argument("--position", type=int, help="место среди соседей (с 0)")
    move.set_defaults(handler=_move, mutates=lambda a: True)

    members = command(catalog, "members", "Добавить или убрать картины", parents)
    members.add_argument("id", type=int, help="номер каталога")
    change = members.add_mutually_exclusive_group(required=True)
    change.add_argument("--add", type=ids, help="номера картин через запятую")
    change.add_argument("--remove", type=ids, help="номера картин через запятую")
    members.set_defaults(handler=_members, mutates=lambda a: True)

    delete = command(catalog, "delete", "Удалить каталог с веткой (в два шага)", parents)
    delete.add_argument("id", type=int, help="номер каталога")
    two_step(delete, preview=_delete_preview, apply=_delete)
