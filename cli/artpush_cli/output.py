"""Вывод: stdout — только данные (JSON или таблица), сообщения — в stderr."""

import json
import sys


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _rows(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(i, dict) for i in value)


def emit(data, *, table: bool = False, columns: list[str] | None = None) -> None:
    """Печатает данные: JSON по умолчанию, простая таблица при table=True."""
    if not table:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict) and _rows(data.get("items")):
        # Список с обвязкой ({"items": [...], "total": …}): строки таблицей, остальное после.
        emit(data["items"], table=True, columns=columns)
        rest = {k: v for k, v in data.items() if k != "items"}
        if rest:
            print()
            print(_table([[k, _cell(v)] for k, v in rest.items()]))
        return
    if isinstance(data, dict):
        rows = [["поле", "значение"], *([k, _cell(v)] for k, v in data.items())]
    elif isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        columns = columns or list(data[0])
        rows = [columns, *([_cell(item.get(c)) for c in columns] for item in data)]
    elif isinstance(data, list):
        rows = [[_cell(item)] for item in data] or [[""]]
    else:
        rows = [[_cell(data)]]
    print(_table(rows))


def err(message: str) -> None:
    """Сообщение человеку — в stderr, чтобы не ломать JSON в stdout."""
    print(message, file=sys.stderr)
