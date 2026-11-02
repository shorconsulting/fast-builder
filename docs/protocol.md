# Protocol

The bus is the orphan branch `fast-builder/bus`, in the repo being built. Its layout is at the top of `scripts/fb.py`.

## Why a branch

- Accounts cannot message each other. Git is the one thing all of them can reach, including cloud sessions, whose
  GitHub proxy does not limit which branches a push can update.
- It has no workflows, so writing to it runs no CI.
- It needs no merge, so branch protection and the merge gate on main do not apply and main stays clean.
- `git push` refuses a non-fast-forward. That refusal is the compare-and-swap: every write is fetch, change, push,
  repeat on rejection, so two accounts claiming a lane cannot both win.
- The branch's history is the audit log of the run.

If a repo's rulesets restrict branch creation, allow `fast-builder/**`.

## Lifecycle

1. `draft` makes the run id and plan name. `size` picks the account count if nobody set one.
2. `partition` proposes lanes. The planner adjusts them to the domain. `render` writes each lane's rules and a plan
   skeleton. The planner fills the plan in.
3. `publish` runs `validate` and refuses a run with overlapping lanes, more accounts than the maximum, a lane without
   rules, a lane missing from the plan, or an unfilled blank. Then it pushes everything in one commit.
4. Listeners see the bus move. For a local run they claim a lane and start `claude -p` in its own clone. For a cloud run
   they fire the account's routine, and the routine claims from inside the cloud. Whatever is left is printed by
   `prompt`, to paste. (`claude --cloud` is not used: it needs a real terminal, and can stop at interactive dialogs.)
5. Each lane loops on `tick`. The conductor frees quiet lanes, watches main, and closes the run.
6. A freed lane's beat keeps `freed_from`. The next claim turns it into `took_from`, and `join` replaces that with one
   `takeover` message to a person and puts how to open the session in the claim's `open`. So a lane that never starts is
   no takeover, and neither is an account retaking its own lane. A `takeover` is news, not a request, so it stays out of
   "Waiting on a person".

## Control states

`running`, `paused`, `draining` (finish what is in hand), `stopping` (now), `stopped`, `finished`. A file named `STOP`
at the top of the bus, or in a run's folder, overrides all of them. It can be added from the GitHub web page.

## Enforcement, by layer

| Layer | What it does | Where it runs |
|---|---|---|
| `validate` at publish | Proves no file is in two lanes, against the real file list and by comparing globs for paths that do not exist yet | Kickoff |
| Guard hook | Denies out-of-lane edits and pushes, and all work once stopped, for subagents too | Local, or cloud after `install-repo` |
| `check-diff` | Proves a branch stays in lane | Any lane, before a push |
| `tick` | Tells the lane what to do | Everywhere; the only layer in a cloud session without the repo install |

## Trust

Anyone who can push to the bus branch can start work on accounts that listen to the repo, so listen only to repos whose
write access you control. The roster in `listeners/` only suggests routine ids. A listener's own routine comes from its
config and goes first, so a healthy listener tries nothing from the bus. An id goes into a Haiku prompt with the whole
`RemoteTrigger` tool granted, so one from the bus must be `trig_` and 24 letters or digits, and any other
`RemoteTrigger` call in that session fails it. That is noticed after the call, not prevented. A `run` that answered 200
still counts as started, because it did, but nothing follows from it: no rename and no saved id, and a person is told.
Any other id must pass a `get` on this login, and the listener reads the raw tool result, not Haiku's reply, for the
routine's name and its `poll --account NAME` line. That is what says the routine is this repo's and whose it is, and a
bus writer cannot edit a routine. The listener renames itself only after its own routine answered 404 or 400. A bus
writer can still spend up to 12 Haiku calls a pass on a listener whose routine is gone, or fill those 12 places and hide
a real swap until those rows are removed. The blocker then says how many went untried, and `switch NAME` still reaches
that name's row. The listener puts no free text from the bus on a command line: names, model and base branch must match
strict patterns, and the prompt is a fixed template. The conductor's `gh api` call takes the repo from the clone's remote
and only a pattern-checked, percent-encoded base branch from the bus. Check names are repo data: they are only compared
and printed, cleaned and capped first, here and again when read back from `main.json`. A bus with a symlink on it is refused. Local lanes run the `fb.py`
that `listen-install` copied to the computer. Cloud lanes cannot, so they run the bus's copy, which is one more reason the
repo's write access matters. A bus file that is not the object it should be reads as empty, except `control.json`, which
then reads as stopping, as an unknown state does. One bad run cannot stop another run's stop being handled. A lane's
model reads the plan, so a hostile plan could still mislead it. That is why the listener's local permission mode
defaults to `auto` and `bypassPermissions` has to be chosen each time it is installed: a reinstall that does not name it
goes back to `auto` and says so.
