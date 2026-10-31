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

A new routine gets every claude.ai connector on the account, Gmail included. This one takes its direction from the repo,
so create it with `enabled: false`, then RemoteTrigger `action: update` with `{"clear_mcp_connections": true}`, check the
response shows `mcp_connections` empty, and only then update `enabled` to true. The server may move the cron minute; that
does not matter.

Record it, and prove this Mac can reach it:

    python3 "$FB" listen-install --account NAME --routine-id TRIGGER_ID [--no-local]
    python3 "$FB" routine-check --routine-id TRIGGER_ID

The daemon wakes the routine through this Mac user's `claude` login, which can differ from the login of the session you
are in. Check first by comparing `claude auth status` with the routine's owner. If the check says 404, the two differ: fix
it with `claude auth login` as the account that owns the routine, or create the routine under the CLI's login by running
the same RemoteTrigger calls through `claude -p --model haiku --allowedTools=RemoteTrigger,ToolSearch`. That login's
environment list can differ, so use its `anthropic_cloud` environment.

`listen-install` also puts the kit on the bus if it is missing, so run it before the routine's first fire: a routine with
no `kit/fb.py` to fetch cannot poll, and tells you so every hour.

On an account with no Mac, skip the daemon and try a GitHub trigger: `action: create_webhook_trigger`, event `issues.opened`
on this repo, so the tracker issue a kickoff opens wakes it. It has not been proven: it needs an approval that was refused in
testing. If it is refused, say the wake-up is hourly.

Relay the routine's link and next run time. The account's GitHub connection must reach this repo.
