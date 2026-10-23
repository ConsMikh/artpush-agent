---
name: artpush-search
description: Используй, когда нужно найти, отобрать, перечислить или посчитать картины в ArtPush — по технике, материалу, стилю, жанру, статусу, году, каталогу, тегу, словам из названия и описания; найти незаполненные карточки (нет описания на языке, нет фото, нет размера); составить список или отчёт по архиву.
---

# Поиск картин в ArtPush

## Назначение

Отбор картин теми же фильтрами, что в браузере. Поиск только читает — подтверждение не нужно.
Общие правила, коды выхода и ключ — в скилле `artpush`.

## Команда

```sh
artpush paintings search [фильтры] [--limit 50] [--offset 0] [--sort -id] [--table]
```

| Фильтр | Пример | Заметки |
|---|---|---|
| `--q` | `--q "закат"` | слова в названии, описании, ключевых словах |
| `--technique`, `--material`, `--style`, `--genre`, `--status` | `--technique Акварель,Масло` | имя из справочника или номер; через запятую — «любое из»; флаг можно повторять |
| `--origin` | `--origin personal` | категория происхождения: personal, student, acquired, gift, study |
| `--year` / `--year-min` / `--year-max` | `--year 2019` | год написания |
| `--catalog` | `--catalog 12` | номер каталога (номера — `artpush catalogs tree`) |
| `--tag` | `--tag море --tag осень` | все указанные теги сразу |
| `--incomplete` | | карточка не заполнена хоть в чём-то |
| `--missing` | `--missing media`, `--missing text:en` | не заполнено конкретное: title, author, date, size, technique, material, status, origin_category, media или `text:<язык>` (нет **названия** на языке) / `text:<язык>:description` (нет **описания** на языке) |

Ответ: `{"items": [...], "total": N}`. В строке — `id`, название, год, техника, статус, `missing` (чего не
хватает), теги. `total: null` значит «сколько всего — неизвестно»: при `--tag`/`--missing` сервер отдал не
всю выборку; уточни фильтры или листай `--offset`.

Подробности картины — `artpush painting get <id> --with notes` (скилл `artpush-painting`).

## Примеры

```sh
artpush paintings search --technique Акварель --year 2019 --missing text:en:description --table
artpush paintings search --catalog 12 --status "В наличии" --table
artpush paintings search --missing media --limit 200        # без единого фото
artpush paintings search --q "Кыштым" --sort -id --limit 10
```

Сколько всего: смотри `total` в JSON (с `--limit 1` быстро).

## Как показывать человеку

Показывай вызов и короткую таблицу (`--table`), длинную выдачу — первые 10–20 строк и «ещё N».
Если человек просит что-то сделать с найденным — переходи к нужному скиллу; для изменения многих картин
сразу — массовое заполнение с предпросмотром (`artpush-painting`).

## Ошибки

- `dictionary_value` (код 1): такого значения в справочнике нет; в `choices` — допустимые. Предложи
  человеку ближайшее, не подставляй молча.
- `usage` (код 1): неверный флаг или значение — сверься с `artpush paintings search --help`.
