---
description: Point this Mac user's listener at the account its claude login now belongs to
argument-hint: "[NAME]"
---

# /fast-builder:switch

Arguments: `$ARGUMENTS`

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fb.py" switch $ARGUMENTS` and show the result. With no name it finds the login
by asking which routine on the bus this login can see. If it fails, this login has never been set up: follow
`${CLAUDE_PLUGIN_ROOT}/docs/listen.md` for it once. See "Swapping logins" there.
