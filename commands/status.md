---
description: See every run, lane, account, listener and anything waiting on a person
argument-hint: "[--run RUN]"
---

# /fast-builder:status

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" status $ARGUMENTS`. Summarize in a short table: lane, account, status, last heartbeat. Add how to open each lane, from its `open`. Then list anything under `needs_human`, the latest `takeovers`, and any lane with no account. For a finished run also run `report --run RUN` and give its "Decided without you" list.

On Windows, use `py -3` in place of `python3`.
