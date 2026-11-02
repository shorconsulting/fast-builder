---
description: See every run, lane, account, listener and anything waiting on a person
argument-hint: "[--run RUN]"
---

# /fast-builder:status

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" status $ARGUMENTS`. Say first where the main session is, from `main_session` (account and computer), and say so plainly if `out_of_usage` is set on it. Then summarize in a short table: lane, account, status, last heartbeat. Name any account under the top-level `out_of_usage`. Then list anything under `needs_human`, and any lane with no account. For a finished run also run `report --run RUN` and give its "Decided without you" list.

On Windows, use `py -3` in place of `python3`.
