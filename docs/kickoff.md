# Kicking off a run

You are the planner. Everything you write here is the only context the other accounts get. Spend the effort here: a good
plan is what lets cheap models do the rest.

## 1. Settle the parameters

| Parameter | Default |
|---|---|
| `--where` | `cloud`. Use `local` only if the user said so. |
| `--autopilot` | `full`. `policy` follows `.github/autopilot.json`. |
| `--max-accounts` | The bus's `settings.json`, else 4. Setting it stores it for the repo. |
| `--code` | The repo name in capitals. A short code like `HPH` gives the file name the user is used to. |

Find the size of the job first, in units you can defend: open issues, unbuilt requirements, uncovered test cases, or
PRs still needed. Then:

- `--accounts N` given: use it, as long as it is within the maximum.
- `--deadline` or `--hours` given: `python3 "$FB" size --items COUNT --hours H [--max-accounts M]`, and use its pick.
- Neither: ask the user one question, how long they have, with AskUserQuestion. Then size it. If they want you to decide,
  use the listeners currently alive (`size` lists them), capped at the maximum.

`size` measures merged PRs a day and treats that as one account's rate. On a repo that already runs several accounts,
divide by how many were running, or pass `--rate`. If it reports `fits_deadline: false`, tell the user the best finish
time at the maximum and go on with the maximum.

    python3 "$FB" draft --goal "..." --accounts N [--max-accounts M] [--where W] [--autopilot A] [--deadline D] [--code C]

It prints the draft folder, run id and plan file name. Set `sizing` in the draft's `job.json` to one line on how you got
the number.

## 2. Read the repo

Use Explore or Haiku agents for the sweep; keep the conclusions, not the file dumps. You need: what exists, what is left,
open PRs and issues by area, how tests and merges work here, the ADRs or rules that bind everyone, and what has gone wrong before.

## 3. Split into lanes

    python3 "$FB" partition --accounts N [--items items.json] > DRAFT/lanes.json

`items.json` is a list of `{id, paths, weight}`, with weight as remaining work. Without it the split comes from directory
sizes. Then make it right:

- Balance on work left, not on file count.
- Each lane should be a coherent domain with few reasons to touch another's files. HPH's split: identity and tenancy,
  commerce, clinical, engagement.
- Rename lanes after their domain. Give each one `modules`.
- Size each lane to outlast the deadline. A lane that clears its list early is an account doing nothing.
- `steward: true` on exactly one lane. It owns everything unclaimed, main going red, CI machinery and shared config.
- Give generated files, lockfiles and other things every lane regenerates to `shared_ok`.
- Keep `digits`: each lane's last digits for migration or other sequence numbers.
- Every open PR belongs to exactly one lane; say which in the plan.

Lean toward putting a file a lot of work touches into a single lane. Where two lanes must change the same file, that is
a reason to redraw the line, or to make the second change wait.

## 4. Write the plan and lane rules

    python3 "$FB" render --draft DRAFT

This writes `lanes/<lane>.md` and the plan, named like `HPH-10-30-26-orchestration-plan.md`. Fill every `{{...}}` blank,
in both. For each lane the plan must let someone start cold, module by module: current state, requirements and where they
live, binding decisions, open PRs and issues, traps, and the first three slices. Numbers come with the commit they were
read at. Where a question is open, write the default to build.

Fill `## Overflow` too, and `validate` refuses a plan without it: what each lane takes next if it clears its list, lane by
lane. It is what a lane reads before it marks itself done.

A deadline the conductor can use is an ISO date or time, such as `2026-11-03T18:00Z`. If only some of the repo's checks say
whether main is healthy, list their names as `main_checks` in `job.json`. Otherwise any failed check on main counts.

## 5. Validate and publish

    python3 "$FB" validate --draft DRAFT
    python3 "$FB" publish --draft DRAFT

Publish refuses until every check passes. Fix the plan, do not weaken it. Moving a path to `shared_ok` to get past an
overlap is only right for files that are truly regenerated.

## 6. Start this account's lane

Open a tracker issue titled `[fast-builder] RUN: goal`, linking the plan on the bus, with `gh issue create` or your GitHub
tool. It is the humans' view, and it wakes any routine with an `issues.opened` trigger.

Tell the user the run is live: run id, lanes, where the plan is on the bus. Then show `status`. For a lane no listener
took within a minute or two, run `python3 "$FB" prompt` and give the user the paste-in prompt for it.

Unless the user said this account should only plan, join the steward lane and follow `lane-lead.md`:

    python3 "$FB" join --lane STEWARD

On Windows, use `py -3` in place of `python3`, unless this is a lane a listener started, which has its own `python3`.
