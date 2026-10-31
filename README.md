# Fast Builder

Runs one repo's build across several Claude accounts without them tripping over each other. You say what to build and
how many accounts, or how long you have. It sizes the team, splits the work into swim lanes it can prove do not overlap,
writes the plan, and starts the build through the repo so every listening account picks it up.

## Set up each account, once per repo

1. Add the plugin to each Mac user:

       /plugin marketplace add shorconsulting/marketplace
       /plugin install fast-builder@shorconsulting

2. In a clone of the repo: `/fast-builder:listen`

That installs a launchd listener, which uses no tokens while idle, and an hourly cloud routine that wakes at once when
the listener sees a run.

If you move a Mac user between Claude logins, set each account up once. After that the listener finds the current login
itself, and `/fast-builder:switch` updates it straight away. See "Swapping logins" in [docs/listen.md](docs/listen.md).

Optional, once per repo: `/fast-builder:install-repo` opens a PR adding the hooks that let cloud lanes enforce stop and
lane rules on every tool call. Without it, cloud lanes obey them through their own loop, so a stop lands at their next
work item rather than within a minute.

## Run a build

    /fast-builder:kickoff "Finish the channel backlog" --hours 48

| Parameter | Default | Meaning |
|---|---|---|
| `--accounts N` | Sized from `--deadline`/`--hours`, or it asks how long you have | Accounts for this run |
| `--max-accounts M` | Stored per repo, else 4 | Most accounts any run may use |
| `--deadline WHEN`, `--hours H` | None | Time to finish in; picks the fewest accounts that make it |
| `--where cloud or local` | `cloud` | Where the lanes run |
| `--autopilot full or policy` | `full` | `full`: nobody is asked anything. `policy`: follow `.github/autopilot.json` |
| `--code HPH` | Repo name | Gives `HPH-10-30-26-orchestration-plan.md` |

## While it runs

| Command | Does |
|---|---|
| `/fast-builder:status` | Every lane, account and heartbeat, and anything waiting on a person |
| `/fast-builder:stop` | Stops every account. `--drain` lets lanes finish what is in hand. `--all` covers every run |
| `/fast-builder:pause`, `/fast-builder:resume` | Pauses; resuming puts stopped lanes back on offer |
| `/fast-builder:send --to LANE "text"` | Sends a note to one lane or `all` |
| `/fast-builder:join` | Takes an open lane in this session now |
| `/fast-builder:switch` | Points this Mac user's listener at its current login |

To stop everything with no Claude at hand, add a file named `STOP` to the `fast-builder/bus` branch on GitHub, from a phone
if you like. `STATUS.md` on that branch shows the state.

## How it works

Accounts cannot message each other, so the repo is the bus: an orphan branch that holds every run's plan, lane rules,
claims, heartbeats, messages and stop switch. It runs no CI and needs no merge. See [docs/protocol.md](docs/protocol.md).

- **No overlap.** Each lane owns paths. Publishing refuses a split where any file, existing or future, is in two lanes.
  One steward lane owns the rest. Lanes also own last digits for migration numbers. The guard hook then denies an
  out-of-lane edit or push.
- **Orchestrators.** Each account's session runs its lane and dispatches subagents. Over all of them, a conductor frees a
  lane when its account goes quiet, closes the run, and answers requests between lanes. It is a lease, not an account, so
  it survives any account running out of usage. Most of it is code: it only spends tokens on a judgement call.
- **Tokens.** `fb.py route` picks the model for every task: Opus for security-sensitive work and design, Sonnet for
  pattern work, Haiku for mechanical work, never Fable. Listening and waiting on CI use no tokens.
- **Autopilot.** On `full` no lane stops to ask. Reversible decisions are logged under "Decided without you". Deleting
  data, spending money, messaging people, credentials and legal or pricing calls are never done on autopilot, in any
  mode: that one item is parked for a person and the build carries on.

## What it runs and sends

- **A launchd agent** on macOS, `com.shorconsulting.fast-builder.listener`, runs `~/.config/fast-builder/fb.py` while the Mac
  user is logged in. `/fast-builder:listen --off` removes it. It runs `git ls-remote` against your repo once a minute.
- **Headless `claude` sessions.** The listener starts `claude -p` for local lanes, with the permission mode you chose
  (default `auto`), and a short Haiku call to start a routine. Local lanes run on your Mac under your user.
- **A cloud routine** on each account, created with your approval. It runs hourly and costs one short session each time,
  whether or not there is work. New routines get every connector on the account, so setup clears them.
- **Git pushes to your own repo.** Plans, lane rules, claims, heartbeats, handoffs, messages and routine ids go to the
  `fast-builder/bus` branch of the repo you run it in. Anyone who can read that repo can read them.
- **Hooks.** `hooks/hooks.json` wires session start, a pre-tool guard that denies out-of-lane edits, and a stop check.
- **Nothing else.** It sends no data to any service but GitHub and Claude, has no telemetry, and reads no credentials
  itself: `git`, `gh` and `claude` use their own logins.

## Limits

- macOS only for the daemon, because it uses launchd. `--routine` needs no Mac.
- Local listeners run while the Mac is on and that Mac user is logged in.
- Each account needs a routine for instant cloud starts, created under the same login as that Mac user's `claude` CLI,
  and its GitHub connection must reach the repo. `routine-check` proves the first.
- Claude usage limits are per account. A lane that runs dry goes quiet, and is freed after 45 minutes.
- The guard hook sees Claude's tools, and `check-diff` catches a shell edit at push time. It is a guardrail for
  well-meaning builders, not a sandbox: a determined shell command can get past it.
- Cloud lanes fetch `fb.py` from the bus, because cloud sessions get no user plugins. Local lanes use the copy on the Mac.
- Anyone who can push to the bus can start work on listening accounts, and can list routine ids a listener will try to
  start under its own login. Listen only to repos whose write access you control.
- A swap is noticed at the next cloud wake, or at once with `/fast-builder:switch`. Until then local lanes claim under the
  previous account's name.
- Check that your use of several accounts fits the terms that apply to them. Fast Builder coordinates accounts you already
  have. It is not a way around usage limits.

## Tests

    python3 scripts/test_fb.py
