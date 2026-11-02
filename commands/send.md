---
description: Send an instruction or note to one lane, or to every account in a run
argument-hint: "--to LANE|all|conductor \"message\" [--run RUN]"
---

# /fast-builder:send

Arguments: `$ARGUMENTS`

Split the arguments into the target and the message, then run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" send --from human --to TARGET --kind note --body "MESSAGE"`, with `--run` if one was given. Lanes read it at their next tick.

On Windows, use `py -3` in place of `python3`.
