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
  runs/<run>/alive/<lane>.json    a local lane's listener saying its session is still working
  runs/<run>/handoffs/<lane>.md   rolling handoff, what a replacement account starts from
  runs/<run>/conductor.json       lease on the cross-account orchestrator role
  runs/<run>/main.json            the base branch's last known check state
  runs/<run>/freed/<lane>.json    which accounts lost this lane to going quiet, and when
  runs/<run>/desk.json            which account and computer hold the session a person talks to
  accounts/<account>.json         when this account ran out of usage, and when its limit said it resets
  runs/<run>/msgs/<id>.json       messages between lanes, the conductor and the human

See docs/protocol.md. Everything is a subcommand: fb.py --help.
"""
import argparse
import datetime
import glob as globlib
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from xml.sax.saxutils import escape

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl


def add_git_settings(env):
    """For every git from here on, lanes' included: a bus file stays byte for byte what it was, and a deep repo checks out.
    Added to any the environment already has, and a lane's own fb.py finds these already there."""
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    held = [env.get("GIT_CONFIG_KEY_%d" % i) for i in range(count)]
    for key, value in (("core.autocrlf", "false"), ("core.longpaths", "true")):
        if key not in held:
            env["GIT_CONFIG_KEY_%d" % count], env["GIT_CONFIG_VALUE_%d" % count] = key, value
            count += 1
    env["GIT_CONFIG_COUNT"] = str(count)


if WINDOWS:
    add_git_settings(os.environ)
NO_WINDOW = {"creationflags": 0x08000000} if WINDOWS else {}  # pythonw has no console, so each child would open its own
DETACHED = {"creationflags": 0x08000200} if WINDOWS else {"start_new_session": True}  # adds CREATE_NEW_PROCESS_GROUP

VERSION = "0.1.12"
BUS_BRANCH = os.environ.get("FB_BUS_BRANCH", "fast-builder/bus")
FORMAT = 1
HOME_DIR = os.environ.get("FB_HOME") or os.path.join(os.path.expanduser("~"), ".config", "fast-builder")
CACHE_DIR = os.environ.get("FB_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "fast-builder")
CLAUDE = os.environ.get("FB_CLAUDE", "claude")
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_MAX_ACCOUNTS = 4
STALE_MINUTES = 45
BEAT_SECONDS = 300
BEAT_RETRY_SECONDS = 60  # after a beat that could not land
CONTROL_CACHE_SECONDS = 15
LEASE_MINUTES = 20
STOP_BLOCK_CAP = 3
MAX_RESTARTS = 5
WAKE_SECONDS = 600
MAX_ROSTER = 12  # each try costs a Haiku call and calls run one at a time, so a bus cannot be let list as many as it likes
PASS_CALLS = 24  # about what one pass over one repo may spend on wakes: those run one at a time, and a stop waits behind the pass
STEERED = "200, with another RemoteTrigger call"  # it started, so say so, but the session cannot be believed about anything else
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

# Not a name Windows keeps for a device: nothing named aux or con, with any extension, can be checked out there.
NOT_A_DEVICE = r"(?!(?:con|prn|aux|nul|com[0-9]|lpt[0-9])\Z)"
DEVICE_RE = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9]|conin\$|conout\$|git~1)\Z")  # git~1 is .git by its short name
STALE_LOCK_SECONDS = 180  # longer than any git this runs in the bus checkout may take
NAME_RE = re.compile(r"^" + NOT_A_DEVICE + r"[a-z0-9][a-z0-9-]{0,40}\Z")
RUN_RE = re.compile(r"^" + NOT_A_DEVICE + r"[a-z0-9][a-z0-9-]{0,60}\Z")
TRIGGER_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}\Z")
BUS_TRIGGER_RE = re.compile(r"^trig_[A-Za-z0-9]{24}\Z")  # an id goes into a prompt, so one from the bus gets no room to say anything
VOLATILE_RE = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}\S*|\b\d{1,2}:\d{2}(?::\d{2})?\b|\b(?:req|msg|toolu|cse|session)_[\w-]+|\b[0-9a-f]{16,}\b", re.I)
POLL_RE = re.compile(r"\bfb\.py poll --account ([a-z0-9][a-z0-9-]{0,40})(?![A-Za-z0-9_-])")
STATUS_RE = re.compile(r"(?:HTTP(?:/[0-9.]+)?\s+|Status:?\s*)?([0-9]{3})(?:\s+(?:OK|Created|Accepted|Bad Request|Unauthorized|Forbidden|Not Found))?\Z", re.I)
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}\Z")
MODEL_RE = re.compile(r"^(opus|sonnet|haiku|claude-[a-z0-9-]{1,40})\Z")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}/[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
CHECK_NAME_DROP = re.compile(r"[^A-Za-z0-9 ._:/()+#=-]+")
LOCK_SECONDS = 30
LANE_STOP_SECONDS = 10
HOOK_LOCK_SECONDS = 4
MAIN_SECONDS = 300  # how often one account asks GitHub about the base branch
MAIN_ASK_SECONDS = 60  # every page of a head: a real one with 1,054 rows took 19.5 seconds
MAIN_RED_MINUTES = 120
MAIN_NAMES = 8
MAIN_NAME_LEN = 60
BYPASS = "bypassPermissions"
KEPT_MODES = ("acceptEdits", "auto", "dontAsk")  # what a reinstall may carry over without being told
COOLDOWN_MINUTES = 180  # how long an account that lost a lane to going quiet is not handed it back
UNCLAIMED_MINUTES = 30
CLAIM_QUIET_MINUTES = 20  # a claim no session ever joined: a live one joins in a minute or two
REQUEST_UNREAD_MINUTES = 60  # a request to the conductor nobody has read: every live lead ticks within 45
REQUESTS_FILE = "requests.json"
MAIN_RED = ("failure", "timed_out", "startup_failure")
MAIN_GREEN = ("success", "neutral", "skipped")
MAIN_VERDICT = MAIN_RED + ("success",)  # what can replace an earlier row of a check. A skip says nothing about the code
CHECK_UNDERWAY = ("queued", "in_progress", "waiting", "requested", "pending")
OVERFLOW_HOURS = 1  # a lane finishing closer to the deadline than this is not worth finding more work for
DRY_MAX_DAYS = 8  # the furthest ahead a limit's own reset time is believed: a weekly limit is the longest there is
RESTART_BACKOFF_SECONDS = 60  # doubled for each restart in a row after the first: a network blip outlasts a minute
SPREAD_MINUTES = 10  # how long a freed lane waits for an account with no lane before one with a lane may take it
DEFAULT_LANE_CAP = 2  # lanes one account may hold in a run, unless the job says otherwise
LISTENER_BEAT_SECONDS = 600
LISTENER_LIVE_MINUTES = 30  # a listener heard from this recently counts as one that could take a lane
VOUCH_AFTER_MINUTES = 10  # a lane's own beat this old, and its listener speaks for it if its process is working
VOUCH_SECONDS = 300
VOUCH_ACTIVE_MINUTES = 15  # what working means: its transcript, log or git written this recently
VOUCH_MAX_MINUTES = 240  # a lead that has not beaten for this long is freed whatever its listener says
LOG_TAIL_BYTES = 65536
DEFAULT_MAX_LOCAL_LANES = 2  # live local lanes per OS user, across repos
LOAD_PER_CPU = 2.0  # no new local lane while the 5-minute load is over this per core: git starts timing out past it
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# What the CLI prints when an account is out, as a whole line of its own: "You've hit your weekly limit · resets Nov 4 at
# 3pm (America/Los_Angeles)", or the older "Claude usage limit reached. Your limit will reset at 3pm (America/Los_Angeles)."
# Only that form: a lead's own summary saying "callers who hit your API limit get a 429" is no limit of this account's.
LIMIT_RE = re.compile(r"(?im)^[ \t]*(?:(?:you've|you have)\s+hit\s+your\s+(?P<kind>[a-z0-9 -]{0,24}?)\s*limit|claude(?:\s+ai)?\s+usage\s+limit\s+reached)"
                      r"(?P<rest>\s*[·.|-]\s*(?:(?:your\s+limit\s+will\s+)?resets?\b|(?:contact|ask)\s+your\s+admin\b)[^\n]{0,120})?[ \t.]*$")
RESET_RE = re.compile(r"(?i)\bresets?(?:\s+at)?\s+(?:(?P<mon>%s)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?(?:\s+at)?\s+)?(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<half>am|pm)?\b"
                      r"(?:\s*\((?P<tz>[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2})\))?" % "|".join(MONTHS))
SAID_RE = re.compile(r"^[a-z0-9 -]{1,40}\Z")


class FbError(Exception):
    """A failure to report in one line rather than as a traceback."""


class BusUnreadable(FbError):
    """The bus fetched but will not check out here, which a file name Windows cannot hold does for good."""


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


def run_cmd_windows(args, cwd, check, timeout, env, stdin_text):
    """subprocess.run after a timeout waits there for the pipes to close, which ssh or node left holding them never do,
    so a hook would sit past its own limit and a stop would be lost."""
    try:
        proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", env=env,
                                stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE, **NO_WINDOW)
    except OSError as exc:
        raise FbError("cannot run %s: %s" % (args[0], exc))
    try:
        stdout, stderr = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
        raise FbError("timed out: " + " ".join(args[:4]))
    if check and proc.returncode != 0:
        raise FbError("%s failed: %s" % (" ".join(args[:4]), (stderr or stdout).strip()[:300]))
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def run_cmd(args, cwd=None, check=True, timeout=60, env=None, stdin_text=None):
    if WINDOWS:
        return run_cmd_windows(args, cwd, check, timeout, env, stdin_text)
    feed = {"stdin": subprocess.DEVNULL} if stdin_text is None else {"input": stdin_text}
    try:
        proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, **feed, **NO_WINDOW)
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
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, RecursionError):
        return default


def write_file(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows refuses while anything has the file open, which a reader only does for a moment
            if not WINDOWS or attempt == 19:
                raise
            time.sleep(0.05)


def windows_cannot_hold(path):
    """A bus path that git on Windows will never check out, whichever computer pushed it."""
    for part in path.split("/"):
        if re.search(r'[<>:"|?*\\\x00-\x1f]', part) or part.endswith((".", " ")) or DEVICE_RE.match(part.split(".")[0].lower()):
            return True
    return False


def lock_file(handle):
    """Take this open file's lock, or raise OSError if another process holds it."""
    if WINDOWS:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


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
        handle = open(self.dir + ".lock", "a")
        end = time.time() + self.lock_seconds
        while True:
            try:
                lock_file(handle)
                self.lock_handle = handle
                return
            except OSError:
                if time.time() > end:
                    handle.close()
                    raise FbError("bus checkout is busy")
                time.sleep(0.05)

    def release(self):
        if self.lock_handle:
            if WINDOWS:  # closing lets it go too, but only when the system gets to it
                self.lock_handle.seek(0)
                msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
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
        self.clear_stale_locks()
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            os.makedirs(self.dir, exist_ok=True)
            git(["init", "-q"], cwd=self.dir)
            git(["remote", "add", "origin", self.remote], cwd=self.dir)
        if not self.head():
            return False
        spec = "+refs/heads/%s:refs/remotes/origin/bus" % BUS_BRANCH
        git(["fetch", "-q", "--depth", "50", "origin", spec], cwd=self.dir, timeout=120)
        checkout = git(["checkout", "-q", "-f", "-B", "bus", "refs/remotes/origin/bus"], cwd=self.dir, check=False)
        if checkout.returncode != 0:
            names = git(["ls-tree", "-r", "--name-only", "-z", "refs/remotes/origin/bus"], cwd=self.dir, check=False).stdout.split("\0")
            if WINDOWS and any(windows_cannot_hold(name) for name in names):
                raise BusUnreadable("the bus holds a file name Windows cannot check out: %s" % ", ".join(n for n in names if windows_cannot_hold(n))[:300])
            raise FbError("cannot check out the bus: %s" % checkout.stderr.strip()[:300])
        git(["clean", "-qfdx"], cwd=self.dir)
        if any(line.startswith("120000") for line in git(["ls-files", "-s"], cwd=self.dir).stdout.splitlines()):
            raise FbError("refusing a bus with a symlink on it")
        return True

    def clear_stale_locks(self):
        """A git killed mid-command leaves its lock behind, and every fetch or checkout after it fails, so a stop would never
        arrive. The bus lock is held here, so no other fb.py is at work, and the age allows for a git a killed hook left running."""
        for rel in ("index.lock", "shallow.lock", "HEAD.lock", "config.lock", "packed-refs.lock", "refs/heads/bus.lock", "refs/remotes/origin/bus.lock"):
            full = self.path(".git", *rel.split("/"))
            try:
                if time.time() - os.path.getmtime(full) > STALE_LOCK_SECONDS:
                    os.remove(full)
            except OSError:
                pass

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def read(self, rel, default=None):
        try:
            with open(self.path(rel), encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return default

    def json(self, rel, default=None):
        return read_json(self.path(rel), default)

    def doc(self, rel):
        """A file that has to be an object: anyone can push anything, and one bad file must not stop what comes after it."""
        value = self.json(rel)
        return value if isinstance(value, dict) else {}

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
                if os.path.isdir(full) and not os.path.islink(full):  # anyone can push one, and it reads as a bad file does: empty, and replaced on writing
                    shutil.rmtree(full)
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
    rel = "runs/%s/control.json" % run
    control = bus.doc(rel)
    state = control.get("state", "running")
    if not isinstance(state, str) or (not isinstance(bus.json(rel), dict) and bus.exists(rel)):
        state = "stopping"  # as for any state nobody knows, and a file nobody can read is one
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
    return bus.doc("runs/%s/beats/%s.json" % (run, lane))


def lane_table(bus, run):
    rows = []
    for name in lane_names(bus, run):
        claim = bus.doc("runs/%s/claims/%s.json" % (run, name))
        beat = beat_of(bus, run, name)
        alive = bus.doc("runs/%s/alive/%s.json" % (run, name))
        vouched = claim.get("session") and alive.get("session") == claim.get("session") and alive.get("account") == claim.get("account")
        rows.append({
            "lane": name, "account": claim.get("account") if claim else None, "status": beat.get("status", "open" if not claim else "claimed"),
            "beat": beat.get("at"), "beat_age_min": None if not beat.get("at") else round(age_minutes(beat["at"]), 1),
            "note": str(beat.get("note", "")), "where": claim.get("where") if claim else None,
            "alive_min": round(age_minutes(alive.get("at")), 1) if vouched and alive.get("at") else None,
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


def cooling(bus, run, lane, account):
    """Whether this lane went quiet under this account too recently to be handed back to it. The beat's note counts too:
    it is all an old copy of this tool leaves behind. A time far ahead of the clock cannot lock an account out."""
    lost = bus.doc("runs/%s/freed/%s.json" % (run, lane)).get("lost")
    at = lost.get(account) if isinstance(lost, dict) else None
    if at is None:
        beat = beat_of(bus, run, lane)
        if beat.get("status") == "open" and beat.get("note") == "freed: %s went quiet" % account:
            at = beat.get("at")
    return abs(age_minutes(at)) < COOLDOWN_MINUTES


def dry_until(doc):
    """The reset time the account's own limit gave, if it is believable: still ahead, and no further than the longest
    limit runs. Without one, the cooldown is all there is to go on."""
    try:
        until = parse_iso(doc.get("until"))
    except (TypeError, ValueError):
        return None
    return doc["until"] if 0 < (until - utcnow()).total_seconds() <= DRY_MAX_DAYS * 86400 else None


def out_of_usage(bus, account):
    """When this account ran out of usage, if that still counts: until the reset time its limit gave, or else for the same
    cooldown as a lost lane. A time far ahead of the clock cannot lock an account out for long."""
    doc = bus.doc("accounts/%s.json" % account) if NAME_RE.match(str(account)) else {}
    at = doc.get("out_of_usage")
    if not at:
        return None
    return at if dry_until(doc) or abs(age_minutes(at)) < COOLDOWN_MINUTES else None


def back_in(bus, account):
    """When an account out of usage gets lanes again, as a person reads it."""
    doc = bus.doc("accounts/%s.json" % account)
    if dry_until(doc):
        return "until %s" % doc["until"]
    try:
        return "until about %s" % (parse_iso(doc.get("out_of_usage")) + datetime.timedelta(minutes=COOLDOWN_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return "for now"


def reset_time(found, now=None):
    """The UTC time a limit's "resets 3pm (America/Los_Angeles)" means: that day if a date is given, else the next time the
    clock shows it. Python 3.8 has no zoneinfo, and the name may be unknown, so this computer's zone stands in: the
    CLI that printed it runs here. None for anything that does not read as a time ahead within the longest limit."""
    now = now or utcnow()
    zone = None
    if found.group("tz"):
        try:
            import zoneinfo
            zone = zoneinfo.ZoneInfo(found.group("tz"))
        except Exception:
            zone = None
    here = now.astimezone(zone) if zone else now.astimezone()
    hour, minute, half = int(found.group("hour")), int(found.group("min") or 0), (found.group("half") or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    tz = zone or here.tzinfo
    try:
        if found.group("mon"):
            at = datetime.datetime(here.year, MONTHS.index(found.group("mon")[:3].lower()) + 1, int(found.group("day")), hour, minute, tzinfo=tz)
            if at < here - datetime.timedelta(days=180):  # December's message read in January
                at = at.replace(year=here.year + 1)
        else:
            at = datetime.datetime(here.year, here.month, here.day, hour, minute, tzinfo=tz)
            if here - datetime.timedelta(hours=1) < at <= here:
                return None  # read just after it passed: likely today's, already over, and not tomorrow's
            if at <= here:
                day = here.date() + datetime.timedelta(days=1)
                at = datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    except ValueError:
        return None
    at = at.astimezone(datetime.timezone.utc)
    if not 0 < (at - now).total_seconds() <= DRY_MAX_DAYS * 86400:
        return None
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def usage_limit(text, now=None):
    """The last thing in text saying the account is out of usage, as {"said": "weekly limit", "until": ISO or None}, or
    None if it says no such thing."""
    found = None
    for found in LIMIT_RE.finditer(text or ""):
        pass
    if not found:
        return None
    kind = " ".join((found.group("kind") or "usage").lower().split())
    reset = RESET_RE.search(found.group("rest") or "")
    return {"said": ("%s limit" % kind)[:40], "until": reset_time(reset, now) if reset else None}


def dry_files(bus, account, limit=None):
    """The file marking an account out of usage. A reset time already there and later than this one is kept: a session
    limit can come and go inside a weekly one."""
    rel = "accounts/%s.json" % account
    held = bus.doc(rel)
    until, kept = (limit or {}).get("until"), dry_until(held)
    if kept and (not until or kept > until):
        until = kept
    doc = {"out_of_usage": now_iso()}
    if until:
        doc["until"] = until
    said = (limit or {}).get("said")
    if isinstance(said, str) and SAID_RE.match(said):
        doc["said"] = said
    return {rel: dumps(doc)}


def lost_lane(bus, run, lane, account):
    """The file saying account lost this lane. Its own file: the next claim overwrites a beat, and old copies never read it."""
    rel = "runs/%s/freed/%s.json" % (run, lane)
    doc = bus.doc(rel)
    held = doc.get("lost") if isinstance(doc.get("lost"), dict) else {}
    lost = {who: at for who, at in held.items() if age_minutes(at) < COOLDOWN_MINUTES}
    if isinstance(account, str):
        lost[account] = now_iso()
    return {rel: dumps(dict(doc, lost=lost))}


def rejoined(bus, run, lane, token):
    """The files for a session taking a lane its account holds. A beat still saying `claimed` has not heard from a session
    of this copy, so its time starts now."""
    files = {"runs/%s/claims/%s.json" % (run, lane): dumps(dict(bus.doc("runs/%s/claims/%s.json" % (run, lane)), session=token))}
    beat = beat_of(bus, run, lane)
    if beat.get("note") == "claimed":
        files["runs/%s/beats/%s.json" % (run, lane)] = dumps(dict(beat, at=now_iso(), note="joined"))
    return files


def claim_lane(bus, run, account, lane=None, where=None, allow_second=False, token=None, cap=None):
    """Atomically claim a lane: the named one, or the first open one. Returns its name.

    An account takes one lane per run unless allow_second is set, which is how a lane orphaned by an account that
    ran out of usage still gets picked up when every account already has one, and then no more than cap, if given. A
    claim only lands if the push wins.

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
        mine = [n for n in names if bus.doc("runs/%s/claims/%s.json" % (run, n)).get("account") == account]
        if lane and lane in mine:
            won["lane"] = lane
            return rejoined(bus, run, lane, token) if token else {}
        if mine and not allow_second:
            if lane:
                raise Taken("%s already holds lane %s in %s" % (account, mine[0], run))
            won["lane"] = mine[0]
            return rejoined(bus, run, mine[0], token) if token else {}
        live = [n for n in mine if beat_of(bus, run, n).get("status", "claimed") in LIVE_BEATS]
        if cap is not None and len(live) >= cap:
            raise Taken("%s already holds %d live lanes in %s, its most" % (account, len(live), run))
        if lane:
            if lane not in names:
                raise FbError("no lane %s in %s" % (lane, run))
            candidates = [lane]
        else:
            candidates = names
        open_lanes = [n for n in candidates if not bus.exists("runs/%s/claims/%s.json" % (run, n))]
        if not lane:  # naming a lane is a person's call, and still works; taking whatever is open is not
            cool = [n for n in open_lanes if cooling(bus, run, n, account)]
            open_lanes = [n for n in open_lanes if n not in cool]
            if cool and not open_lanes:
                raise Taken("%s lost lane %s to going quiet in the last %d hours: name it with --lane to take it anyway" % (account, cool[0], COOLDOWN_MINUTES // 60))
        if not open_lanes:
            raise Taken("no open lane in " + run)
        won["lane"] = open_lanes[0]
        doc = {"account": account, "at": now_iso(), "session": token, "where": where or bus.doc("runs/%s/job.json" % run).get("where", "cloud")}
        beat = {"at": now_iso(), "status": "working", "account": account, "note": "joined" if token else "claimed"}
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


def back_on_offer(bus, run):
    """The files for a person's resume to take a cooldown off every open lane that has one, whichever account and whether
    it is in freed/ or only in an old copy's beat note. The alert's marker goes too and the beat's time starts over, so
    a lane still left open tells a person again."""
    files = {}
    for name in lane_names(bus, run):
        if bus.exists("runs/%s/claims/%s.json" % (run, name)):
            continue
        rel, beat = "runs/%s/freed/%s.json" % (run, name), beat_of(bus, run, name)
        held = os.path.isfile(bus.path(rel))
        if held:
            files[rel] = None
        if beat.get("status") == "open" and (held or str(beat.get("note", "")).startswith("freed: ")):
            files["runs/%s/beats/%s.json" % (run, name)] = dumps(dict(beat, at=now_iso(), note="back on offer after a resume"))
    return files


def accounts_back_in(bus):
    """A person's resume is how a brief 429, or an account that was topped up, gets lanes again."""
    return {"accounts/" + name: None for name in bus.listdir("accounts") if name.endswith(".json") and out_of_usage(bus, name[:-5])}


def has_cooled_lane(bus, run):
    """A running run with a lane a resume would put back on offer, and that is all a resume would do to it. A control file
    nobody can read reads as stopping, and that is not running."""
    rel = "runs/%s/control.json" % run
    if bus.exists("runs/%s/STOP" % run) or (bus.exists(rel) and not isinstance(bus.json(rel), dict)):
        return False
    return bus.doc(rel).get("state", "running") == "running" and bool(back_on_offer(bus, run))


def set_control(bus, run, state, reason, by, cool_only=False):
    """Move a run to state if that is a legal move. pause can never soften a stop, and nothing reopens a finished run.
    Returns whether it moved. A resume also refreshes working lanes' beats: nobody beats while paused, and those lanes
    must not look dead the moment the run starts again. It takes cooldowns off open lanes too, even on a running run,
    where that alone counts as moving: it is a lone account's way to have its lane offered again. cool_only: a run picked
    for that alone, which a stop landing since must not lose to this."""
    moved = {}

    def mutate():
        current = bus.doc("runs/%s/control.json" % run).get("state", "running")
        if cool_only and not has_cooled_lane(bus, run):
            moved["ok"] = False
            return {}
        moved["ok"] = current in TRANSITIONS[state]
        if not moved["ok"]:
            if state != "running" or current != "running":
                return {}
            files = back_on_offer(bus, run)
            if bus.exists("runs/%s/STOP" % run):
                files["runs/%s/STOP" % run] = None
            moved["ok"] = bool(files)
            return files
        files = {"runs/%s/control.json" % run: dumps({"state": state, "reason": reason, "by": by, "at": now_iso()})}
        if state == "running":
            files["runs/%s/STOP" % run] = None
            for name in lane_names(bus, run):
                beat = beat_of(bus, run, name)
                if beat.get("status") == "stopped":
                    files["runs/%s/claims/%s.json" % (run, name)] = None
                    files["runs/%s/beats/%s.json" % (run, name)] = dumps({"at": now_iso(), "status": "open", "account": None, "note": "stopped lane back on offer"})
                    if os.path.isfile(bus.path("runs/%s/freed/%s.json" % (run, name))):
                        files["runs/%s/freed/%s.json" % (run, name)] = None
                elif beat.get("status") == "working":
                    files["runs/%s/beats/%s.json" % (run, name)] = dumps(dict(beat, at=now_iso()))
            files.update(back_on_offer(bus, run))
        return files

    bus.commit(mutate, "control(%s): %s" % (run, state))
    return moved.get("ok", False)


def clean_names(names):
    """Check names are repo data, and they reach STATUS.md, messages and whichever model reads those. Keep what cannot
    end a line or a table cell, and not much of it. They are only ever compared and printed, never run."""
    out = []
    for name in names if isinstance(names, list) else []:
        text = CHECK_NAME_DROP.sub(" ", name).strip()[:MAIN_NAME_LEN] if isinstance(name, str) else ""
        if text and text not in out and len(out) < MAIN_NAMES:
            out.append(text)
    return out


def check_time(text):
    """A time on a check run in seconds, or None if it cannot be read. No offset means UTC."""
    try:
        when = datetime.datetime.fromisoformat(re.sub(r"[Zz]\Z", "+00:00", text))
        return (when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def check_family(row):
    """What must match for one row to replace another: name, app and check suite. A re-run keeps its suite, and any other
    suite may be another workflow with the same job name. A field no row has matches itself, as a test or a plain status
    says nothing of it; a field that makes no sense matches nothing."""
    def part(value, *keys):
        if value is None:
            return None
        for key in keys:
            got = value.get(key) if isinstance(value, dict) else None
            if got is not None:
                return got if isinstance(got, (int, str)) and not isinstance(got, bool) else object()
        return object()
    return row["name"], part(row.get("app"), "id", "slug"), part(row.get("check_suite"), "id")


def check_times(row, now):
    """A row's (began, ended), ended None until it finishes; or None if they cannot be believed: unreadable, ending before
    they begin, or still to come. Nothing but its finish to go on counts as beginning then."""
    done = row.get("status") == "completed"
    ended = check_time(row.get("completed_at")) if done else None
    began = ended if row.get("started_at") is None else check_time(row.get("started_at"))
    if began is None or (done and ended is None):
        return None
    return (began, ended) if began <= (ended if done else began) <= now else None


def standing_checks(rows):
    """What still stands: a row goes only if a later one replaces it, and when in doubt it stays, since a failure wrongly
    dropped tells everyone main is green. To replace, a row must be of the same family, have believable times, begin after
    the other ended, and have a verdict to give or be on its way to one. Rows that overlap, tie or cannot be compared all
    stay, and the worst of them decides. A row a re-dispatch made is in a new suite, so it stands beside the old failure."""
    now = time.time()
    newest, seen = {}, []
    for row in rows:
        family, times = check_family(row), check_times(row, now)
        seen.append((row, family, times))
        said = row.get("conclusion") in MAIN_VERDICT if row.get("status") == "completed" else row.get("status") in CHECK_UNDERWAY
        if times and said:
            newest[family] = max(times[0], newest.get(family, times[0]))
    return [r for r, family, times in seen if not (times and times[1] is not None and newest.get(family, times[1]) > times[1])]


def main_checks(slug, base, only):
    """What the checks on the base branch's head say: red, green, or None for nothing to go on. REST, because cloud
    sessions refuse GraphQL. The slug comes from this clone's remote and not from the bus.

    Red: a check that stands ended in MAIN_RED. Green: every one that stands ended in MAIN_GREEN, at least one of them
    a success, and every name in `only` was seen. Anything else says nothing about this head. Raises FbError if gh runs
    out of time, and every page of a very large head has to fit in that time: look_at_main says so."""
    if not (isinstance(base, str) and SLUG_RE.match(slug) and BRANCH_RE.match(base) and shutil.which("gh")):
        return None
    proc = run_cmd(["gh", "api", "--paginate", "repos/%s/commits/%s/check-runs?per_page=100" % (slug, urllib.parse.quote(base, safe=""))], check=False, timeout=MAIN_ASK_SECONDS)
    if proc.returncode != 0:
        return None
    rows, text, at = [], proc.stdout.strip(), 0
    while at < len(text):  # --paginate prints one object per page, one after the other
        page, at = json.JSONDecoder().raw_decode(text, at)
        rows += page["check_runs"]
        at += len(text[at:]) - len(text[at:].lstrip())
    rows = [r for r in rows if isinstance(r, dict)]
    sha = rows[0].get("head_sha") if rows else None
    if not (isinstance(sha, str) and SHA_RE.match(sha)) or any(r.get("head_sha") != sha for r in rows):
        return None  # main moved between two pages, and a reading across commits is of neither. Before `only`, which could hide it
    rows = standing_checks([r for r in rows if isinstance(r.get("name"), str) and (not only or r["name"] in only)])
    failing = [r["name"] for r in rows if r.get("status") == "completed" and r.get("conclusion") in MAIN_RED]
    if failing:
        return {"state": "red", "sha": sha, "failing": clean_names(sorted(failing))}
    if (all(r.get("status") == "completed" and r.get("conclusion") in MAIN_GREEN for r in rows) and any(r.get("conclusion") == "success" for r in rows)
            and all(any(r["name"] == name for r in rows) for name in only)):
        return {"state": "green", "sha": sha, "failing": []}
    return None


def look_at_main(bus, run, account, asked=None):
    """A fresh reading of main if this pass is the one to take it, else None. Outside the commit: a retry must not ask
    again, and gh must not hold the bus checkout. The throttle is a local file, so a reading with no news costs the bus
    no commit, and a gh that keeps failing costs a tick nothing. Whatever goes wrong here is only an unknown, except gh
    running out of time on every try for as long as main may stay red: that comes back as state `slow`, to tell a person.
    asked: what this pass has learned about gh, so that a gh that hung once costs the pass that once and not once per run."""
    try:
        if asked and asked.get("hung"):
            return None
        lease = bus.doc("runs/%s/conductor.json" % run)
        if lease.get("holder") not in (None, account) and age_minutes(lease.get("at")) < LEASE_MINUTES:
            return None
        if run_state(bus, run)["action"] != GO:
            return None
        mark = "%s.main-%s" % (bus.dir, run)
        if os.path.exists(mark) and abs(time.time() - os.path.getmtime(mark)) < MAIN_SECONDS:
            return None
        write_file(mark, "")
        job = bus.doc("runs/%s/job.json" % run)
        only = job.get("main_checks")
        only = [c for c in only if isinstance(c, str)] if isinstance(only, list) else []
        bus.release()
        slow = "%s.slow-%s" % (bus.dir, run)
        try:
            seen = main_checks(repo_slug(bus.remote), job.get("base", "main"), only)
        except FbError:  # counted and not timed: a lease that was away for days must not make one slow answer look like days
            if asked is not None:
                asked["hung"] = True
            tries = (read_json(slow) or 0) + 1
            write_file(slow, str(tries))
            return {"state": "slow"} if tries >= MAIN_RED_MINUTES * 60 // MAIN_SECONDS else None
        if os.path.exists(slow):
            os.remove(slow)
        return seen
    except Exception:  # bad JSON, a timeout, anything: a conductor pass must not die from this
        return None


def main_view(bus, run, doc=None):
    """main.json as anyone may read it. It is on the bus, so nothing in it is taken on trust."""
    doc = bus.doc("runs/%s/main.json" % run) if doc is None else doc
    if doc.get("state") not in ("green", "red"):
        return {"state": "unknown"}
    sha = doc.get("sha")
    return {"state": doc["state"], "sha": sha if isinstance(sha, str) and SHA_RE.match(sha) else "", "failing": clean_names(doc.get("failing")),
            "since": doc["since"] if age_minutes(doc.get("since")) != float("inf") else ""}


def add_message(files, run, frm, to, kind, body):
    while True:  # ids come from the second and 16 random bits, and two in one commit must not be one file
        rel = "runs/%s/msgs/%s.json" % (run, make_id(frm))
        if rel not in files:
            files[rel] = dumps(message(frm, to, kind, body))
            return


def main_update(bus, run, account, seen):
    """The files a fresh reading of main calls for, from the bus as it now is, so a lost race just works it out again and
    a transition is told once. A note on a change, and one needs-human for each red spell that outlasts the limit."""
    if seen["state"] == "slow":  # says nothing about main, so main.json stays as it is. The text is the marker
        body = "Main's checks could not be read within %d seconds on any try for about %d hours, so main stays at its last known state or unknown, and nobody will be told if it goes red. A head with a very large number of check rows does this, as does a gh that hangs. Look at the checks by hand." % (MAIN_ASK_SECONDS, MAIN_RED_MINUTES // 60)
        files = {}
        if not any(m.get("body") == body for m in messages_for(bus, run, "human")):
            add_message(files, run, account, "human", "needs-human", body)
        return files
    rel = "runs/%s/main.json" % run
    held = bus.doc(rel)
    was = main_view(bus, run)
    same = was["state"] == seen["state"]
    doc = dict(seen, since=was["since"] if same and was["since"] else now_iso())
    short = seen["sha"][:7]
    names = " (%s)" % ", ".join(seen["failing"]) if seen["failing"] else ""
    files = {}
    if seen["state"] == "red" and not same:
        add_message(files, run, account, "all", "note", "Main went red at %s%s. The steward lane fixes it first; every other lane keeps building and leaves it alone unless asked." % (short, names))
    elif seen["state"] == "green" and was["state"] == "red":
        add_message(files, run, account, "all", "note", "Main is green again at %s." % short)
    told = seen["state"] == "red" and same and held.get("told") is True
    if seen["state"] == "red" and not told and age_minutes(doc["since"]) >= MAIN_RED_MINUTES:
        add_message(files, run, account, "human", "needs-human", "Main has been red for over %d hours, now at %s%s, and the steward lane has not cleared it." % (MAIN_RED_MINUTES // 60, short, names))
        told = True
    if told:
        doc["told"] = True
    if doc != held:
        files[rel] = dumps(doc)
    return files


def unclaimed_alerts(bus, run, account, files, skip):
    """One needs-human for a lane that was held once and has sat open too long, once each time it is left open. Its
    marker lands in the same commit as the message, so two conductors cannot both post it."""
    for name in lane_names(bus, run):
        beat = beat_of(bus, run, name)
        if name in skip or beat.get("status") != "open" or bus.exists("runs/%s/claims/%s.json" % (run, name)) or age_minutes(beat.get("at")) < UNCLAIMED_MINUTES:
            continue
        rel = "runs/%s/freed/%s.json" % (run, name)
        doc = bus.doc(rel)
        if doc.get("told") != beat.get("at"):
            files[rel] = dumps(dict(doc, told=beat.get("at")))
            add_message(files, run, account, "human", "needs-human", "Lane %s has had no taker for over %d minutes. On another account, /fast-builder:join --lane %s --run %s starts it here, or fb.py prompt --lane %s --run %s prints a prompt to paste. An account that lost it to going quiet is not offered it for %d hours, but /fast-builder:join --lane %s --run %s on that same account still takes it, and /fast-builder:resume --run %s puts it back on offer: with one account, those are the ways out." % (name, UNCLAIMED_MINUTES, name, run, name, run, COOLDOWN_MINUTES // 60, name, run, run))


def hours_left(deadline):
    """Hours to a job's deadline. It is free text: an ISO date or time can be read, UTC unless it says otherwise, a bare
    date meaning the end of that day. Anything else, or none, reads as no limit: an extra request costs little, and
    saying nothing is the failure this exists for."""
    try:
        text = deadline.strip()
        when = datetime.datetime.fromisoformat(re.sub(r"[Zz]\Z", "+00:00", text + "T23:59:59" if re.match(r"^\d{4}-\d{2}-\d{2}\Z", text) else text))
        return ((when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)) - utcnow()).total_seconds() / 3600.0
    except (AttributeError, ValueError, OverflowError):
        return float("inf")


def overflow_requests(bus, run, account, files, skip):
    """A lane marked done while others work and time is left is a lane that may have stopped early. Once per lane: the
    text is the marker, and it lands in the commit that checked for it."""
    names = lane_names(bus, run)
    if not any(beat_of(bus, run, n).get("status") == "working" and n not in skip for n in names):
        return
    if hours_left(bus.doc("runs/%s/job.json" % run).get("deadline")) <= OVERFLOW_HOURS:
        return
    asked = [m.get("body") for m in messages_for(bus, run, "conductor")]
    for name in names:
        body = "Lane %s is marked done while other lanes are still working and there is time left. Find it more work from the plan's Overflow section or the most loaded lane." % name
        if beat_of(bus, run, name).get("status") == "done" and body not in asked:
            add_message(files, run, account, "conductor", "request", body)


def requests_doc(bus, run):
    """requests.json: which requests to the conductor were read, and which a person was told of. On the bus, because a
    session's own list of what it has seen reaches nobody else. Anything but a list of ids reads as empty: the worst
    that does is an extra alert."""
    doc = bus.doc("runs/%s/%s" % (run, REQUESTS_FILE))
    return {key: [i for i in doc[key] if isinstance(i, str)] if isinstance(doc.get(key), list) else [] for key in ("read", "told")}


def conductor_requests(bus, run, seen=()):
    """What is asked of the conductor and not yet read by anyone."""
    skip = set(seen) | set(requests_doc(bus, run)["read"])
    return [m for m in messages_for(bus, run, "conductor", skip) if m.get("kind") in ("request", "blocker")]


def mark_requests_read(bus, run, ids):
    def mutate():
        doc = requests_doc(bus, run)
        new = [i for i in ids if i not in doc["read"]]
        return {"runs/%s/%s" % (run, REQUESTS_FILE): dumps(dict(doc, read=doc["read"] + new))} if new else {}
    try:
        bus.commit(mutate, "read(%s): conductor requests" % run)
    except (FbError, OSError):
        pass  # the reader has them: all this can cost is an alert saying it did not


def lane_is_live(bus, run, name):
    """Whether a session looks to be on this lane: joined, working and beating. A lane marked done still has its claim,
    and its account's listener still renews the lease, with nobody left to read anything."""
    beat = beat_of(bus, run, name)
    return bool(bus.doc("runs/%s/claims/%s.json" % (run, name)).get("session")) and beat.get("status") == "working" and abs(quiet_minutes(bus, run, name)) < STALE_MINUTES


def unread_requests(bus, run, account, files, skip):
    """One needs-human when a request to the conductor is reaching nobody: at once if the steward lane is done and this
    holder has no live lane, else when one has sat unread past the limit, which also covers an old copy's steward that
    never looks. Its marker lands in the same commit. Ids only: what a lane wrote stays out of a person's status page."""
    doc = requests_doc(bus, run)
    waiting = conductor_requests(bus, run, doc["told"])
    if not waiting:
        return
    names = lane_names(bus, run)
    nobody = any(is_steward(bus, run, n) and beat_of(bus, run, n).get("status") == "done" for n in names) and not any(
        n not in skip and bus.doc("runs/%s/claims/%s.json" % (run, n)).get("account") == account and lane_is_live(bus, run, n) for n in names)
    due = waiting if nobody else [m for m in waiting if abs(age_minutes(m.get("at"))) >= REQUEST_UNREAD_MINUTES]
    if not due:
        return
    ids = [m["id"] for m in due]
    files["runs/%s/%s" % (run, REQUESTS_FILE)] = dumps(dict(doc, told=doc["told"] + ids))
    why = "The steward lane is done and the lease holder has no lane working, so nobody is left to read it." if nobody else "It has sat unread for over %d minutes." % REQUEST_UNREAD_MINUTES
    add_message(files, run, account, "human", "needs-human", "%d %s to the conductor unread (%s). %s Read with fb.py inbox --who conductor --mark, or /fast-builder:join --lane the steward to answer." % (
        len(ids), "request" if len(ids) == 1 else "requests", ", ".join(clean_names(ids)), why))


def conduct(bus, run, account, stale_minutes=STALE_MINUTES, asked=None):
    """The deterministic half of the cross-account orchestrator. Needs no model, so a listener can run it too.

    Takes or renews the lease; frees lanes whose account went quiet, so another account takes them over; closes
    the run once every lane is done; watches main, unclaimed lanes and lanes done early; rewrites STATUS.md. Returns
    what it did and what needs judgement."""
    report = {"run": run, "holder": None, "freed": [], "finished": False}
    if too_old(bus, run):  # it would free and hand out lanes by rules the run has moved past
        report["too_old"] = True
        return report
    seen = look_at_main(bus, run, account, asked)

    def mutate():
        files = {}
        report.update(freed=[], finished=False)
        lease = bus.doc("runs/%s/conductor.json" % run)
        report["holder"] = lease.get("holder")
        if lease.get("holder") not in (None, account) and age_minutes(lease.get("at")) < LEASE_MINUTES:
            return {}
        held = lease.get("holder") == account and age_minutes(lease.get("at")) < LEASE_MINUTES / 2.0
        if held and (bus.exists("STOP") or bus.exists("runs/%s/STOP" % run)):
            return {}  # the stop files decide this run: control.json stays as it was, so removing them restores it
        main = main_update(bus, run, account, seen) if seen else {}
        if held and run_state(bus, run)["action"] == GO:
            stale = _stale(bus, run, stale_minutes)
            names = lane_names(bus, run)
            if not main and not stale and not (names and all(beat_of(bus, run, n).get("status") in ("done", "blocked") for n in names)):
                return {}
        report["holder"] = account
        files["runs/%s/conductor.json" % run] = dumps({"holder": account, "at": now_iso()})
        state = run_state(bus, run)
        names = lane_names(bus, run)
        if state["action"] == GO:
            for name in _stale(bus, run, stale_minutes):
                claim = bus.doc("runs/%s/claims/%s.json" % (run, name))
                limit = quiet_limit(claim, stale_minutes)
                said = "%s went quiet for over %d minutes" % (claim.get("account"), limit) if claim.get("session") else "%s claimed it, and no session joined within %d minutes" % (claim.get("account"), limit)
                files.update(lost_lane(bus, run, name, claim.get("account")))
                files["runs/%s/claims/%s.json" % (run, name)] = None
                files["runs/%s/beats/%s.json" % (run, name)] = dumps({"at": now_iso(), "status": "open", "account": None, "note": "freed: %s went quiet" % claim.get("account")})
                note = message(account, "all", "note", "Lane %s freed: %s." % (name, said))
                files["runs/%s/msgs/%s.json" % (run, make_id(account))] = dumps(note)
                report["freed"].append(name)
            files.update(main)
            old_listener_alerts(bus, run, account, files)
            unclaimed_alerts(bus, run, account, files, report["freed"])
            overflow_requests(bus, run, account, files, report["freed"])
            unread_requests(bus, run, account, files, report["freed"])
            statuses = [beat_of(bus, run, n).get("status") for n in names]
            if names and all(s in ("done", "blocked") for s in statuses):
                blocked = statuses.count("blocked")
                reason = "every lane is done" if not blocked else "%d lanes done, %d blocked" % (statuses.count("done"), blocked)
                files["runs/%s/control.json" % run] = dumps({"state": "finished", "reason": reason, "by": account, "at": now_iso()})
                report["finished"] = True
        elif bus.doc("runs/%s/control.json" % run).get("state") in ("draining", "stopping"):
            quiet = _stale(bus, run, stale_minutes)
            if all(n in quiet or beat_of(bus, run, n).get("status") in ("done", "blocked", "stopped", "open", None) for n in names):
                files["runs/%s/control.json" % run] = dumps({"state": "stopped", "reason": state["reason"], "by": account, "at": now_iso()})
        fresh = files.get("runs/%s/main.json" % run)
        files["STATUS.md"] = render_status(bus, {run: json.loads(fresh)} if fresh else {})
        return files

    bus.commit(mutate, "conduct(%s): %s" % (run, account))
    return report


def old_listener_alerts(bus, run, account, files):
    """One needs-human per listener heard from in the last day that runs a copy older than the run asks for. Its conductor
    and claims follow older rules, and nothing on the bus can make it stop: only updating it can."""
    need = bus.doc("runs/%s/job.json" % run).get("min_version")
    if not need:
        return
    told = set(m.get("body") for m in messages_for(bus, run, "human"))
    shown = re.sub(r"[^0-9.]", "", str(need))[:20]
    for name in bus.listdir("listeners"):
        who, doc = name[:-5], bus.doc("listeners/" + name)
        if not name.endswith(".json") or not NAME_RE.match(who) or abs(age_minutes(doc.get("at"))) >= 24 * 60:
            continue
        if version_tuple(doc.get("version")) >= version_tuple(need):
            continue
        have = re.sub(r"[^0-9.]", "", str(doc.get("version") or ""))[:20] or "an unknown version"
        body = "Listener %s runs fast-builder %s, older than the %s run %s needs, so it still frees and takes lanes by older rules. Update the plugin on that account and run /fast-builder:listen again." % (who, have, shown, run)
        if body not in told:
            add_message(files, run, account, "human", "needs-human", body)


def quiet_limit(claim, stale_minutes):
    """How long a lane may go without a beat. A claim no session joined gets less. The token is what says so and not the
    beat's note: an old copy's join sets the token and leaves the note `claimed` until its first tick."""
    return stale_minutes if claim.get("session") else min(stale_minutes, CLAIM_QUIET_MINUTES)


def quiet_minutes(bus, run, name):
    """How long a lane has gone unheard: since its own beat, or since its listener last spoke for this very session if that
    is later. Never longer than VOUCH_MAX_MINUTES on a listener's word alone."""
    beat, claim = beat_of(bus, run, name), bus.doc("runs/%s/claims/%s.json" % (run, name))
    quiet = age_minutes(beat.get("at"))
    alive = bus.doc("runs/%s/alive/%s.json" % (run, name))
    if claim.get("session") and alive.get("session") == claim.get("session") and alive.get("account") == claim.get("account") and quiet < VOUCH_MAX_MINUTES:
        quiet = min(quiet, max(0.0, age_minutes(alive.get("at"))))
    return quiet


def _stale(bus, run, stale_minutes):
    out = []
    for name in lane_names(bus, run):
        beat = beat_of(bus, run, name)
        if bus.exists("runs/%s/claims/%s.json" % (run, name)) and beat.get("status") == "working" and quiet_minutes(bus, run, name) >= quiet_limit(bus.doc("runs/%s/claims/%s.json" % (run, name)), stale_minutes):
            out.append(name)
    return out


def main_session(bus, run):
    """Where the session a person talks to is. Everything else in a run can move by itself, but that conversation can't."""
    desk = bus.doc("runs/%s/desk.json" % run)

    def plain(key):  # anyone can push anything, and this is read out to a person and to the model summarising for them
        return re.sub(r"[^\w.:-]", "", str(desk.get(key) or ""))[:64] or None
    account = plain("account") if NAME_RE.match(str(desk.get("account"))) else None
    return {"account": account, "computer": plain("computer"), "since": plain("since"), "out_of_usage": out_of_usage(bus, account) if account else None}


def who_runs_it(bus, run):
    """The lines that say who to go to when something needs a person."""
    main = main_session(bus, run)
    if main["account"]:
        where = " on %s" % main["computer"] if main["computer"] else ""
        line = "Main session: **%s**%s, since %s." % (main["account"], where, main["since"])
        if main["out_of_usage"]:
            line += " **That account ran out of usage at %s: continue from another account.**" % main["out_of_usage"]
    else:
        line = "Main session: not recorded for this run."
    holder = bus.doc("runs/%s/conductor.json" % run).get("holder")
    lines = [line, "Conductor: %s." % (holder if NAME_RE.match(str(holder)) else "none yet")]
    dry = sorted(n[:-5] for n in bus.listdir("accounts") if n.endswith(".json") and out_of_usage(bus, n[:-5]))
    if dry:
        lines.append("Out of usage, so offered no lanes: %s." % ", ".join("%s %s" % (name, back_in(bus, name)) for name in dry))
    return lines


def render_status(bus, fresh=None):
    """fresh: main.json docs this very commit is about to write, by run."""
    lines = ["# fast-builder status", "", "Updated %s." % now_iso(), ""]
    if bus.exists("STOP"):
        lines += ["**STOP is set: every run is stopping.**", ""]
    runs = list_runs(bus)
    for run in runs[-6:]:
        job = bus.doc("runs/%s/job.json" % run)
        state = run_state(bus, run)
        lines += ["## %s: %s" % (run, state["state"]), "", job.get("goal", ""), ""]
        lines += who_runs_it(bus, run) + [""]
        main = main_view(bus, run, (fresh or {}).get(run))
        if main["state"] != "unknown":
            lines += ["Main is %s at %s since %s.%s" % (main["state"], main["sha"][:7], main["since"], " Failing: %s." % ", ".join(main["failing"]) if main["failing"] else ""), ""]
        lines += ["| Lane | Account | Status | Last beat (min) | Note |", "|---|---|---|---|---|"]
        for row in lane_table(bus, run):
            note = row["note"]
            if row["alive_min"] is not None and row["beat_age_min"] is not None and row["alive_min"] < row["beat_age_min"]:
                note = "working, says its listener %s min ago; %s" % (row["alive_min"], note)
            lines.append("| %s | %s | %s | %s | %s |" % (row["lane"], row["account"] or "-", row["status"], row["beat_age_min"] if row["beat_age_min"] is not None else "-", note.replace("|", "/")[:80]))
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
    checks = job.get("main_checks", [])
    if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
        errors.append("main_checks must be a list of check names")
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
        overflow = re.search(r"^## Overflow[ \t]*$(.*?)(?=^## |\Z)", text, re.M | re.S)
        if not overflow or not overflow.group(1).strip():
            errors.append("plan has no Overflow: what each lane takes next if it clears its list")
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
        with open(path, encoding="utf-8") as handle:
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
## Overflow
{{{{What each lane takes next if it clears its list, lane by lane, with enough there that no lane runs dry before the deadline.}}}}

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
    repo's writers. A local lane uses the copy listen-install made, so the bus never gets to run code on this computer."""
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
    claim = bus.doc("runs/%s/claims/%s.json" % (run, lane))
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
    LOST: ("Another account holds this lane now. Stop work and tell subagents to stop. fb.py has pushed this lane's own unpushed "
           "branches and a snapshot of the rest, and noted both in the lane's handoff (see `saved`). Then run fb.py leave and end the session."),
    FINISHED: "The run is finished. Write your end-of-run summary and end the session.",
}


MAIN_RED_STEWARD = "Main is red at %s: see main.failing. Fixing it comes before your next work item; then carry on."
MAIN_RED_OTHER = "Main is red at %s, and that is the steward lane's to fix. Keep building your next work item, and do not fix main unless asked."


def remember_control(path, action):
    """A lane that has just been told the run's action must not have its own hook act on an older answer."""
    write_file(os.path.join(os.path.dirname(path), "control-cache.json"), json.dumps({"ts": time.time(), "sha": None, "action": action}))


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
    except BusUnreadable:  # no blip: it stays this way, and a stop pushed after it could never reach this lane
        return UNREADABLE
    except FbError:
        return held.get("action", GO)


# Hooks


UNREADABLE = "bus-unreadable"  # the guard's alone: a tick cannot report it, since it could not read the bus to run
UNREADABLE_NOTE = ("fast-builder: the bus has a file name Windows cannot check out, so this lane cannot hear a stop or send anything. Stop work "
                   "and tell a person: the listener's log names the file, and it has to come off the bus. The run itself has not been stopped.")
GUARD_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell|Agent|Task"
STOP_FAILURE_MATCHER = "rate_limit|billing_error"
OUT_OF_USAGE_ERRORS = STOP_FAILURE_MATCHER.split("|")
# A snapshot goes up unreviewed. These go by what a file is, whatever it is called otherwise. SECRET_NAME_RE is only a name,
# so a source file is let through: SecretaryService.ts and CredentialForm.tsx are work, and leaving them out strands it.
SECRET_RE = re.compile(r"""(?ix) (?:^|/)\.env(?:$|[./]) | \.env$ | (?:^|/)\.envrc$ | (?:^|/)\.dev\.vars$ | \.(?:pem|key|p12|pfx|jks|keystore|tfvars|tfstate)$
    | (?:^|/)id_(?:rsa|dsa|ecdsa|ed25519) | (?:^|/)(?:\.git-credentials|[._]netrc|\.npmrc|\.pypirc|\.pgpass|auth\.json)$ | (?:^|/)(?:secrets?|credentials?)(?:/|\.[^/]*$|$)
    | (?:^|/)\.(?:kube|aws|ssh|gnupg)/ | settings\.local\.json$ | (?:^|/)\.claude/worktrees/""")
SECRET_NAME_RE = re.compile(r"(?i)(?:^|/)[^/]*(?:secret|credential|service[-_]?account)[^/]*$")
SOURCE_RE = re.compile(r"(?i)\.(?:ts|tsx|js|jsx|mjs|cjs|py|rb|go|java|kt|rs|php|swift|cs|c|h|cpp|vue|svelte|css)$")
WIP_MAX_BYTES = 50000000  # GitHub refuses a whole push for one file over 100 MB, and warns at 50
WIP_NAMES_SHOWN = 15
LIVE_BEATS = ("working", "claimed")
STOP_FAILURE_LOCK_SECONDS = 45
SHELL_TOOLS = ("Bash", "PowerShell")  # Claude Code can offer PowerShell on Windows with no change on this computer
# is_simple reads a command as a POSIX shell would. PowerShell ends a string at \" or a curly quote where that sees one going
# on, and a lone carriage return ends a line there, so a halted lane's PowerShell gets no character that could start
# another command, quoted or not.
PLAIN_POWERSHELL = re.compile(r"""^[\w \t.,:=/\\"'-]*\Z""")
READ_ONLY_BASH = re.compile(r"^\s*(git\s+(status|log|diff|show|fetch)|ls|cat|grep|rg|head|tail|wc|pwd|echo)\b")
KIT_BASH = re.compile(r"""^\s*(?:python3?|py(?:\s+-3)?)\s+(?:"\$FB"|\$FB|"[^"-][^"]*fb\.py"|'[^'-][^']*fb\.py'|(?:[^\s"'-][^\s"']*)?fb\.py)\s""")
PUSH_BASH = re.compile(r"""\bgit(?:\.exe)?\b["']?(?:\s+-\S+(?:\s+\S+)?)*\s+push\b|\bgh(?:\.exe)?\b["']?\s+pr\s+create\b""", re.I)


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
    if tool not in SHELL_TOOLS or not is_simple(command):
        return False
    if tool == "PowerShell" and not PLAIN_POWERSHELL.match(command):
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
    if payload.get("session_id") and not session.get("claude_session"):  # join starts a new file, so this is the lane's own
        session["claude_session"] = payload["session_id"]
        save_session(path, session)
    try:
        guard_lane_call(payload, cwd, session, path)
    except Exception as exc:
        if not WINDOWS:
            raise
        deny("fast-builder: this call could not be checked, so it is refused: %r" % exc)


def odd_windows_path(target):
    r"""A spelling the checks cannot line up with the repo: \\?\ and \\server forms, a drive with no slash, a rooted
    path with no drive, and a stream (a.ts:b)."""
    rest = re.sub(r"^[A-Za-z]:[\\/]", "", target)
    return rest.startswith(("\\", "/")) or ":" in rest


def guard_lane_call(payload, cwd, session, path):
    tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
    command = tool_input.get("command", "") if tool in SHELL_TOOLS else ""
    action = control_action(session, path)
    if action == FINISHED:
        return
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    writes = bool(target) and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit")
    if writes and WINDOWS and odd_windows_path(target):
        deny("fast-builder: write to this file with a full path that starts with a drive letter, like C:\\repo\\src\\a.ts.")
        return
    if writes and inside_git_dir(target, cwd, path):
        deny("fast-builder: no edits inside .git: a lane's rules live there.")
        return
    # A halted lane still has to write its handoff somewhere, so a file outside the repo stays open.
    if action in (STOP, LOST, PAUSE, UNREADABLE) and not allowed_when_halted(tool, command) and not (writes and repo_relative(target, cwd, path) is None and outside_tree(target, cwd)):
        deny(UNREADABLE_NOTE if action == UNREADABLE else "fast-builder: run %s says %s. %s %s" % (session["run"], action, INSTRUCTIONS[action], halted_help(action)))
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


def halted_help(action):
    """A lane that chains its handoff or sets $FB in the same command is refused, and then loses the handoff."""
    script = shlex.quote(os.path.abspath(__file__))
    rule = "Only single commands get through now, with no chaining and no $(...). "
    python = "py -3" if WINDOWS and not os.environ.get("FB_PYTHON") else "python3"  # a listener's lane has its own python3
    if action == LOST:
        return rule + "Run: %s %s leave" % (python, script)
    if action == PAUSE:
        return (rule + "Write the handoff with the Write tool to a file outside the repo, then run these as separate commands: "
                "%s %s handoff FILE, and %s %s wait --until-resumed --seconds 540." % (python, script, python, script))
    return (rule + "Write the handoff with the Write tool to a file outside the repo, then run these as separate commands: "
            "%s %s handoff FILE, and %s %s lane-state stopped." % (python, script, python, script))


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
        if os.path.dirname(folder) == folder:  # a drive that is not there is its own parent
            return None
        folder = os.path.dirname(folder)
    proc = git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"], cwd=folder, check=False, timeout=5)
    lines = proc.stdout.split("\n")
    if proc.returncode != 0 or len(lines) < 2:
        return None
    if os.path.realpath(lines[1]) != os.path.realpath(os.path.dirname(os.path.dirname(session_file))):
        return None
    return os.path.relpath(full, os.path.realpath(lines[0])).replace(os.sep, "/")  # lanes own paths written with /


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


def win_process(pid):
    """Is this pid running, and since when, as (alive, start), from the Win32 API. os.kill cannot ask: there, signal 0
    is a Ctrl+C sent to the process, and any other number kills it."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:  # another user's process counts as not alive, as it does for os.kill elsewhere
        return False, None
    try:
        if kernel.WaitForSingleObject(handle, 0) != 0x102:  # WAIT_TIMEOUT means it has not exited
            return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return True, None
        return True, str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def alive(pid):
    """Reaps the pid first: an exited child of this process otherwise still answers signal 0."""
    if WINDOWS:
        return win_process(pid)[0]
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
    another OS user."""
    path = clone_path(remote, run, lane)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        git(["clone", "-q", "--branch", base, remote, path], timeout=900)
    else:
        git(["fetch", "-q", "origin"], cwd=path, timeout=300)  # a restarted lane keeps its branch and work
    return path


def clone_path(remote, run, lane):
    return os.path.join(CACHE_DIR, "work", hashlib.sha1(remote.encode()).hexdigest()[:10], "%s-%s" % (run, lane))


def transcript_dirs(folder):
    """Where Claude Code keeps the transcripts of sessions run in folder, named for the folder with everything but letters
    and digits made a dash. Every turn and every subagent's step lands there, so it moves while a lead works even if the
    lead itself calls nothing for an hour. A long name is cut and given a suffix, so that one is found by its start."""
    base = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")
    found = []
    for path in sorted(set([folder, os.path.realpath(folder)])):
        name = re.sub(r"[^A-Za-z0-9]", "-", path)
        if os.path.isdir(os.path.join(base, name)):
            found.append(os.path.join(base, name))
        elif len(name) > 200:
            found += [p for p in globlib.glob(os.path.join(globlib.escape(base), globlib.escape(name[:200]) + "*")) if os.path.isdir(p)]
    return found


def last_activity(remote, run, lane):
    """When anything of this lane's last moved: its session transcripts, subagents' included, its log, or its clone's git,
    worktrees' too. A hung process moves none of them. None if nothing can be read."""
    folder = clone_path(remote, run, lane)
    git_dir = os.path.join(folder, ".git")
    paths = [lane_log(run, lane), os.path.join(git_dir, "index"), os.path.join(git_dir, "HEAD"), os.path.join(git_dir, "logs", "HEAD")]
    paths += globlib.glob(os.path.join(globlib.escape(git_dir), "worktrees", "*", "index")) + globlib.glob(os.path.join(globlib.escape(git_dir), "worktrees", "*", "logs", "HEAD"))
    for top in transcript_dirs(folder):
        for root, _, names in os.walk(top):
            paths += [os.path.join(root, name) for name in names]
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            pass
    return newest or None


def vouch(bus, account):
    """Speak for this listener's own lanes whose process is alive and working, so that a lead blocked on long subagents, or
    one whose pushes keep timing out on a loaded computer, is not freed for going quiet. Only a joined, working claim of
    this account's, only a process proved to be ours, and only while something of the lane has moved lately: a hung one
    is still freed. A lead unheard past VOUCH_MAX_MINUTES is not spoken for at all."""
    due = {}
    for key, child in (read_json(children_path()) or {}).items():
        run, _, lane = key.partition("/")
        if child.get("remote") != bus.remote or not (RUN_RE.match(run) and NAME_RE.match(lane)):
            continue
        claim, beat = bus.doc("runs/%s/claims/%s.json" % (run, lane)), beat_of(bus, run, lane)
        if claim.get("account") != account or not claim.get("session") or beat.get("status") != "working":
            continue
        if not VOUCH_AFTER_MINUTES <= age_minutes(beat.get("at")) < VOUCH_MAX_MINUTES:
            continue
        held = bus.doc("runs/%s/alive/%s.json" % (run, lane))
        if held.get("session") == claim.get("session") and abs(age_minutes(held.get("at"))) * 60 < VOUCH_SECONDS:
            continue
        seen = last_activity(bus.remote, run, lane) if child_state(child) == "ours" else None
        if seen and time.time() - seen < VOUCH_ACTIVE_MINUTES * 60:
            due[(run, lane)] = claim["session"]
    if not due:
        return

    def mutate():
        files = {}
        for (run, lane), token in due.items():
            claim = bus.doc("runs/%s/claims/%s.json" % (run, lane))
            if claim.get("account") == account and claim.get("session") == token and beat_of(bus, run, lane).get("status") == "working":
                files["runs/%s/alive/%s.json" % (run, lane)] = dumps({"account": account, "session": token, "at": now_iso()})
        return files
    try:
        bus.commit(mutate, "alive: %s" % account)
    except FbError:
        pass  # a vouch that cannot land is one the next pass tries again


def process_start(pid):
    """When a process began, or None if `ps` cannot say. In UTC and the C locale, so the same process reads the same after
    a change of time zone. The command line would not do: a live CLI claude shows in `ps` as bare `claude`."""
    if WINDOWS:
        return win_process(pid)[1]
    try:
        return run_cmd(["ps", "-o", "lstart=", "-p", str(pid)], check=False, timeout=10, env=dict(os.environ, LC_ALL="C", TZ="UTC")).stdout.strip() or None
    except FbError:
        return None


def end_tree(pid):
    """End a lane and what it started, or raise OSError if that cannot be asked. Whether it worked is for alive() to say."""
    if not WINDOWS:
        os.killpg(pid, signal.SIGTERM)
        return
    # ponytail: /F because a process with no window has no SIGTERM. /T misses a child whose parent already exited, and a
    # job object would catch that one if it ever matters.
    try:
        run_cmd(["taskkill", "/T", "/F", "/PID", str(pid)], check=False, timeout=30)
    except FbError as exc:
        raise OSError(str(exc))


def child_state(child):
    """Is this entry still our process? A pid outlives its process, and a computer that restarted hands the same numbers out
    again, so a live pid alone proves nothing. "gone": dead, or another process with its number, so not ours to touch.
    "ours": the start time recorded at launch still matches. "unsure": alive, but with no recorded time, from an older
    version, or none to read now. That gets no signal, and nothing starts beside it."""
    if not alive(child["pid"]):
        return "gone"
    recorded, actual = child.get("started"), process_start(child["pid"])
    if not recorded or not actual:
        return "unsure"
    return "ours" if recorded == actual else "gone"


def unsure_note(key, child):
    return "%s: pid %d cannot be told from an unrelated process, so it was not signalled. End it by hand if it is the lane, or remove %s from %s." % (key, child["pid"], key, children_path())


def end_child(key, remote):
    """Clear the way for a new lane. A hung lane is still alive when its lane is freed, and a second process in its clone,
    with the first no longer tracked, is what this prevents: it gets the signal a stopped lane gets, and time to go. If it
    will not go, this raises, and its entry stays in children.json so it is never untracked. Only a process proved to be
    ours is signalled."""
    child = (read_json(children_path()) or {}).get(key)
    state = child_state(child) if child else "gone"
    if state == "gone":
        return
    if child.get("remote") != remote:
        raise FbError("%s is already running for %s" % (key, repo_slug(child.get("remote") or "another repo")))
    if state == "unsure":
        raise FbError(unsure_note(key, child))
    try:
        end_tree(child["pid"])
    except OSError:
        pass
    end = time.time() + LANE_STOP_SECONDS
    while alive(child["pid"]) and time.time() < end:
        time.sleep(0.1)
    if alive(child["pid"]):
        raise FbError("its last process, pid %d, will not stop, so no second one was started in its clone" % child["pid"])


def start_joining_clock(bus, run, lane, account):
    """A claim's time to be joined starts when a session can start. A first clone can take most of it. Only ever this
    account's own claim that nobody joined: the clone may have outlasted it."""
    def mutate():
        beat = beat_of(bus, run, lane)
        claim = bus.doc("runs/%s/claims/%s.json" % (run, lane))
        if claim.get("account") != account or claim.get("session") or beat.get("status") != "working":
            return {}
        return {"runs/%s/beats/%s.json" % (run, lane): dumps(dict(beat, at=now_iso()))}
    try:
        bus.commit(mutate, "beat(%s): %s starting" % (run, lane))
    except FbError:
        pass  # only a clock: it must not cost the lane its start


def claude_call(prompt, *rest):
    """A claude command, and the prompt if it has to go on stdin. npm's claude.cmd runs through cmd.exe, which ends an
    argument at a newline and acts on & and |. Windows also finds only an .exe without the full path."""
    if WINDOWS:
        found = shutil.which(CLAUDE) or CLAUDE
        if found.lower().endswith((".cmd", ".bat")) and any(set(arg) & set('&|<>^%"') for arg in [found] + list(rest)):
            raise FbError("a path here has a character cmd.exe would act on, so npm's claude.cmd cannot be used safely: move the folder, or install claude natively")
        return [found, "-p"] + list(rest), prompt
    return [CLAUDE, "-p", prompt] + list(rest), None


def console_python():
    """This Python with a console, since the listener itself runs under pythonw, which prints nothing."""
    folder, name = os.path.split(sys.executable)
    return os.path.join(folder, "python.exe") if name.lower() == "pythonw.exe" else sys.executable


def lane_env(account):
    env = dict(os.environ, FB_ACCOUNT=account, FB=os.path.join(HOME_DIR, "fb.py"))
    if WINDOWS:  # a lane's brief says python3, which there is missing or the Store's stub. Its Bash tool is Git Bash.
        shim = os.path.join(HOME_DIR, "bin")
        write_file(os.path.join(shim, "python3"), '#!/bin/sh\nexec "%s" "$@"\n' % console_python().replace("\\", "/"))
        env["PATH"] = shim + os.pathsep + env.get("PATH", "")
        env["FB_PYTHON"] = console_python()  # for hooks: this one is known to work, whatever else is installed
    return env


def launch_lane(bus, repo_cfg, run, lane, account, attempt=0):
    """Start a local lane under this account, in its own clone. The prompt is a fixed template: nothing from the bus
    reaches a command line."""
    job = bus.doc("runs/%s/job.json" % run)
    errors = job_errors(job)
    if errors or not (RUN_RE.match(run) and NAME_RE.match(lane)):
        raise FbError("refusing an unsafe run: %s" % ", ".join(errors or ["bad run or lane name"]))
    bus.release()  # a first clone can take minutes, and lanes on this account need the bus checkout meanwhile
    end_child("%s/%s" % (run, lane), bus.remote)
    clone = work_clone(bus.remote, run, lane, job.get("base", "main"))
    bus.sync()
    start_joining_clock(bus, run, lane, account)
    os.makedirs(os.path.join(HOME_DIR, "logs"), exist_ok=True)
    log_path = lane_log(run, lane)
    log_from = os.path.getsize(log_path) if os.path.exists(log_path) else 0  # where this session's words start: the log is appended to
    log = open(log_path, "a")
    rest = ["--model", job.get("lane_model", "opus"), "--permission-mode", repo_cfg.get("permission_mode", "auto"), "--permission-prompts", "none", "--name", "fb %s %s" % (run, lane)]
    if os.path.isdir(plugin_copy()):
        rest += ["--plugin-dir", plugin_copy()]
    args, prompt = claude_call(lane_prompt(run, lane, account, local=True), *rest)
    stdin = subprocess.DEVNULL
    if prompt is not None:
        stdin = tempfile.TemporaryFile("w+", encoding="utf-8")
        stdin.write(prompt)
        stdin.seek(0)
    try:
        proc = subprocess.Popen(args, cwd=clone, stdout=log, stderr=log, stdin=stdin, env=lane_env(account), **DETACHED)
    except OSError as exc:
        raise FbError("cannot start claude: %s" % exc)
    finally:
        log.close()
        if prompt is not None:
            stdin.close()
    children = read_json(children_path()) or {}
    children["%s/%s" % (run, lane)] = {"pid": proc.pid, "started": process_start(proc.pid), "remote": bus.remote, "attempt": attempt, "at": now_iso(), "log_from": log_from}
    write_file(children_path(), dumps(children))
    return proc.pid


def lane_log(run, lane):
    return os.path.join(HOME_DIR, "logs", "%s-%s.log" % (run, lane))


def lane_said(child, run, lane):
    """What a lane's last session printed. Only from where its launch began, since earlier sessions wrote to the same log.
    An entry from before log_from was recorded gets only the very end, and only if the log was written after it started."""
    path = lane_log(run, lane)
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if isinstance(child.get("log_from"), int):
                start = child["log_from"] if child["log_from"] <= size else 0  # a log cut short since: all of it is newer
                start = max(start, size - LOG_TAIL_BYTES)
            else:
                if os.path.getmtime(path) < parse_iso(child.get("at")).timestamp():
                    return ""
                start = max(0, size - 2048)
            handle.seek(start)
            return handle.read().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        return ""


def ran_dry(bus, run, lane, account, limit):
    """A lane's session said its account is out of usage. Nothing could restart it, so the lane goes back at once, not to
    this account, and the account is offered no lane until its limit resets. lane None: only mark the account."""
    back = ", back %s" % limit["until"] if limit.get("until") else ""
    why = "freed: %s ran out of usage (%s%s)" % (account, limit.get("said", "usage limit"), back)

    def mutate():
        files = dry_files(bus, account, limit)
        if lane:
            claim, beat = bus.doc("runs/%s/claims/%s.json" % (run, lane)), beat_of(bus, run, lane)
            if claim.get("account") == account and beat.get("status") in LIVE_BEATS:
                files.update(lost_lane(bus, run, lane, account))
                files["runs/%s/claims/%s.json" % (run, lane)] = None
                files["runs/%s/beats/%s.json" % (run, lane)] = dumps({"at": now_iso(), "status": "open", "account": None, "note": why[:200]})
                add_message(files, run, account, "all", "note", "Lane %s %s." % (lane, why))
        return files
    bus.commit(mutate, "out of usage(%s): %s" % (run, account))


def stated_status(text):
    """The HTTP status a result opens with. The whole first line has to be the status: "200 was expected, but the tool
    returned 404" is no 200. Whatever follows that line does not matter."""
    line = text.strip().split("\n")[0].strip()
    found = STATUS_RE.match(line)
    return found.group(1) if found else line[:40]


def tool_reply(stream, trigger, action):
    """What RemoteTrigger answered, as (status, body), from the stream-json transcript and not from the model. Only the
    result of this very call counts, so a reply that says 200 proves nothing, and neither does a call that never ran.
    The whole tool is granted, so any other RemoteTrigger call means the model was steered, and that fails it, with one
    exception: a `run` that answered 200 has started and cannot be unstarted, which STEERED reports."""
    calls, reply, other = [], ("no tool result", ""), False
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
                if isinstance(given, dict) and given.get("action") == action and given.get("trigger_id") == trigger:
                    calls.append(block.get("id"))
                else:
                    other = True
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls and reply[0] != "200":
                content = block.get("content")
                text = content if isinstance(content, str) else "".join(str(b.get("text", "")) for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
                reply = ("error" if block.get("is_error") else stated_status(text), text.partition("\n")[2])
    if not other:
        return reply
    return (STEERED, reply[1]) if action == "run" and reply[0] == "200" else ("another RemoteTrigger call", "")


def exit_note(stream, code):
    """Why claude failed, in words that stay the same from one try to the next, or a repeated failure is a new blocker each
    time: stdout opens with events full of fresh ids, and stderr is often empty. A login failure exits 1 with the subtype
    `success`, and only its result text says what went wrong."""
    subtype, said = None, None
    for line in stream.split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            subtype, said = event.get("subtype"), event.get("result")
    note = "claude exited %d" % code
    if subtype and subtype != "success":
        note += " (%s)" % str(subtype)[:40]
    said = " ".join(VOLATILE_RE.sub("", said.strip().split("\n")[0]).split())[:120] if isinstance(said, str) else ""
    return note + ": " + said if said else note


def routine_call(trigger, action):
    """Call the routine API through a headless Haiku, and return the (status, body) the tool got. Exit 0 proves nothing
    here: a routine on another login answers 404 or 400 and claude still exits cleanly, so only a 200 from the tool
    counts. It counts on any exit, since by then the routine has started."""
    if not TRIGGER_RE.match(trigger):
        raise FbError("bad routine id")
    prompt = "Use the RemoteTrigger tool once, with action %s and trigger_id %s. Then reply with only the word done." % (action, trigger)
    args, stdin_text = claude_call(prompt, "--model", "haiku", "--allowedTools", "RemoteTrigger", "--permission-prompts", "none", "--max-turns", "4", "--output-format", "stream-json", "--verbose")
    os.makedirs(HOME_DIR, exist_ok=True)
    proc = run_cmd(args, cwd=HOME_DIR, check=False, timeout=120, stdin_text=stdin_text)
    reply = tool_reply(proc.stdout or "", trigger, action)
    if proc.returncode != 0 and reply[0] not in ("200", STEERED):
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
    Logins get swapped on an OS user, and a routine belongs to its login, so the bus is what lets any OS user find the
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
    """Start the routine of whichever account this OS user is signed in to, and return (account, routine). `claude
    --cloud` needs a real terminal and can stop at interactive dialogs, so a daemon cannot use it. A routine on another
    login answers 404 or 400 and starts nothing, so walking the roster finds the login without trusting a cached profile.

    The bus only suggests ids. `own` came from this OS user's config and goes first, so a healthy listener tries nothing
    from the bus. Any other row must first prove with `get` that it is a listener for this repo, and that routine, not the
    bus, says whose it is. About half a cent of Haiku a call. The third value is False for a steered session: the routine
    started, and nothing else that session said can be relied on."""
    first = None
    for who, trigger in rows:
        if trigger != own:
            status, who = routine_owner(trigger, slug)
            if not who:
                first = first or status
                continue
        status = ask_routine_api(trigger, "run")
        if status in ("200", STEERED):
            return who, trigger, status == "200"
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


ROUTINE_HELP = ": this OS user's claude login owns none of the routines on the bus, see claude auth status, or set this login up once with /fast-builder:listen"


def roster_help(cut):
    """What to do when nothing started. With rows left off the list, nobody can say this login owns none of them."""
    if not cut:
        return ROUTINE_HELP
    return ": %d more routines on the bus went untried past the limit of %d, so this login may own one of them: remove the rows that do not belong from listeners/ on the bus, or run /fast-builder:switch NAME" % (cut, MAX_ROSTER)


def supervise(bus, repo_cfg, account, stops_only=False):
    """Restart a local lane whose claude exited with its lane unfinished, up to a cap, and end children of a stopped
    run after giving them time to write a handoff. Cloud lanes are not children: the conductor covers those.
    stops_only: just the second half, for before conducting, which must come first for the restarts."""
    children = read_json(children_path()) or {}
    changed = False
    for key in list(children):
        child = children.get(key)  # from the dict as it is now: a restart below reads it back from disk
        if not isinstance(child, dict) or child.get("remote") != bus.remote:
            continue
        run, lane = key.split("/", 1)
        action = run_state(bus, run)["action"]
        state = child_state(child)
        running = state != "gone"
        if action in (STOP, FINISHED):
            if running:
                child.setdefault("term_at", time.time())
                if time.time() - child["term_at"] > 120:
                    try:
                        if state == "ours":
                            end_tree(child["pid"])
                        else:
                            post_once(bus, run, account, unsure_note(key, child))
                    except (OSError, FbError):
                        pass
                changed = True
            elif not running:
                del children[key]
                changed = True
            continue
        if stops_only or running or action != GO:
            continue
        status = beat_of(bus, run, lane).get("status")
        claim = bus.doc("runs/%s/claims/%s.json" % (run, lane))
        ours = status == "working" and claim.get("account") == account
        if not child.get("exited"):
            child["exited"] = time.time()
            changed = True
            # The CLI's own last words, not anything earlier: a lead's summary may quote a limit message
            limit = usage_limit("\n".join([line for line in lane_said(child, run, lane).splitlines() if line.strip()][-3:]))
            if limit:  # no restart can work, and every one would print the same thing again
                child["dry"] = limit
        if child.get("dry"):  # kept until it lands: the log is the only evidence, and the next pass reads no further than this
            try:
                ran_dry(bus, run, lane if ours else None, account, child["dry"])
            except FbError:
                continue
            del children[key]
            changed = True
            continue
        lived = age_minutes(child.get("at"))
        attempt = 0 if STALE_MINUTES <= lived < float("inf") else child.get("attempt", 0)  # the cap is for sessions that die young, not for a lane that restarts at its context limit
        wait = RESTART_BACKOFF_SECONDS * 2 ** (attempt - 1) if attempt else 0
        if ours and attempt < MAX_RESTARTS and time.time() - child["exited"] < wait:
            continue  # an outage that killed it kills a restart too, and restarts are few
        del children[key]
        changed = True
        if ours and attempt < MAX_RESTARTS:
            write_file(children_path(), dumps(children))
            try:
                launch_lane(bus, repo_cfg, run, lane, account, attempt + 1)
            except FbError as exc:
                release_lane(bus, run, lane, account, str(exc))
            children = read_json(children_path()) or {}
        elif ours:  # nothing left will restart it, so the lane must not sit here looking alive until the conductor frees it
            write_file(children_path(), dumps(children))
            try:
                release_lane(bus, run, lane, account, "its session exited %d times in a row on %s" % (attempt + 1, account), cool=True)
            except FbError:
                pass  # still claimed, so the conductor frees it in the end
    if changed:
        write_file(children_path(), dumps(children))


def pick_lane(bus, run, account, skip=()):
    """The lane this account should take now, if any. A lane freed from an account that went quiet or stopped is fair
    game for an account already in the run; a fresh lane is not, so one account never ends up carrying a whole run.
    A lane this account just lost to going quiet is neither: it probably has no usage left to run it with. An account that
    said it has none gets no lane at all, or it would start one, stop at once and free it, one lane after another.
    skip: lanes this pass freed itself, which go to whichever listener sees them next, never straight back to this one.
    No account holds more than the run's lane cap."""
    if out_of_usage(bus, account) or too_old(bus, run):
        return None
    table = lane_table(bus, run)
    open_lanes = [r["lane"] for r in table if r["account"] is None and r["lane"] not in skip and not cooling(bus, run, r["lane"], account)]
    if not open_lanes:
        return None
    held = [r for r in table if r["account"] == account]
    if not held:
        return open_lanes[0]
    live = [r for r in held if r["status"] in LIVE_BEATS]  # a lane marked done keeps its claim and costs nothing
    if len(live) >= lane_cap(bus, run):
        return None
    freed = [r for r in table if r["lane"] in open_lanes and r["status"] == "open" and beat_of(bus, run, r["lane"])]
    # A second lane doubles how fast this account runs dry, so one with no lane gets the first go at it.
    ready = [r["lane"] for r in freed if not live or abs(age_minutes(r["beat"])) >= SPREAD_MINUTES or not other_takers(bus, run, r["lane"], account)]
    return ready[0] if ready else None


def lane_cap(bus, run):
    cap = bus.doc("runs/%s/job.json" % run).get("lane_cap")
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= 20 else DEFAULT_LANE_CAP


def version_tuple(text):
    parts = re.findall(r"\d+", str(text or ""))[:4]
    return tuple(int(p) for p in parts) if parts else (0,)


def too_old(bus, run):
    """Whether this copy is older than the run asks for. An older copy frees and takes lanes by older rules, which is how
    one account out of usage ended up holding a whole run."""
    return version_tuple(VERSION) < version_tuple(bus.doc("runs/%s/job.json" % run).get("min_version"))


def other_takers(bus, run, lane, account):
    """Listeners besides this account that could take this lane now and hold none in the run: heard from lately, able to
    run lanes where this run runs them, not out of usage, not cooling on this lane, and new enough for the run."""
    job = bus.doc("runs/%s/job.json" % run)
    holders = set(r["account"] for r in lane_table(bus, run) if r["account"] and r["status"] in LIVE_BEATS)
    found = []
    for name in bus.listdir("listeners"):
        who = name[:-5]
        if not name.endswith(".json") or who == account or who in holders or not NAME_RE.match(who):
            continue
        doc = bus.doc("listeners/" + name)
        if abs(age_minutes(doc.get("at"))) >= LISTENER_LIVE_MINUTES:
            continue
        if not (doc.get("local", True) if job.get("where", "cloud") == "local" else doc.get("cloud", True) and doc.get("routine")):
            continue
        if out_of_usage(bus, who) or cooling(bus, run, lane, who) or version_tuple(doc.get("version")) < version_tuple(job.get("min_version")):
            continue
        found.append(who)
    return found


def claim_picked(bus, run, lane, account):
    second = any(r["account"] == account for r in lane_table(bus, run))
    try:
        return claim_lane(bus, run, account, lane, allow_second=second, cap=lane_cap(bus, run)) == lane
    except Taken:
        return False


def start_lane(bus, repo_cfg, run, lane, account, memory, budget):
    """Local: claim and launch. Cloud: wake this account's routine, which claims a lane from inside the cloud. `budget` is
    what this pass may still spend on wakes: a run it cannot pay for keeps its turn for the next pass."""
    job = bus.doc("runs/%s/job.json" % run)
    failed = memory.setdefault("failed", {})
    if job.get("where", "cloud") == "local":
        if time.time() - failed.get(run, 0) < WAKE_SECONDS:  # a lane that cannot start must not churn the bus
            return []
        if not repo_cfg.get("local", True):
            return []
        dry = any(dry_pending(c) for c in (read_json(children_path()) or {}).values())
        busy = "a lane here said this account is out of usage, and the bus has not heard it yet" if dry else no_room_here(load_config())
        if busy:  # unclaimed, so a listener on another computer can have it meanwhile
            said = memory.get("held") == busy
            memory["held"] = busy
            return [] if said else [{"did": "held", "run": run, "lane": lane, "why": busy}]
        memory.pop("held", None)
        if not claim_picked(bus, run, lane, account):
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
        who, trigger, clean = wake_roster(rows, own, slug, cut)
    except FbError as exc:
        budget["calls"] = 0  # the same rows and the same login: another run's walk would find the same nothing
        post_once(bus, run, account, "%s could not start its routine: %s" % (account, exc))
        return [{"did": "wake-failed", "run": run, "error": str(exc)}]
    # It started, so nothing after this line may say otherwise. The walk only gets past `own` on a 400 or 404, so a new
    # name with an `own` is a proved swap. With no `own` nothing was disproved: the routine ran, and the name stays. A
    # steered session proves nothing either.
    if clean:
        memory["proved"] = [who, trigger]
        if who == account:
            repo_cfg["routine_id"] = trigger
        if trigger != own and (who == account or own):
            adopt_login(who, {slug: trigger})
    budget["calls"] -= 1 if trigger == own else len(rows) + 1
    if not clean or (who != account and own):
        budget["calls"] = 0  # the rest of this pass is working under the old name, or from a session nobody can trust
    action = {"did": "woke-routine", "run": run, "routine": trigger, "account": who}
    try:
        if not clean:
            post_once(bus, run, account, "%s started routine %s, but that session also made another RemoteTrigger call, so it changed nothing here. Check the routine ids under listeners/ on the bus." % (account, trigger))
        bus.sync()
    except FbError as exc:
        action["sync_failed"] = str(exc)
    return [action]


def dry_pending(child):
    """Whether a lane here said its account is out and the bus may not know yet. Only until the reset it gave, or for the
    cooldown without one: the entry can outlive its repo, or wait on a paused run, and must not hold every lane forever."""
    limit = child.get("dry") if isinstance(child, dict) else None
    if not isinstance(limit, dict):
        return False
    if limit.get("until"):
        return bool(dry_until(limit))
    try:
        return time.time() - float(child.get("exited") or 0) < COOLDOWN_MINUTES * 60
    except (TypeError, ValueError):
        return False


def no_room_here(config):
    """Why this computer should start no more local lanes now, or None. Lanes on one computer share it: three OS users with
    four lanes each pushed the load to 35 on 14 cores, git timed out a thousand times, and healthy lanes went quiet and were
    freed. Counts this OS user's live lanes across every repo, and the load of the whole computer."""
    cap = config.get("max_local_lanes", DEFAULT_MAX_LOCAL_LANES)
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        live = [k for k, c in (read_json(children_path()) or {}).items() if isinstance(c, dict) and isinstance(c.get("pid"), int) and child_state(c) != "gone"]
        if len(live) >= cap:
            return "%d local lanes already running here, the most this OS user runs (max_local_lanes)" % len(live)
    per_cpu = config.get("max_load_per_cpu", LOAD_PER_CPU)
    if isinstance(per_cpu, (int, float)) and not isinstance(per_cpu, bool) and per_cpu > 0 and hasattr(os, "getloadavg"):
        try:
            load, cores = os.getloadavg()[1], os.cpu_count() or 1
        except OSError:
            return None
        if load > per_cpu * cores:
            return "this computer's load is %.0f on %d cores, over max_load_per_cpu %s" % (load, cores, per_cpu)
    return None


def release_lane(bus, run, lane, account, reason, cool=False):
    """Give a lane back, so the next listener can take it. cool: and not to this account, for the cooldown."""
    text = "Could not start lane %s: %s" % (lane, reason)

    def mutate():
        files = {"runs/%s/claims/%s.json" % (run, lane): None,
                 "runs/%s/beats/%s.json" % (run, lane): dumps({"at": now_iso(), "status": "open", "account": None, "note": reason[:200]})}
        if cool:
            files.update(lost_lane(bus, run, lane, account))
        if not any(m.get("body") == text for m in messages_for(bus, run, "human")):  # a lane refused all day is one blocker
            files["runs/%s/msgs/%s.json" % (run, make_id(account))] = dumps(message(account, "human", "blocker", text))
        return files
    bus.commit(mutate, "release(%s): %s" % (run, lane))


def poll_repo(remote, repo_cfg, account, heads, launch=True):
    """One pass over one repo. Costs one ls-remote and no tokens unless the bus moved."""
    bus = Bus(remote, account)
    sha = bus.head()
    actions = []
    last = heads.setdefault(remote, {})  # here and not at the end: a pass that dies must still remember its wake
    beat_due = time.time() - last.get("beat", 0) > LISTENER_BEAT_SECONDS  # often enough to tell a listener gone from one with no lane
    conduct_due = time.time() - last.get("conduct", 0) > 300
    if not sha or (sha == last.get("sha") and not beat_due and not conduct_due and not (read_json(children_path()) or {})):
        return actions
    bus.sync()
    freed = {}
    if conduct_due:  # before restarts: a dead lane it frees must not be restarted there and started again below
        supervise(bus, repo_cfg, account, stops_only=True)  # and after stops: asking gh can take a minute
        asked = {}
        for run in active_runs(bus):
            if too_old(bus, run):
                try:
                    post_once(bus, run, account, "%s's listener runs fast-builder %s, older than run %s needs, so it takes no lanes there and does not conduct it. Update the plugin on %s and run /fast-builder:listen again." % (account, VERSION, run, account))
                except FbError:
                    pass
                continue
            try:
                report = conduct(bus, run, account, asked=asked)
            except Exception as exc:  # one bad run must not cost another run's stop
                actions.append({"did": "conduct-failed", "run": run, "error": repr(exc)[:200]})
                continue
            freed[run] = report["freed"]
            if report["freed"] or report["finished"]:
                actions.append(dict(report, did="conduct"))
    supervise(bus, repo_cfg, account)  # before any wake: those run one Haiku call at a time, and a stop must not wait
    vouch(bus, account)
    budget = {"calls": PASS_CALLS}
    for run in sorted(active_runs(bus), key=lambda r: last.get("woke", {}).get(r, 0)):  # or a failing walk starves all but the first
        if launch and run_state(bus, run)["action"] == GO:
            pick = pick_lane(bus, run, account, skip=freed.get(run, ()))
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
# The repeating trigger brings a dead listener back, as launchd and systemd would: RestartOnFailure is only for a task that
# could not start, and IgnoreNew makes it do nothing while the listener runs.
TASK = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>fast-builder listener</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
    <TimeTrigger><Enabled>true</Enabled><StartBoundary>2026-01-01T00:00:00</StartBoundary><Repetition><Interval>PT5M</Interval></Repetition></TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{python}</Command><Arguments>"{script}" listen</Arguments></Exec></Actions>
</Task>
"""
# KillMode=process: stopping the listener must leave its lanes running, and a systemd default would end them with it.
UNIT = """[Unit]
Description=fast-builder listener

[Service]
ExecStart="{python}" "{script}" listen
Environment="PATH={path}" "FB_ACCOUNT={account}"
Restart=always
RestartSec=30
KillMode=process
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""
LABEL = "com.shorconsulting.fast-builder.listener"
UNIT_NAME = "fast-builder-listener.service"


def plugin_copy():
    return os.path.join(HOME_DIR, "plugin")


def service_file():
    """Where this operating system's job for the listener is kept."""
    home = os.path.expanduser("~")
    if WINDOWS:
        return os.path.join(HOME_DIR, "listener-task.xml")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "LaunchAgents", LABEL + ".plist")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "systemd", "user", UNIT_NAME)


def task_name():
    """Scheduled tasks share one list per computer, so each Windows user needs a name of their own."""
    return "fast-builder-listener-" + re.sub(r"[^\w.-]", "-", os.environ.get("USERNAME", "user"))


def git_bash():
    """Git Bash, which Claude Code runs hooks and Bash in on Windows. Without it they run in PowerShell, which cannot parse
    the hooks, so every edit would be refused while the PowerShell tool ran on."""
    usual = [os.path.join(os.environ.get(name, default), "Git", "bin", "bash.exe") for name, default in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
    for path in [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or ""] + usual:  # where Claude Code looks first
        if path and os.path.exists(path):
            return path
    folder = os.path.dirname(shutil.which("git") or "")
    for _ in range(3):  # Git\cmd\git.exe or Git\mingw64\bin\git.exe. WSL's bash in System32 is another thing.
        path = os.path.join(folder, "bin", "bash.exe")
        if os.path.exists(path):
            return path
        folder = os.path.dirname(folder)
    return None


def windows_problem():
    """What stops a lane being guarded on this computer, if anything."""
    if not WINDOWS:
        return None
    if not git_bash():
        return "Git Bash was not found, and lanes need it for their hooks: install Git for Windows, or set CLAUDE_CODE_GIT_BASH_PATH"
    if any(folder.startswith("\\\\") for folder in (HOME_DIR, CACHE_DIR, os.getcwd())):
        return "a folder here is on a network share, which the guard cannot check paths on: set FB_HOME and FB_CACHE to a local drive, and clone there"
    return None


def windows_task(python, script, user):
    folder, name = os.path.split(python)
    quiet = os.path.join(folder, "pythonw.exe")  # no console window for as long as the listener runs
    if name.lower() == "python.exe" and os.path.exists(quiet):
        python = quiet
    return TASK.format(user=escape(user), python=escape(python), script=escape(script))


def systemd_unit(python, script, path, account, log):
    def plain(text):  # systemd expands % everywhere
        return text.replace("%", "%%")

    def command(text):  # and $ only in a command
        return plain(text).replace("$", "$$")
    return UNIT.format(python=command(python), script=command(script), path=plain(path), account=account, log=plain(log))


def service_text(account):
    script, log = os.path.join(HOME_DIR, "fb.py"), os.path.join(HOME_DIR, "listener.log")
    claude = shutil.which(CLAUDE) or ""
    if WINDOWS:
        user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
        return windows_task(sys.executable, script, user)
    if sys.platform == "darwin":
        path = os.pathsep.join(p for p in [os.path.dirname(claude), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"] if p)
        return PLIST.format(label=LABEL, python=sys.executable, script=script, path=path, account=account, log=log)
    path = os.pathsep.join(p for p in [os.path.dirname(claude), os.environ.get("PATH", "")] if p)  # a user service starts with almost none
    return systemd_unit(sys.executable, script, path, account, log)


def start_service(text):
    """Install the listener's job and start it now. A reinstall replaces the old one and leaves its lanes running."""
    if WINDOWS:
        if windows_problem():
            raise FbError(windows_problem())
        os.makedirs(HOME_DIR, exist_ok=True)
        with open(service_file(), "w", encoding="utf-16", newline="\r\n") as handle:  # schtasks wants what the XML declares
            handle.write(text)
        run_cmd(["schtasks", "/End", "/TN", task_name()], check=False)
        proc = run_cmd(["schtasks", "/Create", "/TN", task_name(), "/XML", service_file(), "/F"], check=False)
        if proc.returncode != 0:
            raise FbError("Task Scheduler refused the listener: %s. If this says access denied, run this once from a terminal opened as administrator." % (proc.stderr or proc.stdout).strip()[:300])
        run_cmd(["schtasks", "/Run", "/TN", task_name()])
    elif sys.platform == "darwin":
        write_file(service_file(), text)
        domain = "gui/%d" % os.getuid()
        run_cmd(["launchctl", "bootout", domain + "/" + LABEL], check=False)
        load_listener(domain)
    else:
        write_file(service_file(), text)
        for step in (["daemon-reload"], ["enable", UNIT_NAME], ["restart", UNIT_NAME]):
            proc = run_cmd(["systemctl", "--user"] + step, check=False)
            if proc.returncode != 0:
                raise FbError("systemctl --user %s failed: %s. The listener needs a systemd user session, which WSL only has with systemd on in /etc/wsl.conf." % (step[0], (proc.stderr or proc.stdout).strip()[:300]))


def stop_service():
    if WINDOWS:
        run_cmd(["schtasks", "/End", "/TN", task_name()], check=False)
        run_cmd(["schtasks", "/Delete", "/TN", task_name(), "/F"], check=False)
        if run_cmd(["schtasks", "/Query", "/TN", task_name()], check=False).returncode == 0:
            raise FbError("the task %s is still there, so run this from a terminal opened as administrator" % task_name())
    elif sys.platform == "darwin":
        run_cmd(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], check=False)
    else:
        run_cmd(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    if os.path.exists(service_file()):
        os.remove(service_file())
    if not WINDOWS and sys.platform != "darwin":
        run_cmd(["systemctl", "--user", "daemon-reload"], check=False)


def load_listener(domain):
    """launchd refuses a bootstrap for a moment after a bootout, so a reinstall has to retry."""
    for attempt in range(10):
        proc = run_cmd(["launchctl", "bootstrap", domain, service_file()], check=False)
        if proc.returncode == 0:
            return
        time.sleep(1)
    raise FbError("launchctl bootstrap failed: %s" % (proc.stderr or proc.stdout).strip()[:300])


def kit_older(bus):
    """Whether the bus's kit is a copy older than this one. A kit with no version to read is left alone: a person or a
    publish may have put it there on purpose."""
    found = re.search(r'^VERSION = "([0-9.]{1,20})"', bus.read("kit/fb.py") or "", re.M)
    return bool(found) and version_tuple(found.group(1)) < version_tuple(VERSION)


def announce_listener(bus, account, doc, proved=False):
    """Say this account is listening, and put the kit on the bus so its routine can poll before any run exists. A routine
    nothing has just `proved` never replaces the one on the bus: the config can be the stale side of the two."""
    def mutate():
        held = bus.json("listeners/%s.json" % account)
        kept = held.get("routine") if isinstance(held, dict) else None
        if not proved and isinstance(kept, str) and BUS_TRIGGER_RE.match(kept):
            doc["routine"] = kept
        files = {"listeners/%s.json" % account: dumps(doc)}
        if not bus.exists("kit/fb.py") or kit_older(bus):  # cloud lanes run the bus's copy
            files.update(kit_files())
        return files
    bus.commit(mutate, "listener: %s" % account)


def listen_install(args):
    """Register a repo for this account and install the listener's background job, so it survives reboots and costs no tokens."""
    account = account_name(args)
    config = load_config()
    same_account = config.get("account") in (None, account)
    if not same_account:  # a routine belongs to one login, so no repo keeps the previous name's
        for cfg in config.get("repos", {}).values():
            if isinstance(cfg, dict):
                cfg["routine_id"] = None
    config["account"] = account
    remote = args.remote or cwd_remote()
    slug = repo_slug(remote)
    held = config.setdefault("repos", {}).get(slug)
    held = held if isinstance(held, dict) else {}
    routine = args.routine_id or (held.get("routine_id") if same_account else None)
    # A flag not given keeps what was chosen, except bypass: that takes saying so at the install that sets it
    kept = held.get("permission_mode")
    mode = args.permission_mode or (kept if kept in KEPT_MODES else "auto")
    warning = None
    if kept == BYPASS and not args.permission_mode:
        warning = "WARNING: permission mode %s is not kept across a reinstall, and local lanes in %s now run in auto. To keep it, say so: listen-install --permission-mode %s" % (BYPASS, slug, BYPASS)
        print(warning, file=sys.stderr)
    cloud = held.get("cloud") is not False if args.cloud is None else args.cloud
    local = held.get("local") is not False if args.local is None else args.local
    config["repos"][slug] = {"remote": remote, "permission_mode": mode, "cloud": cloud, "local": local, "routine_id": routine}
    if args.max_local_lanes is not None:  # this computer's, so for every repo: 0 means no limit
        config["max_local_lanes"] = args.max_local_lanes
    if args.max_load_per_cpu is not None:
        config["max_load_per_cpu"] = args.max_load_per_cpu
    save_config(config)
    script = os.path.join(HOME_DIR, "fb.py")
    os.makedirs(HOME_DIR, exist_ok=True)
    if os.path.abspath(__file__) != os.path.abspath(script):
        shutil.copyfile(os.path.abspath(__file__), script)
    for path in globlib.glob(os.path.join(docs_dir(), "*.md")):
        write_file(os.path.join(HOME_DIR, "docs", os.path.basename(path)), read_text(path))
    root = os.path.dirname(HERE)
    if os.path.isdir(os.path.join(root, ".claude-plugin")) and os.path.abspath(root) != plugin_copy():
        # Headless lanes load this copy, so the guard runs even on an account that never installed the plugin.
        shutil.rmtree(plugin_copy(), ignore_errors=True)
        shutil.copytree(root, plugin_copy(), ignore=shutil.ignore_patterns(".git", "__pycache__", "*.zip"))
    text = service_text(account)
    out = {"account": account, "repo": slug, "permission_mode": mode, "cloud": cloud, "local": local, "service": service_file()}
    if warning:
        out["warning"] = warning
    if args.dry_run:
        out["service_text"] = text
    else:
        start_service(text)
        bus = Bus(remote, account)
        doc = {"account": account, "at": now_iso(), "cloud": cloud, "local": local, "routine": routine, "version": VERSION}
        announce_listener(bus, account, doc, proved=bool(args.routine_id))
    print(json.dumps(out, indent=2))


def listen_uninstall(args):
    config = load_config()
    if args.remote or args.repo:
        slug = args.repo or repo_slug(args.remote)
        config.get("repos", {}).pop(slug, None)
        save_config(config)
    if not config.get("repos") or args.all:
        stop_service()
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
    settings = bus.doc("settings.json")
    code = re.sub(r"[^A-Za-z0-9-]+", "-", args.code or slug.split("/")[-1]).strip("-").upper()
    today = datetime.datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else datetime.date.today()
    run, plan_file = run_names(code, today, set(list_runs(bus)))
    max_accounts = args.max_accounts or settings.get("max_accounts") or DEFAULT_MAX_ACCOUNTS
    draft = args.dir or tempfile.mkdtemp(prefix="fb-draft-")
    job = {"run": run, "repo": slug, "code": code.upper(), "stamp": today.strftime("%m-%d-%y"), "kicked_off": now_iso(), "goal": args.goal,
           "accounts": args.accounts or 0, "max_accounts": max_accounts, "where": args.where or "cloud", "autopilot": args.autopilot or "full",
           "plan_file": plan_file, "deadline": args.deadline, "base": args.base or default_branch(), "created_by": bus.account, "lane_model": args.lane_model, "sizing": "",
           "min_version": VERSION, "lane_cap": args.lane_cap or DEFAULT_LANE_CAP}
    write_file(os.path.join(draft, "job.json"), dumps(job))
    out({"draft": draft, "run": run, "plan_file": plan_file, "max_accounts": max_accounts, "where": job["where"], "autopilot": job["autopilot"]})


def cmd_size(args):
    root = args.root or os.getcwd()
    measured = None if args.rate else throughput(root)
    rate = args.rate or measured or DEFAULT_RATE_PER_DAY
    bus = get_bus(args)
    bus.sync()
    max_accounts = args.max_accounts or bus.doc("settings.json").get("max_accounts") or DEFAULT_MAX_ACCOUNTS
    result = size(args.items, args.hours, max_accounts, rate)
    result.update(rate_per_account_day=rate, rate_source="--rate" if args.rate else ("measured: merged PRs a day, last 14 days" if measured else "assumed default, no history"), max_accounts=max_accounts)
    result["listeners"] = [n[:-5] for n in bus.listdir("listeners") if age_minutes(bus.doc("listeners/" + n).get("at")) < 90]
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
                 prefix + "control.json": dumps({"state": "running", "reason": "kickoff", "by": bus.account, "at": now_iso()}),
                 prefix + "desk.json": dumps({"account": bus.account, "computer": socket.gethostname(), "since": now_iso()})}
        for name in os.listdir(os.path.join(args.draft, "lanes")):
            files[prefix + "lanes/" + name] = read_text(os.path.join(args.draft, "lanes", name))
        files.update(kit_files())
        settings = bus.doc("settings.json") or {"format": FORMAT}
        settings["max_accounts"] = job["max_accounts"]
        files["settings.json"] = dumps(settings)
        return files

    bus.commit(mutate, "kickoff: %s" % run)
    bus.sync()
    out({"ok": True, "run": run, "bus": BUS_BRANCH, "plan": "runs/%s/%s" % (run, job["plan_file"]), "lanes": lane_names(bus, run), "facts": facts,
         "listeners": [n[:-5] for n in bus.listdir("listeners")]})


def cmd_join(args):
    if windows_problem():
        raise FbError(windows_problem())
    bus = get_bus(args)
    account = account_name(args)
    bus.account = account
    if not bus.sync():
        raise FbError("no bus on this repo yet")
    if args.lane in (None, "any", "<any open lane>") and out_of_usage(bus, account):
        raise FbError("%s ran out of usage at %s, so it is offered no lanes: name one with --lane, or have a person run resume" % (account, out_of_usage(bus, account)))
    run = pick_run(bus, argparse.Namespace(run=None if args.run in (None, "any") else args.run))
    if too_old(bus, run):
        raise FbError("run %s needs fast-builder %s or later, and this is %s: update the plugin first" % (run, re.sub(r"[^0-9.]", "", str(bus.doc("runs/%s/job.json" % run).get("min_version")))[:20], VERSION))
    lane = None if args.lane in (None, "any", "<any open lane>") else args.lane
    if lane and not NAME_RE.match(lane):
        raise FbError("bad lane name")
    token = uuid.uuid4().hex
    lane = claim_lane(bus, run, account, lane, allow_second=args.second, token=token)
    bus.sync()
    claim = bus.doc("runs/%s/claims/%s.json" % (run, lane))
    if claim.get("account") != account or claim.get("session") != token:
        raise Taken("lane %s is held by %s" % (lane, claim.get("account")))
    job = bus.doc("runs/%s/job.json" % run)
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


def is_steward(bus, run, lane):
    return any(l["name"] == lane and l.get("steward") for l in lanes_of(bus, run)["lanes"])


def answers_conductor(bus, session, holder):
    """Whether this lane reads the conductor's inbox: the lease holder's does, or the steward's when the holder has no live
    lane in this run. A listener holds the lease as long as it runs, even on an account with no usage left or whose lane
    is done, and no session of its own would ever read a request. If both read, the first to mark it read has it."""
    if holder == session["account"]:
        return True
    return is_steward(bus, session["run"], session["lane"]) and not any(r["account"] == holder and lane_is_live(bus, session["run"], r["lane"]) for r in lane_table(bus, session["run"]))


def keep_lost_work(bus, session, path):
    """A lane that lost its claim pushes what it has before it stops, so the account now on the lane can start from it
    rather than rebuild it: its own unpushed branches, and a snapshot of the rest of its own files, as when an account runs
    out. It costs no tokens, runs once per session, and says what it did in the lane's handoff. Returns what it saved."""
    run, lane, account = session["run"], session["lane"], session["account"]
    if session.get("lost_saved") or bus.doc("runs/%s/claims/%s.json" % (run, lane)).get("account") == account:
        return None  # a later session of this same account has it, in this same clone, with everything here
    session["lost_saved"] = True
    save_session(path, session)
    bus.release()  # a large push should not keep this account's other lanes off the bus
    try:
        saved = save_snapshot(session, path, read_json(os.path.join(os.path.dirname(path), "lanes.json")), why="another account took the lane")
    except Exception as exc:  # whatever is wrong with the lane's files, it still has to stop
        saved = {"failed": str(exc)[:200]}
    if not any(saved.get(k) for k in ("branch", "pushed", "held", "left", "failed")):
        return saved

    def mutate():
        handoff = "runs/%s/handoffs/%s.md" % (run, lane)
        files = {handoff: (bus.read(handoff) or "") + snapshot_note(account, saved, "%s lost this lane at %s" % (account, now_iso()))}
        if bus.doc("runs/%s/claims/%s.json" % (run, lane)).get("account") not in (None, account) and (saved.get("branch") or saved.get("pushed")):
            add_message(files, run, account, lane, "note", "%s left unfinished work on this lane when it lost it. The lane's handoff says where." % account)
        return files
    try:
        bus.commit(mutate, "handoff(%s): %s lost %s" % (run, account, lane))
    except FbError:
        pass  # what was pushed stays pushed: only the note did not land
    return saved


def cmd_tick(args):
    session, path, bus = need_session()
    bus.sync()
    result = build_tick(bus, session)
    remember_control(path, result["action"])
    if result["action"] == LOST:
        saved = keep_lost_work(bus, session, path)
        if saved:
            result["saved"] = {k: saved.get(k) for k in ("branch", "pushed", "held", "failed") if saved.get(k)}
    if result["action"] in (GO, DRAIN, PAUSE) and (time.time() - session.get("last_beat", 0) >= BEAT_SECONDS or args.note):
        # Before conducting: a lane's own quiet spell must not get it freed by its own tick.
        session["note"] = args.note or session.get("note", "")
        write_beat(bus, session, beat_status(bus, session), session["note"])
        session["last_beat"] = time.time()
    if result["action"] in (GO, DRAIN):
        report = conduct(bus, session["run"], session["account"])
        result = build_tick(bus, session)  # gh can hold a pass for a minute, and a stop may have landed. This is the checkout conduct just synced: no network
        remember_control(path, result["action"])
        waiting = conductor_requests(bus, session["run"], session.get("conductor_seen", []))
        result["conductor"] = report["holder"]
        result["requests_for_conductor"] = len(waiting) if answers_conductor(bus, session, report["holder"]) else 0
        result["freed"] = report["freed"]
    result["main"] = main_view(bus, session["run"])  # after conducting: it may just have learned this
    if result["main"]["state"] == "red" and result["action"] == GO:
        result["instruction"] = (MAIN_RED_STEWARD if is_steward(bus, session["run"], session["lane"]) else MAIN_RED_OTHER) % result["main"]["sha"][:7]
    if result["inbox"]:
        session["seen"] = (session.get("seen", []) + [m["id"] for m in result["inbox"]])[-1000:]
    session["stop_blocks"] = 0
    save_session(path, session)
    out(result)
    sys.exit(EXIT_CODES[result["action"]] if args.exit_code else 0)


def beat_status(bus, session):
    return beat_of(bus, session["run"], session["lane"]).get("status", "working")


def write_beat(bus, session, status, note, attempts=10):
    """Write the lane's heartbeat, but only while this session still holds the lane: a session that lost it must not
    be able to mark the new holder's lane done."""
    run, lane = session["run"], session["lane"]
    doc = {"at": now_iso(), "status": status, "account": session["account"], "note": str(note)}

    def mutate():
        if not holds_claim(bus.doc("runs/%s/claims/%s.json" % (run, lane)), session):
            raise Lost("this session no longer holds lane %s" % lane)
        return {"runs/%s/beats/%s.json" % (run, lane): dumps(doc)}

    bus.commit(mutate, "beat(%s): %s %s" % (run, lane, status), attempts=attempts)


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
    saved = None
    try:
        if bus.sync() and build_tick(bus, session)["action"] == LOST:  # a lead told by its guard may come straight here
            saved = keep_lost_work(bus, session, path)
    except FbError:
        pass  # leaving must always work
    leave_session(path)
    result = {"left": session["lane"], "run": session["run"]}
    if saved:
        result["saved"] = {k: saved.get(k) for k in ("branch", "pushed", "held", "failed") if saved.get(k)}
    out(result)


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
            if time.time() - last_beat >= BEAT_SECONDS:  # first: a wake below returns, and a lane that keeps waking must still beat
                try:
                    write_beat(bus, session, beat_status_fresh(bus, session), session.get("note", ""), attempts=3)
                    last_beat = session["last_beat"] = time.time()
                    save_session(path, session)
                except Lost:
                    raise
                except Exception:  # its own try: a beat that cannot land must not cost a stop or a message their wake
                    last_beat = time.time() - BEAT_SECONDS + BEAT_RETRY_SECONDS
            sha = bus.head()
            if sha != seen:
                bus.sync()
                seen = sha  # only once synced: a busy checkout means this change is looked at again next pass
                tick = build_tick(bus, session)
                remember_control(path, tick["action"])
                if tick["inbox"] or tick["action"] not in (GO, PAUSE):
                    out({"woke": "bus", "action": tick["action"], "messages": len(tick["inbox"])})
                    return
                if tick["action"] == GO and args.until_resumed:
                    out({"woke": "resumed", "action": GO})
                    return
        except Lost:
            keep_lost_work(bus, session, path)
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
    run = pick_run(bus, args)
    seen = set((session or {}).get("conductor_seen", [])) | set(requests_doc(bus, run)["read"]) if args.who == "conductor" else set()
    got = messages_for(bus, run, args.who, seen)
    if args.mark and args.who == "conductor" and got:
        if session:
            session["conductor_seen"] = (session.get("conductor_seen", []) + [m["id"] for m in got])[-1000:]
            save_session(path, session)
        mark_requests_read(bus, run, [m["id"] for m in got])
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
        back_in = sorted(n[9:-5] for n in accounts_back_in(bus)) if state == "running" else []
        if back_in:  # about accounts, not any one run, so it holds whichever run this goes on to pick or fails to
            bus.commit(lambda: accounts_back_in(bus), "resume: accounts back in")
        cool = [r for r in list_runs(bus) if has_cooled_lane(bus, r)] if state == "running" and (args.all or not (args.run or load_session()[0])) else []
        if args.all and state == "running":
            runs = [r for r in list_runs(bus) if r in cool or bus.exists("runs/%s/STOP" % r) or bus.doc("runs/%s/control.json" % r).get("state") in ("paused", "draining", "stopping")]
        elif args.all:
            runs = [r for r in list_runs(bus) if run_state(bus, r)["state"] not in ("finished", "stopped")]
        elif state == "running" and not args.run and not load_session()[0]:
            runs = [r for r in list_runs(bus) if r in cool or bus.doc("runs/%s/control.json" % r).get("state", "running") in TRANSITIONS["running"]]
            if len(runs) != 1 and (runs or not back_in):
                raise FbError("%d runs could resume: pass --run or --all (%s)" % (len(runs), ", ".join(runs)))
        else:
            runs = [pick_run(bus, args)]
        moved = [r for r in runs if set_control(bus, r, state, args.reason or reason_default, bus.account, cool_only=r in cool)]
        cleared = False
        if state == "running" and args.all:
            cleared = bus.commit(lambda: {"STOP": None}, "resume: clear the stop on every run")
        result = {"state": state, "runs": moved, "unchanged": [r for r in runs if r not in moved]}
        if back_in:
            result["accounts_back_in"] = back_in
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
        job = bus.doc("runs/%s/job.json" % run)
        report.append({"run": run, "goal": job.get("goal"), "where": job.get("where"), "autopilot": job.get("autopilot"), "state": run_state(bus, run)["state"],
                       "main": main_view(bus, run), "main_session": main_session(bus, run), "lanes": lane_table(bus, run), "conductor": bus.doc("runs/%s/conductor.json" % run).get("holder"),
                       "needs_human": [m["body"] for m in messages_for(bus, run, "human") if m.get("kind") == "needs-human"]})
    dry = [{"account": n[:-5], "at": out_of_usage(bus, n[:-5])} for n in bus.listdir("accounts") if n.endswith(".json") and out_of_usage(bus, n[:-5])]
    out({"runs": report, "out_of_usage": dry, "listeners": [dict(bus.doc("listeners/" + n), stale=age_minutes(bus.doc("listeners/" + n).get("at")) > 90) for n in bus.listdir("listeners")]})


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
        print("=== lane %s: paste into a new %s session on the account that takes it ===\n" % (row["lane"], bus.doc("runs/%s/job.json" % run).get("where", "cloud")))
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
        settings = bus.doc("settings.json") or {"format": FORMAT}
        if args.max_accounts:
            settings["max_accounts"] = args.max_accounts
        return {"settings.json": dumps(settings)}

    if args.max_accounts:
        bus.commit(mutate, "settings: max_accounts %s" % args.max_accounts)
    bus.sync()
    out(bus.json("settings.json"))


def cmd_listen(args):
    if sys.stdout is None:  # pythonw has nowhere to print, and launchd and systemd are told where this goes
        os.makedirs(HOME_DIR, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(HOME_DIR, "listener.log"), "a", buffering=1, encoding="utf-8")
    listen_loop(args)


def cmd_poll(args):
    """One listener pass, for a routine that has no daemon: claims a lane and prints what to do, spending nothing else."""
    bus = get_bus(args)
    account = account_name(args)
    bus.account = account
    if not bus.sync():
        print("NOTHING")
        return
    asked = {}
    for run in active_runs(bus):
        report = conduct(bus, run, account, asked=asked)
        if run_state(bus, run)["action"] != GO:
            continue
        lane = pick_lane(bus, run, account, skip=report["freed"])
        if lane and claim_picked(bus, run, lane, account):
            print("LANE run=%s lane=%s\n" % (run, lane))
            print(lane_prompt(run, lane, account))
            return
    print("NOTHING")


def hook_command(script, name):
    """python3 on a Windows PC is missing, or the Store's stub that only prints a hint, and a hook that cannot start blocks
    nothing. Claude Code runs hooks there in Git Bash, which sets OSTYPE. With no Git Bash they run in PowerShell, which
    cannot parse this and would refuse every tool in every session, not just a lane's, unless onFailure says to continue.
    join and the listener refuse to start a lane on such a computer."""
    return 'P=python3; case "$OSTYPE" in msys*|cygwin*) P=${FB_PYTHON:-$(command -v py || echo python)};; esac; "$P" "%s" hook %s' % (script, name)


def looks_secret(path):
    return bool(SECRET_RE.search(path) or (SECRET_NAME_RE.search(path) and not SOURCE_RE.search(path)))


def push_lane_branches(snap, session, lanes):
    """Push this lane's own branches, fb/<run>/<lane>/..., that have commits no remote has: a subagent's slice in its
    worktree is one, and is exactly what a replacement would otherwise rebuild. Under their own names and never forced, so
    one that moved on the remote is left alone. One whose commits touch another lane's files or look like secrets stays here."""
    prefix = "fb/%s/%s/" % (session["run"], session["lane"])
    pushed, held = [], []
    for row in snap(["for-each-ref", "--format=%(refname:short) %(upstream:track)", "refs/heads/" + prefix]).split("\n"):
        branch, _, track = row.partition(" ")
        if not branch.startswith(prefix) or not BRANCH_RE.match(branch) or "gone" in track:  # gone: merged and deleted, most likely
            continue
        if snap(["rev-list", "--count", branch, "--not", "--remotes"]) == "0":
            continue
        try:
            main = "origin/" + session.get("base", "main")
            base = snap(["merge-base", branch, main])
            mine = [n for n in snap(["log", "--format=", "--name-only", "-z", "--no-renames", "--no-merges", "%s..%s" % (base, branch)]).split("\0") if n.strip()]
            same = run_cmd(["git", "-c", "safe.directory=*", "diff", "--quiet", main, branch, "--"] + mine, cwd=snap.top, check=False, timeout=15, env=dict(os.environ, GIT_LITERAL_PATHSPECS="1"))
            if mine and same.returncode == 0:
                continue  # its files already read so on the base: squash-merged, and pushing it again would bring it back
            every = [n for n in snap(["log", "--format=", "--name-only", "-z", "--no-renames", "--diff-merges=first-parent", "%s..%s" % (base, branch)]).split("\0") if n.strip()]
            if any(looks_secret(n) for n in every) or not lanes or any(not may_touch(n, session["lane"], lanes) for n in mine):
                held.append(branch)
                continue
            snap(["push", "-q", "--no-verify", "origin", "refs/heads/%s:refs/heads/%s" % (branch, branch)], timeout=60)
            pushed.append(branch)
        except FbError:
            held.append(branch)
    return pushed, held


def save_snapshot(session, session_file, lanes, why="its account ran out of usage"):
    """Put this lane's unfinished work on a branch of its own, for a replacement on another account who cannot read this
    clone. Returns what happened, for the handoff. It spends no tokens and leaves the clone as it was: the snapshot is built
    in a throwaway index, and status reads the real one, so a file hidden with --skip-worktree stays hidden. Only this lane's
    own files go, since it is unreviewed, and each firing gets a new branch, so nothing can overwrite an earlier save.
    Names go to update-index, which takes them literally: a file called * must not read as a pattern. The lane's own
    branches with unpushed commits go up first, under their own names."""
    git_dir = os.path.dirname(os.path.dirname(session_file))
    top = os.path.dirname(git_dir)
    plain = dict(os.environ, GIT_CONFIG_COUNT="0")  # this clone's own line endings, or every file would read as changed

    def snap(args, timeout=15, env=plain, **kw):
        return run_cmd(["git", "-c", "safe.directory=*", "-c", "core.longpaths=true", "--no-optional-locks"] + args, cwd=top, env=env, timeout=timeout, **kw).stdout.rstrip("\n")

    snap.top = top
    pushed, held = push_lane_branches(snap, session, lanes)
    head = snap(["rev-parse", "--verify", "HEAD"])
    saved, left = [], []
    for entry in snap(["status", "--porcelain", "-z", "--no-renames", "--untracked-files=all"], timeout=45).split("\0"):
        name = entry[3:]
        if not name:
            continue
        full = os.path.join(top, name)
        apart = os.path.isdir(full) and not os.path.islink(full)  # a nested repo or a submodule: only a pointer could go up
        too_big = os.path.isfile(full) and os.path.getsize(full) > WIP_MAX_BYTES
        if apart or name.endswith("/") or "�" in name or too_big or looks_secret(name) or not (lanes and may_touch(name, session["lane"], lanes)):
            left.append(name)
        else:
            saved.append(name)
    notes = []
    if snap(["rev-list", "--count", "--branches", "--not", "--remotes", head]) != "0":
        notes.append("Other local branches there have commits that were never pushed.")
    if snap(["worktree", "list", "--porcelain"]).count("worktree ") > 1:
        notes.append("It has other worktrees, such as a subagent's, and none of them was looked at.")
    if any(os.path.exists(os.path.join(git_dir, marker)) for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD")):
        notes.append("It was in the middle of a rebase or merge, so this is not all of that work, and it may hold conflict markers.")
    result = {"branch": None, "left": left, "notes": notes, "pushed": pushed, "held": held}
    pushed = bool(snap(["branch", "-r", "--contains", head]))
    if pushed and not saved:
        return result
    parent, suspect = head, []
    if not pushed:  # every commit, not the net change: a secret added and then removed is still in the history
        try:
            base = snap(["merge-base", head, "origin/" + session.get("base", "main")])
        except FbError:
            raise FbError("its branch shares no history with origin/%s, so its commits could not be checked for secrets" % session.get("base", "main"))
        suspect = sorted(set(n for n in snap(["log", "--format=", "--name-only", "-z", "--no-renames", "--diff-merges=first-parent", "%s..%s" % (base, head)]).split("\0") if n.strip() and looks_secret(n.strip())))
        if suspect:  # so the history cannot go, but the work can
            parent = base
            result["left"] = left + suspect
            notes.append("Its unpushed commits touch files that look like secrets, so they did not go up as they are: anything on a snapshot branch is squashed into one commit without those files.")
    folder = tempfile.mkdtemp(prefix="fb-wip-")
    try:
        env = dict(plain, GIT_INDEX_FILE=os.path.join(folder, "index"))
        snap(["read-tree", head], env=env)
        if suspect:
            snap(["reset", "-q", base, "--pathspec-from-file=-", "--pathspec-file-nul"], env=dict(env, GIT_LITERAL_PATHSPECS="1"), stdin_text="\0".join(suspect))
        if saved:
            snap(["update-index", "--add", "--remove", "--replace", "-z", "--stdin"], timeout=45, env=env, stdin_text="\0".join(saved) + "\0")
        tree = snap(["write-tree"], env=env)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    sha = parent
    if tree != snap(["rev-parse", parent + "^{tree}"]):
        who = ["-c", "user.name=fast-builder", "-c", "user.email=fast-builder@noreply.local"]
        sha = snap(who + ["commit-tree", tree, "-p", parent, "-m", "wip: %s stopped when %s" % (session["lane"], why)])
    elif pushed or suspect:
        return result  # staged and then undone, or commits of nothing but secrets: nothing here that the remote lacks
    branch = "fast-builder/wip/%s/%s/%s-%s" % (session["run"], session["lane"], session["account"], utcnow().strftime("%Y%m%dT%H%M%SZ"))
    snap(["push", "-q", "--no-verify", "origin", "%s:refs/heads/%s" % (sha, branch)], timeout=45)  # never --force
    result["branch"] = branch
    return result


def snapshot_note(account, saved, heading=None):
    """The part of a handoff that says what a replacement has to go on, and exactly what it does not have."""
    lines = ["", "", "## %s" % (heading or "%s ran out of usage at %s" % (account, now_iso())), ""]
    if saved.get("pushed"):
        lines.append("Its lane branches that no remote had are pushed now, under their own names: %s." % ", ".join("`%s`" % b for b in saved["pushed"][:WIP_NAMES_SHOWN]))
    if saved.get("held"):
        lines.append("Its lane branches still only on that computer, because their commits touch another lane's files or files that look like secrets, share no history with the base, or would not push: %s." % ", ".join("`%s`" % b for b in saved["held"][:WIP_NAMES_SHOWN]))
    if saved.get("failed"):
        lines.append("Its unfinished work could not be pushed: %s. Anything uncommitted or unpushed is still in its clone." % saved["failed"])
    elif saved.get("branch"):
        lines.append("Its work, limited to this lane's files, is on `%s`. Nobody has reviewed it, and it is a snapshot of whatever was there, so check it builds before you trust it. Start from it with `git fetch origin %s && git switch -c LANE-BRANCH FETCH_HEAD`." % (saved["branch"], saved["branch"]))
    else:
        lines.append("There is no snapshot branch, because nothing could be saved beyond what is already pushed.")
    if saved.get("left"):
        names = [re.sub(r"[^\w./ -]", "?", name)[:120] for name in saved["left"][:WIP_NAMES_SHOWN]]
        more = len(saved["left"]) - len(names)
        lines.append("Not saved, so still only on that computer, because they are another lane's, look like secrets, are very large or are separate repos: %s%s." % (", ".join(names), " and %d more" % more if more > 0 else ""))
    lines += saved.get("notes", [])
    return "\n".join(lines) + "\n"


def already_told(bus, run, lane, account):
    """Whether the handoff already covers this account's latest loss of this lane. By the loss and not by any old note: an
    account can get a lane back, and leave new work the second time."""
    doc = bus.doc("runs/%s/freed/%s.json" % (run, lane))
    told, lost = doc.get("told"), doc.get("lost")
    return isinstance(told, dict) and account in told and told[account] == (lost.get(account) if isinstance(lost, dict) else None)


def hook_stop_failure(payload):
    """StopFailure: this account has run out, so a replacement should not have to wait out the silence. It runs with no
    tokens, which is why it can do this after the lane no longer can. If it fails, going quiet still frees the lane. A
    session file outlives its run and any session can sit in this clone, so nothing happens unless the bus agrees this
    clone's session holds the lane, and this is the session the guard first saw there. A subagent's failure is not its
    lead's."""
    if payload.get("error") not in OUT_OF_USAGE_ERRORS or payload.get("agent_id"):
        return
    session, session_file = load_session(payload.get("cwd") or os.getcwd())
    if not session or session.get("claude_session") not in (None, payload.get("session_id")):
        return
    run, lane, account = session["run"], session["lane"], session["account"]
    bus = Bus(session["remote"], account)
    bus.lock_seconds = STOP_FAILURE_LOCK_SECONDS  # a listener's pass holds it a while, and nothing else will free this lane soon
    if not bus.sync() or run_state(bus, run)["action"] == FINISHED:
        return
    claim, beat = bus.doc("runs/%s/claims/%s.json" % (run, lane)), beat_of(bus, run, lane)
    mine = holds_claim(claim, session)
    if not mine and (claim.get("account") == account or already_told(bus, run, lane, account)):
        return  # a session this account has moved past, or the same news about this lane a second time
    lost = bus.doc("runs/%s/freed/%s.json" % (run, lane)).get("lost")
    if not mine and not (beat.get("note") == "freed: %s went quiet" % account or (isinstance(lost, dict) and account in lost)):
        return
    saved = None
    if not mine or beat.get("status") in LIVE_BEATS:
        bus.release()  # a large push should not keep this account's other lanes off the bus
        try:
            saved = save_snapshot(session, session_file, read_json(os.path.join(os.path.dirname(session_file), "lanes.json")))
        except Exception as exc:  # whatever is wrong with the lane's files, it still has to move on, and lanes.json came off the bus
            saved = {"failed": str(exc)[:200]}

    # The reset time, if the event carries the CLI's words. Not the model's: its last message may quote some other limit.
    limit = usage_limit("\n".join(v for k, v in payload.items() if isinstance(v, str) and k not in ("last_assistant_message", "cwd", "transcript_path")))

    def mutate():
        files = dry_files(bus, account, limit)
        now_claim, now_beat = bus.doc("runs/%s/claims/%s.json" % (run, lane)), beat_of(bus, run, lane)
        freeing = holds_claim(now_claim, session) and now_beat.get("status") in LIVE_BEATS
        if freeing:
            reason = "freed: %s ran out of usage" % account
            files.update(lost_lane(bus, run, lane, account))
            files["runs/%s/claims/%s.json" % (run, lane)] = None
            files["runs/%s/beats/%s.json" % (run, lane)] = dumps({"at": now_iso(), "status": "open", "account": None, "note": reason})
            add_message(files, run, account, "all", "note", "Lane %s %s." % (lane, reason))
        first_word = not out_of_usage(bus, account)
        if saved is not None and (freeing or not already_told(bus, run, lane, account)):  # the plugin's hook and a repo's can both fire
            handoff = "runs/%s/handoffs/%s.md" % (run, lane)
            files[handoff] = (bus.read(handoff) or "") + snapshot_note(account, saved)
            rel = "runs/%s/freed/%s.json" % (run, lane)
            doc = json.loads(files[rel]) if rel in files else bus.doc(rel)
            told = doc.get("told") if isinstance(doc.get("told"), dict) else {}
            lost = doc.get("lost") if isinstance(doc.get("lost"), dict) else {}
            files[rel] = dumps(dict(doc, told=dict(told, **{account: lost.get(account)})))
            if now_claim.get("account") not in (None, account):
                add_message(files, run, account, lane, "note", "%s left unfinished work on this lane when it ran out of usage. The lane's handoff says where." % account)
        if main_session(bus, run)["account"] == account and first_word:
            add_message(files, run, account, "human", "needs-human", "%s ran out of usage, and it holds the main session of %s. Continue from another account with status and the plan on the bus." % (account, run))
        return files
    bus.commit(mutate, "out of usage(%s): %s" % (run, account))


def cmd_hook(args):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    try:
        {"guard": hook_guard, "stop": hook_stop, "session-start": hook_session_start, "stop-failure": hook_stop_failure}[args.event](payload)
    except Exception:  # a hook failing must never take a working session down with it
        pass


def cmd_routine_check(args):
    """Prove this OS user's headless claude can reach the routine, which is what a daemon's wake depends on."""
    status = ask_routine_api(args.routine_id, "get")
    out({"routine": args.routine_id, "reachable": status == "200", "status": status, "help": "" if status == "200" else ROUTINE_HELP.lstrip(": ")})
    if status != "200":
        sys.exit(1)


def cmd_switch(args):
    """Point this OS user's listener at the account its claude login now belongs to, after a swap. Names one, or with no
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

    def wire(event, matcher, name, timeout=None):
        command = hook_command("$CLAUDE_PROJECT_DIR/.claude/fast-builder/fb.py", name)
        rows = hooks.setdefault(event, [])
        older = 'python3 "$CLAUDE_PROJECT_DIR/.claude/fast-builder/fb.py" hook %s' % name
        for row in rows:
            row["hooks"] = [h for h in row.get("hooks", []) if h.get("command") != older]
        rows[:] = [row for row in rows if row["hooks"]]
        if not any(h.get("command") == command for row in rows for h in row.get("hooks", [])):
            entry = {"hooks": [dict({"type": "command", "command": command, "onFailure": "continue"}, **({"timeout": timeout} if timeout else {}))]}  # see hooks.json
            if matcher:
                entry["matcher"] = matcher
            rows.append(entry)

    wire("PreToolUse", GUARD_MATCHER, "guard")
    wire("Stop", None, "stop")
    wire("StopFailure", STOP_FAILURE_MATCHER, "stop-failure", 300)
    write_file(settings_path, dumps(settings))
    written = [".claude/fast-builder/fb.py", ".claude/settings.json"]
    for path in sorted(globlib.glob(os.path.join(HERE, "..", "agents", "*.md"))):
        name = "fb-" + os.path.basename(path)
        shutil.copyfile(path, os.path.join(root, ".claude", "agents", name)) if os.path.isdir(os.path.join(root, ".claude", "agents")) else (os.makedirs(os.path.join(root, ".claude", "agents")), shutil.copyfile(path, os.path.join(root, ".claude", "agents", name)))
        written.append(".claude/agents/" + name)
    out({"written": written})


def cmd_doctor(args):
    checks = {"git": bool(shutil.which("git")), "python": sys.version.split()[0], "claude": shutil.which(CLAUDE), "gh": bool(shutil.which("gh")), "account": load_config().get("account"), "platform": sys.platform, "listener_installed": os.path.exists(service_file())}
    if WINDOWS:
        checks["git_bash"] = git_bash()
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
    p.add_argument("--lane-cap", type=int, choices=range(1, 21), metavar="N", help="most lanes one account may hold (default %d)" % DEFAULT_LANE_CAP)
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
    p.add_argument("--permission-mode", choices=list(KEPT_MODES) + [BYPASS], help="default: what this repo has, else auto. %s is never kept: it has to be given each time" % BYPASS)
    for name in ("cloud", "local"):  # default: what this repo has, else on
        pair = p.add_mutually_exclusive_group()
        pair.add_argument("--" + name, dest=name, action="store_true", default=None)
        pair.add_argument("--no-" + name, dest=name, action="store_false", default=None)
    p.add_argument("--routine-id", help="this account's listener routine, so the daemon can start cloud lanes")
    p.add_argument("--max-local-lanes", type=int, metavar="N", help="most local lanes this OS user runs at once, across repos (default %d, 0 for no limit)" % DEFAULT_MAX_LOCAL_LANES)
    p.add_argument("--max-load-per-cpu", type=float, metavar="X", help="start no local lane while the 5-minute load is over X per core (default %s, 0 for no limit)" % LOAD_PER_CPU)
    p.add_argument("--dry-run", action="store_true")

    p = add("listen-uninstall", listen_uninstall, help="remove the listener")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("hook", help="called by the plugin's hooks")
    p.set_defaults(func=cmd_hook)
    p.add_argument("event", choices=["guard", "stop", "session-start", "stop-failure"])

    p = add("routine-check", cmd_routine_check, help="prove the daemon can reach a routine")
    p.add_argument("--routine-id", required=True)

    add("install-repo", cmd_install_repo, help="write the repo-side hooks that cloud sessions need")
    p = add("switch", cmd_switch, help="point the listener at this OS user's current claude login")
    p.add_argument("name", nargs="?", help="the account to switch to (default: find it)")

    add("doctor", cmd_doctor, help="check this account's setup")
    return parser


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):  # Windows defaults to its old code page, and pythonw has none of them
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    try:
        args.func(args)
    except FbError as exc:
        print("fast-builder: %s" % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
