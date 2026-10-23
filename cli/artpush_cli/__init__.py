"""Утилита artpush — агентский интерфейс ArtPush."""

# Версия системы: при сборке образа подставляется из тега; в разработке — следующая версия с -dev
# (равна version в agent/.claude-plugin/plugin.json без суффикса — см. «Выпуск» в agent/README.md).
__version__ = "0.8.1"

# Версия контракта /api/agent/contract, которую понимает утилита.
SUPPORTED_CONTRACT = 1
