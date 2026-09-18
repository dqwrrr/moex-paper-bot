#!/usr/bin/env bash
# Публикует состояние бота (папка $1) в ветку live одним коммитом без истории,
# чтобы репозиторий не разрастался от публикаций каждые несколько минут.
set -euo pipefail
cd "$1"
git add -A
msg="state $(TZ=Europe/Moscow date '+%Y-%m-%d %H:%M') МСК"
if git rev-parse -q --verify HEAD >/dev/null; then
  git commit -q --amend -m "$msg"
else
  git commit -q -m "$msg"
fi
git push -q -f origin HEAD:refs/heads/live
