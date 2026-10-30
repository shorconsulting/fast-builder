---
description: Stop a build across every account, at once or after the work in hand
argument-hint: "[--run RUN | --all] [--drain] [--reason TEXT]"
---

# /fast-builder:stop

Arguments: `$ARGUMENTS`

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" stop $ARGUMENTS` then `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" status`. Say which runs are stopping and which lanes have confirmed. Lanes see it within a minute where the guard hook runs, and at their next tick otherwise; `--drain` lets each lane finish the slice in hand.

If this is run with no Claude available, the same thing can be done by adding a file named `STOP` to branch `fast-builder/bus` on the GitHub page.
