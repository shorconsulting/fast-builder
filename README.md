# Fast Builder

Runs one repo's build across several Claude accounts without them tripping over each other. You say what to build and
how many accounts, or how long you have. It sizes the team, splits the work into swim lanes it can prove do not overlap,
writes the plan, and starts the build through the repo so every listening account picks it up.

## Set up each account, once per repo

1. Add the plugin to each OS user, on macOS, Windows or Linux:

       /plugin marketplace add shorconsulting/marketplace
       /plugin install fast-builder@shorconsulting

2. In a clone of the repo: `/fast-builder:listen`

That installs a background listener, which uses no tokens while idle, and an hourly cloud routine that wakes at once when
the listener sees a run.

If you move an OS user between Claude logins, set each account up once. After that the listener finds the current login
itself, and `/fast-builder:switch` updates it straight away. See "Swapping logins" in [docs/listen.md](docs/listen.md).
Keep each routine's name and its `poll --account NAME` line as that page gives them: they are what proves whose it is.

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
| `/fast-builder:status` | Where the main session is, every lane, account and heartbeat, each lane's merged and open PRs, and anything waiting on a person |
| `/fast-builder:stop` | Stops every account. `--drain` lets lanes finish what is in hand. `--all` covers every run |
| `/fast-builder:pause`, `/fast-builder:resume` | Pauses; resuming puts stopped lanes, lanes that went quiet, and accounts marked out of usage back on offer |
| `/fast-builder:send --to LANE "text"` | Sends a note to one lane or `all` |
| `/fast-builder:join` | Takes an open lane in this session now |
| `/fast-builder:switch` | Points this OS user's listener at its current login |

To stop everything with no Claude at hand, add a file named `STOP` to the `fast-builder/bus` branch on GitHub, from a phone
if you like. `STATUS.md` on that branch shows the state, including each lane's PRs merged since the run began and open now.

## How it works

Accounts cannot message each other, so the repo is the bus: an orphan branch that holds every run's plan, lane rules,
claims, heartbeats, messages and stop switch. It runs no CI and needs no merge. See [docs/protocol.md](docs/protocol.md).

- **No overlap.** Each lane owns paths. Publishing refuses a split where any file, existing or future, is in two lanes.
  One steward lane owns the rest. Lanes also own last digits for migration numbers. The guard hook then denies an
  out-of-lane edit or push.
- **Orchestrators.** Each account's session runs its lane and dispatches subagents. Over all of them, a conductor frees a
  lane when its account goes quiet, closes the run, and answers requests between lanes. It is a lease, not an account, so
  it survives any account running out of usage. Most of it is code: it only spends tokens on a judgement call.
- **Red main.** The conductor reads the base branch's checks about every 5 minutes with `gh`. Lanes are told when main
  changes, the steward fixes it first, the rest keep building, and a person hears if it stays red over 2 hours. Green
  takes every check passing, and a re-run in place replaces what it re-ran.
- **Lost lanes.** A lane freed for going quiet does not go back to that account for 3 hours, unless a person names it with
  `/fast-builder:join --lane` or `/fast-builder:resume --run`. A claim no session joined is freed after 20 minutes, and a local
  lane out of restarts at once. A lane left open for 30 minutes tells a person. A lane that loses its claim pushes its own
  unpushed branches and a snapshot of the rest before it stops, so the next account starts from them.
- **Spreading lanes.** The listener that frees a lane never takes it in the same pass. For 10 minutes a freed lane waits
  for a listening account that holds no lane, and no account holds more than 2 lanes in a run (`--lane-cap` at kickoff).
- **Out of usage.** A local lane whose session prints a usage limit goes back at once, and its account is offered no
  lanes until the reset time the limit gave, however many days off, instead of restarting into the same limit. Restarts
  for any other exit come at 0, 1, 2, 4 and 8 minutes, so a network blip does not use them all up.
- **Lanes that are working.** A listener speaks on the bus for its own local lanes while their process is alive and their
  transcript, log or git is moving, so a slow network or a loaded computer does not get a working lane freed. A hung
  process moves none of those and is still freed. A listener starts no new local lane while it runs 2 already, or while
  the computer's load is over 2 per core (`listen-install --max-local-lanes`, `--max-load-per-cpu`).
- **One version.** A run records the version that published it. An older listener takes no lanes in it and does not
  conduct it, and a person is told about any older listener still heard from, since it frees and claims by older rules.
- **Running out of work.** Every plan says what each lane takes next if it clears its list. A lane reads that, then asks
  the conductor and waits for its answer, still working, before it marks itself done. A lane done early with time left
  raises a request by itself. A request nobody is left to read, or nobody has read in an hour, tells a person.
- **Done is not for good.** `fb.py reopen` hands a lane more work and, if it was marked done or blocked, puts it back on
  offer so a listener starts it again. The run does not close when every lane is done: the steward lane is reopened to
  check the product against the plan's Goal and every lane's Done means, reopens the lanes with gaps, and records the
  check with `fb.py final-check`. Only then does the run close.
- **Building ahead of CI.** Each lane runs two tracks. Builders open a PR and go straight to their next slice. One
  lander per lane waits on checks, merges what is green, rebases PRs that fall behind, and hands real failures back
  as fix slices. A lane is not done while any of its PRs are still open. The lander reads GitHub through REST
  (`fb.py prs`, `fb.py pr-wait`), so it works in cloud sessions, which refuse GraphQL. In a repo whose
  `.github/autopilot.json` says it has its own merge pipeline, the lander leaves merging to it.
- **Tokens.** `fb.py route` picks the model for every task: Opus for security-sensitive work and design, Sonnet for
  pattern work, Haiku for mechanical work, never Fable. Listening and waiting on CI use no tokens.
- **Autopilot.** On `full` no lane stops to ask. Reversible decisions are logged under "Decided without you". Deleting
  data, spending money, messaging people, credentials and legal or pricing calls are never done on autopilot, in any
  mode: that one item is parked for a person and the build carries on.

## What it runs and sends

- **A background job** (the launchd agent `com.shorconsulting.fast-builder.listener`, a Windows task or a systemd user service) runs `~/.config/fast-builder/fb.py` while the OS
  user is logged in. `/fast-builder:listen --off` removes it. It runs `git ls-remote` against your repo once a minute.
- **Headless `claude` sessions.** The listener starts `claude -p` for local lanes, with the permission mode you chose
  (default `auto`), and a short Haiku call to start a routine. Local lanes run on your computer under your user.
- **A cloud routine** on each account, created with your approval. It runs hourly and costs one short session each time,
  whether or not there is work. New routines get every connector on the account, so setup clears them.
- **Git pushes to your own repo.** Plans, lane rules, claims, heartbeats, handoffs, messages and routine ids go to the
  `fast-builder/bus` branch of the repo you run it in. Anyone who can read that repo can read them.
- **Hooks.** `hooks/hooks.json` wires session start, a pre-tool guard that denies out-of-lane edits, and a stop check.
- **Nothing else.** It sends no data to any service but GitHub and Claude, has no telemetry, and reads no credentials
  itself: `git`, `gh` and `claude` use their own logins.

## Limits

- The daemon runs on macOS (launchd), Windows (Task Scheduler) and Linux (a systemd user service). `--routine` needs no computer at all.
- To check a Windows or Linux computer, run `scripts/platform_check.py --real-service`. It uses a throwaway repo and spends no tokens.
- A repo set up with `/fast-builder:install-repo` before 0.1.9 has a hook that cannot start on Windows, so run that command again.
- Local listeners run while the computer is on and that OS user is logged in, and on Linux only with `loginctl enable-linger` once they log out.
- Each account needs a routine for instant cloud starts, created under the same login as that OS user's `claude` CLI,
  and its GitHub connection must reach the repo. `routine-check` proves the first.
- Claude usage limits are per account. A local lane that runs dry is freed at once. A cloud lane, or a local one whose
  limit message was not seen, goes quiet and is freed after 45 minutes.
- The version gate only works from 0.1.12 on: a listener older than that does not read it, so update every account's
  listener by hand once. A 0.1.12 listener brings the bus's `kit/fb.py`, which cloud lanes run, up to its own version.
- A lead with subagents must loop on `fb.py wait` or it looks quiet too. That is the lead's own instructions, not something
  the tool can enforce: a lead stuck on a foreground subagent in a cloud session without the repo hook beats nothing.
- Red main is only seen by a lease holder with `gh` logged in. Without one, main reads `unknown`, or stays at its last
  known state. Red is a check ending in `failure`, `timed_out` or `startup_failure`. Green is every check ending in
  `success`, `neutral` or `skipped`, with at least one success and every name in `main_checks` seen. Anything else,
  including `cancelled`, `stale`, `action_required` and checks still running, leaves main as it was. Rows sharing a name
  count one at a time only if they share an app and a check suite, the later one started after the earlier one ended, and
  it has a verdict to give or is still running; `skipped`, `neutral` and `cancelled` replace nothing, nor does a time that
  is unreadable, ends before it begins or is in the future. Otherwise the worst of them stands, because a false green
  tells everyone main recovered. So main reads red on what may be stale: a re-run in place keeps its suite and clears a
  failure, but a workflow dispatched again gets a new suite, and these fields cannot tell that from another workflow with
  the same job name. Its old failure stands until a commit lands or the job is re-run in place, and a scheduled workflow
  that has failed on a quiet head can do this: `main_checks` leaves it out. Two jobs with one name in one suite, one after
  the other, read as a re-run. A reading across two commits is thrown away. All of a head's pages must arrive in 60 seconds, about 3,000 rows,
  so a head larger than that reads `unknown`, and a person hears after about 2 hours of it. A deadline in free text, such as `Friday`, reads
  as no deadline.
- An account that hits a limit frees its lane at once, through Claude Code's `StopFailure` hook, which spends no tokens. Its current branch and that lane's changed files go to a new branch under `fast-builder/wip/`, and it is offered no lanes until its limit resets, or for 3 hours when no reset time was seen, unless a person runs `/fast-builder:resume`. A brief 429 looks the same, so that is the way back. Files that look like secrets, other lanes' files, huge files and separate repos stay on that computer, and so does a subagent's worktree. The lane's handoff names what stayed. These branches are not cleaned up. A repo set up with `/fast-builder:install-repo` needs it run again for cloud lanes to do this, and the hook has not yet been seen firing on a real limit.
- The main session is whichever account published the run. If that account runs out while holding a lane, a person is told. A session that only planned and holds no lane has nothing to report this, and moving the role to another account is not built yet.
- The 3-hour rule cannot tell an account out of usage from a session that hung, so a hung local lane goes to another
  account too, and its old process stays until it ticks or the run stops. With no other account, a person hears at 30 minutes.
- A lane reopened with `fb.py reopen` restarts only where a listener runs: a cloud run needs an account listening with a
  routine, as any open lane does. With none, a person is told after 30 minutes.
- The final check is a model's judgement of the product against the plan. A run published before 0.1.14 closes without one.
- All of this is in the copy of `fb.py` each account runs. A listener gets it at `/fast-builder:listen`, and cloud lanes at
  the next kickoff. While an older copy holds the conductor lease, main goes unwatched and nothing is raised, and an older
  listener ignores the 3-hour rule. An older session reads requests without marking them read, so a person can hear of one
  it did read.
- The guard hook sees Claude's tools, and `check-diff` catches a shell edit at push time. It is a guardrail for
  well-meaning builders, not a sandbox: a determined shell command can get past it.
- Cloud lanes fetch `fb.py` from the bus, because cloud sessions get no user plugins. Local lanes use the copy on the computer.
- Anyone who can push to the bus can start work on listening accounts, read every routine id on it, and change the `fb.py`
  that cloud lanes run. Listen only to repos whose write access you control.
- A bus writer can list routine ids, but that alone cannot rename a healthy listener or start another routine, and a
  listener whose own routine answers never tries the list. Once it does not, a listed routine runs only if it has the
  real id shape, is on this login and is a fast-builder listener for this repo, and that routine says whose it is. A bus
  writer can make such a listener spend up to 12 Haiku calls a pass, or crowd real entries out of those 12. The blocker
  then says how many went untried, and `/fast-builder:switch NAME` still reaches that name.
- A swap is noticed at the next cloud wake, or at once with `/fast-builder:switch`. Until then local lanes claim under the
  previous account's name. A repo with no routine of its own yet starts a proved one but keeps the old name.
- Check that your use of several accounts fits the terms that apply to them. Fast Builder coordinates accounts you already
  have. It is not a way around usage limits.

## Tests

    python3 scripts/test_fb.py
