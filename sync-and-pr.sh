#!/usr/bin/env bash
# Automates the safe/reversible part of the "push to fork -> deploy to
# production" workflow documented in .github/workflows/deploy.yml:
#   1. Fast-forward local main to origin/main (your fork).
#   2. Merge upstream/main (colgre/wareraNL-bot) into it, same as the
#      "Merge branch 'colgre:main' into main" commits already in this
#      repo's history — mirrors the existing manual habit, doesn't change it.
#   3. Push the result back to origin (your fork).
#   4. Open (or create, if none exists yet) the pull request against
#      colgre/wareraNL-bot and open it in the browser.
#
# Deliberately NOT automated: merging that PR, and triggering the
# production deploy workflow (workflow_dispatch on deploy.yml). Both touch
# a shared repo / a live production bot and stay explicit actions you take
# yourself — see the bottom of this file for the one-line command for each.
#
# Requires: gh CLI, authenticated (`gh auth login`).

set -euo pipefail

GH="${GH_BIN:-$HOME/.local/bin/gh}"
UPSTREAM_REPO="colgre/wareraNL-bot"
BASE_BRANCH="main"

cd "$(git rev-parse --show-toplevel)"

if ! command -v "$GH" >/dev/null 2>&1; then
    echo "❌ gh CLI not found at $GH — install it or set GH_BIN." >&2
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "❌ Working tree has uncommitted changes — commit or stash them first." >&2
    exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "$BASE_BRANCH" ]]; then
    echo "❌ Not on '$BASE_BRANCH' (currently on '$current_branch'). Switch first." >&2
    exit 1
fi

fork_owner="$("$GH" api user --jq '.login')"

echo "==> Fetching origin and upstream..."
git fetch origin
git fetch upstream

echo "==> Fast-forwarding local $BASE_BRANCH to origin/$BASE_BRANCH..."
git merge --ff-only "origin/$BASE_BRANCH"

echo "==> Merging upstream/$BASE_BRANCH..."
if ! git merge --no-edit "upstream/$BASE_BRANCH"; then
    echo "❌ Merge conflict with upstream/$BASE_BRANCH — resolve manually (git status" >&2
    echo "   shows the conflicted files), commit, then re-run this script." >&2
    exit 1
fi

if [[ -z "$(git log "origin/$BASE_BRANCH..$BASE_BRANCH" --oneline)" ]]; then
    echo "==> Nothing new to push — local $BASE_BRANCH already matches origin/$BASE_BRANCH."
else
    echo "==> Pushing to origin/$BASE_BRANCH..."
    git push origin "$BASE_BRANCH"
fi

if [[ -z "$(git log "upstream/$BASE_BRANCH..$BASE_BRANCH" --oneline)" ]]; then
    echo "==> Fork is even with upstream — nothing to open a PR for."
    exit 0
fi

echo "==> Looking for an existing open PR..."
existing_url="$("$GH" pr list --repo "$UPSTREAM_REPO" \
    --head "${fork_owner}:${BASE_BRANCH}" --state open \
    --json url --jq '.[0].url' 2>/dev/null || true)"

if [[ -n "$existing_url" && "$existing_url" != "null" ]]; then
    pr_url="$existing_url"
    echo "==> Existing PR found: $pr_url"
else
    echo "==> Creating PR..."
    pr_url="$("$GH" pr create --repo "$UPSTREAM_REPO" \
        --base "$BASE_BRANCH" --head "${fork_owner}:${BASE_BRANCH}" --fill)"
    echo "==> Created: $pr_url"
fi

echo "==> Opening in browser..."
"$GH" pr view "$pr_url" --web || echo "(couldn't auto-open — open it manually: $pr_url)"

cat <<EOF

When you're ready:
  Merge:  $GH pr merge --repo $UPSTREAM_REPO --merge $pr_url
  Deploy: $GH workflow run deploy.yml --repo $UPSTREAM_REPO --ref $BASE_BRANCH -f confirm=ja
EOF
