#!/bin/zsh
# Прогон одного аккаунта. Пишет лог в папку аккаунта и печатает итог.
#
#   zsh scripts/run_pass.sh hh              подъём резюме + прогон по SEARCH_URL
#   zsh scripts/run_pass.sh hh2 --deferred  только дозабор отложенных
#   zsh scripts/run_pass.sh hh3 --foryou    подборка «Для вас»
#
# Доп. поисковые запросы: .env.search2, .env.search3 … в папке аккаунта (строка SEARCH_URL=),
# проходятся после основного в обычном прогоне; пустой SEARCH_URL — файл пропускается.
#
# Аккаунты лежат рядом: hh → hh_autoreply, hh2 → hh_autoreply_2, hh3 → hh_autoreply_3.
# Python берётся из venv первого аккаунта: у остальных своего нет.
BASE=/Users/mr_macbook/Documents
case "$1" in
  hh)  ACC=$BASE/hh_autoreply ;;
  hh2) ACC=$BASE/hh_autoreply_2 ;;
  hh3) ACC=$BASE/hh_autoreply_3 ;;
  *)   echo "аккаунт: hh, hh2 или hh3"; exit 2 ;;
esac
MODE="$2"
PY=$BASE/hh_autoreply/venv/bin/python
cd "$ACC" || exit 1
STAMP=$(date +%m-%d_%H-%M)

if [ -z "$MODE" ]; then
  echo "=== подъём $(date +%T)"
  $PY hh_resume_bump.py 2>&1 | tail -3
  LOG="run_serp_$STAMP.log"
else
  LOG="run_${MODE#--}_$STAMP.log"
fi

summary() {
  echo "=== завершился $(date +%T), код $1"
  echo "отправлено: $(grep -cE '^\[(applied|answered)' "$2"), отложено: $(grep -c '^\[questions\]' "$2")"
  grep -E '^\[(applied|answered)|просмотрено|За сегодня|Traceback|ОШИБКА|капч' "$2" | tail -30
}

echo "=== прогон $1 ${MODE:-по SEARCH_URL} $(date +%T), лог $LOG"
$PY -u hh_searchapply.py $MODE > "$LOG" 2>&1
summary $? "$LOG"

# Дополнительные запросы: .env.search2, .env.search3 … в папке аккаунта, в каждом — свой
# SEARCH_URL. Идут после основного и только в обычном прогоне. load_dotenv не перебивает
# переменную окружения, поэтому SEARCH_URL из файла главнее, чем в .env.
if [ -z "$MODE" ]; then
  for EXTRA in .env.search*(N); do
    URL=$(grep -E '^SEARCH_URL=' "$EXTRA" | tail -1 | cut -d= -f2-)
    [ -z "$URL" ] && continue
    N=${EXTRA#.env.search}
    XLOG="run_serp${N}_$STAMP.log"
    echo
    if grep -qE '^COLLECT_ONLY=1' "$EXTRA"; then
      # только сбор вакансий и компаний, без откликов
      XLOG="collect${N}_$STAMP.log"
      echo "=== сбор $N ($EXTRA) $(date +%T), лог $XLOG"
      $PY -u ${0:A:h}/hh_collect.py $1 "$EXTRA" > "$XLOG" 2>&1
      echo "=== завершился $(date +%T), код $?"
      grep -A 20 '^остановка' "$XLOG"
      continue
    fi
    echo "=== доп. запрос $N ($EXTRA) $(date +%T), лог $XLOG"
    SEARCH_URL="$URL" $PY -u hh_searchapply.py > "$XLOG" 2>&1
    summary $? "$XLOG"
  done
fi
