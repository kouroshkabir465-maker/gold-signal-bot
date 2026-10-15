#!/bin/bash
# Persist bot state files on a separate 'state' branch.  usage: ci/state.sh pull|push
set -e
FILES="state.json trades.json listener_offset.json"
if [ "$1" = pull ]; then
  if git fetch -q origin state 2>/dev/null; then
    for f in $FILES; do git show origin/state:$f > $f 2>/dev/null || rm -f $f; done
  fi
  exit 0
fi
tmp=$(mktemp -d); for f in $FILES; do [ -f $f ] && cp $f $tmp/; done
git config user.name "gold-bot"; git config user.email "gold-bot@users.noreply.github.com"
git worktree add -q --detach $tmp/wt 2>/dev/null
cd $tmp/wt
if git fetch -q origin state 2>/dev/null; then git checkout -q --detach origin/state; else git checkout -q --orphan "st-$$"; git rm -rqf . ; fi
for f in $FILES; do [ -f $tmp/$f ] && cp $tmp/$f .; done
git add -A $FILES 2>/dev/null || true
if ! git diff --cached --quiet; then
  git commit -qm "state $(date -u +%FT%TZ)"
  for i in 1 2 3; do git push -q origin HEAD:refs/heads/state && break || { git fetch -q origin state && git rebase -q origin/state || git rebase --abort; }; done
fi
