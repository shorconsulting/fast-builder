# Fixer lane brief

Every run has a lane named `fixer`, added by `publish`. It owns no paths. Its work is the PRs other lanes got stuck on,
handed over with `fb.py stuck`, so those lanes keep building instead of stopping. You are its account orchestrator.

lane-lead.md still holds for the loop (`tick` first and between steps), waiting on subagents, the stop switch,
handoffs and autopilot. This brief replaces its build loop, its two tracks and its Finishing section.

## What you may touch

Only the files of the stuck PRs on your list. Each one's files are lent to this lane from the moment it is handed over
until it is resolved, and its own lane may not edit them meanwhile. The guard enforces both. If a fix needs one more
file of that PR's lane, borrow it first:

    python3 "$FB" stuck-widen --pr N --path src/pay/refund.test.ts

A file of any other lane cannot be borrowed. Ask that lane with `fb.py send`, or hand the PR back.

## The loop

1. `python3 "$FB" tick`. Its `stuck` field lists the PRs waiting and the ones you hold, and its instruction names the
   next step.
2. Take the oldest waiting PR: `python3 "$FB" stuck-take --pr N`. That records your account as its fixer on the bus
   and on the PR's GitHub comment.
3. Read why it is stuck (`stuck-list` shows the lane's reason and attempts) and the PR's checks and reviews. Then work
   on the PR's own branch (`git fetch origin <branch>`), never on a new one.
4. Route it with `fb.py route`, as any slice. A PR is handed over because its lane already failed at it, so treat it
   as `stalled`: the builder is `security-builder` (Opus) and the reviewer is `security-reviewer`, whatever the paths.
   The reviewer is never the agent that wrote the fix.
5. Push, then land it the way a lander would (`prs --lane fixer` lists every PR labelled `fb:stuck`), or re-task one
   lander for this lane as lane-lead.md describes.
6. Resolve it, which gives its files back to its lane:

       python3 "$FB" stuck-done --pr N --outcome merged --note "what was wrong"

   `returned` hands it back to its lane with your note as a request, for a fix only that lane can make or one that
   needs another lane's files. `closed` is for a PR that should not land; say in the note what replaces it.
7. Back to 1. Hold one PR at a time.

A PR that waits on a person (kind `question`, or a decision only a person can make): post a `needs-human` message with
`fb.py send --to human --kind needs-human`, then hand it back with `stuck-done --outcome returned --note "waits on a
person: <the question>"` and go on to the next one. Its lane takes the answer from there.

## When the queue is empty

`python3 "$FB" lane-state done --note "queue empty"`, write your handoff, and end the session. The next `fb.py stuck`
reopens the lane. It waits for this account to take it again while it could (15 minutes, or 60 if the job named it as
the fixer account), then goes to any account. That way the run's stuck work stays with one account.

Everything left waiting on a person: `lane-state blocked --note "what on"`.
