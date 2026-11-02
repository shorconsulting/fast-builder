---
description: Resume a paused or stopped build, or put lanes that went quiet back on offer on a running one
argument-hint: "[--run RUN | --all]"
---

# /fast-builder:resume

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" resume $ARGUMENTS`, then `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" status`. With no `--run` it takes this session's run, or else the one run that could resume. A running run with a lane that lost its account to going quiet counts, and so does `--all`, which still leaves a stopped run alone. Report which lanes are running and which are open. For open lanes with no listener to take them, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" prompt` and give the prompts to paste.
