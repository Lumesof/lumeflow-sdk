#!/bin/bash
# workspace_status.sh
# This script is invoked by Bazel to gather workspace status information.
# The output (key-value pairs) becomes available as stamping variables.

# Check if we are inside a Git repository
if git rev-parse --is-inside-work-tree &>/dev/null; then
  # Get the short Git commit hash of the current HEAD
  GIT_COMMIT=$(git rev-parse --short HEAD)

  # Check if the working tree is dirty (has uncommitted changes)
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    # Append '-dirty' if there are uncommitted changes
    GIT_COMMIT="${GIT_COMMIT}-dirty"
  fi
else
  # Fallback if not in a Git repository (e.g., downloaded source archive)
  GIT_COMMIT="no-git-repo"
fi

# Output the Git commit as a STABLE variable.
# STABLE_ variables cause Bazel to re-run stamped actions if their value changes,
# which is desired for unique image tags based on code changes.
echo "STABLE_BUILD_SCM_REVISION ${GIT_COMMIT}"
echo "STABLE_BUILD_USER $(whoami)"

# You can add other useful info like:
# echo "BUILD_TIMESTAMP $(date +%s)" # This is a volatile variable; use with care for caching.
