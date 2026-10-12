#!/bin/zsh
# Собеседования и чаты на всех трёх аккаунтах. Ничего не отправляет.
#
#   zsh scripts/interviews.sh        счётчики + 8 свежих приглашений на аккаунт
#   zsh scripts/interviews.sh 20     то же, но читать до 20 приглашений
#
# Полный вывод пишется в hh_autoreply/interviews_<дата>.log и печатается.
BASE=/Users/mr_macbook/Documents
PY=$BASE/hh_autoreply/venv/bin/python
SCRIPTS=${0:A:h}
LOG=$BASE/hh_autoreply/interviews_$(date +%m-%d_%H-%M).log
{
  echo "######## счётчики и непрочитанные"
  $PY $SCRIPTS/inbox_counts.py
  echo
  echo "######## вкладка «Собеседование»"
  $PY $SCRIPTS/interviews_read.py ${1:-8}
} > $LOG 2>&1
echo "лог: $LOG"
cat $LOG
