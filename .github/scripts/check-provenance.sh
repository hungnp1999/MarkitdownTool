#!/usr/bin/env bash
# Check that every non-merge commit in BASE..HEAD names an allowed
# development environment (docs/DEVELOPMENT_PROVENANCE.md).
# Usage: check-provenance.sh BASE [HEAD]
set -euo pipefail

# Keep in sync with the environment table in docs/DEVELOPMENT_PROVENANCE.md.
allowed="cloud pc-01"

base="$1"
head="${2:-HEAD}"
fail=0

for sha in $(git rev-list --no-merges "$base..$head"); do
  short=$(git rev-parse --short "$sha")
  email=$(git log -1 --format=%ae "$sha")
  case "$email" in
    *"[bot]"*) echo "skip $short: bot author $email"; continue ;;
  esac
  label=$(git log -1 --format='%(trailers:key=Dev-Environment,valueonly,separator=%x20)' "$sha" | xargs)
  session=$(git log -1 --format='%(trailers:key=Claude-Session,valueonly,separator=%x20)' "$sha" | xargs)
  if [ -z "$label" ]; then
    echo "::error::$short has no Dev-Environment trailer"; fail=1; continue
  fi
  case " $allowed " in
    *" $label "*) ;;
    *) echo "::error::$short Dev-Environment '$label' is not an allowed environment"; fail=1; continue ;;
  esac
  if [ "$label" = cloud ] && [ -z "$session" ]; then
    echo "::error::$short is a cloud commit without a Claude-Session trailer"; fail=1; continue
  fi
  echo "ok $short: $label${session:+ $session}"
done

exit "$fail"
