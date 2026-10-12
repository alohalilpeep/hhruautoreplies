#!/bin/zsh
# Прогон одного аккаунта. Пишет лог в папку аккаунта и печатает итог.
#
#   zsh scripts/run_pass.sh hh              подъём резюме + прогон по SEARCH_URL
#   zsh scripts/run_pass.sh hh2 --deferred  только дозабор отложенных
#   zsh scripts/run_pass.sh hh3 --foryou    подборка «Для вас»
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

echo "=== прогон $1 ${MODE:-по SEARCH_URL} $(date +%T), лог $LOG"
$PY -u hh_searchapply.py $MODE > "$LOG" 2>&1
CODE=$?
echo "=== завершился $(date +%T), код $CODE"
echo "отправлено: $(grep -cE '^\[(applied|answered)' "$LOG"), отложено: $(grep -c '^\[questions\]' "$LOG")"
grep -E '^\[(applied|answered)|просмотрено|За сегодня|Traceback|ОШИБКА|капч' "$LOG" | tail -30
