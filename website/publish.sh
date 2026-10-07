#!/usr/bin/env bash
# Push the scanner's latest snapshot to the terrarium-data branch as a single
# commit (amended each minute so the branch never grows a long history).
set -euo pipefail
cd "${DATA_DIR:-site-data}"
git add -A
if git rev-parse -q --verify HEAD >/dev/null; then
  git commit -q --amend -m "Scanner data" --reset-author || true
else
  git commit -q -m "Scanner data"
fi
for i in 1 2 3; do
  git push -q -f origin HEAD:terrarium-data && exit 0
  sleep $((i * 5))
done
echo "publish: push failed" >&2
exit 1
