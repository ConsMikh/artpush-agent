#!/bin/sh
# Установка набора ArtPush для агента: утилита artpush и скиллы. Ключ не нужен.
#
#   curl -fsSL https://raw.githubusercontent.com/ConsMikh/artpush-agent/main/install.sh | sh
#
# Флаги: --version X.Y.Z (выпуск вместо последнего; или переменная ARTPUSH_VERSION), --from ПАПКА
# (папка с artpush_cli-*.whl и skills/ вместо скачивания), --no-skills (только утилита).
# Повторный запуск обновляет установку без дублей. Linux и macOS.
set -eu

MIRROR="https://github.com/ConsMikh/artpush-agent"
RAW="https://raw.githubusercontent.com/ConsMikh/artpush-agent"
VERSION="${ARTPUSH_VERSION:-}"
FROM=""
WITH_SKILLS=1
SKILLS_HOME="$HOME/.local/share/artpush/skills"

say() { printf '%s\n' "$*"; }
warn() { printf 'artpush: внимание: %s\n' "$*" >&2; }
die() {
  printf 'artpush: %s\n' "$1" >&2
  exit "${2:-1}"
}
need_value() {
  # $1 — флаг, $2 — сколько аргументов осталось, $3 — следующий аргумент.
  [ "$2" -ge 2 ] || die "флагу $1 нужно значение" 2
  case "$3" in
    --*) die "флагу $1 нужно значение, а не флаг $3" 2 ;;
  esac
}
usage() {
  say "Установка набора ArtPush для агента (утилита artpush и скиллы)."
  say "  sh install.sh [--version X.Y.Z] [--from ПАПКА] [--no-skills]"
}

fetch() {
  # $1 — адрес, $2 — куда сохранить, $3 — что качаем (для сообщения).
  code=0
  curl -fsSL --proto =https --tlsv1.2 -o "$2" "$1" || code=$?
  case "$code" in
    0) ;;
    # 22 — сервер ответил ошибкой (404): файла выпуска нет, интернет в порядке.
    22)
      [ -n "$VERSION" ] || die "в зеркале ArtPush пока нет выпусков — обратитесь к администратору ArtPush"
      die "выпуска $VERSION в зеркале нет — проверьте номер выпуска или обратитесь к администратору ArtPush"
      ;;
    *) die "не удалось скачать $3 — проверьте интернет и запустите установку ещё раз" ;;
  esac
}

unpack() {
  # $1 — zip, $2 — куда распаковать.
  if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "$1" -d "$2"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m zipfile -e "$1" "$2"
  else
    uv run --no-project --quiet python -m zipfile -e "$1" "$2"
  fi
}

# --- скиллы --------------------------------------------------------------------------------------
link_skill() {
  # $1 — каталог скилла в общем каталоге, $2 — каталог скиллов агента.
  link="$2/$(basename "$1")"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$1" ]; then
    return 0
  elif [ -L "$link" ] || [ -e "$link" ]; then
    warn "$link — не ссылка набора, оставляю как есть"
    return 0
  fi
  ln -s "$1" "$link" || warn "не удалось сделать ссылку $link"
}

drop_stale_links() {
  # Ссылки на скиллы, которых в наборе больше нет.
  for link in "$1"/artpush*; do
    [ -L "$link" ] || continue
    [ -e "$link" ] && continue
    case $(readlink "$link") in
      "$SKILLS_HOME"/*) rm -f "$link" ;;
    esac
  done
}

# Всё тело — в функции: оборванная загрузка «curl | sh» ничего не выполнит, а оболочка прочитает
# скрипт целиком до первой запущенной программы.
main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --version) need_value "$1" $# "${2-}"; VERSION=$2; shift 2 ;;
      --version=*) VERSION=${1#--version=}; shift ;;
      --from) need_value "$1" $# "${2-}"; FROM=$2; shift 2 ;;
      --from=*) FROM=${1#--from=}; shift ;;
      --no-skills) WITH_SKILLS=0; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "неизвестный параметр: $1 (см. --help)" 2 ;;
    esac
  done

  OS=$(uname -s)
  case "$OS" in
    Linux | Darwin) ;;
    *) die "система $OS не поддерживается: этот установщик — для macOS и Linux" 1 ;;
  esac

  # --- uv ----------------------------------------------------------------------------------------
  USER_PATH=$PATH
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
  if ! command -v uv >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || die "нужна программа curl — установите её и запустите установку ещё раз"
    say "Ставлю uv (менеджер Python-утилит)…"
    curl -fsSL --proto =https --tlsv1.2 https://astral.sh/uv/install.sh | sh ||
      die "не удалось поставить uv — проверьте интернет и запустите установку ещё раз"
    command -v uv >/dev/null 2>&1 || die "uv поставлен, но не найден — откройте терминал заново и запустите установку ещё раз"
  fi

  # --- набор -------------------------------------------------------------------------------------
  # Всё скачивается и распаковывается во временную папку до первых изменений: при ошибке
  # прежняя установка остаётся целой.
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/artpush-install.XXXXXX")
  trap 'rm -rf "$WORK"' EXIT
  trap 'exit 130' INT TERM

  if [ -n "$FROM" ]; then
    [ -d "$FROM" ] || die "папка набора не найдена: $FROM"
    set -- "$FROM"/artpush_cli-*.whl
    [ -f "$1" ] || die "в папке $FROM нет колеса утилиты (artpush_cli-*.whl)"
    [ $# -eq 1 ] || die "в папке $FROM несколько колёс утилиты — оставьте одно"
    WHEEL=$1
    SKILLS_SRC="$FROM/skills"
  else
    command -v curl >/dev/null 2>&1 || die "нужна программа curl — установите её и запустите установку ещё раз"
    if [ -z "$VERSION" ]; then
      fetch "$RAW/main/VERSION" "$WORK/VERSION" "номер последнего выпуска ArtPush"
      VERSION=$(tr -d ' \t\r\n' <"$WORK/VERSION")
    fi
    VERSION=${VERSION#v}
    case "$VERSION" in
      "" | *[!0-9A-Za-z.-]*) die "непонятный номер выпуска «$VERSION» — укажите его как 1.2.3" 2 ;;
    esac
    RELEASE="$MIRROR/releases/download/v$VERSION"
    say "Скачиваю набор ArtPush $VERSION…"
    # Имя колеса сохраняется: uv узнаёт по нему пакет и версию.
    WHEEL="$WORK/artpush_cli-$VERSION-py3-none-any.whl"
    fetch "$RELEASE/artpush_cli-$VERSION-py3-none-any.whl" "$WHEEL" "утилиту ArtPush $VERSION"
    SKILLS_SRC="$WORK/skills"
    if [ "$WITH_SKILLS" = 1 ]; then
      fetch "$RELEASE/artpush-skills-$VERSION.zip" "$WORK/skills.zip" "скиллы ArtPush $VERSION"
      unpack "$WORK/skills.zip" "$SKILLS_SRC" >/dev/null 2>&1 ||
        die "не удалось распаковать скиллы — запустите установку ещё раз"
    fi
  fi
  if [ "$WITH_SKILLS" = 1 ]; then
    [ -d "$SKILLS_SRC" ] || die "в наборе нет скиллов (skills/)"
  fi

  # --- утилита -----------------------------------------------------------------------------------
  say "Ставлю утилиту artpush…"
  uv tool install --force --reinstall --quiet "$WHEEL" ||
    die "не удалось поставить утилиту artpush — запустите установку ещё раз"
  BIN=$(uv tool dir --bin)
  PATH="$BIN:$PATH"
  command -v artpush >/dev/null 2>&1 || die "утилита поставлена, но не найдена в $BIN — откройте терминал заново и запустите установку ещё раз"

  if [ "$WITH_SKILLS" = 1 ]; then
    say "Кладу скиллы в $SKILLS_HOME…"
    rm -rf "$SKILLS_HOME"
    mkdir -p "$SKILLS_HOME"
    cp -R "$SKILLS_SRC/." "$SKILLS_HOME/"
    # Каталоги скиллов агентов — явным списком: пути с пробелами не разбиваются.
    # OpenCode читает оба этих каталога сам.
    set -- "$HOME/.claude/skills" "$HOME/.agents/skills"
    for dir; do
      if ! mkdir -p "$dir" 2>/dev/null; then
        warn "не удалось создать $dir — скиллы для этого агента не подключены"
        continue
      fi
      for skill in "$SKILLS_HOME"/*/; do
        [ -d "$skill" ] || continue
        link_skill "${skill%/}" "$dir"
      done
      drop_stale_links "$dir"
    done
  fi

  # --- итог --------------------------------------------------------------------------------------
  say ""
  say "Установлено: $(artpush --version)."
  if [ "$WITH_SKILLS" = 1 ]; then
    say "Скиллы для Claude Code, Codex и OpenCode: ~/.claude/skills и ~/.agents/skills."
  fi
  case ":$USER_PATH:" in
    *":$BIN:"*) ;;
    *)
      uv tool update-shell >/dev/null 2>&1 || warn "не удалось добавить $BIN в PATH"
      say "Откройте терминал (и агента) заново, чтобы команда artpush нашлась."
      ;;
  esac
  # Код 3 — ключ не принят; ошибка конфигурации (код 1, "code": "config") — ключ ещё не задан.
  # Остальные сбои (сеть, сервер) про ключ ничего не говорят.
  code=0
  WHOAMI=$(artpush whoami 2>/dev/null) || code=$?
  if [ "$code" = 0 ]; then
    say "Готово: ArtPush подключён. Обновление после нового выпуска: artpush update"
  elif [ "$code" = 3 ] || printf '%s' "$WHOAMI" | grep -q '"code": "config"'; then
    say "Что дальше:"
    say "  1. Откройте ArtPush → Настройки → Ключи агента и выпустите ключ."
    say "  2. В терминале выполните: artpush init — и вставьте ключ, когда спросит."
    say "  3. Или скажите своему агенту: «подключи меня к ArtPush»."
  else
    say "Набор установлен; проверить связь не удалось — выполните artpush whoami позже."
  fi
}

main "$@"
