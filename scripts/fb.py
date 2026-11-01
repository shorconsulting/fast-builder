#!/usr/bin/env python3
"""fast-builder: run one repo's build across several Claude accounts without them tripping over each other.

Accounts cannot message each other, so the repo is the bus: an orphan branch (fast-builder/bus) holds every run's
job, plan, lane rules, claims, heartbeats, messages and stop switch. It has no workflows, so writing to it costs no
CI, and it needs no merge, so branch protection on main never gets in the way. A rejected `git push` is the
compare-and-swap that makes claiming a lane atomic.

One file, standard library only, Python 3.8+: publish copies it to the bus as kit/fb.py so a cloud session, which
does not get user plugins, can fetch it with nothing but git.

Layout of the bus:

  STOP                            any content: stop every run (can be created from the GitHub web page or a phone)
  STATUS.md                       human view, rewritten by the conductor
  settings.json                   repo-wide settings, such as max_accounts
  kit/                            fb.py and the role docs
  listeners/<account>.json        who is listening, what they can do, and their routine
  runs/<run>/job.json             goal and parameters
  runs/<run>/lanes.json           machine-readable ownership
  runs/<run>/<plan file>          REPO-MM-DD-YY-orchestration-plan.md
  runs/<run>/lanes/<lane>.md      that account's swim-lane rules
  runs/<run>/control.json         running | paused | draining | stopping | finished
  runs/<run>/STOP                 any content: stop this run
  runs/<run>/claims/<lane>.json   which account holds a lane
  runs/<run>/beats/<lane>.json    heartbeat and lane status
  runs/<run>/handoffs/<lane>.md   rolling handoff, what a replacement account starts from
  runs/<run>/conductor.json       lease on the cross-account orchestrator role
  runs/<run>/msgs/<id>.json       messages between lanes, the conductor and the human

See docs/protocol.md. Everything is a subcommand: fb.py --help.
"""
import argparse
import datetime
import fcntl
import glob as globlib
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = "0.1.5"
BUS_BRANCH = os.environ.get("FB_BUS_BRANCH", "fast-builder/bus")
FORMAT = 1
HOME_DIR = os.environ.get("FB_HOME") or os.path.join(os.path.expanduser("~"), ".config", "fast-builder")
CACHE_DIR = os.environ.get("FB_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "fast-builder")
CLAUDE = os.environ.get("FB_CLAUDE", "claude")
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_MAX_ACCOUNTS = 4
STALE_MINUTES = 45
BEAT_SECONDS = 300
CONTROL_CACHE_SECONDS = 60
LEASE_MINUTES = 20
STOP_BLOCK_CAP = 3
MAX_RESTARTS = 5
WAKE_SECONDS = 600
MAX_ROSTER = 12  # each try costs a Haiku call and calls run one at a time, so a bus cannot be let list as many as it likes
PASS_CALLS = 24  # about what one pass over one repo may spend on wakes: those run one at a time, and a stop waits behind the pass
GONE = ("400", "404")  # what a routine on another login answers; neither starts anything

# The default guess at throughput when a repo has no merged history to measure, and what each extra account costs
# in coordination. Both are assumptions: `size` says so in its output, and --rate overrides the first.
DEFAULT_RATE_PER_DAY = 12.0
COORDINATION_LOSS = 0.08

SENSITIVE_DEFAULTS = [
    "**/auth/**", "**/*login*", "**/*session*", "**/tenan*/**", "**/payments/**", "**/billing/**", "**/phi/**",
    "**/crypto/**", "**/migrations/**", "**/secrets*", "**/.env*", ".github/**",
]

# Who does what: the cheapest model that does the job well. Opus for security-sensitive
# work, design and orchestration; Sonnet for pattern-following work; Haiku for anything mechanical; Fable never.
ROUTES = {
    "orchestrate": ("opus", "opus"),
    "design": ("opus", "opus"),
    "build": ("sonnet", "opus"),
    "fix": ("sonnet", "opus"),
    "review": ("sonnet", "opus"),
    "rereview": ("sonnet", "opus"),
    "land": ("sonnet", "sonnet"),
    "conflict": ("sonnet", "sonnet"),
    "bump": ("sonnet", "sonnet"),
    "mechanical": ("haiku", "haiku"),
    "search": ("haiku", "haiku"),
    "poll": ("haiku", "haiku"),
}
EFFORTS = {"opus": "high", "sonnet": "medium", "haiku": "low"}

GO, PAUSE, DRAIN, STOP, LOST, FINISHED = "continue", "pause", "drain", "stop", "lost", "finished"
EXIT_CODES = {GO: 0, PAUSE: 10, DRAIN: 11, STOP: 20, LOST: 21, FINISHED: 22}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\Z")
TRIGGER_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}\Z")
BUS_TRIGGER_RE = re.compile(r"^trig_[A-Za-z0-9]{24}\Z")  # an id goes into a prompt, so one from the bus gets no room to say anything
POLL_RE = re.compile(r"\bfb\.py poll --account ([a-z0-9][a-z0-9-]{0,40})(?![A-Za-z0-9_-])")
STATUS_RE = re.compile(r"(?:HTTP(?:/[0-9.]+)?\s+|Status:?\s*)?([0-9]{3})(?:\s+(?:OK|Created|Accepted|Bad Request|Unauthorized|Forbidden|Not Found))?\Z", re.I)
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}\Z")
MODEL_RE = re.compile(r"^(opus|sonnet|haiku|claude-[a-z0-9-]{1,40})\Z")
LOCK_SECONDS = 30
HOOK_LOCK_SECONDS = 4


class FbError(Exception):
    """A failure to report in one line rather than as a traceback."""


class Taken(FbError):
    """The bus already holds what we tried to create."""


class Lost(FbError):
    """This session no longer holds its lane."""


# Small helpers


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def now_iso():
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def age_minutes(text):
    try:
        return (utcnow() - parse_iso(text)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return float("inf")


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def run_cmd(args, cwd=None, check=True, timeout=60, env=None):
    try:
        proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              universal_newlines=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise FbError("timed out: " + " ".join(args[:4]))
    except OSError as exc:
        raise FbError("cannot run %s: %s" % (args[0], exc))
    if check and proc.returncode != 0:
        raise FbError("%s failed: %s" % (" ".join(args[:4]), (proc.stderr or proc.stdout).strip()[:300]))
    return proc


def git(args, cwd=None, check=True, timeout=60):
    return run_cmd(["git", "-c", "safe.directory=*"] + args, cwd=cwd, check=check, timeout=timeout)


def read_json(path, default=None):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError, RecursionError):
        return default


def write_file(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


def repo_slug(remote):
    """owner/name from a remote URL, or the URL's last two segments for anything else."""
    text = re.sub(r"\.git/?$", "", remote.rstrip("/"))
    match = re.search(r"[:/]([^/:]+)/([^/:]+)$", text)
    return "%s/%s" % (match.group(1), match.group(2)) if match else text


def cwd_remote(cwd=None):
    proc = git(["remote", "get-url", "origin"], cwd=cwd or os.getcwd(), check=False)
    if proc.returncode != 0:
        raise FbError("no origin remote here: pass --remote")
    return proc.stdout.strip()


def default_branch(cwd=None):
    proc = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cwd, check=False)
    return proc.stdout.strip().split("/", 1)[-1] if proc.returncode == 0 and proc.stdout.strip() else "main"


# Globs. Lane ownership is written as gitignore-style globs, where ** crosses directories and * does not.


def clean_path(text):
    return (text[2:] if text.startswith("./") else text).lstrip("/")


def glob_re(pattern):
    text = clean_path(pattern)
    if text.endswith("/"):
        text += "**"
    out, i = "", 0
    while i < len(text):
        if text.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif text.startswith("**", i):
            out, i = out + ".*", i + 2
        elif text[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif text[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(text[i]), i + 1
    return re.compile("^" + out + "\\Z")


def matches(path, patterns):
    path = clean_path(path)
    return any(glob_re(p).match(path) for p in patterns)


def glob_segments(pattern):
    text = clean_path(pattern)
    if text.endswith("/"):
        text += "**"
    return [seg for seg in text.split("/") if seg]


def segments_meet(a, b):
    """Can one directory or file name match both of these single-segment patterns? Errs towards yes."""
    wild_a, wild_b = re.search(r"[*?]", a), re.search(r"[*?]", b)
    if not wild_a and not wild_b:
        return a == b
    if not wild_b:
        return bool(glob_re(a).match(b))
    if not wild_a:
        return bool(glob_re(b).match(a))
    head_a, head_b = a[:wild_a.start()], b[:wild_b.start()]
    tail_a, tail_b = re.split(r"[*?]", a)[-1], re.split(r"[*?]", b)[-1]
    return (head_a.startswith(head_b) or head_b.startswith(head_a)) and (tail_a.endswith(tail_b) or tail_b.endswith(tail_a))


def globs_meet(first, second):
    """Could any path, existing or future, match both globs? This is what lets lanes be proven disjoint in a repo that
    does not have the files yet."""
    a, b, seen = glob_segments(first), glob_segments(second), {}

    def walk(i, j):
        key = (i, j)
        if key not in seen:
            seen[key] = False
            if i == len(a) and j == len(b):
                seen[key] = True
            elif i < len(a) and a[i] == "**":
                seen[key] = walk(i + 1, j) or (j < len(b) and walk(i, j + 1))
            elif j < len(b) and b[j] == "**":
                seen[key] = walk(i, j + 1) or (i < len(a) and walk(i + 1, j))
            elif i < len(a) and j < len(b):
                seen[key] = segments_meet(a[i], b[j]) and walk(i + 1, j + 1)
        return seen[key]

    return walk(0, 0)


def owner_of(path, lanes):
    """The lane owning a path: the one whose globs match, else the steward, else nobody."""
    for lane in lanes["lanes"]:
        if not lane.get("steward") and matches(path, lane.get("owns", [])):
            return lane["name"]
    for lane in lanes["lanes"]:
        if lane.get("steward") and matches(path, lane.get("owns", [])):
            return lane["name"]
    for lane in lanes["lanes"]:
        if lane.get("steward"):
            return lane["name"]
    return None


def may_touch(path, lane, lanes):
    return matches(path, lanes.get("shared_ok", [])) or owner_of(path, lanes) == lane


def allocate_digits(count):
    """Hand each lane last digits for sequence numbers, the way HPH's lanes never collide on a migration number."""
    usable = list(range(1, 9))
    out = [[] for _ in range(count)]
    for i, digit in enumerate(usable):
        out[i % count].append(digit)
    if count == 1:
        return [list(range(10))]
    if count >= 9:
        return [[i % 10] for i in range(count)]
    out[0].extend([0, 9])
    return out


# The bus


class Bus:
    """A cached checkout of the bus branch. Every write runs inside a fetch, mutate, push loop, so a mutator always
    sees the newest state and a push that loses the race simply runs it again."""

    def __init__(self, remote, account="unknown"):
        self.remote = remote
        self.account = account
        key = hashlib.sha1(remote.encode()).hexdigest()[:12]
        self.dir = os.path.join(CACHE_DIR, "bus", "%s-%s" % (repo_slug(remote).replace("/", "__"), key))
        self.lock_handle = None
        self.lock_seconds = LOCK_SECONDS

    def lock(self):
        """Hold the checkout for this process. Hooks, the listener and commands all share it, so one must not move it
        under another. Held until release() or exit: what a command reads after sync stays what it synced."""
        if self.lock_handle:
            return
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        handle = open(self.dir + ".lock", "w")
        end = time.time() + self.lock_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_handle = handle
                return
            except OSError:
                if time.time() > end:
                    handle.close()
                    raise FbError("bus checkout is busy")
                time.sleep(0.05)

    def release(self):
        if self.lock_handle:
            self.lock_handle.close()
            self.lock_handle = None

    def head(self, timeout=15):
        """The bus branch's sha, or None if it does not exist yet. The cheap call the listener polls with."""
        proc = git(["ls-remote", "--heads", self.remote, "refs/heads/" + BUS_BRANCH], check=False, timeout=timeout)
        if proc.returncode != 0:
            raise FbError("cannot reach %s: %s" % (self.remote, proc.stderr.strip()[:200]))
        return proc.stdout.split()[0] if proc.stdout.strip() else None

    def sync(self):
        """Bring the cache to the remote's bus branch. False when the branch does not exist yet."""
        self.lock()
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            os.makedirs(self.dir, exist_ok=True)
            git(["init", "-q"], cwd=self.dir)
            git(["remote", "add", "origin", self.remote], cwd=self.dir)
        if not self.head():
            return False
        spec = "+refs/heads/%s:refs/remotes/origin/bus" % BUS_BRANCH
        git(["fetch", "-q", "--depth", "50", "origin", spec], cwd=self.dir, timeout=120)
        git(["checkout", "-q", "-f", "-B", "bus", "refs/remotes/origin/bus"], cwd=self.dir)
        git(["clean", "-qfdx"], cwd=self.dir)
        if any(line.startswith("120000") for line in git(["ls-files", "-s"], cwd=self.dir).stdout.splitlines()):
            raise FbError("refusing a bus with a symlink on it")
        return True

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def read(self, rel, default=None):
        try:
            with open(self.path(rel)) as handle:
                return handle.read()
        except OSError:
            return default

    def json(self, rel, default=None):
        return read_json(self.path(rel), default)

    def listdir(self, rel):
        try:
            return sorted(n for n in os.listdir(self.path(rel)) if not n.startswith("."))
        except OSError:
            return []

    def commit(self, mutate, message, attempts=10):
        """Fetch, run mutate() for {path: text or None}, commit, push; repeat on a lost race.

        mutate may raise Taken to give up. Returning an empty dict means there is nothing to do."""
        for attempt in range(attempts):
            exists = self.sync()
            if not exists:
                self._bootstrap()
            files = mutate()
            if not files:
                return False
            for rel, text in files.items():
                full = self.path(rel)
                if text is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    write_file(full, text)
            git(["add", "-A"], cwd=self.dir)
            if git(["diff", "--cached", "--quiet"], cwd=self.dir, check=False).returncode == 0:
                return False
            who = "fast-builder (%s)" % self.account
            git(["-c", "user.name=" + who, "-c", "user.email=fast-builder@noreply.local", "commit", "-q", "-m", message],
                cwd=self.dir)
            push = git(["push", "-q", "origin", "HEAD:refs/heads/" + BUS_BRANCH], cwd=self.dir, check=False, timeout=60)
            if push.returncode == 0:
                return True
            text = push.stderr.lower()
            if not any(w in text for w in ("rejected", "fetch first", "non-fast-forward", "stale info", "cannot lock", "failed to update ref")):
                raise FbError("bus push failed: " + push.stderr.strip()[:300])
            time.sleep(min(2.0, 0.1 * (2 ** attempt)) * (0.5 + random.random()))
        raise FbError("bus is too busy: gave up after %d attempts" % attempts)

    def _bootstrap(self):
        """Start the orphan branch locally. The loop's push then races any other account doing the same."""
        git(["checkout", "-q", "--orphan", "bus"], cwd=self.dir, check=False)
        git(["rm", "-rfq", "--ignore-unmatch", "."], cwd=self.dir, check=False)
        write_file(self.path("README.md"), BUS_README)
        write_file(self.path("settings.json"), dumps({"format": FORMAT, "max_accounts": DEFAULT_MAX_ACCOUNTS}))


BUS_README = """# fast-builder bus

Orphan branch for coordinating one build across several Claude accounts. Do not merge it. Nothing here runs in CI.

To stop every run from a phone: add a file named `STOP` at the top of this branch. See `STATUS.md` for the state.
"""


# Run state


def run_state(bus, run):
    """What a builder should do now, from control.json and the stop switches. A STOP file overrides a live run only:
    a closed run stays closed."""
    control = bus.json("runs/%s/control.json" % run) or {}
    state = control.get("state", "running")
    if state in ("finished", "stopped"):
        return {"state": state, "action": FINISHED if state == "finished" else STOP, "reason": control.get("reason", "")}
    if bus.exists("STOP") or bus.exists("runs/%s/STOP" % run):
        return {"state": "stopping", "action": STOP, "reason": "STOP file on the bus"}
    action = {"running": GO, "paused": PAUSE, "draining": DRAIN, "stopping": STOP}.get(state, STOP)
    return {"state": state, "action": action, "reason": control.get("reason", "")}


def list_runs(bus):
    return [r for r in bus.listdir("runs") if RUN_RE.match(r)]


def active_runs(bus):
    return [r for r in list_runs(bus) if run_state(bus, r)["state"] in ("running", "paused", "draining", "stopping")]


def lanes_of(bus, run):
    doc = bus.json("runs/%s/lanes.json" % run)
    rows = doc.get("lanes") if isinstance(doc, dict) else None
    good = [l for l in rows if isinstance(l, dict) and isinstance(l.get("name"), str)] if isinstance(rows, list) else []
    return dict(doc, lanes=good) if good else {"lanes": []}


def lane_names(bus, run):
    return [l["name"] for l in lanes_of(bus, run)["lanes"] if NAME_RE.match(l["name"])]


def beat_of(bus, run, lane):
    return bus.json("runs/%s/beats/%s.json" % (run, lane)) or {}


def lane_table(bus, run):
    rows = []
    for name in lane_names(bus, run):
        claim = bus.json("runs/%s/claims/%s.json" % (run, name))
        beat = beat_of(bus, run, name)
        rows.append({
            "lane": name, "account": claim.get("account") if claim else None, "status": beat.get("status", "open" if not claim else "claimed"),
            "beat": beat.get("at"), "beat_age_min": None if not beat.get("at") else round(age_minutes(beat["at"]), 1),
            "note": str(beat.get("note", "")), "where": claim.get("where") if claim else None,
        })
    return rows


def make_id(account):
    return "%s-%s-%04x" % (utcnow().strftime("%Y%m%dT%H%M%SZ"), account, random.getrandbits(16))


def message(frm, to, kind, body):
    return {"from": frm, "to": to, "kind": kind, "body": body, "at": now_iso()}


def post(bus, run, frm, to, kind, body):
    rel = "runs/%s/msgs/%s.json" % (run, make_id(frm))
    bus.commit(lambda: {rel: dumps(message(frm, to, kind, body))}, "msg(%s): %s to %s" % (run, kind, to))


def post_once(bus, run, frm, body):
    """A blocker for a person, unless that exact text is already there: a wake that keeps failing must not fill the inbox."""
    def mutate():
        if any(m.get("body") == body for m in messages_for(bus, run, "human")):
            return {}
        return {"runs/%s/msgs/%s.json" % (run, make_id(frm)): dumps(message(frm, "human", "blocker", body))}
    bus.commit(mutate, "msg(%s): blocker to human" % run)


def messages_for(bus, run, who, seen=()):
    """Messages for who, skipping ids already seen. An id set, not a watermark: a slow push can land an older id late."""
    out = []
    for name in bus.listdir("runs/%s/msgs" % run):
        if name in seen:
            continue
        msg = bus.json("runs/%s/msgs/%s" % (run, name))
        if isinstance(msg, dict) and msg and (who is None or msg.get("to") in (who, "all")) and msg.get("from") != who:
            msg["id"] = name
            out.append(msg)
    return out


def claim_lane(bus, run, account, lane=None, where=None, allow_second=False, token=None):
    """Atomically claim a lane: the named one, or the first open one. Returns its name.

    An account takes one lane per run unless allow_second is set, which is how a lane orphaned by an account that
    ran out of usage still gets picked up when every account already has one. A claim only lands if the push wins.

    token marks the one session holding the lane. Rejoining a lane the account holds moves the token, so a restarted
    session replaces its dead predecessor, and the predecessor, if it was not dead, is told it lost the lane."""
    won = {}

    def mutate():
        names = lane_names(bus, run)
        if not names:
            raise FbError("no such run: " + run)
        state = run_state(bus, run)
        if state["action"] != GO:
            raise FbError("run %s is %s" % (run, state["state"]))
        mine = [n for n in names if (bus.json("runs/%s/claims/%s.json" % (run, n)) or {}).get("account") == account]
        if lane and lane in mine:
            won["lane"] = lane
            if not token:
                return {}
            doc = dict(bus.json("runs/%s/claims/%s.json" % (run, lane)) or {}, session=token)
            return {"runs/%s/claims/%s.json" % (run, lane): dumps(doc)}
        if mine and not allow_second:
            if lane:
                raise Taken("%s already holds lane %s in %s" % (account, mine[0], run))
            won["lane"] = mine[0]
            if not token:
                return {}
            doc = dict(bus.json("runs/%s/claims/%s.json" % (run, mine[0])) or {}, session=token)
            return {"runs/%s/claims/%s.json" % (run, mine[0]): dumps(doc)}
        if lane:
            if lane not in names:
                raise FbError("no lane %s in %s" % (lane, run))
            candidates = [lane]
        else:
            candidates = names
        open_lanes = [n for n in candidates if not bus.exists("runs/%s/claims/%s.json" % (run, n))]
        if not open_lanes:
            raise Taken("no open lane in " + run)
        won["lane"] = open_lanes[0]
        doc = {"account": account, "at": now_iso(), "session": token, "where": where or (bus.json("runs/%s/job.json" % run) or {}).get("where", "cloud")}
        beat = {"at": now_iso(), "status": "working", "account": account, "note": "claimed"}
        return {"runs/%s/claims/%s.json" % (run, won["lane"]): dumps(doc),
                "runs/%s/beats/%s.json" % (run, won["lane"]): dumps(beat)}

    bus.commit(mutate, "claim(%s): %s" % (run, account))
    return won.get("lane")


TRANSITIONS = {
    "paused": ("running",),
    "draining": ("running", "paused"),
    "stopping": ("running", "paused", "draining"),
    "running": ("paused", "draining", "stopping", "stopped"),
}


def set_control(bus, run, state, reason, by):
    """Move a run to state if that is a legal move. pause can never soften a stop, and nothing reopens a finished run.
    Returns whether it moved. A resume also refreshes working lanes' beats: nobody beats while paused, and those lanes
    must not look dead the moment the run starts again."""
    moved = {}

    def mutate():
        current = (bus.json("runs/%s/control.json" % run) or {}).get("state", "running")
        moved["ok"] = current in TRANSITIONS[state]
        if not moved["ok"]:
            if state == "running" and current == "running" and bus.exists("runs/%s/STOP" % run):
                moved["ok"] = True
                return {"runs/%s/STOP" % run: None}
            return {}
        files = {"runs/%s/control.json" % run: dumps({"state": state, "reason": reason, "by": by, "at": now_iso()})}
        if state == "running":
            files["runs/%s/STOP" % run] = None
            for name in lane_names(bus, run):
                beat = beat_of(bus, run, name)
                if beat.get("status") == "stopped":
                    files["runs/%s/claims/%s.json" % (run, name)] = None
                    files["runs/%s/beats/%s.json" % (run, name)] = dumps({"at": now_iso(), "status": "open", "account": None, "note": "stopped lane back on offer"})
                elif beat.get("status") == "working":
                    files["runs/%s/beats/%s.json" % (run, name)] = dumps(dict(beat, at=now_iso()))
        return files

    bus.commit(mutate, "control(%s): %s" % (run, state))
    return moved.get("ok", False)


def conduct(bus, run, account, stale_minutes=STALE_MINUTES):
    """The deterministic half of the cross-account orchestrator. Needs no model, so a listener can run it too.

    Takes or renews the lease; frees lanes whose account went quiet, so another account takes them over; closes
    the run once every lane is done; rewrites STATUS.md. Returns what it did and what needs judgement."""
    report = {"run": run, "holder": None, "freed": [], "finished": False}

    def mutate():
        files = {}
        report.update(freed=[], finished=False)
        lease = bus.json("runs/%s/conductor.json" % run) or {}
        report["holder"] = lease.get("holder")
        if lease.get("holder") not in (None, account) and age_minutes(lease.get("at")) < LEASE_MINUTES:
            return {}
        held = lease.get("holder") == account and age_minutes(lease.get("at")) < LEASE_MINUTES / 2.0
        if held and (bus.exists("STOP") or bus.exists("runs/%s/STOP" % run)):
            return {}  # the stop files decide this run: control.json stays as it was, so removing them restores it
        if held and run_state(bus, run)["action"] == GO:
            stale = _stale(bus, run, stale_minutes)
            names = lane_names(bus, run)
            if not stale and not (names and all(beat_of(bus, run, n).get("status") in ("done", "blocked") for n in names)):
                return {}
        report["holder"] = account
        files["runs/%s/conductor.json" % run] = dumps({"holder": account, "at": now_iso()})
        state = run_state(bus, run)
        names = lane_names(bus, run)
        if state["action"] == GO:
            for name in _stale(bus, run, stale_minutes):
                claim = bus.json("runs/%s/claims/%s.json" % (run, name)) or {}
                files["runs/%s/claims/%s.json" % (run, name)] = None
                files["runs/%s/beats/%s.json" % (run, name)] = dumps({"at": now_iso(), "status": "open", "account": None, "note": "freed: %s went quiet" % claim.get("account")})
                note = message(account, "all", "note", "Lane %s freed: %s went quiet for over %d minutes." % (name, claim.get("account"), stale_minutes))
                files["runs/%s/msgs/%s.json" % (run, make_id(account))] = dumps(note)
                report["freed"].append(name)
            statuses = [beat_of(bus, run, n).get("status") for n in names]
            if names and all(s in ("done", "blocked") for s in statuses):
                blocked = statuses.count("blocked")
                reason = "every lane is done" if not blocked else "%d lanes done, %d blocked" % (statuses.count("done"), blocked)
                files["runs/%s/control.json" % run] = dumps({"state": "finished", "reason": reason, "by": account, "at": now_iso()})
                report["finished"] = True
        elif (bus.json("runs/%s/control.json" % run) or {}).get("state") in ("draining", "stopping"):
            quiet = _stale(bus, run, stale_minutes)
            if all(n in quiet or beat_of(bus, run, n).get("status") in ("done", "blocked", "stopped", "open", None) for n in names):
                files["runs/%s/control.json" % run] = dumps({"state": "stopped", "reason": state["reason"], "by": account, "at": now_iso()})
        files["STATUS.md"] = render_status(bus)
        return files

    bus.commit(mutate, "conduct(%s): %s" % (run, account))
    return report


def _stale(bus, run, stale_minutes):
    out = []
    for name in lane_names(bus, run):
        beat = beat_of(bus, run, name)
        if bus.exists("runs/%s/claims/%s.json" % (run, name)) and beat.get("status") == "working" and age_minutes(beat.get("at")) >= stale_minutes:
            out.append(name)
    return out


def render_status(bus):
    lines = ["# fast-builder status", "", "Updated %s." % now_iso(), ""]
    if bus.exists("STOP"):
        lines += ["**STOP is set: every run is stopping.**", ""]
    runs = list_runs(bus)
    for run in runs[-6:]:
        job = bus.json("runs/%s/job.json" % run) or {}
        state = run_state(bus, run)
        lines += ["## %s: %s" % (run, state["state"]), "", job.get("goal", ""), "",
                  "| Lane | Account | Status | Last beat (min) | Note |", "|---|---|---|---|---|"]
        for row in lane_table(bus, run):
            lines.append("| %s | %s | %s | %s | %s |" % (row["lane"], row["account"] or "-", row["status"], row["beat_age_min"] if row["beat_age_min"] is not None else "-", row["note"].replace("|", "/")[:80]))
        human = [m for m in messages_for(bus, run, "human") if m.get("kind") == "needs-human"]
        if human:
            lines += ["", "Waiting on a person:"] + ["- %s: %s" % (m["from"], m["body"]) for m in human]
        lines.append("")
    return "\n".join(lines) + "\n"


# Validation: the proof that lanes cannot overlap, and the gate publish goes through.


def validate(draft, files=None):
    """Errors and facts about a draft. files is the repo's file list; passing it proves disjointness on real paths."""
    errors, facts = [], {}
    job = read_json(os.path.join(draft, "job.json"))
    lanes = read_json(os.path.join(draft, "lanes.json"))
    if not job or not lanes:
        return ["job.json or lanes.json is missing or unreadable"], facts
    for field in ("run", "repo", "goal", "where", "autopilot", "plan_file", "accounts", "max_accounts"):
        if field not in job:
            errors.append("job.json has no " + field)
    if job.get("where") not in ("cloud", "local"):
        errors.append("where must be cloud or local")
    errors += job_errors(job)
    if job.get("autopilot") not in ("full", "policy"):
        errors.append("autopilot must be full or policy")
    rows = lanes.get("lanes", [])
    names = [l.get("name", "") for l in rows]
    if len(rows) != job.get("accounts"):
        errors.append("%d lanes for %s accounts: one lane per account" % (len(rows), job.get("accounts")))
    if isinstance(job.get("accounts"), int) and isinstance(job.get("max_accounts"), int) and job["accounts"] > job["max_accounts"]:
        errors.append("%d accounts is over the maximum of %d" % (job["accounts"], job["max_accounts"]))
    if len(set(names)) != len(names):
        errors.append("lane names repeat")
    for name in names:
        if not NAME_RE.match(name):
            errors.append("bad lane name: %r" % name)
    stewards = [l["name"] for l in rows if l.get("steward")]
    if len(stewards) != 1:
        errors.append("exactly one lane must be the steward (it owns everything no other lane does); found %d" % len(stewards))
    digits = {}
    for lane in rows:
        if not lane.get("owns") and not lane.get("steward"):
            errors.append("lane %s owns nothing" % lane.get("name"))
        for digit in lane.get("digits", []):
            if digit in digits:
                errors.append("digit %s is on both %s and %s" % (digit, digits[digit], lane["name"]))
            digits[digit] = lane.get("name")
    shared = lanes.get("shared_ok", [])
    for a in rows:
        for b in rows:
            if a.get("name", "") >= b.get("name", "") or a.get("steward") or b.get("steward"):
                continue
            for pattern in a.get("owns", []):
                for other in b.get("owns", []):
                    if pattern in shared or other in shared:
                        continue
                    if globs_meet(pattern, other):
                        errors.append("lanes %s and %s overlap: %s and %s" % (a["name"], b["name"], pattern, other))
    if files is not None:
        counts, doubled = {}, []
        for path in files:
            owners = [l["name"] for l in rows if not l.get("steward") and matches(path, l.get("owns", []))]
            if len(owners) > 1 and not matches(path, shared):
                doubled.append("%s (%s)" % (path, ", ".join(owners)))
            owner = owners[0] if owners else (stewards[0] if stewards else "none")
            counts[owner] = counts.get(owner, 0) + 1
        for item in doubled[:10]:
            errors.append("file in two lanes: " + item)
        if len(doubled) > 10:
            errors.append("and %d more files in two lanes" % (len(doubled) - 10))
        facts["files_by_lane"] = counts
        facts["files_checked"] = len(files)
    plan = os.path.join(draft, job.get("plan_file", "plan.md"))
    text = read_text(plan)
    if text is None:
        errors.append("no plan file: %s" % job.get("plan_file"))
    else:
        for name in names:
            if "## Lane: %s" % name not in text:
                errors.append("plan has no section for lane %s" % name)
    for name in names:
        doc = read_text(os.path.join(draft, "lanes", name + ".md"))
        if doc is None:
            errors.append("no rules file for lane %s" % name)
        else:
            text = (text or "") + doc
    if text and re.search(r"\{\{[^}]*\}\}", text):
        errors.append("unfilled placeholders remain: " + ", ".join(sorted(set(re.findall(r"\{\{[^}]*\}\}", text)))[:6]))
    return errors, facts


def job_errors(job):
    """The job fields that end up in a path or on a command line. Checked at publish and again by whoever reads them."""
    errors = []
    if not isinstance(job.get("plan_file"), str) or not FILE_RE.match(job.get("plan_file", "")):
        errors.append("bad plan file name")
    if not MODEL_RE.match(str(job.get("lane_model", "opus"))):
        errors.append("bad lane model")
    if not BRANCH_RE.match(str(job.get("base", "main"))):
        errors.append("bad base branch")
    return errors


def read_text(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None


# Partition and sizing


def partition(items, count):
    """Split work into count balanced lanes that share no path.

    Items touching a common directory stick together (union-find over paths), then the groups are packed heaviest
    first into the lightest lane. A starting proposal: the planner adjusts it to the domain, then validate proves it."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, item in enumerate(items):
        for path in item.get("paths", []):
            if path in seen:
                parent[find(i)] = find(seen[path])
            seen[path] = i
    groups = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)
    bins = [{"items": [], "paths": [], "weight": 0.0} for _ in range(count)]
    ordered = sorted(groups.values(), key=lambda g: (-sum(float(x.get("weight", 1)) for x in g), g[0]["id"]))
    for group in ordered:
        target = min(bins, key=lambda b: b["weight"])
        target["items"] += [x["id"] for x in group]
        target["weight"] += sum(float(x.get("weight", 1)) for x in group)
        for item in group:
            target["paths"] += [p for p in item.get("paths", []) if p not in target["paths"]]
    return bins


def tree_items(root, depth=2):
    """Work items from the repo's own tree when no backlog is given: one per directory, weighted by file count."""
    proc = git(["ls-files"], cwd=root, check=False, timeout=30)
    weights = {}
    for path in proc.stdout.splitlines():
        parts = path.split("/")
        if len(parts) == 1:
            continue
        key = "/".join(parts[:min(depth, len(parts) - 1)]) + "/"
        weights[key] = weights.get(key, 0) + 1
    return [{"id": key, "paths": [key + "**"], "weight": count} for key, count in sorted(weights.items())]


def throughput(root=None, days=14):
    """Merged PRs per day per active author-and-lane over the last fortnight, measured with gh. None when unknown."""
    if not shutil.which("gh"):
        return None
    proc = run_cmd(["gh", "pr", "list", "--state", "merged", "--limit", "300", "--json", "mergedAt"], cwd=root, check=False, timeout=30)
    if proc.returncode != 0:
        return None
    try:
        stamps = [parse_iso(row["mergedAt"]) for row in json.loads(proc.stdout) if row.get("mergedAt")]
    except (ValueError, KeyError):
        return None
    recent = [s for s in stamps if (utcnow() - s).days < days]
    return round(len(recent) / float(days), 2) if recent else None


def size(items, hours, max_accounts, rate):
    """How many accounts finish items in hours at rate merges a day per account. Each extra account loses a little
    to coordination, so the answer is the smallest count that fits, capped at max_accounts."""
    table = []
    for n in range(1, max_accounts + 1):
        per_day = n * rate * (1 - COORDINATION_LOSS) ** (n - 1)
        table.append({"accounts": n, "per_day": round(per_day, 1), "hours_to_finish": round(items / per_day * 24, 1)})
    fits = [row for row in table if hours is None or row["hours_to_finish"] <= hours]
    pick = fits[0] if fits else table[-1]
    return {"accounts": pick["accounts"], "fits_deadline": bool(fits), "hours_to_finish": pick["hours_to_finish"], "table": table}


def route(kind, paths, sensitive, changes_verdicts=0, stalled=False):
    """The model for a task. Sensitive paths and repeated failure move it up; nothing moves it to Fable."""
    if kind not in ROUTES:
        raise FbError("unknown kind %s: one of %s" % (kind, ", ".join(sorted(ROUTES))))
    is_sensitive = any(matches(p, sensitive) for p in paths)
    model = ROUTES[kind][1 if is_sensitive else 0]
    why = "sensitive path" if is_sensitive else "standard"
    if kind in ("build", "fix", "review", "rereview", "land", "conflict") and (changes_verdicts >= 2 or stalled):
        model, why = "opus", "escalated: repeated CHANGES or stalled"
    return {"kind": kind, "model": model, "effort": EFFORTS[model], "sensitive": is_sensitive, "why": why}


def sensitive_globs(root):
    policy = read_json(os.path.join(root or ".", ".github", "autopilot.json")) or {}
    return SENSITIVE_DEFAULTS + list((policy.get("sensitive") or {}).get("paths", []))


# Drafting a run


def run_names(code, today, taken):
    """The run id and the plan's file name, as in HPH-10-30-26-orchestration-plan.md, with -2 on a second run that day."""
    stamp = today.strftime("%m-%d-%y")
    base, n = "%s-%s" % (code.upper(), stamp), 1
    label = base
    while label.lower() in taken:
        n += 1
        label = "%s-%d" % (base, n)
    return label.lower(), label + "-orchestration-plan.md"


LANE_DOC = """# Swim lane: {lane}

Run `{run}` of `{repo}`. Plan: `{plan}`. This file is binding for the account holding this lane.

## You own
{owns}

{steward_note}
## Modules
{modules}

## You must not
{forbidden}
- Touch any path outside your lanes's ownership, except these shared files: {shared}
- Open a PR from any branch not named `fb/{run}/{lane}/<slice>`.
- Drive a PR another lane opened.
- Merge main into a branch unless it conflicts or needs code from main.

The guard hook blocks an out-of-lane edit, and `fb.py check-diff` checks a branch before it is pushed. Neither is a
reason to look for a way round: if you need a change in another lane, ask for it:

    python3 "$FB" send --to <lane> --kind request --body "what you need, and why"

and build the rest of your slice around the gap.

## Numbers
{numbers}

## Labels
Every PR: `lane:{lane}`, `fast-builder`, and whatever the repo's own rules require.

## Where to start
{{{{start}}}}

## Done means
{{{{done}}}}
"""

PLAN_DOC = """# {code} orchestration plan ({stamp})

Run `{run}` of `{repo}`, kicked off {kicked}. This is everything a builder needs: no account shares a conversation
with any other, so what is not written here does not exist for them.

## Goal
{goal}

## Parameters
| Setting | Value |
|---|---|
| Accounts this run | {accounts} (max {max_accounts}) |
| Where | {where} |
| Autopilot | {autopilot} |
| Deadline | {deadline} |
| Base branch | {base} |
| Sizing | {sizing} |

## Where we are
{{{{Baseline from the repo: what is built, what is left, what is open, what is red. Numbers, with the commit they were read at.}}}}

## The lanes
{lane_table}

## Why this split
{{{{Why these boundaries keep the lanes from sharing files, and how the load balances.}}}}

{lane_sections}
## Shared ground
Files any lane may touch: {shared}. A conflict in a generated file is resolved by regenerating it, never by hand.
The steward lane (`{steward}`) owns every path no other lane does, fixes main when it goes red, and answers
requests about shared machinery.

## Build, test and land
{{{{The exact commands: install, test, lint, generate; how a PR gets merged here; what the gates require; CI facts a newcomer would trip on.}}}}

## Autopilot
{autopilot_rules}

## Model routing
Run `python3 "$FB" route --kind <kind> --paths <paths>` and set `model` on every subagent. Standard work is Sonnet,
security-sensitive work and design are Opus, mechanical work is Haiku. Never Fable. Never leave `model` to inherit.

## Coordination
Accounts talk only through the bus. Every lane runs `python3 "$FB" tick` between work items and obeys what it says.
Stop, pause or resume from any account with `/fast-builder:stop`, `/fast-builder:pause` or `/fast-builder:resume`, or
add a file named `STOP` to branch `{bus}` from the GitHub page.
"""

AUTOPILOT_FULL = """100% autopilot. No lane stops to ask anybody anything. Reversible decisions are made and logged with
`fb.py decide`, and they appear under "Decided without you" in the PR and the end-of-run report. What needs a person
and is not reversible (deleting data, spending money, messaging people, credentials, legal or pricing calls) is never
done on autopilot: post it with `fb.py send --to human --kind needs-human`, park that one item, and keep building."""

AUTOPILOT_POLICY = """Follow the repo's `.github/autopilot.json`: `always_ask` lists the only decisions that go to a person, `level`
says how far a PR goes, `questions` says whether to ask or decide. Decisions made without asking are logged with
`fb.py decide` and listed under "Decided without you"."""


def render_draft(draft, today):
    """Write the lane rules and the plan's skeleton from job.json and lanes.json. The planner fills the {{...}} blanks."""
    job = read_json(os.path.join(draft, "job.json"))
    lanes = read_json(os.path.join(draft, "lanes.json"))
    if not job or not lanes:
        raise FbError("draft needs job.json and lanes.json")
    rows = lanes["lanes"]
    shared = ", ".join("`%s`" % s for s in lanes.get("shared_ok", [])) or "none"
    steward = next((l["name"] for l in rows if l.get("steward")), "-")
    for lane in rows:
        others = [o for o in rows if o["name"] != lane["name"]]
        forbidden = "\n".join("- Edit `%s` (lane %s)" % (p, o["name"]) for o in others if not o.get("steward") for p in o.get("owns", []))
        note = "You are the steward: you also own every path no lane claims, and main going red is yours to fix first.\n" if lane.get("steward") else ""
        digits = lane.get("digits", [])
        numbers = ("Sequence numbers (migrations, ADRs) end in %s. Take the smallest number ending in your digit that is above main's newest and every open PR's." % " or ".join(str(d) for d in digits)) if digits else "No numbered files."
        text = LANE_DOC.format(lane=lane["name"], run=job["run"], repo=job["repo"], plan=job["plan_file"], shared=shared,
                               owns="\n".join("- `%s`" % p for p in lane.get("owns", [])) or "- (only what no lane claims)",
                               steward_note=note, modules="\n".join("- %s" % m for m in lane.get("modules", [])) or "- see the plan",
                               forbidden=forbidden or "- (no other lane owns specific paths)", numbers=numbers)
        path = os.path.join(draft, "lanes", lane["name"] + ".md")
        if not os.path.exists(path):
            write_file(path, text)
    table = ["| Lane | Owns | Modules | Digits | Weight |", "|---|---|---|---|---|"]
    sections = []
    for lane in rows:
        table.append("| %s%s | %s | %s | %s | %s |" % (lane["name"], " (steward)" if lane.get("steward") else "", ", ".join("`%s`" % p for p in lane.get("owns", [])) or "-", ", ".join(lane.get("modules", [])) or "-", ", ".join(str(d) for d in lane.get("digits", [])) or "-", lane.get("weight", "-")))
        sections.append("## Lane: %s\n{{Everything to start this lane cold, module by module: current state, requirements and where they live, relevant decisions, open PRs and issues, known traps, first three slices.}}\n" % lane["name"])
    text = PLAN_DOC.format(code=job["code"], stamp=job["stamp"], run=job["run"], repo=job["repo"], kicked=job["kicked_off"], goal=job["goal"],
                           accounts=job["accounts"], max_accounts=job["max_accounts"], where=job["where"], autopilot=job["autopilot"],
                           deadline=job.get("deadline") or "none set", base=job.get("base", "main"), sizing=job.get("sizing") or "set by hand",
                           lane_table="\n".join(table), lane_sections="\n".join(sections), shared=shared, steward=steward,
                           autopilot_rules=AUTOPILOT_FULL if job["autopilot"] == "full" else AUTOPILOT_POLICY, bus=BUS_BRANCH)
    path = os.path.join(draft, job["plan_file"])
    if not os.path.exists(path):
        write_file(path, text)


def docs_dir():
    """The role docs: beside scripts/ in the plugin, beside fb.py in any copy of it. Outside the plugin HERE/../docs is
    somebody else's folder, ~/.config/docs for the installed copy, and whatever is there would go on the bus."""
    root = os.path.dirname(HERE)
    return os.path.join(root if os.path.isdir(os.path.join(root, ".claude-plugin")) else HERE, "docs")


def kit_files():
    """fb.py and the role docs, as they go on the bus."""
    out = {"kit/fb.py": read_text(os.path.abspath(__file__))}
    for path in sorted(globlib.glob(os.path.join(docs_dir(), "*.md"))):
        out["kit/docs/" + os.path.basename(path)] = read_text(path)
    return out


BOOTSTRAP = """You are the account orchestrator for lane {lane} of fast-builder run {run} on account {account}. Set up, then follow the lane brief.

git fetch -q origin +refs/heads/{bus}:refs/remotes/origin/fb-bus
mkdir -p .git/fast-builder-kit
for f in fb.py docs/lane-lead.md docs/conductor.md; do mkdir -p ".git/fast-builder-kit/$(dirname "$f")"; git show "refs/remotes/origin/fb-bus:kit/$f" > ".git/fast-builder-kit/$f"; done
export FB="$(pwd)/.git/fast-builder-kit/fb.py"
python3 "$FB" join --run {run} --lane {lane} --account {account}

Then read .git/fast-builder-kit/docs/lane-lead.md and do what it says. `join` printed the plan and your lane rules: those are binding. Run unattended until the lane is done, blocked or stopped."""


LOCAL_BOOTSTRAP = """You are the account orchestrator for lane {lane} of fast-builder run {run} on account {account}. Set up, then follow the lane brief.

export FB="{kit}/fb.py"
python3 "$FB" join --run {run} --lane {lane} --account {account}

Then read {kit}/docs/lane-lead.md and do what it says. `join` printed the plan and your lane rules: those are binding. Run unattended until the lane is done, blocked or stopped."""


def lane_prompt(run, lane, account, local=False):
    """The session's first message. A cloud session has no plugin, so it fetches the kit from the bus, trusting the
    repo's writers. A local lane uses the copy listen-install made, so the bus never gets to run code on this Mac."""
    if local:
        return LOCAL_BOOTSTRAP.format(run=run, lane=lane, account=account, kit=HOME_DIR)
    return BOOTSTRAP.format(run=run, lane=lane or "<any open lane>", account=account, bus=BUS_BRANCH)


# Session state. Kept inside the git directory, so it is never committed and every worktree of a clone sees it.


def session_path(cwd=None):
    proc = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd or os.getcwd(), check=False, timeout=5)
    return os.path.join(proc.stdout.strip(), "fast-builder", "session.json") if proc.returncode == 0 and proc.stdout.strip() else None


def load_session(cwd=None):
    path = session_path(cwd)
    return (read_json(path), path) if path and os.path.exists(path) else (None, path)


def save_session(path, session):
    write_file(path, dumps(session))


def need_session(cwd=None):
    session, path = load_session(cwd)
    if not session:
        raise FbError("not in a lane here: run join first")
    return session, path, Bus(session["remote"], session["account"])


def holds_claim(claim, session):
    return claim.get("account") == session["account"] and claim.get("session") in (None, session.get("token"))


def build_tick(bus, session):
    """Where the lane stands: the action to take, plus anything new for it."""
    run, lane = session["run"], session["lane"]
    state = run_state(bus, run)
    claim = bus.json("runs/%s/claims/%s.json" % (run, lane)) or {}
    action = state["action"]
    if action in (GO, PAUSE, DRAIN) and not holds_claim(claim, session):
        action = LOST
    inbox = messages_for(bus, run, lane, set(session.get("seen", [])))
    return {"run": run, "lane": lane, "account": session["account"], "state": state["state"], "action": action, "reason": state["reason"],
            "inbox": inbox, "instruction": INSTRUCTIONS[action]}


INSTRUCTIONS = {
    GO: "Carry on with the next work item.",
    PAUSE: "Finish nothing new. Save your handoff, then run fb.py wait until the run resumes.",
    DRAIN: "Finish the slice in hand, land or park it, start nothing new, save your handoff, then fb.py lane-state stopped.",
    STOP: "Stop now. Tell subagents to stop, save your handoff, run fb.py lane-state stopped, and end the session.",
    LOST: "Another account holds this lane now. Stop work and run fb.py leave, then end the session.",
    FINISHED: "The run is finished. Write your end-of-run summary and end the session.",
}


def control_action(session, path):
    """The run's action for the hooks: cached, cheap, and open on any failure so a network blip stops nothing."""
    cache = os.path.join(os.path.dirname(path), "control-cache.json")
    held = read_json(cache) or {}
    if time.time() - held.get("ts", 0) < CONTROL_CACHE_SECONDS:
        return held.get("action", GO)
    try:
        bus = Bus(session["remote"], session["account"])
        bus.lock_seconds = HOOK_LOCK_SECONDS
        sha = bus.head(timeout=4)
        if sha == held.get("sha") and sha:
            action = held.get("action", GO)
        else:
            bus.sync()
            action = build_tick(bus, session)["action"]
        write_file(cache, json.dumps({"ts": time.time(), "sha": sha, "action": action}))
        if action in (GO, DRAIN) and time.time() - session.get("last_beat", 0) >= BEAT_SECONDS:
            # A lead waiting on a long subagent runs no tick, but that subagent's tool calls come through here.
            write_beat(bus, session, beat_status(bus, session), session.get("note", ""))
            session["last_beat"] = time.time()
            save_session(path, session)
        return action
    except FbError:
        return held.get("action", GO)


# Hooks


GUARD_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|Agent|Task"
READ_ONLY_BASH = re.compile(r"^\s*(git\s+(status|log|diff|show|fetch)|ls|cat|grep|rg|head|tail|wc|pwd|echo)\b")
KIT_BASH = re.compile(r"""^\s*python3?\s+(?:"\$FB"|\$FB|"?\S*fb\.py"?)\s""")
PUSH_BASH = re.compile(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+push\b|\bgh\s+pr\s+create\b")


def is_simple(command):
    """One command, nothing chained, redirected or substituted. Allows ; or | inside quotes, as in a message body."""
    if "$(" in command or "`" in command or "\n" in command.strip():
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return not any(set(token) <= set(";&|<>()") for token in lexer)
    except ValueError:
        return False


def allowed_when_halted(tool, command):
    """What a stopped or paused lane may still run: reading, and fb.py itself so it can save a handoff and wait."""
    if tool in ("Read", "Grep", "Glob"):
        return True
    if tool != "Bash" or not is_simple(command):
        return False
    return bool(KIT_BASH.match(command) or READ_ONLY_BASH.match(command))


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


def hook_guard(payload):
    """PreToolUse: enforces the stop switch and the lane boundary on every tool call of a lane session, subagents
    included. A session that is not in a lane returns at once."""
    cwd = payload.get("cwd") or os.getcwd()
    session, path = load_session(cwd)
    if not session:
        return
    tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
    command = tool_input.get("command", "") if tool == "Bash" else ""
    action = control_action(session, path)
    if action == FINISHED:
        return
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    writes = bool(target) and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit")
    if writes and inside_git_dir(target, cwd, path):
        deny("fast-builder: no edits inside .git: a lane's rules live there.")
        return
    # A halted lane still has to write its handoff somewhere, so a file outside the repo stays open.
    if action in (STOP, LOST, PAUSE) and not allowed_when_halted(tool, command) and not (writes and repo_relative(target, cwd, path) is None and outside_tree(target, cwd)):
        deny("fast-builder: run %s says %s. %s" % (session["run"], action, INSTRUCTIONS[action]))
        return
    if action == DRAIN and tool in ("Agent", "Task"):
        deny("fast-builder: run is draining, so no new work. " + INSTRUCTIONS[DRAIN])
        return
    lanes = read_json(os.path.join(os.path.dirname(path), "lanes.json"))
    if not lanes:
        return
    if writes:
        rel = repo_relative(target, cwd, path)
        if rel and not may_touch(rel, session["lane"], lanes):
            deny("fast-builder: %s belongs to lane %s, not %s. Ask for it with: fb.py send --to %s --kind request --body ..." % (rel, owner_of(rel, lanes), session["lane"], owner_of(rel, lanes)))
        return
    if PUSH_BASH.search(command):
        bad = out_of_lane(cwd, session, lanes)
        if bad:
            deny("fast-builder: this branch cannot be pushed from lane %s: %s" % (session["lane"], ", ".join(bad[:5])))


def inside_git_dir(target, cwd, session_file):
    full = os.path.realpath(target if os.path.isabs(target) else os.path.join(cwd, target))
    common = os.path.realpath(os.path.dirname(os.path.dirname(session_file)))
    return full.casefold().startswith(common.casefold() + os.sep)  # macOS paths ignore case: .GIT is .git


def outside_tree(target, cwd):
    """True for a target outside this checkout altogether. A submodule is inside it, though it is another repo."""
    top = git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False, timeout=5).stdout.strip()
    full = os.path.realpath(target if os.path.isabs(target) else os.path.join(cwd, target))
    return bool(top) and not full.casefold().startswith(os.path.realpath(top).casefold() + os.sep)


def repo_relative(target, cwd, session_file):
    """A target's path inside this repo, in whichever worktree of it, or None if it is outside the repo. A subagent's
    worktree may be a sibling of the clone, or nested in it, so the check goes by the target's own working tree."""
    full = os.path.realpath(target if os.path.isabs(target) else os.path.join(cwd, target))
    folder = os.path.dirname(full)
    while folder and not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    proc = git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"], cwd=folder, check=False, timeout=5)
    lines = proc.stdout.split("\n")
    if proc.returncode != 0 or len(lines) < 2:
        return None
    if os.path.realpath(lines[1]) != os.path.realpath(os.path.dirname(os.path.dirname(session_file))):
        return None
    return os.path.relpath(full, os.path.realpath(lines[0]))


def out_of_lane(cwd, session, lanes):
    """Files this branch changes outside the lane. A diff that cannot run counts as a failure, not as clean; renames
    are off so that moving a file out of another lane shows up as the deletion it is."""
    base = "origin/" + session.get("base", "main")
    proc = git(["diff", "--no-renames", "--name-only", base + "...HEAD"], cwd=cwd, check=False, timeout=20)
    if proc.returncode != 0:
        return ["cannot diff against %s (shallow clone? git fetch --unshallow origin)" % base]
    return [p for p in proc.stdout.splitlines() if p and not may_touch(p, session["lane"], lanes)]


def hook_stop(payload):
    """Stop: on full autopilot a lane does not end its turn with work left, up to a cap so it can never spin."""
    session, path = load_session(payload.get("cwd"))
    if not session or session.get("autopilot") != "full":
        return
    if control_action(session, path) != GO:
        return
    try:
        bus = Bus(session["remote"], session["account"])
        bus.lock_seconds = HOOK_LOCK_SECONDS
        bus.sync()
        status = beat_of(bus, session["run"], session["lane"]).get("status")
    except FbError:
        return
    if status != "working":
        return
    count = session.get("stop_blocks", 0)
    if count >= STOP_BLOCK_CAP:
        return
    session["stop_blocks"] = count + 1
    save_session(path, session)
    print(json.dumps({"decision": "block", "reason": "Lane %s is not finished and this run is on autopilot. Run fb.py tick and take the next work item. Waiting on CI: fb.py wait. Finished: fb.py lane-state done. Everything left is blocked: fb.py lane-state blocked." % session["lane"]}))


def hook_session_start(payload):
    """SessionStart: remind a lane session who it is; tell any other session in a listened-to repo about open lanes."""
    cwd = payload.get("cwd") or os.getcwd()
    session, _ = load_session(cwd)
    if session:
        text = "fast-builder: this session is lane %s of run %s on account %s. Run fb.py tick between work items and obey it." % (session["lane"], session["run"], session["account"])
    else:
        config = load_config()
        try:
            remote = cwd_remote(cwd)
        except FbError:
            return
        if repo_slug(remote) not in config.get("repos", {}):
            return
        try:
            bus = Bus(remote, config.get("account", "unknown"))
            if not bus.sync():
                return
        except FbError:
            return
        waiting = []
        for run in active_runs(bus):
            names = [r["lane"] for r in lane_table(bus, run) if r["account"] is None]
            if names:
                waiting.append("%s (open lanes: %s)" % (run, ", ".join(names)))
        if not waiting:
            return
        text = "fast-builder: open lanes in this repo: %s. /fast-builder:join picks one up." % "; ".join(waiting)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


# The listener


def load_config():
    return read_json(os.path.join(HOME_DIR, "config.json")) or {"account": None, "repos": {}}


def save_config(config):
    write_file(os.path.join(HOME_DIR, "config.json"), dumps(config))


def account_name(args=None):
    name = (getattr(args, "account", None) if args else None) or os.environ.get("FB_ACCOUNT") or load_config().get("account")
    if name and NAME_RE.match(name):
        return name
    if name:
        raise FbError("bad account name: %r" % name)
    raise FbError("no account name: pass --account or run /fast-builder:listen")


def children_path():
    return os.path.join(HOME_DIR, "children.json")


def alive(pid):
    """Reaps the pid first: an exited child of this process otherwise still answers signal 0."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def work_clone(remote, run, lane, base):
    """A private clone per lane, on the run's base branch: lanes never share a checkout, and no checkout belongs to
    another Mac user."""
    key = hashlib.sha1(remote.encode()).hexdigest()[:10]
    path = os.path.join(CACHE_DIR, "work", key, "%s-%s" % (run, lane))
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        git(["clone", "-q", "--branch", base, remote, path], timeout=900)
    else:
        git(["fetch", "-q", "origin"], cwd=path, timeout=300)  # a restarted lane keeps its branch and work
    return path


def launch_lane(bus, repo_cfg, run, lane, account, attempt=0):
    """Start a local lane under this account, in its own clone. The prompt is a fixed template: nothing from the bus
    reaches a command line."""
    job = bus.json("runs/%s/job.json" % run) or {}
    errors = job_errors(job)
    if errors or not (RUN_RE.match(run) and NAME_RE.match(lane)):
        raise FbError("refusing an unsafe run: %s" % ", ".join(errors or ["bad run or lane name"]))
    bus.release()  # a first clone can take minutes, and lanes on this account need the bus checkout meanwhile
    clone = work_clone(bus.remote, run, lane, job.get("base", "main"))
    bus.sync()
    os.makedirs(os.path.join(HOME_DIR, "logs"), exist_ok=True)
    log = open(os.path.join(HOME_DIR, "logs", "%s-%s.log" % (run, lane)), "a")
    args = [CLAUDE, "-p", lane_prompt(run, lane, account, local=True), "--model", job.get("lane_model", "opus"), "--permission-mode", repo_cfg.get("permission_mode", "auto"),
            "--permission-prompts", "none", "--name", "fb %s %s" % (run, lane)]
    if os.path.isdir(plugin_copy()):
        args += ["--plugin-dir", plugin_copy()]
    try:
        proc = subprocess.Popen(args, cwd=clone, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=dict(os.environ, FB_ACCOUNT=account, FB=os.path.join(HOME_DIR, "fb.py")), start_new_session=True)
    except OSError as exc:
        raise FbError("cannot start claude: %s" % exc)
    finally:
        log.close()
    children = read_json(children_path()) or {}
    children["%s/%s" % (run, lane)] = {"pid": proc.pid, "remote": bus.remote, "attempt": attempt, "at": now_iso()}
    write_file(children_path(), dumps(children))
    return proc.pid


def stated_status(text):
    """The HTTP status a result opens with. The whole first line has to be the status: "200 was expected, but the tool
    returned 404" is no 200. Whatever follows that line does not matter."""
    line = text.strip().split("\n")[0].strip()
    found = STATUS_RE.match(line)
    return found.group(1) if found else line[:40]


def tool_reply(stream, trigger, action):
    """What RemoteTrigger answered, as (status, body), from the stream-json transcript and not from the model. Only the
    result of this very call counts, so a reply that says 200 proves nothing, and neither does a call that never ran.
    The whole tool is granted, so any other RemoteTrigger call means the model was steered, and that fails it outright."""
    calls, reply = [], ("no tool result", "")
    for line in stream.split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        note = event.get("message") if isinstance(event, dict) else None
        blocks = note.get("content") if isinstance(note, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "RemoteTrigger":
                given = block.get("input")
                if not isinstance(given, dict) or given.get("action") != action or given.get("trigger_id") != trigger:
                    return "another RemoteTrigger call", ""
                calls.append(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls and reply[0] != "200":
                content = block.get("content")
                text = content if isinstance(content, str) else "".join(str(b.get("text", "")) for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
                reply = ("error" if block.get("is_error") else stated_status(text), text.partition("\n")[2])
    return reply


def exit_note(stream, code):
    """Why claude failed, in words that stay the same from one try to the next, or a repeated failure is a new blocker each
    time: stdout opens with events full of fresh ids, and stderr is often empty."""
    subtype = None
    for line in stream.split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            subtype = event.get("subtype")
    return "claude exited %d%s" % (code, " (%s)" % str(subtype)[:40] if subtype else "")


def routine_call(trigger, action):
    """Call the routine API through a headless Haiku, and return the (status, body) the tool got. Exit 0 proves nothing
    here: a routine on another login answers 404 or 400 and claude still exits cleanly, so only a 200 from the tool
    counts. It counts on any exit, since by then the routine has started."""
    if not TRIGGER_RE.match(trigger):
        raise FbError("bad routine id")
    prompt = "Use the RemoteTrigger tool once, with action %s and trigger_id %s. Then reply with only the word done." % (action, trigger)
    args = [CLAUDE, "-p", prompt, "--model", "haiku", "--allowedTools", "RemoteTrigger", "--permission-prompts", "none", "--max-turns", "4", "--output-format", "stream-json", "--verbose"]
    os.makedirs(HOME_DIR, exist_ok=True)
    proc = run_cmd(args, cwd=HOME_DIR, check=False, timeout=120)
    reply = tool_reply(proc.stdout or "", trigger, action)
    if proc.returncode != 0 and reply[0] != "200":
        raise FbError("could not reach routine %s: %s" % (trigger, exit_note(proc.stdout or "", proc.returncode)))
    return reply


def ask_routine_api(trigger, action):
    return routine_call(trigger, action)[0]


def routine_owner(trigger, slug):
    """Which account a routine listens as, as (status, account or None), from the routine's own definition on this login.
    The bus can file any id under any name, so a 200 proves the id and only the routine can say whose it is: listen.md
    has it named `fast-builder listener OWNER/NAME`, with the line `fb.py poll --account NAME` in its prompt."""
    status, body = routine_call(trigger, "get")
    if status != "200":
        return status, None
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    routine = doc.get("trigger") if isinstance(doc, dict) else None
    state = routine.get("derived_state") if isinstance(routine, dict) else None
    prompt = state.get("prompt") if isinstance(state, dict) else None
    names = set(POLL_RE.findall(prompt)) if isinstance(prompt, str) else set()
    if len(names) != 1 or str(routine.get("name") if routine else "").lower() != ("fast-builder listener " + slug).lower():
        return "not a fast-builder listener for " + slug, None
    return status, names.pop()


def routine_roster(bus, account, own=None, first=None):
    """Routine ids to try, this account's own first whatever the bus says, then the row for `first` or this account.
    Logins get swapped on a Mac user, and a routine belongs to its login, so the bus is what lets any Mac user find the
    routine for whoever is signed in now. It can list anything, so no id comes twice, one has to look like a real routine
    id, and the list stops at MAX_ROSTER. Returns the rows and how many it left off."""
    rows, cut, seen = [(account, own)] if own else [], 0, {own}
    for name in sorted(bus.listdir("listeners"), key=lambda n: n != "%s.json" % (first or account)):
        doc = bus.json("listeners/" + name)
        who, trigger = name[:-5], doc.get("routine") if isinstance(doc, dict) else None
        if NAME_RE.match(who) and isinstance(trigger, str) and BUS_TRIGGER_RE.match(trigger) and trigger not in seen:
            seen.add(trigger)
            if len(rows) < MAX_ROSTER:
                rows.append((who, trigger))
            else:
                cut += 1
    return rows, cut


def wake_roster(rows, own, slug, cut=0):
    """Start the routine of whichever account this Mac user is signed in to, and return (account, routine). `claude
    --cloud` needs a real terminal and can stop at interactive dialogs, so a daemon cannot use it. A routine on another
    login answers 404 or 400 and starts nothing, so walking the roster finds the login without trusting a cached profile.

    The bus only suggests ids. `own` came from this Mac user's config and goes first, so a healthy listener tries nothing
    from the bus. Any other row must first prove with `get` that it is a listener for this repo, and that routine, not the
    bus, says whose it is. About half a cent of Haiku a call."""
    first = None
    for who, trigger in rows:
        if trigger != own:
            status, who = routine_owner(trigger, slug)
            if not who:
                first = first or status
                continue
        status = ask_routine_api(trigger, "run")
        if status == "200":
            return who, trigger
        if status not in GONE:
            raise FbError("routine %s did not start (%s)" % (trigger, status))
        first = first or status
    raise FbError("routine %s did not start (%s)%s" % (rows[0][1], first, roster_help(cut)))


def adopt_login(account, routines):
    """Listen as `account` from the next pass, with `routines` by repo. A routine belongs to one login, so a change of name
    also clears every other repo's: each finds its own at its next wake. The same name changes only the repos given."""
    config = load_config()
    renamed = config.get("account") != account
    config["account"] = account
    for slug, cfg in config.get("repos", {}).items():
        if slug in routines:
            cfg["routine_id"] = routines[slug]
        elif renamed:
            cfg["routine_id"] = None
    save_config(config)


ROUTINE_HELP = ": this Mac user's claude login owns none of the routines on the bus, see claude auth status, or set this login up once with /fast-builder:listen"


def roster_help(cut):
    """What to do when nothing started. With rows left off the list, nobody can say this login owns none of them."""
    if not cut:
        return ROUTINE_HELP
    return ": %d more routines on the bus went untried past the limit of %d, so this login may own one of them: remove the rows that do not belong from listeners/ on the bus, or run /fast-builder:switch NAME" % (cut, MAX_ROSTER)


def supervise(bus, repo_cfg, account):
    """Restart a local lane whose claude exited with its lane unfinished, up to a cap, and end children of a stopped
    run after giving them time to write a handoff. Cloud lanes are not children: the conductor covers those."""
    children = read_json(children_path()) or {}
    changed = False
    for key, child in list(children.items()):
        if child.get("remote") != bus.remote:
            continue
        run, lane = key.split("/", 1)
        action = run_state(bus, run)["action"]
        running = alive(child["pid"])
        if action in (STOP, FINISHED):
            if running:
                child.setdefault("term_at", time.time())
                if time.time() - child["term_at"] > 120:
                    try:
                        os.killpg(child["pid"], signal.SIGTERM)
                    except OSError:
                        pass
                changed = True
            elif not running:
                del children[key]
                changed = True
            continue
        if running or action != GO:
            continue
        status = beat_of(bus, run, lane).get("status")
        claim = bus.json("runs/%s/claims/%s.json" % (run, lane)) or {}
        del children[key]
        changed = True
        if status == "working" and claim.get("account") == account and child.get("attempt", 0) < MAX_RESTARTS:
            write_file(children_path(), dumps(children))
            try:
                launch_lane(bus, repo_cfg, run, lane, account, child.get("attempt", 0) + 1)
            except FbError as exc:
                release_lane(bus, run, lane, account, str(exc))
            children = read_json(children_path()) or {}
    if changed:
        write_file(children_path(), dumps(children))


def pick_lane(bus, run, account):
    """The lane this account should take now, if any. A lane freed from an account that went quiet or stopped is fair
    game for an account already in the run; a fresh lane is not, so one account never ends up carrying a whole run."""
    table = lane_table(bus, run)
    open_lanes = [r["lane"] for r in table if r["account"] is None]
    if not open_lanes:
        return None
    if not [r for r in table if r["account"] == account]:
        return open_lanes[0]
    freed = [r["lane"] for r in table if r["account"] is None and r["status"] == "open" and beat_of(bus, run, r["lane"])]
    return freed[0] if freed else None


def claim_picked(bus, run, lane, account):
    second = any(r["account"] == account for r in lane_table(bus, run))
    try:
        return claim_lane(bus, run, account, lane, allow_second=second) == lane
    except Taken:
        return False


def start_lane(bus, repo_cfg, run, lane, account, memory, budget):
    """Local: claim and launch. Cloud: wake this account's routine, which claims a lane from inside the cloud. `budget` is
    what this pass may still spend on wakes: a run it cannot pay for keeps its turn for the next pass."""
    job = bus.json("runs/%s/job.json" % run) or {}
    failed = memory.setdefault("failed", {})
    if job.get("where", "cloud") == "local":
        if time.time() - failed.get(run, 0) < WAKE_SECONDS:  # a lane that cannot start must not churn the bus
            return []
        if not repo_cfg.get("local", True) or not claim_picked(bus, run, lane, account):
            return []
        try:
            return [{"did": "launch", "run": run, "lane": lane, "where": "local", "pid": launch_lane(bus, repo_cfg, run, lane, account)}]
        except FbError as exc:
            failed[run] = time.time()
            release_lane(bus, run, lane, account, str(exc))
            return [{"did": "launch-failed", "run": run, "lane": lane, "error": str(exc)}]
    woke = memory.setdefault("woke", {})
    if time.time() - woke.get(run, 0) < WAKE_SECONDS:
        return []
    if not repo_cfg.get("cloud", True):
        return []
    own = repo_cfg.get("routine_id")
    rows, cut = routine_roster(bus, account, own)
    if rows and budget["calls"] < len(rows):
        return []
    woke[run] = time.time()
    if not rows:
        post_once(bus, run, account, "%s is listening but has no cloud routine, so it cannot take a cloud lane. Run /fast-builder:listen --routine on it." % account)
        return [{"did": "no-routine", "run": run}]
    slug = repo_slug(bus.remote)
    try:
        bus.release()  # the wake takes seconds, and lanes on this account need the bus checkout meanwhile
        who, trigger = wake_roster(rows, own, slug, cut)
    except FbError as exc:
        budget["calls"] = 0  # the same rows and the same login: another run's walk would find the same nothing
        post_once(bus, run, account, "%s could not start its routine: %s" % (account, exc))
        return [{"did": "wake-failed", "run": run, "error": str(exc)}]
    # It started, so nothing after this line may say otherwise. The walk only gets past `own` on a 400 or 404, so a new
    # name with an `own` is a proved swap. With no `own` nothing was disproved: the routine ran, and the name stays.
    memory["proved"] = [who, trigger]
    if who == account:
        repo_cfg["routine_id"] = trigger
    if trigger != own and (who == account or own):
        adopt_login(who, {slug: trigger})
    budget["calls"] -= 1 if trigger == own else len(rows) + 1
    if who != account and own:
        budget["calls"] = 0  # the rest of this pass is working under the old name: the next one starts under the new
    action = {"did": "woke-routine", "run": run, "routine": trigger, "account": who}
    try:
        bus.sync()
    except FbError as exc:
        action["sync_failed"] = str(exc)
    return [action]


def release_lane(bus, run, lane, account, reason):
    """Give a lane back, so the next listener can take it."""
    def mutate():
        return {"runs/%s/claims/%s.json" % (run, lane): None,
                "runs/%s/beats/%s.json" % (run, lane): dumps({"at": now_iso(), "status": "open", "account": None, "note": reason[:200]}),
                "runs/%s/msgs/%s.json" % (run, make_id(account)): dumps(message(account, "human", "blocker", "Could not start lane %s: %s" % (lane, reason)))}
    bus.commit(mutate, "release(%s): %s" % (run, lane))


def poll_repo(remote, repo_cfg, account, heads, launch=True):
    """One pass over one repo. Costs one ls-remote and no tokens unless the bus moved."""
    bus = Bus(remote, account)
    sha = bus.head()
    actions = []
    last = heads.setdefault(remote, {})  # here and not at the end: a pass that dies must still remember its wake
    beat_due = time.time() - last.get("beat", 0) > 3600
    conduct_due = time.time() - last.get("conduct", 0) > 300
    if not sha or (sha == last.get("sha") and not beat_due and not conduct_due and not (read_json(children_path()) or {})):
        return actions
    bus.sync()
    if conduct_due:  # before supervise: a dead lane it frees must not be restarted there and started again below
        for run in active_runs(bus):
            report = conduct(bus, run, account)
            if report["freed"] or report["finished"]:
                actions.append(dict(report, did="conduct"))
    supervise(bus, repo_cfg, account)  # before any wake: those run one Haiku call at a time, and a stop must not wait
    budget = {"calls": PASS_CALLS}
    for run in active_runs(bus):
        if launch and run_state(bus, run)["action"] == GO:
            pick = pick_lane(bus, run, account)
            if pick:
                actions += start_lane(bus, repo_cfg, run, pick, account, last, budget)
                if actions and actions[-1].get("sync_failed"):  # the checkout is not one to read: the next pass syncs again
                    bus.release()
                    return actions
    if conduct_due:
        last["conduct"] = time.time()
    if beat_due:
        routine = repo_cfg.get("routine_id")
        doc = {"account": account, "at": now_iso(), "cloud": repo_cfg.get("cloud", True), "local": repo_cfg.get("local", True), "routine": routine, "version": VERSION}
        announce_listener(bus, account, doc, proved=last.pop("proved", None) == [account, routine])
        last["beat"] = time.time()
    last["sha"] = bus.head()
    bus.release()
    return actions


def listen_loop(args):
    heads = {}
    stopped = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stopped.update(now=True))
    while not stopped["now"]:
        config = load_config()
        # Read every pass: a swapped login changes this without a restart, and lanes started below inherit it.
        account = getattr(args, "account", None) or config.get("account") or account_name(args)
        if os.environ.get("FB_ACCOUNT") != account:
            os.environ["FB_ACCOUNT"] = account
            for last in heads.values():  # beat and look again under the new name, but what was started stays started
                for key in ("sha", "beat", "conduct"):
                    last.pop(key, None)
        for slug, cfg in config.get("repos", {}).items():
            try:
                for action in poll_repo(cfg["remote"], cfg, account, heads):
                    print(now_iso(), slug, json.dumps(action), flush=True)
            except Exception as exc:  # bus content comes from other accounts: no repo may take the listener down
                print(now_iso(), slug, "error:", repr(exc), flush=True)
        if args.once:
            return
        for _ in range(int(args.seconds)):
            if stopped["now"]:
                return
            time.sleep(1)


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{python}</string><string>{script}</string><string>listen</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string><key>FB_ACCOUNT</key><string>{account}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
LABEL = "com.shorconsulting.fast-builder.listener"


def plugin_copy():
    return os.path.join(HOME_DIR, "plugin")


def plist_path():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")


def load_listener(domain):
    """launchd refuses a bootstrap for a moment after a bootout, so a reinstall has to retry."""
    for attempt in range(10):
        proc = run_cmd(["launchctl", "bootstrap", domain, plist_path()], check=False)
        if proc.returncode == 0:
            return
        time.sleep(1)
    raise FbError("launchctl bootstrap failed: %s" % (proc.stderr or proc.stdout).strip()[:300])


def announce_listener(bus, account, doc, proved=False):
    """Say this account is listening, and put the kit on the bus so its routine can poll before any run exists. A routine
    nothing has just `proved` never replaces the one on the bus: the config can be the stale side of the two."""
    def mutate():
        held = bus.json("listeners/%s.json" % account)
        kept = held.get("routine") if isinstance(held, dict) else None
        if not proved and isinstance(kept, str) and BUS_TRIGGER_RE.match(kept):
            doc["routine"] = kept
        files = {"listeners/%s.json" % account: dumps(doc)}
        if not bus.exists("kit/fb.py"):
            files.update(kit_files())
        return files
    bus.commit(mutate, "listener: %s" % account)


def listen_install(args):
    """Register a repo for this account and install the launchd listener, so it survives reboots and costs no tokens."""
    account = account_name(args)
    config = load_config()
    same_account = config.get("account") in (None, account)
    if not same_account:  # a routine belongs to one login, so no repo keeps the previous name's
        for cfg in config.get("repos", {}).values():
            cfg["routine_id"] = None
    config["account"] = account
    remote = args.remote or cwd_remote()
    slug = repo_slug(remote)
    held = config.setdefault("repos", {}).get(slug, {})
    routine = args.routine_id or (held.get("routine_id") if same_account else None)
    config["repos"][slug] = {"remote": remote, "permission_mode": args.permission_mode, "cloud": not args.no_cloud, "local": not args.no_local, "routine_id": routine}
    save_config(config)
    script = os.path.join(HOME_DIR, "fb.py")
    os.makedirs(HOME_DIR, exist_ok=True)
    if os.path.abspath(__file__) != script:
        shutil.copyfile(os.path.abspath(__file__), script)
    for path in globlib.glob(os.path.join(docs_dir(), "*.md")):
        write_file(os.path.join(HOME_DIR, "docs", os.path.basename(path)), read_text(path))
    root = os.path.dirname(HERE)
    if os.path.isdir(os.path.join(root, ".claude-plugin")) and os.path.abspath(root) != plugin_copy():
        # Headless lanes load this copy, so the guard runs even on an account that never installed the plugin.
        shutil.rmtree(plugin_copy(), ignore_errors=True)
        shutil.copytree(root, plugin_copy(), ignore=shutil.ignore_patterns(".git", "__pycache__", "*.zip"))
    claude = shutil.which(CLAUDE) or ""
    path = os.pathsep.join(p for p in [os.path.dirname(claude), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"] if p)
    text = PLIST.format(label=LABEL, python=sys.executable, script=script, path=path, account=account, log=os.path.join(HOME_DIR, "listener.log"))
    out = {"account": account, "repo": slug, "permission_mode": args.permission_mode, "plist": plist_path()}
    if args.dry_run:
        out["plist_text"] = text
    else:
        write_file(plist_path(), text)
        domain = "gui/%d" % os.getuid()
        run_cmd(["launchctl", "bootout", domain + "/" + LABEL], check=False)
        load_listener(domain)
        bus = Bus(remote, account)
        doc = {"account": account, "at": now_iso(), "cloud": not args.no_cloud, "local": not args.no_local, "routine": routine, "version": VERSION}
        announce_listener(bus, account, doc, proved=bool(args.routine_id))
    print(json.dumps(out, indent=2))


def listen_uninstall(args):
    config = load_config()
    if args.remote or args.repo:
        slug = args.repo or repo_slug(args.remote)
        config.get("repos", {}).pop(slug, None)
        save_config(config)
    if not config.get("repos") or args.all:
        run_cmd(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], check=False)
        if os.path.exists(plist_path()):
            os.remove(plist_path())
    print(json.dumps({"repos_left": sorted(config.get("repos", {}))}))


# Commands


def get_bus(args):
    remote = getattr(args, "remote", None) or (load_session()[0] or {}).get("remote") or cwd_remote()
    try:
        account = account_name(args)
    except FbError:
        account = "unknown"
    return Bus(remote, account)


def pick_run(bus, args):
    """The named run, the session's run, or the only active one."""
    if getattr(args, "run", None):
        if not RUN_RE.match(args.run):
            raise FbError("bad run name")
        return args.run
    session = load_session()[0]
    if session:
        return session["run"]
    runs = active_runs(bus)
    if len(runs) == 1:
        return runs[0]
    raise FbError("%d active runs: pass --run (%s)" % (len(runs), ", ".join(runs)))


def out(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def cmd_draft(args):
    bus = get_bus(args)
    bus.sync()
    remote = bus.remote
    slug = repo_slug(remote)
    settings = bus.json("settings.json") or {}
    code = re.sub(r"[^A-Za-z0-9-]+", "-", args.code or slug.split("/")[-1]).strip("-").upper()
    today = datetime.datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else datetime.date.today()
    run, plan_file = run_names(code, today, set(list_runs(bus)))
    max_accounts = args.max_accounts or settings.get("max_accounts") or DEFAULT_MAX_ACCOUNTS
    draft = args.dir or tempfile.mkdtemp(prefix="fb-draft-")
    job = {"run": run, "repo": slug, "code": code.upper(), "stamp": today.strftime("%m-%d-%y"), "kicked_off": now_iso(), "goal": args.goal,
           "accounts": args.accounts or 0, "max_accounts": max_accounts, "where": args.where or "cloud", "autopilot": args.autopilot or "full",
           "plan_file": plan_file, "deadline": args.deadline, "base": args.base or default_branch(), "created_by": bus.account, "lane_model": args.lane_model, "sizing": ""}
    write_file(os.path.join(draft, "job.json"), dumps(job))
    out({"draft": draft, "run": run, "plan_file": plan_file, "max_accounts": max_accounts, "where": job["where"], "autopilot": job["autopilot"]})


def cmd_size(args):
    root = args.root or os.getcwd()
    measured = None if args.rate else throughput(root)
    rate = args.rate or measured or DEFAULT_RATE_PER_DAY
    bus = get_bus(args)
    bus.sync()
    max_accounts = args.max_accounts or (bus.json("settings.json") or {}).get("max_accounts") or DEFAULT_MAX_ACCOUNTS
    result = size(args.items, args.hours, max_accounts, rate)
    result.update(rate_per_account_day=rate, rate_source="--rate" if args.rate else ("measured: merged PRs a day, last 14 days" if measured else "assumed default, no history"), max_accounts=max_accounts)
    result["listeners"] = [n[:-5] for n in bus.listdir("listeners") if age_minutes((bus.json("listeners/" + n) or {}).get("at")) < 90]
    out(result)


def cmd_partition(args):
    root = args.root or os.getcwd()
    items = read_json(args.items) if args.items else tree_items(root, args.depth)
    if not items:
        raise FbError("nothing to partition")
    bins = partition(items, args.accounts)
    digits = allocate_digits(args.accounts)
    lanes = []
    for i, entry in enumerate(bins):
        lanes.append({"name": "lane-%d" % (i + 1), "owns": entry["paths"], "modules": entry["items"], "digits": digits[i], "weight": entry["weight"], "steward": i == 0})
    out({"lanes": lanes, "shared_ok": []})


def repo_files(root):
    proc = git(["ls-files"], cwd=root or os.getcwd(), check=False, timeout=30)
    return proc.stdout.splitlines() if proc.returncode == 0 else None


def cmd_render(args):
    render_draft(args.draft, None)
    job = read_json(os.path.join(args.draft, "job.json"))
    out({"plan": os.path.join(args.draft, job["plan_file"]), "lanes": sorted(os.listdir(os.path.join(args.draft, "lanes")))})


def cmd_validate(args):
    errors, facts = validate(args.draft, repo_files(args.root))
    out({"ok": not errors, "errors": errors, "facts": facts})
    if errors:
        sys.exit(1)


def cmd_publish(args):
    errors, facts = validate(args.draft, repo_files(args.root))
    if errors:
        out({"ok": False, "errors": errors})
        sys.exit(1)
    job = read_json(os.path.join(args.draft, "job.json"))
    run = job["run"]
    bus = get_bus(args)
    bus.account = job.get("created_by", bus.account)

    def mutate():
        if bus.exists("runs/%s/job.json" % run):
            raise Taken("run %s already exists" % run)
        prefix = "runs/%s/" % run
        files = {prefix + "job.json": read_text(os.path.join(args.draft, "job.json")), prefix + "lanes.json": read_text(os.path.join(args.draft, "lanes.json")),
                 prefix + job["plan_file"]: read_text(os.path.join(args.draft, job["plan_file"])),
                 prefix + "control.json": dumps({"state": "running", "reason": "kickoff", "by": bus.account, "at": now_iso()})}
        for name in os.listdir(os.path.join(args.draft, "lanes")):
            files[prefix + "lanes/" + name] = read_text(os.path.join(args.draft, "lanes", name))
        files.update(kit_files())
        settings = bus.json("settings.json") or {"format": FORMAT}
        settings["max_accounts"] = job["max_accounts"]
        files["settings.json"] = dumps(settings)
        return files

    bus.commit(mutate, "kickoff: %s" % run)
    bus.sync()
    out({"ok": True, "run": run, "bus": BUS_BRANCH, "plan": "runs/%s/%s" % (run, job["plan_file"]), "lanes": lane_names(bus, run), "facts": facts,
         "listeners": [n[:-5] for n in bus.listdir("listeners")]})


def cmd_join(args):
    bus = get_bus(args)
    account = account_name(args)
    bus.account = account
    if not bus.sync():
        raise FbError("no bus on this repo yet")
    run = pick_run(bus, argparse.Namespace(run=None if args.run in (None, "any") else args.run))
    lane = None if args.lane in (None, "any", "<any open lane>") else args.lane
    if lane and not NAME_RE.match(lane):
        raise FbError("bad lane name")
    token = uuid.uuid4().hex
    lane = claim_lane(bus, run, account, lane, allow_second=args.second, token=token)
    bus.sync()
    claim = bus.json("runs/%s/claims/%s.json" % (run, lane)) or {}
    if claim.get("account") != account or claim.get("session") != token:
        raise Taken("lane %s is held by %s" % (lane, claim.get("account")))
    job = bus.json("runs/%s/job.json" % run) or {}
    if job_errors(job):
        raise FbError("refusing run %s: %s" % (run, ", ".join(job_errors(job))))
    path = session_path()
    if not path:
        raise FbError("join from inside a clone of the repo")
    save_session(path, {"run": run, "lane": lane, "account": account, "remote": bus.remote, "autopilot": job.get("autopilot"), "base": job.get("base", "main"), "joined": now_iso(), "token": token})
    write_file(os.path.join(os.path.dirname(path), "lanes.json"), dumps(lanes_of(bus, run)))
    print("JOINED run=%s lane=%s account=%s where=%s autopilot=%s" % (run, lane, account, claim.get("where"), job.get("autopilot")))
    print("\n--- LANE RULES (binding) ---\n")
    print(bus.read("runs/%s/lanes/%s.md" % (run, lane), ""))
    print("--- HANDOFF ---\n")
    print(bus.read("runs/%s/handoffs/%s.md" % (run, lane), "(none: this lane is starting fresh)\n"))
    print("--- PLAN ---\n")
    print(bus.read("runs/%s/%s" % (run, job.get("plan_file")), ""))


def cmd_tick(args):
    session, path, bus = need_session()
    bus.sync()
    result = build_tick(bus, session)
    if result["action"] in (GO, DRAIN, PAUSE) and (time.time() - session.get("last_beat", 0) >= BEAT_SECONDS or args.note):
        # Before conducting: a lane's own quiet spell must not get it freed by its own tick.
        session["note"] = args.note or session.get("note", "")
        write_beat(bus, session, beat_status(bus, session), session["note"])
        session["last_beat"] = time.time()
    if result["action"] in (GO, DRAIN):
        report = conduct(bus, session["run"], session["account"])
        waiting = [m for m in messages_for(bus, session["run"], "conductor", set(session.get("conductor_seen", []))) if m.get("kind") in ("request", "blocker")]
        result["conductor"] = report["holder"]
        result["requests_for_conductor"] = len(waiting) if report["holder"] == session["account"] else 0
        result["freed"] = report["freed"]
    if result["inbox"]:
        session["seen"] = (session.get("seen", []) + [m["id"] for m in result["inbox"]])[-1000:]
    session["stop_blocks"] = 0
    save_session(path, session)
    out(result)
    sys.exit(EXIT_CODES[result["action"]] if args.exit_code else 0)


def beat_status(bus, session):
    return beat_of(bus, session["run"], session["lane"]).get("status", "working")


def write_beat(bus, session, status, note):
    """Write the lane's heartbeat, but only while this session still holds the lane: a session that lost it must not
    be able to mark the new holder's lane done."""
    run, lane = session["run"], session["lane"]
    doc = {"at": now_iso(), "status": status, "account": session["account"], "note": str(note)}

    def mutate():
        if not holds_claim(bus.json("runs/%s/claims/%s.json" % (run, lane)) or {}, session):
            raise Lost("this session no longer holds lane %s" % lane)
        return {"runs/%s/beats/%s.json" % (run, lane): dumps(doc)}

    bus.commit(mutate, "beat(%s): %s %s" % (run, lane, status))


def cmd_lane_state(args):
    session, path, bus = need_session()
    try:
        write_beat(bus, session, args.status, args.note or "")
    except Lost:
        if args.status == "stopped":
            leave_session(path)
        raise
    if args.status == "stopped":
        leave_session(path)
    out({"lane": session["lane"], "status": args.status})


def leave_session(path):
    """Turn this clone back into a plain clone, so the hooks stop treating its sessions as a lane."""
    folder = os.path.dirname(path)
    for name in ("session.json", "control-cache.json", "lanes.json"):
        if os.path.exists(os.path.join(folder, name)):
            os.remove(os.path.join(folder, name))


def cmd_leave(args):
    session, path, bus = need_session()
    leave_session(path)
    out({"left": session["lane"], "run": session["run"]})


def cmd_wait(args):
    """Wait without spending tokens, waking at once if the bus has something for this lane, and keeping the lane's
    heartbeat alive so that waiting on CI never looks like a dead account."""
    session, path, bus = need_session()
    end = time.time() + args.seconds
    seen = bus.head()
    last_beat = session.get("last_beat", 0)
    while time.time() < end:
        time.sleep(min(args.poll, max(0.0, end - time.time())))
        try:
            sha = bus.head()
            if sha != seen:
                bus.sync()
                seen = sha  # only once synced: a busy checkout means this change is looked at again next pass
                tick = build_tick(bus, session)
                if tick["inbox"] or tick["action"] not in (GO, PAUSE):
                    out({"woke": "bus", "action": tick["action"], "messages": len(tick["inbox"])})
                    return
                if tick["action"] == GO and args.until_resumed:
                    out({"woke": "resumed", "action": GO})
                    return
            if time.time() - last_beat >= BEAT_SECONDS:
                write_beat(bus, session, beat_status_fresh(bus, session), session.get("note", ""))
                last_beat = session["last_beat"] = time.time()
                save_session(path, session)
                seen = bus.head()
        except Lost:
            out({"woke": "lost", "action": LOST})
            return
        except FbError:
            pass
        finally:
            bus.release()
    out({"woke": "timeout"})


def beat_status_fresh(bus, session):
    bus.sync()
    return beat_status(bus, session)


def cmd_send(args):
    session = load_session()[0]
    bus = get_bus(args)
    run = pick_run(bus, args)
    frm = args.frm or (session["lane"] if session else account_name(args))
    post(bus, run, frm, args.to, args.kind, args.body)
    out({"sent": True, "run": run, "from": frm, "to": args.to, "kind": args.kind})


def cmd_inbox(args):
    bus = get_bus(args)
    bus.sync()
    session, path = load_session()
    seen = set((session or {}).get("conductor_seen", [])) if args.who == "conductor" else set()
    got = messages_for(bus, pick_run(bus, args), args.who, seen)
    if args.mark and session and args.who == "conductor" and got:
        session["conductor_seen"] = (session.get("conductor_seen", []) + [m["id"] for m in got])[-1000:]
        save_session(path, session)
    out(got)


def cmd_decide(args):
    session, _, bus = need_session()
    body = "Chose: %s. Why: %s. Undo: %s." % (args.what, args.why, args.undo)
    post(bus, session["run"], session["lane"], "human", "decision", body)
    out({"logged": body})


def cmd_handoff(args):
    session, _, bus = need_session()
    text = read_text(args.file)
    if text is None:
        raise FbError("cannot read " + args.file)
    rel = "runs/%s/handoffs/%s.md" % (session["run"], session["lane"])
    bus.commit(lambda: {rel: text}, "handoff(%s): %s" % (session["run"], session["lane"]))
    out({"saved": rel})


def control_cmd(state, reason_default):
    def run(args):
        bus = get_bus(args)
        if not bus.sync():
            raise FbError("no bus on this repo yet")
        if args.all and state == "running":
            runs = [r for r in list_runs(bus) if bus.exists("runs/%s/STOP" % r) or (bus.json("runs/%s/control.json" % r) or {}).get("state") in ("paused", "draining", "stopping")]
        elif args.all:
            runs = [r for r in list_runs(bus) if run_state(bus, r)["state"] not in ("finished", "stopped")]
        elif state == "running" and not args.run and not load_session()[0]:
            runs = [r for r in list_runs(bus) if (bus.json("runs/%s/control.json" % r) or {}).get("state", "running") in TRANSITIONS["running"]]
            if len(runs) != 1:
                raise FbError("%d runs could resume: pass --run or --all (%s)" % (len(runs), ", ".join(runs)))
        else:
            runs = [pick_run(bus, args)]
        moved = [r for r in runs if set_control(bus, r, state, args.reason or reason_default, bus.account)]
        cleared = False
        if state == "running" and args.all:
            cleared = bus.commit(lambda: {"STOP": None}, "resume: clear the stop on every run")
        result = {"state": state, "runs": moved, "unchanged": [r for r in runs if r not in moved]}
        if state == "running" and args.all:
            result["not_resumed_stopped"] = [r for r in list_runs(bus) if r not in runs and run_state(bus, r)["state"] == "stopped"]
        if state == "running":
            bus.sync()
            if bus.exists("STOP"):
                result["warning"] = "A STOP file is still on the bus, so every run stays stopped: resume --all clears it"
            result["cleared_stop_file"] = bool(cleared)
        out(result)
    return run


def cmd_stop(args):
    state = "draining" if args.drain else "stopping"
    control_cmd(state, "stopped by %s" % (args.reason or "request"))(args)


def cmd_status(args):
    bus = get_bus(args)
    if not bus.sync():
        out({"bus": False, "note": "no bus on this repo yet"})
        return
    runs = [pick_run(bus, args)] if getattr(args, "run", None) else list_runs(bus)
    if args.human:
        print(render_status(bus))
        return
    report = []
    for run in runs:
        job = bus.json("runs/%s/job.json" % run) or {}
        report.append({"run": run, "goal": job.get("goal"), "where": job.get("where"), "autopilot": job.get("autopilot"), "state": run_state(bus, run)["state"],
                       "lanes": lane_table(bus, run), "conductor": (bus.json("runs/%s/conductor.json" % run) or {}).get("holder"),
                       "needs_human": [m["body"] for m in messages_for(bus, run, "human") if m.get("kind") == "needs-human"]})
    out({"runs": report, "listeners": [dict(bus.json("listeners/" + n) or {}, stale=age_minutes((bus.json("listeners/" + n) or {}).get("at")) > 90) for n in bus.listdir("listeners")]})


def cmd_report(args):
    bus = get_bus(args)
    bus.sync()
    run = pick_run(bus, args)
    print("# Run %s: %s\n" % (run, run_state(bus, run)["state"]))
    for row in lane_table(bus, run):
        print("- %s (%s): %s. %s" % (row["lane"], row["account"] or "unheld", row["status"], row["note"]))
    all_msgs = messages_for(bus, run, None)
    decisions = [m for m in all_msgs if m.get("kind") == "decision"]
    human = [m for m in all_msgs if m.get("kind") == "needs-human"]
    print("\n## Decided without you\n")
    print("\n".join("- %s: %s" % (m["from"], m["body"]) for m in decisions) or "None.")
    print("\n## Waiting on a person\n")
    print("\n".join("- %s: %s" % (m["from"], m["body"]) for m in human) or "Nothing.")


def cmd_prompt(args):
    bus = get_bus(args)
    bus.sync()
    run = pick_run(bus, args)
    rows = [r for r in lane_table(bus, run) if r["account"] is None or args.lane == r["lane"]]
    if args.lane:
        rows = [r for r in rows if r["lane"] == args.lane]
    for row in rows:
        print("=== lane %s: paste into a new %s session on the account that takes it ===\n" % (row["lane"], (bus.json("runs/%s/job.json" % run) or {}).get("where", "cloud")))
        print(lane_prompt(run, row["lane"], args.account or "<account name>"))
        print()


def cmd_route(args):
    out(route(args.kind, args.paths or [], sensitive_globs(args.root), args.changes_verdicts, args.stalled))


def cmd_check_diff(args):
    session, path, _ = need_session()
    lanes = read_json(os.path.join(os.path.dirname(path), "lanes.json")) or {"lanes": []}
    bad = out_of_lane(os.getcwd(), session, lanes)
    out({"ok": not bad, "outside_lane": bad})
    if bad:
        sys.exit(1)


def cmd_conduct(args):
    bus = get_bus(args)
    bus.sync()
    run = pick_run(bus, args)
    out(conduct(bus, run, bus.account, args.stale_minutes))


def cmd_settings(args):
    bus = get_bus(args)

    def mutate():
        settings = bus.json("settings.json") or {"format": FORMAT}
        if args.max_accounts:
            settings["max_accounts"] = args.max_accounts
        return {"settings.json": dumps(settings)}

    if args.max_accounts:
        bus.commit(mutate, "settings: max_accounts %s" % args.max_accounts)
    bus.sync()
    out(bus.json("settings.json"))


def cmd_listen(args):
    listen_loop(args)


def cmd_poll(args):
    """One listener pass, for a routine that has no daemon: claims a lane and prints what to do, spending nothing else."""
    bus = get_bus(args)
    account = account_name(args)
    bus.account = account
    if not bus.sync():
        print("NOTHING")
        return
    for run in active_runs(bus):
        conduct(bus, run, account)
        if run_state(bus, run)["action"] != GO:
            continue
        lane = pick_lane(bus, run, account)
        if lane and claim_picked(bus, run, lane, account):
            print("LANE run=%s lane=%s\n" % (run, lane))
            print(lane_prompt(run, lane, account))
            return
    print("NOTHING")


def cmd_hook(args):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    try:
        {"guard": hook_guard, "stop": hook_stop, "session-start": hook_session_start}[args.event](payload)
    except Exception:  # a hook failing must never take a working session down with it
        pass


def cmd_routine_check(args):
    """Prove this Mac user's headless claude can reach the routine, which is what a daemon's wake depends on."""
    status = ask_routine_api(args.routine_id, "get")
    out({"routine": args.routine_id, "reachable": status == "200", "status": status, "help": "" if status == "200" else ROUTINE_HELP.lstrip(": ")})
    if status != "200":
        sys.exit(1)


def cmd_switch(args):
    """Point this Mac user's listener at the account its claude login now belongs to, after a swap. Names one, or with no
    name finds it, by asking which routine on the bus this login can see. The bus only suggests ids: `get` proves this
    login has the routine, and the routine says whose it is. One repo that cannot be read costs that repo, not the switch."""
    config = load_config()
    current = config.get("account")
    known, unreachable, cut = {}, {}, 0
    for slug, cfg in config.get("repos", {}).items():
        bus = Bus(cfg["remote"], current or "unknown")
        try:
            if bus.sync():
                rows, left = routine_roster(bus, current, cfg.get("routine_id"), first=args.name)
                cut += left
                for who, trigger in rows:
                    known.setdefault(who, []).append((slug, trigger))
        except (FbError, OSError) as exc:
            unreachable[slug] = str(exc)[:200]
        finally:
            bus.release()
    if args.name and args.name not in known:
        raise FbError("no routine for %s on the bus: run /fast-builder:listen once on that login%s" % (args.name, " (could not read: %s)" % ", ".join(sorted(unreachable)) if unreachable else ""))
    for who in [args.name] if args.name else sorted(known, key=lambda n: n != current):
        routines, unsure = {}, None
        for slug, trigger in known[who]:
            if slug in routines:
                continue
            if who == current and trigger == config["repos"][slug].get("routine_id"):  # the config's own: nothing to rename
                status = ask_routine_api(trigger, "get")
                if status == "200":
                    routines[slug] = trigger
                elif status not in GONE:
                    unsure = "routine %s answered %s, which proves no swap" % (trigger, status)
            elif routine_owner(trigger, slug)[1] == who:
                routines[slug] = trigger
        if routines:
            adopt_login(who, routines)
            out({"account": who, "was": current, "switched": True, "routines": routines, "unreachable": unreachable})
            return
        if unsure and not args.name:
            raise FbError(unsure)
    out({"account": current, "switched": False, "help": roster_help(cut).lstrip(": "), "unreachable": unreachable})
    sys.exit(1)


def cmd_install_repo(args):
    """Commit-ready files for cloud enforcement: cloud sessions skip user plugins but run the repo's own hooks and
    agents. Writes into this clone only; the caller opens the PR."""
    root = git(["rev-parse", "--show-toplevel"]).stdout.strip()
    kit = os.path.join(root, ".claude", "fast-builder")
    os.makedirs(kit, exist_ok=True)
    shutil.copyfile(os.path.abspath(__file__), os.path.join(kit, "fb.py"))
    settings_path = os.path.join(root, ".claude", "settings.json")
    settings = read_json(settings_path) or {}
    hooks = settings.setdefault("hooks", {})

    def wire(event, matcher, name):
        command = 'python3 "$CLAUDE_PROJECT_DIR/.claude/fast-builder/fb.py" hook %s' % name
        rows = hooks.setdefault(event, [])
        if not any(h.get("command") == command for row in rows for h in row.get("hooks", [])):
            entry = {"hooks": [{"type": "command", "command": command}]}
            if matcher:
                entry["matcher"] = matcher
            rows.append(entry)

    wire("PreToolUse", GUARD_MATCHER, "guard")
    wire("Stop", None, "stop")
    write_file(settings_path, dumps(settings))
    written = [".claude/fast-builder/fb.py", ".claude/settings.json"]
    for path in sorted(globlib.glob(os.path.join(HERE, "..", "agents", "*.md"))):
        name = "fb-" + os.path.basename(path)
        shutil.copyfile(path, os.path.join(root, ".claude", "agents", name)) if os.path.isdir(os.path.join(root, ".claude", "agents")) else (os.makedirs(os.path.join(root, ".claude", "agents")), shutil.copyfile(path, os.path.join(root, ".claude", "agents", name)))
        written.append(".claude/agents/" + name)
    out({"written": written})


def cmd_doctor(args):
    checks = {"git": bool(shutil.which("git")), "python": sys.version.split()[0], "claude": shutil.which(CLAUDE), "gh": bool(shutil.which("gh")), "account": load_config().get("account"), "listener_plist": os.path.exists(plist_path())}
    try:
        remote = cwd_remote()
        bus = Bus(remote)
        checks.update(remote=repo_slug(remote), bus_reachable=True, bus_exists=bool(bus.head()))
        if bus.sync():
            checks["routines_on_bus"] = sorted(who for who, _ in routine_roster(bus, checks["account"])[0])
        bus.release()
    except FbError as exc:
        checks.update(bus_reachable=False, error=str(exc))
    checks["repos"] = sorted(load_config().get("repos", {}))
    out(checks)


def build_parser():
    parser = argparse.ArgumentParser(prog="fb.py", description="fast-builder: one repo, several Claude accounts")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd")
    sub.required = True

    def add(name, func, **kw):
        p = sub.add_parser(name, **kw)
        p.set_defaults(func=func)
        p.add_argument("--remote", help="bus remote (default: origin here)")
        p.add_argument("--account", help="this account's name (default: from the listener config)")
        return p

    p = add("draft", cmd_draft, help="start a run's draft")
    p.add_argument("--goal", required=True)
    p.add_argument("--accounts", type=int)
    p.add_argument("--max-accounts", type=int)
    p.add_argument("--where", choices=["cloud", "local"])
    p.add_argument("--autopilot", choices=["full", "policy"])
    p.add_argument("--deadline")
    p.add_argument("--code", help="short repo code for the plan's file name, such as HPH")
    p.add_argument("--base")
    p.add_argument("--lane-model", default="opus")
    p.add_argument("--dir")
    p.add_argument("--today", help="YYYY-MM-DD")

    p = add("size", cmd_size, help="how many accounts finish in time")
    p.add_argument("--items", type=int, required=True)
    p.add_argument("--hours", type=float)
    p.add_argument("--max-accounts", type=int)
    p.add_argument("--rate", type=float, help="merges a day per account")
    p.add_argument("--root")

    p = add("partition", cmd_partition, help="propose lanes")
    p.add_argument("--accounts", type=int, required=True)
    p.add_argument("--items", help="JSON list of {id, paths, weight}")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--root")

    p = add("render", cmd_render, help="write lane rules and plan skeleton from a draft")
    p.add_argument("--draft", required=True)

    for name, func in (("validate", cmd_validate), ("publish", cmd_publish)):
        p = add(name, func, help=name + " a draft")
        p.add_argument("--draft", required=True)
        p.add_argument("--root")

    p = add("join", cmd_join, help="claim a lane and make this clone a lane session")
    p.add_argument("--run")
    p.add_argument("--lane")
    p.add_argument("--second", action="store_true", help="take a second lane in a run")

    p = add("tick", cmd_tick, help="heartbeat, read control and inbox")
    p.add_argument("--note")
    p.add_argument("--exit-code", action="store_true")

    p = add("lane-state", cmd_lane_state, help="set this lane's status")
    p.add_argument("status", choices=["working", "blocked", "done", "stopped"])
    p.add_argument("--note")

    add("leave", cmd_leave, help="turn this clone back into a plain clone")

    p = add("wait", cmd_wait, help="wait without tokens")
    p.add_argument("--seconds", type=float, default=540)
    p.add_argument("--poll", type=float, default=30)
    p.add_argument("--until-resumed", action="store_true")

    p = add("send", cmd_send, help="message a lane, all, conductor or human")
    p.add_argument("--run")
    p.add_argument("--to", required=True)
    p.add_argument("--kind", default="note", choices=["note", "request", "blocker", "decision", "needs-human", "answer"])
    p.add_argument("--body", required=True)
    p.add_argument("--from", dest="frm")

    p = add("inbox", cmd_inbox, help="read messages")
    p.add_argument("--run")
    p.add_argument("--who")
    p.add_argument("--mark", action="store_true", help="the conductor has dealt with these")

    p = add("decide", cmd_decide, help="log a decision made without asking")
    p.add_argument("--what", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--undo", required=True)

    p = add("handoff", cmd_handoff, help="save this lane's rolling handoff")
    p.add_argument("file")

    p = add("stop", cmd_stop, help="stop a run")
    p.add_argument("--run")
    p.add_argument("--all", action="store_true")
    p.add_argument("--drain", action="store_true", help="finish what is in hand first")
    p.add_argument("--reason")
    for name, state in (("pause", "paused"), ("resume", "running")):
        p = add(name, control_cmd(state, name), help=name + " a run")
        p.add_argument("--run")
        p.add_argument("--all", action="store_true")
        p.add_argument("--reason")

    p = add("status", cmd_status, help="runs, lanes, listeners")
    p.add_argument("--run")
    p.add_argument("--human", action="store_true")

    p = add("report", cmd_report, help="end-of-run summary")
    p.add_argument("--run")

    p = add("prompt", cmd_prompt, help="paste-able prompts for lanes no listener took")
    p.add_argument("--run")
    p.add_argument("--lane")

    p = add("route", cmd_route, help="the model for a task")
    p.add_argument("--kind", required=True)
    p.add_argument("--paths", nargs="*")
    p.add_argument("--changes-verdicts", type=int, default=0)
    p.add_argument("--stalled", action="store_true")
    p.add_argument("--root")

    add("check-diff", cmd_check_diff, help="prove this branch stays in lane")

    p = add("conduct", cmd_conduct, help="one deterministic conductor pass")
    p.add_argument("--run")
    p.add_argument("--stale-minutes", type=float, default=STALE_MINUTES)

    p = add("settings", cmd_settings, help="read or set repo-wide settings")
    p.add_argument("--max-accounts", type=int)

    p = add("listen", cmd_listen, help="the listener loop")
    p.add_argument("--once", action="store_true")
    p.add_argument("--seconds", type=float, default=60)

    add("poll", cmd_poll, help="one listener pass for a routine")

    p = add("listen-install", listen_install, help="install the always-on listener")
    p.add_argument("--permission-mode", default="auto", choices=["acceptEdits", "auto", "bypassPermissions", "dontAsk"])
    p.add_argument("--no-cloud", action="store_true")
    p.add_argument("--no-local", action="store_true")
    p.add_argument("--routine-id", help="this account's listener routine, so the daemon can start cloud lanes")
    p.add_argument("--dry-run", action="store_true")

    p = add("listen-uninstall", listen_uninstall, help="remove the listener")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("hook", help="called by the plugin's hooks")
    p.set_defaults(func=cmd_hook)
    p.add_argument("event", choices=["guard", "stop", "session-start"])

    p = add("routine-check", cmd_routine_check, help="prove the daemon can reach a routine")
    p.add_argument("--routine-id", required=True)

    add("install-repo", cmd_install_repo, help="write the repo-side hooks that cloud sessions need")
    p = add("switch", cmd_switch, help="point the listener at this Mac user's current claude login")
    p.add_argument("name", nargs="?", help="the account to switch to (default: find it)")

    add("doctor", cmd_doctor, help="check this account's setup")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except FbError as exc:
        print("fast-builder: %s" % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
