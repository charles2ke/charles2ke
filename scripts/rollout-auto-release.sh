#!/usr/bin/env bash
#
# rollout-auto-release.sh
#
# Adds the thin "Weekly release" caller workflow to every repository owned by
# charles2ke, so they all share the reusable auto-release workflow kept in
# charles2ke/charles2ke. Each repository gets its own cron minute so 20+
# scheduled runs do not fire at the same moment.
#
# Usage:
#   scripts/rollout-auto-release.sh [--dry-run] [--direct] [repo-name]
#
# Examples:
#   scripts/rollout-auto-release.sh --dry-run          # preview every repo
#   scripts/rollout-auto-release.sh --dry-run travel   # preview a single repo
#   scripts/rollout-auto-release.sh                    # open a PR per repo
#   scripts/rollout-auto-release.sh --direct travel    # commit straight to main
#
# Requires:
#   - Bash 4 or later.
#   - For non-dry runs, GitHub CLI (`gh`) installed and authenticated
#     (`gh auth login`) with a token that can push branches and open pull
#     requests (`repo` scope, plus `workflow` to write files under
#     .github/workflows/).
#
# Environment variables:
#   OWNER   GitHub owner whose repos are updated (default: charles2ke)
#   BRANCH  Branch name used for the pull request (default: add-auto-release)

set -euo pipefail

OWNER="${OWNER:-charles2ke}"
BRANCH="${BRANCH:-add-auto-release}"
WORKFLOW_PATH=".github/workflows/weekly-release.yml"

# Repositories that should cut weekly releases. Keep this list in the same
# order as the cron stagger below so the schedule stays easy to read.
REPO_ORDER=(
  "5-Mins"
  "Advantage"
  "Agent-Chaos-Monkey"
  "baby-model"
  "basa"
  "crabs"
  "design-patterns"
  "GitDb"
  "GraphQL"
  "Message-Flow"
  "Nakshatra"
  "Night-Sky"
  "OpenTrading"
  "platform-shared"
  "Portfolio-Watcher"
  "social"
  "tax-break"
  "TitoOS"
  "travel"
  "workout"
  "X-Big-Brother"
)

DRY_RUN=0
DIRECT=0
TARGET_REPO=""

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    --direct)
      DIRECT=1
      ;;
    -h|--help)
      sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      if [[ -n "$TARGET_REPO" ]]; then
        echo "Error: multiple repo names provided ('$TARGET_REPO' and '$arg')." >&2
        exit 1
      fi
      TARGET_REPO="$arg"
      ;;
  esac
done

check_prerequisites() {
  if ! command -v gh >/dev/null 2>&1; then
    echo "Error: GitHub CLI ('gh') is not installed. Install it from https://cli.github.com/ and try again." >&2
    exit 1
  fi

  if ! gh auth status >/dev/null 2>&1; then
    echo "Error: 'gh' is not authenticated. Run 'gh auth login' (with the 'repo' and 'workflow' scopes) and try again." >&2
    exit 1
  fi
}

repo_index() {
  local repo="$1"
  local index=0
  local candidate

  for candidate in "${REPO_ORDER[@]}"; do
    if [[ "$candidate" == "$repo" ]]; then
      echo "$index"
      return 0
    fi
    index=$((index + 1))
  done

  echo "Error: unknown repo '${repo}'. Known repos: ${REPO_ORDER[*]}" >&2
  return 1
}

# Spread the runs across Monday morning: every repo gets its own 20-minute
# slot starting at 07:00 UTC, wrapping onto the next hour after three repos.
cron_for_repo() {
  local index="$1"
  local minute=$(((index % 3) * 20))
  local hour=$((7 + index / 3))

  printf '%d %d * * 1' "$minute" "$hour"
}

workflow_for_repo() {
  local cron="$1"

  cat <<YAML
name: Weekly release

# Calls the reusable auto-release workflow kept in ${OWNER}/charles2ke. It cuts
# a release only when this repository's default branch has unreleased work that
# has settled. Managed by scripts/rollout-auto-release.sh in ${OWNER}/charles2ke.

on:
  schedule:
    - cron: "${cron}"
  workflow_dispatch:
    inputs:
      dry-run:
        description: Report the decision without creating a release.
        type: boolean
        default: false

permissions:
  contents: write

jobs:
  release:
    uses: ${OWNER}/charles2ke/.github/workflows/auto-release.yml@main
    permissions:
      contents: write
    with:
      dry-run: \${{ inputs.dry-run || false }}
YAML
}

roll_out_repo() {
  local repo="$1"
  local index
  local cron
  local workflow

  if ! index=$(repo_index "$repo"); then
    echo "FAIL  ${OWNER}/${repo}: not in the rollout list"
    return 1
  fi

  cron=$(cron_for_repo "$index")
  workflow=$(workflow_for_repo "$cron")

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "DRY-RUN ${OWNER}/${repo}: would add ${WORKFLOW_PATH} (cron '${cron}')"
    echo "$workflow" | sed 's/^/          /'
    return 0
  fi

  local default_branch
  if ! default_branch=$(gh repo view "${OWNER}/${repo}" --json defaultBranchRef --jq '.defaultBranchRef.name'); then
    echo "FAIL  ${OWNER}/${repo}: could not read the default branch"
    return 1
  fi

  local target_branch="$default_branch"
  if [[ "$DIRECT" -eq 0 ]]; then
    target_branch="$BRANCH"
    if ! gh api --method POST "repos/${OWNER}/${repo}/git/refs" \
      -f "ref=refs/heads/${target_branch}" \
      -f "sha=$(gh api "repos/${OWNER}/${repo}/git/ref/heads/${default_branch}" --jq '.object.sha')" \
      >/dev/null 2>&1; then
      echo "NOTE  ${OWNER}/${repo}: branch '${target_branch}' already exists, reusing it"
    fi
  fi

  local existing_sha
  existing_sha=$(gh api "repos/${OWNER}/${repo}/contents/${WORKFLOW_PATH}?ref=${target_branch}" --jq '.sha' 2>/dev/null || true)

  local args=(api --method PUT "repos/${OWNER}/${repo}/contents/${WORKFLOW_PATH}"
    -f "message=Add weekly auto-release workflow"
    -f "branch=${target_branch}"
    -f "content=$(printf '%s\n' "$workflow" | base64 | tr -d '\n')")
  if [[ -n "$existing_sha" ]]; then
    args+=(-f "sha=${existing_sha}")
  fi

  if ! gh "${args[@]}" >/dev/null; then
    echo "FAIL  ${OWNER}/${repo}: could not write ${WORKFLOW_PATH}"
    return 1
  fi

  if [[ "$DIRECT" -eq 1 ]]; then
    echo "OK    ${OWNER}/${repo}: committed ${WORKFLOW_PATH} to ${target_branch} (cron '${cron}')"
    return 0
  fi

  if gh pr create --repo "${OWNER}/${repo}" \
    --head "$target_branch" \
    --base "$default_branch" \
    --title "Add weekly auto-release workflow" \
    --body "Calls the reusable auto-release workflow in ${OWNER}/charles2ke. It cuts a release only when this repository has unreleased, settled work on ${default_branch}." \
    >/dev/null 2>&1; then
    echo "OK    ${OWNER}/${repo}: pull request opened (cron '${cron}')"
  else
    echo "OK    ${OWNER}/${repo}: workflow pushed to '${target_branch}' (pull request already exists)"
  fi
}

main() {
  if [[ "$DRY_RUN" -eq 0 ]]; then
    check_prerequisites
  fi

  local repos=()
  if [[ -n "$TARGET_REPO" ]]; then
    repo_index "$TARGET_REPO" >/dev/null
    repos=("$TARGET_REPO")
  else
    repos=("${REPO_ORDER[@]}")
  fi

  local failures=0
  for repo in "${repos[@]}"; do
    if ! roll_out_repo "$repo"; then
      failures=$((failures + 1))
    fi
  done

  if [[ "$failures" -gt 0 ]]; then
    echo "Completed with ${failures} failure(s)." >&2
    exit 1
  fi

  echo "Completed successfully."
}

main
