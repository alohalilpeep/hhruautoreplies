#!/bin/zsh
# Отклики на getmatch.ru. Аргументы — как у getmatch_apply.py:
#   zsh scripts/getmatch.sh hh --dry        что было бы отправлено
#   zsh scripts/getmatch.sh hh --limit 1    один настоящий отклик
exec /Users/mr_macbook/Documents/hh_autoreply/venv/bin/python -u ${0:A:h}/getmatch_apply.py "$@"
