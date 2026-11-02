---
description: Add the repo-side hooks that let cloud sessions enforce lanes and stops, in one pull request
---

# /fast-builder:install-repo

Cloud sessions do not load user plugins, but they do run hooks and agents committed to the repo. Without this, a cloud lane obeys stop and lane rules through its `tick` loop only. With it, the guard hook runs there too.

1. From a fresh clone, on a new branch `fast-builder/install`, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" install-repo`.
2. Commit the files it lists and open a pull request explaining what they do. Hooks do nothing in a session that is not a lane. Follow the repo's own rules on who may land `.claude/` changes.

The hooks run a copy of `fb.py` committed to the repo, so upgrading the plugin does not reach them. Run this again after an upgrade.

On Windows, use `py -3` in place of `python3`.
