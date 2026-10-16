#!/bin/zsh
# Вопросы работодателей на всех трёх аккаунтах.
#
#   zsh scripts/questions.sh export   выгрузить новые вопросы в questions/new/
#   zsh scripts/questions.sh take     забрать ответы: проверка → импорт → сколько готово
#                                     (ответы между аккаунтами не переносятся)
#
# take берёт в каждом аккаунте самый свежий файл из questions/new/ — тот,
# что выгрузил последний export. Файл с ошибками формата не импортируется.
BASE=/Users/mr_macbook/Documents
PY=$BASE/hh_autoreply/venv/bin/python
SCRIPTS=${0:A:h}
ACCS=(hh_autoreply hh_autoreply_2 hh_autoreply_3)

case "$1" in
  export)
    for a in $ACCS; do
      echo "== $a"
      (cd $BASE/$a && $PY hh_answers.py --new 2>&1 | grep -E "выгружено|draft|needs_input|выбором|заполни")
      # правило «повторы не копировать, а пересказывать» — в шапку свежего файла
      f=$(ls -t $BASE/$a/questions/new/*.txt 2>/dev/null | head -1)
      [ -n "$f" ] && $PY $SCRIPTS/questions_note.py "$f"
    done
    ;;
  take)
    for a in $ACCS; do
      f=$(ls -t $BASE/$a/questions/new/*.txt 2>/dev/null | head -1)
      echo "== $a: ${f:t}"
      [ -z "$f" ] && continue
      if (cd $BASE/$a && $PY hh_answers.py --check "$f"); then
        (cd $BASE/$a && $PY hh_answers.py --import "$f" 2>&1 \
          | grep -E "^обновлено|пропущен|не принято|неизвестные|удалено")
      else
        echo "   файл с ошибками — не импортирую, поправь и запусти take ещё раз"
      fi
    done
    # Перенос ответов между аккаунтами (merge_answers.py) выключен 16.10.2026:
    # у аккаунтов разный опыт и формулировки, каждый отвечает на свои вопросы сам.
    echo "== готовность"
    $PY $SCRIPTS/answers_ready.py
    ;;
  *)
    echo "использование: questions.sh export | take"; exit 2 ;;
esac
