#!/usr/bin/env bash
#
# Деплой CreamcheckBot на сервер.
#
# Запускать НА СЕРВЕРЕ, из папки с репозиторием:
#
#     ./deploy.sh              — обновить код и перезапустить
#     ./deploy.sh --key        — то же плюс сменить OPENAI_API_KEY
#     ./deploy.sh --no-pull    — пересобрать из того, что уже лежит локально
#
# Скрипт идемпотентен: его можно гонять сколько угодно раз.
# Данные (статистика, логи, кэш, правки базы комедогенов) живут в томе
# creamcheck-data и переживают пересборку.

set -euo pipefail

IMAGE="creamcheck"
CONTAINER="creamcheck"
VOLUME="creamcheck-data"
PORT="${PORT:-10000}"

PULL=1
CHANGE_KEY=0
for arg in "$@"; do
    case "$arg" in
        --key)     CHANGE_KEY=1 ;;
        --no-pull) PULL=0 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Неизвестный аргумент: $arg (см. --help)" >&2; exit 1 ;;
    esac
done

say()  { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker не установлен. Поставь: apt install docker.io"
[ -f Dockerfile ] || die "Запускать из папки с репозиторием (здесь нет Dockerfile)."

# ── Код ──────────────────────────────────────────────────────
if [ "$PULL" = 1 ]; then
    say "Обновляю код"
    git pull --ff-only origin main
fi
echo "Версия: $(git log --oneline -1)"

# ── .env ─────────────────────────────────────────────────────
if [ ! -f .env ]; then
    say "Файла .env нет — создаю из шаблона"
    cp .env.example .env
    warn "Заполни .env (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, DASHBOARD_TOKEN) и запусти снова:"
    warn "    nano .env && ./deploy.sh"
    exit 1
fi

if [ "$CHANGE_KEY" = 1 ]; then
    say "Смена OPENAI_API_KEY"
    # -s: ключ не попадёт ни на экран, ни в историю команд
    read -rsp "Вставь новый ключ (ввод не отображается): " NEW_KEY
    echo
    [ -n "$NEW_KEY" ] || die "Пустой ключ, ничего не менял."
    case "$NEW_KEY" in
        sk-*) ;;
        *) warn "Ключ не начинается на 'sk-' — проверь, что скопировала целиком." ;;
    esac

    cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
    # Через grep+append, а не sed: ключ может содержать символы,
    # которые sed примет за разделитель и всё испортит.
    grep -v '^OPENAI_API_KEY=' .env > .env.tmp || true
    printf 'OPENAI_API_KEY=%s\n' "$NEW_KEY" >> .env.tmp
    mv .env.tmp .env
    chmod 600 .env
    unset NEW_KEY
    echo "Ключ заменён, прежний .env сохранён рядом как .env.bak.*"
fi

for required in TELEGRAM_BOT_TOKEN OPENAI_API_KEY; do
    if ! grep -qE "^${required}=.+" .env; then
        die "В .env не заполнен ${required}."
    fi
done
grep -qE '^DASHBOARD_TOKEN=.+' .env || warn \
    "DASHBOARD_TOKEN пуст — он сгенерируется при старте и будет меняться при каждом перезапуске."

# ── Сборка ───────────────────────────────────────────────────
say "Собираю образ"
docker build -t "$IMAGE" .

# ── Перезапуск ───────────────────────────────────────────────
# Старый контейнер надо погасить ДО старта нового: два процесса
# на одном токене Telegram конфликтуют за long polling.
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    say "Останавливаю прежний контейнер"
    docker stop "$CONTAINER" >/dev/null
    docker rm "$CONTAINER" >/dev/null
fi

docker volume inspect "$VOLUME" >/dev/null 2>&1 || {
    say "Создаю том для данных"
    docker volume create "$VOLUME" >/dev/null
}

say "Запускаю"
docker run -d \
    --name "$CONTAINER" \
    --env-file .env \
    -p "${PORT}:10000" \
    -v "${VOLUME}:/app/data" \
    --restart unless-stopped \
    "$IMAGE" >/dev/null

# ── Проверка ─────────────────────────────────────────────────
say "Жду, пока поднимется"
for i in $(seq 1 30); do
    if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        echo
        warn "Контейнер упал. Последние строки лога:"
        docker logs --tail 40 "$CONTAINER" || true
        die "Старт не удался."
    fi
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        printf '\n\033[32m✓ Бот работает\033[0m\n'
        echo "Дашборд:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/dash?key=<DASHBOARD_TOKEN>"
        echo "Логи:     docker logs -f ${CONTAINER}"
        echo "Проверка: /start в боте, затем /panel с админского аккаунта"
        exit 0
    fi
    printf '.'
    sleep 2
done

echo
warn "Health-проверка не ответила за минуту. Контейнер запущен, но что-то не так."
docker logs --tail 40 "$CONTAINER" || true
exit 1
