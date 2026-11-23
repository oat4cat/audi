#!/usr/bin/env bash
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

index_file="$(mktemp)"
trap 'rm -f "$index_file"' EXIT
rm -f "$index_file"

GIT_INDEX_FILE="$index_file" git read-tree --empty
GIT_INDEX_FILE="$index_file" git add --force data/auditories.json data/groups.json
tree="$(GIT_INDEX_FILE="$index_file" git write-tree)"
commit="$(printf 'auto: update generated data\n' | git commit-tree "$tree")"

git push --force origin "$commit:refs/heads/data"
