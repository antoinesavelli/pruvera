#!/usr/bin/env bash
# Build the public mirror of this repo into a new directory (plans/PUBLIC_RELEASE.md, Phase 2). Nothing is pushed.
#
# Usage: release/build.sh <source-commit> <out-dir>
#
# The output holds only the history of <source-commit>, on one branch (main), with no remote, rewritten by
# git-filter-repo: paths outside release/allow_paths.txt removed from every commit, private terms replaced in
# every blob and message (release/replace_text.local.txt), emails mapped (release/mailmap.local.txt). The two
# .local files are gitignored and must exist. The build checks nothing itself: run release/check_mirror.py on
# the output, and never trust filter-repo's own summary.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
commit=$(git -C "$root" rev-parse --verify --quiet "${1:?usage: release/build.sh <source-commit> <out-dir>}^{commit}") \
    || { echo "build: not a commit: $1" >&2; exit 2; }
out=${2:?usage: release/build.sh <source-commit> <out-dir>}

[[ -e $out ]] && { echo "build: refusing, $out already exists" >&2; exit 2; }
case $(realpath -m "$out")/ in
    "$root"/*) echo "build: refusing, the output must be outside the repo" >&2; exit 2 ;;
esac
git filter-repo --version >/dev/null 2>&1 || { echo "build: git-filter-repo is not installed" >&2; exit 2; }
for f in allow_paths.txt replace_text.local.txt mailmap.local.txt; do
    [[ -s $root/release/$f ]] || { echo "build: release/$f is missing or empty" >&2; exit 2; }
done

git init -q -b main "$out"
git -C "$out" fetch -q --no-tags --update-head-ok "$root" "$commit:refs/heads/main"
cd "$out"
# --force: this is a new repo made above, not a clone filter-repo can recognise as fresh.
git filter-repo --force --quiet \
    --paths-from-file "$root/release/allow_paths.txt" \
    --replace-text "$root/release/replace_text.local.txt" \
    --replace-message "$root/release/replace_text.local.txt" \
    --mailmap "$root/release/mailmap.local.txt"
git reset -q --hard main
echo "built $out from $commit: main is $(git rev-parse main), $(git rev-list --count main) commits"
