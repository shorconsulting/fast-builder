---
description: Resume a paused or stopped build; stopped lanes go back on offer to listeners
argument-hint: "[--run RUN | --all]"
---

# /fast-builder:resume

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" resume $ARGUMENTS`, then `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" status`. Report which lanes are running and which are open. For open lanes with no listener to take them, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" prompt` and give the prompts to paste.

On Windows, use `py -3` in place of `python3`.
