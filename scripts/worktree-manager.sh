#!/bin/bash
# Worktree manager for develop-loop
# Usage: scripts/worktree-manager.sh {create|assign|clean|preserve|list-preserved} <N> <slug> [reason]

ACTION=$1
N=$2
SLUG=$3
REASON=$4

WORKTREES_DIR=".worktrees"
STATUS_DIR=".status"

case "$ACTION" in
  create)
    BRANCH="tmp/issue-${N}-${SLUG}"
    WORKTREE_PATH="${WORKTREES_DIR}/issue-${N}-${SLUG}"
    mkdir -p "$WORKTREES_DIR"
    mkdir -p "$STATUS_DIR"
    git worktree add -b "$BRANCH" "$WORKTREE_PATH" HEAD
    echo "$WORKTREE_PATH"
    ;;
  assign)
    WORKTREE_PATH="${WORKTREES_DIR}/issue-${N}-${SLUG}"
    echo "$WORKTREE_PATH"
    ;;
  clean)
    WORKTREE_PATH="${WORKTREES_DIR}/issue-${N}-${SLUG}"
    git worktree remove "$WORKTREE_PATH" 2>/dev/null || true
    git branch -D "tmp/issue-${N}-${SLUG}" 2>/dev/null || true
    ;;
  preserve)
    WORKTREE_PATH="${WORKTREES_DIR}/issue-${N}-${SLUG}"
    mkdir -p "$STATUS_DIR"
    echo "$(date -Iseconds): Preserved $WORKTREE_PATH for issue $N ($SLUG): $REASON" >> "$STATUS_DIR/preserved-worktrees.log"
    echo "$WORKTREE_PATH preserved"
    ;;
  list-preserved)
    if [ -f "$STATUS_DIR/preserved-worktrees.log" ]; then
      cat "$STATUS_DIR/preserved-worktrees.log"
    else
      echo "No preserved worktrees"
    fi
    ;;
  *)
    echo "Usage: $0 {create|assign|clean|preserve|list-preserved} <N> <slug> [reason]"
    exit 1
    ;;
esac
