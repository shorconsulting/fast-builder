#!/usr/bin/env python3
"""tests.skip_duplicate_main: skip re-running the tests on the default branch after a merge, when the pull request
already passed them on exactly the same code.

Two halves, both in the repo's test workflow:
  - the workflow's `run-name` puts the commit GitHub actually tested on a pull request (the PR merged into its
    base, `github.sha`) at the start of the run's title: "[tested <sha>] ...". Only that workflow file can set
    its own runs' titles, so another workflow can't fake it;
  - on `push` to the default branch, `python3 .github/autopilot/skip_duplicate_main.py test.yml` (this file) writes
    `skip=true` to $GITHUB_OUTPUT only when all of these hold (anything else, including any error, says `false`):
      1. .github/autopilot.json on the pushed commit has "tests": {"skip_duplicate_main": true};
      2. the pushed commit is the merge of exactly one pull request into the default branch;
      3. that pull request changed nothing under .github/ (so its runs used the default branch's test workflow);
      4. a passing pull_request run of this test workflow for the pull request's head says it tested a commit
         whose files are exactly the pushed commit's (the same git tree).
    A repo with a merge queue has a shorter path (CI cost rule ci-queue-then-main): the queue tests a candidate
    commit and that exact commit is what lands, so when a passing `merge_group` run of this workflow exists for the
    pushed commit itself (same sha, same workflow file), the push run skips on that alone -- no tree to compare, no
    pull request to inspect, and a batch of several pull requests that landed as one push skips too.

A skip is a step automation drops on a person's behalf, so it is never silent: the run's summary says "Decided without
you", why, and how to undo it (run the tests anyway, or turn the setting off), and so does a comment on the merged pull
request when the push merged exactly one (a batch the queue landed as one push has only the summary).

Needs GITHUB_TOKEN with contents: read, pull-requests: write (the comment), actions: read. Standard library only.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate import GitHub  # noqa: E402

ZERO = "0" * 40
TESTED_RE = re.compile(r"^\[tested ([0-9a-f]{40})\]")


def wants_skip(checkout):
    try:
        data = json.loads((Path(checkout) / ".github" / "autopilot.json").read_text(encoding="utf-8"))
        return isinstance(data, dict) and (data.get("tests") or {}).get("skip_duplicate_main") is True
    except (OSError, ValueError, AttributeError):
        return False


def queue_passed(gh, repo, workflow_file, sha):
    """The passing merge_group run of this workflow for exactly this commit, or None. An unreadable queue history is
    no queue run: the pull-request path still applies."""
    try:
        runs = (gh.get("/repos/%s/actions/workflows/%s/runs?event=merge_group&head_sha=%s&status=success"
                       % (repo, workflow_file, sha)) or {}).get("workflow_runs") or []
    except Exception:
        return None
    for r in runs:
        if (r.get("conclusion") == "success" and r.get("head_sha") == sha and r.get("event") == "merge_group"
                and r.get("path") == ".github/workflows/%s" % workflow_file):
            return r
    return None


def check(gh, repo, event, workflow_file, checkout):
    """-> (skip, reason, pr_number or None). Pure apart from `gh` reads; tests drive it with a fake."""
    default = event["repository"]["default_branch"]
    sha, before = event.get("after", ""), event.get("before", "")
    if event.get("ref") != "refs/heads/" + default:
        return False, "not a push to %s" % default, None
    if not before or before == ZERO or event.get("forced"):
        return False, "new or force-pushed branch", None
    if not wants_skip(checkout):
        return False, "tests.skip_duplicate_main is off", None
    queue = queue_passed(gh, repo, workflow_file, sha)
    if queue:
        prs = [p for p in gh.get("/repos/%s/commits/%s/pulls" % (repo, sha)) if p.get("merged_at")]
        return True, "the merge queue already passed %s on this exact commit (%s, run %s)" % (
            workflow_file, sha[:7], queue.get("html_url") or queue.get("id")), prs[0]["number"] if len(prs) == 1 else None
    merged = [p for p in gh.get("/repos/%s/commits/%s/pulls" % (repo, sha))
              if p.get("merged_at") and p.get("merge_commit_sha") == sha and p["base"]["ref"] == default]
    if len(merged) != 1:
        return False, "the commit is not the merge of exactly one pull request", None
    pr = merged[0]
    head = pr["head"]["sha"]
    files = gh.paged("/repos/%s/pulls/%d/files" % (repo, pr["number"]), cap=3000)
    if len(files) >= 3000 or any(f.get("filename", "").startswith(".github/") or
                                 (f.get("previous_filename") or "").startswith(".github/") for f in files):
        return False, "pull request #%d changed .github/ (or too many files to check)" % pr["number"], None
    tree = gh.get("/repos/%s/git/commits/%s" % (repo, sha))["tree"]["sha"]
    runs = gh.get("/repos/%s/actions/workflows/%s/runs?event=pull_request&head_sha=%s&status=success"
                  % (repo, workflow_file, head)).get("workflow_runs", [])
    tested = False
    for r in runs:
        m = TESTED_RE.search(r.get("display_title") or "")
        if (r.get("conclusion") != "success" or r.get("head_sha") != head or r.get("event") != "pull_request" or
                r.get("path") != ".github/workflows/%s" % workflow_file or not m):
            continue
        if gh.get("/repos/%s/git/commits/%s" % (repo, m.group(1)))["tree"]["sha"] == tree:
            tested = True
            break
    if not tested:
        return False, "no passing %s run tested pull request #%d on exactly these files" % (
            workflow_file, pr["number"]), None
    return True, "pull request #%d already passed %s on identical code (%s)" % (
        pr["number"], workflow_file, head[:7]), pr["number"]


def decided_text(reason, workflow_file, server, repo):
    """The "Decided without you" note for a skip: what, why, how to undo."""
    return ("### Decided without you\n"
            "- **Skipped the %s tests on this merge.** Why: %s. To undo: run them anyway from %s/%s/actions/workflows/%s "
            "(Run workflow on the default branch), or turn off \"skip duplicate tests on main\" on the Build "
            "Autopilot settings page.\n" % (workflow_file, reason, server, repo, workflow_file))


def report(gh, repo, pr_number, text):
    """Put the note on the merged pull request. Best effort: the run summary has it either way."""
    try:
        gh.send("POST", "/repos/%s/issues/%d/comments" % (repo, pr_number),
                {"body": "<!-- build-autopilot:skip-duplicate-main -->\n" + text})
    except Exception as e:  # the run summary has the note either way
        print("Could not comment on pull request #%s: %s" % (pr_number, e))


def main(argv):
    workflow_file = argv[1] if len(argv) > 1 else "test.yml"
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    gh = GitHub(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    try:
        skip, reason, pr_number = check(gh, os.environ["GITHUB_REPOSITORY"], event, workflow_file,
                                        os.environ.get("GITHUB_WORKSPACE", "."))
    except Exception as e:  # any doubt means the tests run
        skip, reason, pr_number = False, "could not confirm (%s); running the tests" % e, None
    print(("Skipping the tests: " if skip else "Running the tests: ") + reason)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    text = decided_text(reason, workflow_file,
                        os.environ.get("GITHUB_SERVER_URL", "https://github.com"), repo) if skip \
        else "**Decided without you: nothing** (the tests run: %s)\n" % reason
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    if skip and pr_number is not None:  # a batched queue push has no single pull request to note it on
        report(gh, repo, pr_number, text)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("skip=%s\n" % ("true" if skip else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
