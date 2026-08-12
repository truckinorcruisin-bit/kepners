#!/usr/bin/env bash
# trigger-and-wait.sh <workflow-file.yml> [input=value ...]
#
# Triggers a workflow_dispatch run via the gh CLI and blocks until it
# finishes, propagating its exit code. Used by run-full-pipeline.yml instead
# of a third-party marketplace action (see that file's header comment for why).
#
# `gh workflow run` does not return a run ID directly -- there's no built-in
# "trigger and wait" in gh itself (a long-standing open feature request:
# github.com/cli/cli/issues/12967) -- so this polls `gh run list` for the new
# run, filtered to ones created after this script started, to avoid ever
# grabbing a stale or unrelated run.
#
# Requires: GH_TOKEN and REPO environment variables already set (see the env:
# block in run-full-pipeline.yml), and the gh CLI (pre-installed on every
# GitHub-hosted runner).

set -euo pipefail

WORKFLOW_FILE="$1"
shift
INPUT_FLAGS=()
for kv in "$@"; do
  INPUT_FLAGS+=(-f "$kv")
done

echo "::group::Triggering $WORKFLOW_FILE"
BEFORE=$(date -u +%Y-%m-%dT%H:%M:%S)

gh workflow run "$WORKFLOW_FILE" --repo "$REPO" --ref main "${INPUT_FLAGS[@]}"

RUN_ID=""
for i in $(seq 1 20); do
  sleep 3
  CANDIDATE=$(gh run list --repo "$REPO" --workflow "$WORKFLOW_FILE" --branch main \
    --json databaseId,createdAt,event --limit 10 \
    --jq "[.[] | select(.event==\"workflow_dispatch\" and .createdAt>\"$BEFORE\")] | sort_by(.createdAt) | .[0].databaseId" \
    2>/dev/null || true)
  # Only accept a plain integer -- anything else (empty, "null", or malformed
  # output from an unexpected gh/jq version behavior) should keep polling or
  # fail loudly, never get silently passed to `gh run watch`.
  if [[ "$CANDIDATE" =~ ^[0-9]+$ ]]; then
    RUN_ID="$CANDIDATE"
    break
  fi
done

if [ -z "$RUN_ID" ]; then
  echo "::error::Could not find the triggered run for $WORKFLOW_FILE after 60 seconds. It may still be starting -- check the Actions tab manually, or this workflow's name/filename may have changed."
  exit 1
fi

echo "Found run $RUN_ID for $WORKFLOW_FILE -- watching until it finishes..."
echo "::endgroup::"

gh run watch "$RUN_ID" --repo "$REPO" --exit-status

