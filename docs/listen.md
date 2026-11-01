# Setting up the listener

A listener is how an account picks up a build nobody started on it. Each account is set up once, per repo. Pick by
arguments, default `--both`:

| Mode | How it wakes | Cost while idle | Needs |
|---|---|---|---|
| `--daemon` | launchd job polls `git ls-remote` once a minute | No tokens | A Mac that is on, logged into this account |
| `--routine` | A scheduled cloud agent, at most hourly | One short session an hour | Nothing local |
| `--both` | Daemon wakes the routine at once; hourly routine covers a Mac that is off | The same hourly session | Recommended, and the only instant cloud start |

## --off

Run `python3 "$FB" listen-uninstall --repo OWNER/NAME`. Disable the routine with RemoteTrigger `update`, setting `enabled` to false, and say that you did.

## Name the account

`--account NAME`, or use a short name for this login (lowercase, digits, hyphens): `aiuser2`, `core`. Each account on a
machine needs its own name. The Mac user is a good default.

## --daemon

    python3 "$FB" listen-install --account NAME --permission-mode MODE

It runs from a clone of the repo, copies `fb.py` to `~/.config/fast-builder/`, and loads a launchd agent that restarts at
login. Then run `python3 "$FB" doctor` and show it.

Local lanes run with the permission mode chosen here, default `auto`, and anything that would prompt is refused rather
than left hanging. Only use `bypassPermissions` if the user says so in as many words: a local lane would then run
whatever the plan on the bus leads it to, under this Mac user, with nothing asking. Say that before installing with it.

A local lane that hangs is still alive when its lane is freed. Before starting that lane again the listener ends the old
process and waits 10 seconds. If it will not stop, the lane is given back with a blocker, and no second process starts in
that clone.

A launchd agent runs while this Mac user is logged in, so fast user switching keeps several accounts alive at once, and
logging a user out stops theirs.

## --routine

Cloud lanes run through a routine on each account: it is how an account starts a cloud session with nobody at a terminal.
Load `/schedule` to get this account's environment list, and use its `anthropic_cloud` one. Then call RemoteTrigger
`action: create`, with `name` set to `fast-builder listener OWNER/NAME`, `cron_expression` `0 * * * *`, and `job_config.ccr`
holding that `environment_id`, `session_context.model` `claude-opus-5-5` (the session becomes a lane lead when it finds work),
`session_context.sources` this repo, and this as the prompt, with NAME filled in:

    git fetch -q origin +refs/heads/fast-builder/bus:refs/remotes/origin/fb-bus || exit 0
    git show refs/remotes/origin/fb-bus:kit/fb.py > /tmp/fb.py
    python3 /tmp/fb.py poll --account NAME
    If that printed NOTHING, end the session now without doing anything else.
    If it printed LANE, you hold that lane: do what the rest of its output says.

Set `session_context.allowed_tools` to `Bash, Read, Write, Edit, Glob, Grep, Agent`.

Keep that name and the `poll --account NAME` line as they are. They are how a listener proves a routine is this repo's and
whose it is, and it will not start a roster routine without them.

A new routine gets every claude.ai connector on the account, Gmail included. This one takes its direction from the repo,
so create it with `enabled: false`, then RemoteTrigger `action: update` with `{"clear_mcp_connections": true}`, check the
response shows `mcp_connections` empty, and only then update `enabled` to true. The server may move the cron minute; that
does not matter.

Record it, and prove this Mac can reach it:

    python3 "$FB" listen-install --account NAME --routine-id TRIGGER_ID [--no-local]
    python3 "$FB" routine-check --routine-id TRIGGER_ID

The daemon wakes the routine through this Mac user's `claude` login, which can differ from the login of the session you
are in. Check first by comparing `claude auth status` with the routine's owner. If the check says 404 or 400, the two
differ: fix it with `claude auth login` as the account that owns the routine, or create the routine under the CLI's login by running
the same RemoteTrigger calls through `claude -p --model haiku --allowedTools=RemoteTrigger,ToolSearch`. That login's
environment list can differ, so use its `anthropic_cloud` environment.

`listen-install` also puts the kit on the bus if it is missing, so run it before the routine's first fire: a routine with
no `kit/fb.py` to fetch cannot poll, and tells you so every hour.

`listen-install --routine-id` also writes the routine's id to `listeners/NAME.json` on the bus, so every Mac user listening
to this repo can find it. That is what makes swapping logins work. A listener's hourly beat leaves a different id there
alone unless a wake has just proved its own: this Mac's config can be the stale one.

## Swapping logins

A routine belongs to one login, and a Mac user has one `claude` login at a time. If you move a Mac user between accounts,
set each account up once: sign it in, then run `--both` or `--routine` above with its own `--account NAME`. After that:

- The listener follows the swap by itself. When a run needs a cloud lane it runs this account's routine, the one in its own
  config. If that starts, it stops there and tries nothing from the bus. A routine on another login answers 404, or 400
  for an id it cannot parse, and starts nothing, so it then walks the other routines on the bus, at most 12. An id on the
  bus must have the real shape, `trig_` and 24 letters or digits, because it goes into a prompt. Each must first pass a
  `get` showing it is on this login, is named for this repo and has one `poll --account NAME` line. It then runs that
  routine. The name it listens under comes from that line, never from the bus, and it changes only after its own routine
  answered 404 or 400. A `get` and a `run` each cost about half a cent of Haiku.
- One pass over a repo spends about 24 of those calls at most, so a stop never waits long behind them. A walk that finds
  nothing, or renames the listener, ends waking for that pass. The run woken longest ago goes first, so runs it did not
  reach go first in a later pass.
- A repo with no routine in its config yet starts a proved routine but keeps its name, because nothing has disproved the
  current one. After a rename every repo's routine is cleared, and each finds its own at its next wake.
- `python3 "$FB" switch` makes the same checks with `get`, so nothing starts. Run it after a swap to put local lanes and
  `status` under the right name before the next wake. `python3 "$FB" switch NAME` checks that one account and refuses
  unless this login owns a routine that carries that name. A repo it cannot read is listed under `unreachable` and finds
  its routine at its next wake.
- A login that was never set up has no routine on the bus: the wake posts one blocker saying so, however often it retries,
  and nothing starts.

An account that has run out of usage cannot wake anything, because the wake itself is a Claude call. Its lanes go quiet and
are freed after 45 minutes for another account, which starts from the lane's last saved handoff.

## No Mac

On an account with no Mac, skip the daemon and try a GitHub trigger: `action: create_webhook_trigger`, event `issues.opened`
on this repo, so the tracker issue a kickoff opens wakes it. It has not been proven: it needs an approval that was refused in
testing. If it is refused, say the wake-up is hourly.

Relay the routine's link and next run time. The account's GitHub connection must reach this repo.
