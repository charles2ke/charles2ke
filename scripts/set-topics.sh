#!/usr/bin/env bash
#
# set-topics.sh
#
# Applies a curated set of GitHub topics to charles2ke's repositories using
# the GitHub CLI. Topics are metadata, not files, so this script exists to
# make that one-time (or re-runnable) operation reviewable and repeatable
# instead of requiring 13 manual visits to the repo settings UI.
#
# Usage:
#   scripts/set-topics.sh [--dry-run] [repo-name]
#
# Examples:
#   scripts/set-topics.sh --dry-run          # preview all changes, no API calls
#   scripts/set-topics.sh                     # apply topics to every repo below
#   scripts/set-topics.sh --dry-run Nakshatra # preview a single repo
#   scripts/set-topics.sh Nakshatra           # apply to a single repo
#
# Requires:
#   - GitHub CLI (`gh`) installed and authenticated (`gh auth login`) with a
#     token that has the `public_repo` scope (or `repo` for private repos).
#
# Environment variables:
#   OWNER   GitHub owner/org whose repos will be updated (default: charles2ke)

set -euo pipefail

OWNER="${OWNER:-charles2ke}"

# Repo -> space-separated list of topics. Keep this list readable and easy to
# edit; add/remove entries here to change what gets applied.
declare -A REPO_TOPICS=(
  ["Agent-Chaos-Monkey"]="chaos-engineering ai-agents llm resilience-testing evals dotnet react copilot-studio"
  ["Message-Flow"]="chain-of-responsibility design-patterns dotnet csharp java middleware pipeline"
  ["design-patterns"]="design-patterns typescript software-architecture learning"
  ["tax-break"]="tax-calculator fintech typescript"
  ["Portfolio-Watcher"]="portfolio-tracker finance investing typescript"
  ["Night-Sky"]="astronomy stargazing generative-art javascript"
  ["OpenTrading"]="trading stock-market fintech javascript"
  ["GraphQL"]="graphql microservices api javascript"
  ["Nakshatra"]="ecommerce online-shopping csharp dotnet"
  ["basa"]="elder-care dashboard healthcare html"
  ["travel"]="travel html static-site"
  ["workout"]="fitness workout-tracker javascript"
  ["charles2ke"]="profile-readme"
)

# Preserve a stable, readable iteration order regardless of associative-array
# hashing order.
REPO_ORDER=(
  "Agent-Chaos-Monkey"
  "Message-Flow"
  "design-patterns"
  "tax-break"
  "Portfolio-Watcher"
  "Night-Sky"
  "OpenTrading"
  "GraphQL"
  "Nakshatra"
  "basa"
  "travel"
  "workout"
  "charles2ke"
)

DRY_RUN=0
TARGET_REPO=""

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
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
    echo "Error: 'gh' is not authenticated. Run 'gh auth login' (with the 'public_repo' scope) and try again." >&2
    exit 1
  fi
}

# Normalises a topic to GitHub's requirements: lowercase, and validates length.
# GitHub topic names must be <= 35 characters.
validate_topic() {
  local topic="$1"
  local normalized="${topic,,}"

  if [[ ${#normalized} -gt 35 ]]; then
    echo "Error: topic '${topic}' exceeds GitHub's 35 character limit." >&2
    return 1
  fi

  if [[ ! "$normalized" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "Error: topic '${topic}' contains invalid characters (only lowercase letters, digits and hyphens are allowed)." >&2
    return 1
  fi

  echo "$normalized"
}

apply_topics_for_repo() {
  local repo="$1"
  local topics="${REPO_TOPICS[$repo]}"
  local normalized_topics=()
  local topic
  local normalized

  for topic in $topics; do
    if ! normalized=$(validate_topic "$topic"); then
      echo "FAIL  ${OWNER}/${repo}: invalid topic '${topic}'"
      return 1
    fi
    normalized_topics+=("$normalized")
  done

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "DRY-RUN ${OWNER}/${repo}: would set topics -> ${normalized_topics[*]}"
    return 0
  fi

  local gh_args=(api --method PUT "repos/${OWNER}/${repo}/topics")
  for normalized in "${normalized_topics[@]}"; do
    gh_args+=(-f "names[]=${normalized}")
  done

  if gh "${gh_args[@]}" >/dev/null; then
    echo "OK    ${OWNER}/${repo}: topics set -> ${normalized_topics[*]}"
    return 0
  else
    echo "FAIL  ${OWNER}/${repo}: gh api call failed"
    return 1
  fi
}

main() {
  if [[ "$DRY_RUN" -eq 0 ]]; then
    check_prerequisites
  fi

  local repos=()
  if [[ -n "$TARGET_REPO" ]]; then
    if [[ -z "${REPO_TOPICS[$TARGET_REPO]+set}" ]]; then
      echo "Error: unknown repo '${TARGET_REPO}'. Known repos: ${REPO_ORDER[*]}" >&2
      exit 1
    fi
    repos=("$TARGET_REPO")
  else
    repos=("${REPO_ORDER[@]}")
  fi

  local failures=0
  for repo in "${repos[@]}"; do
    if ! apply_topics_for_repo "$repo"; then
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
