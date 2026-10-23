---
name: artpush-painting
description: Используй, когда нужно посмотреть или изменить карточку картины в ArtPush — автора, технику, материал, размер, дату, статус, местонахождение, теги, название и описание на любом языке, ключевые слова, цены; завести новую или производную картину; заполнить одно поле сразу у многих картин; при конфликте версий (409) после правки; отметить продажу картины (продана: статус, событие, цена).
---

# Карточка картины в ArtPush

## Назначение

Чтение и правка карточки, текстов на языках и цен; массовое заполнение одного поля с предпросмотром.
Общие правила, коды выхода и ключ — в скилле `artpush`. Номер картины — из поиска (`artpush-search`).

## Чтение

```sh
artpush painting get 184 --with notes,history,placements --table
```

`--with` добавляет заметки, историю, размещения в каталогах (медиа уже в карточке). В карточке есть
`version` — утилита сама отправляет её при правке.

## Правка одной картины

```sh
artpush painting set 184 --field technique=Акварель --field height_cm=30 --field width_cm=40
artpush painting set 184 --field tags=море,осень --field location="Мастерская"
artpush painting text 184 en --title "Sunset" --description "Watercolour on paper, 2019." --keywords sea,sunset
artpush painting price 184 --amount 15000 --currency RUB --purpose Продажа
```

- Подписи полей, как в браузере («Где нарисовано» = `painted_place`): `artpush contract --fields`.
  Тела запросов операций — `artpush contract --op ID`.
- Поля `--field`: author, style, genre, material, technique, status (по имени из справочника или номеру),
  location, painted_place, date_text, height_cm, width_cm, depth_cm, origin_category (personal, student,
  acquired, gift, study), framing (none, passepartout, frame), publication_consent (да/нет), tags
  (через запятую — заменяет весь список). Пустое значение (`--field status=`) очищает поле.
- `painting text` меняет только указанные части; остальные части текста этого языка сохраняются.
- `painting price` заменяет цену с той же валютой и назначением или добавляет новую; прочие цены сохраняются.
- Правка одной картины по прямой просьбе человека подтверждения не требует. Если правки придумал ты
  (например, сочинил описания) — сначала покажи их человеку списком и спроси «Применить? (да / нет /
  поправить)».

## Продажа: статус, событие, цена

Картину продали — три отдельных шага; сверь значения со справочниками и скажи человеку, что запишешь:

```sh
artpush dictionaries status --table          # есть ли «Продана»
artpush dictionaries "тип события" --table   # есть ли «Продажа»
artpush dictionaries --table                 # все справочники, в т. ч. назначения цен
artpush painting set 184 --field status=Продана
artpush events add 184 --type Продажа --date 2026-10-12 --comment "Покупатель: …, через Artnow"
artpush painting price 184 --amount 15000 --currency RUB --purpose Продажа
```

- `painting price` заменяет цену с той же валютой и назначением (прочие цены сохраняются), поэтому
  повтор с новой суммой меняет цену, а не добавляет вторую. Конфликт версий (код 2) — как в разделе ниже:
  `--overwrite` только после «да» человека.
- Дату продажи и сумму спроси у человека, не выдумывай. `--purpose` — назначение цены по имени из
  справочника (основная, Artnow и т. д.): сверь с `artpush dictionaries --table`.
- Ссылка на Artnow и другие размещения: `artpush painting placements 184`.

## Новая и производная картина

```sh
artpush painting create --catalog 12 --field technique=Масло --field date_text=2024
artpush painting derive 184 --groups author,style_genre,material_technique --catalog 12
```

`derive` копирует выбранные группы полей в новую картину (по умолчанию все). Если `create` упал на полях,
картина уже создана — в ошибке `painting_id`; правь её через `painting set`, новую не заводи.

## Массовое заполнение — только с подтверждением

```sh
artpush bulk preview --filter catalog=12 --field status --value "В архиве" --table
# показать человеку found / changed / skipped и строки preview, спросить «Применить к N картинам? (да / нет)»
artpush bulk apply pv_… --filter catalog=12 --field status --value "В архиве"
artpush bulk undo 58
```

- Выборка: `--ids 101,118,184` или `--filter поле=значение` (q, incomplete, sort, status, technique,
  material, style, genre, origin, catalog, year_min, year_max; можно несколько `--filter`).
- Поля: author, style, genre, material, technique, status, origin_category, framing, location,
  painted_place, date_text, tags (добавляются к имеющимся), description и keywords (нужен `--language`;
  ключевые слова добавляются). `--only-empty` — только у кого поле пусто.
- `bulk apply` — **ровно с той же выборкой, полем и значением**, что в preview, и только после «да».
  Жетон — 10 минут, одноразовый; после ошибки — новый preview и новый вопрос.
- Итог `operation.id` назови человеку: отмена — `artpush bulk undo <номер>` или кнопка в истории картины.

## Конфликт версий (код 2, `conflict`)

Кто-то изменил картину после того, как ты её прочитал (в браузере — даже сам человек — или другим
агентом). Утилита помнит версию карточки, которую ты видел (`painting get`, своя правка; сутки), и
шлёт её при `set`, `text`, `price`, поэтому такая правка не затирается, а даёт код 2. В JSON ошибки —
`field`, `by`, `at`, `their_label`, `mine_label`, `hint`. Покажи человеку: «Картину № 187 изменил …
в 12:41: Местонахождение «Склад», я хотел «Мастерская»». Спроси «записать поверх или оставить?».
Поверх — только после «да»: та же команда с `--overwrite` (возьмёт текущую версию). Оставить —
ничего не делай. Повтор той же команды без `--overwrite` снова даст конфликт — это не сбой.

## Ошибки

- `dictionary_value` (код 1): значения нет в справочнике, в `choices` — допустимые. Спроси человека;
  пополнить справочник может администратор (`artpush-admin`).
- `usage`: поле не из списка или неверное значение — `artpush painting set --help`.
- Код 3 на правке — ключ только для чтения: попроси ключ с правкой.
