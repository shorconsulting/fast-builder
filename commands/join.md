---
description: Take an open lane in a running build, here and now, without waiting for the listener
argument-hint: "[--run RUN] [--lane LANE] [--account NAME]"
---

# /fast-builder:join

Arguments: `$ARGUMENTS`

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" join $ARGUMENTS` from a clone of the repo that no other session is working in. If this checkout belongs to another Mac user or another session is using it, make a fresh clone first. It prints your lane rules, handoff and the plan. If it says the lane is taken or nothing is open, say so and stop.
2. `export FB="${CLAUDE_PLUGIN_ROOT}/scripts/fb.py"`, then read `${CLAUDE_PLUGIN_ROOT}/docs/lane-lead.md` and follow it. This session is now that lane's account orchestrator.

On Windows, use `py -3` in place of `python3`.
