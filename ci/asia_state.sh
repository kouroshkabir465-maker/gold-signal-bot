#!/bin/bash
# Asian-session add-on state on the 'state' branch, isolated from ci/state.sh.
# usage: ci/asia_state.sh pull | push <file...>   (push commits ONLY the given files)
set -e
if [ "$1" = pull ]; then
  if git fetch -q origin state 2>/dev/null; then
    for f in asia.json asia_prefs.json; do git show origin/state:$f > $f 2>/dev/null || rm -f $f; done
  fi
  exit 0
fi
shift; FILES="$*"
tmp=$(mktemp -d); for f in $FILES; do [ -f $f ] && cp $f $tmp/; done
git config user.name "gold-bot"; git config user.email "gold-bot@users.noreply.github.com"
for i in 1 2 3 4; do
  rm -rf $tmp/wt; git worktree prune
  git fetch -q origin state && git worktree add -q --detach $tmp/wt origin/state
  ( cd $tmp/wt
    for f in $FILES; do [ -f $tmp/$f ] && cp $tmp/$f .; done
    git add -A $FILES 2>/dev/null || true
    git diff --cached --quiet && exit 0
    git commit -qm "asia state $(date -u +%FT%TZ)" && git push -q origin HEAD:refs/heads/state ) && exit 0
  sleep $((i * 3))
done
echo "asia_state push failed"; exit 0
