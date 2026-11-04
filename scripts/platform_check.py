#!/usr/bin/env python3
"""
Check fast-builder on this computer, end to end, without touching your own setup or any real repo.

Built for Windows (py -3 scripts\\platform_check.py) and Linux (python3 scripts/platform_check.py). It makes a throwaway
folder with a space in its path and a throwaway git remote, and uses a stand-in `claude` (a .cmd file on Windows, like the
npm install), so it spends no tokens. It drives this computer's real file locks, process handling and Git Bash.

--real-service also creates the real scheduled task or systemd service for a few seconds and removes it. It is skipped if
this computer already has a listener set up.

Results are printed and saved to fast-builder-check-results.txt in the current folder.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FB = os.path.join(HERE, "fb.py")
sys.path.insert(0, HERE)
WIN = os.name == "nt"
REAL_SERVICE = "--real-service" in sys.argv
LOG = []
FAILED = []

STAND_IN = r'''
import json, os, subprocess, sys, time
record = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(), "FB": os.environ.get("FB"), "account": os.environ.get("FB_ACCOUNT"), "pid": os.getpid()}
bash = os.environ.get("CHECK_BASH")
if bash:  # the lane's Bash tool, running what its brief says to run
    done = subprocess.run([bash, "-c", 'python3 "$FB" --version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    record["python3_says"] = done.stdout.strip()
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
record["grandchild"] = grandchild.pid
out = os.environ["CHECK_LOG"]
with open(out + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
os.replace(out + ".tmp", out)
time.sleep(300)
'''


def say(text=""):
    LOG.append(text)
    print(text.encode("ascii", "replace").decode(), flush=True)


def check(name, func):
    try:
        note = func()
        say("  ok    %s%s" % (name, " (%s)" % note if note else ""))
    except Exception as exc:
        FAILED.append(name)
        say("  FAIL  %s: %s" % (name, str(exc)[:600]))


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def run(args, cwd=None, env=None, stdin=None, check_exit=True, timeout=180):
    proc = subprocess.run(args, cwd=cwd, env=env, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", timeout=timeout)
    if check_exit and proc.returncode != 0:
        raise AssertionError("%s exited %d: %s %s" % (" ".join(str(a) for a in args[:5]), proc.returncode, proc.stdout[-300:], proc.stderr[-300:]))
    return proc


def git_bash():
    import fb
    return fb.git_bash() if WIN else shutil.which("bash")


def force_remove(path):
    def again(func, target, _):  # git keeps its objects read-only, which stops a delete on Windows
        os.chmod(target, stat.S_IWRITE)
        func(target)
    shutil.rmtree(path, onerror=again)


class World:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="fb check ")
        self.remote = os.path.join(self.root, "owner", "app.git").replace("\\", "/")
        self.bash = git_bash()
        self.log = os.path.join(self.root, "stand-in.json")
        os.makedirs(self.remote)
        run(["git", "init", "-q", "--bare", "-b", "main", self.remote])
        seed = os.path.join(self.root, "seed")
        run(["git", "clone", "-q", self.remote, seed])
        for rel in ("src/auth/login.ts", "src/pay/charge.ts", "README.md"):
            os.makedirs(os.path.dirname(os.path.join(seed, rel)) or seed, exist_ok=True)
            with open(os.path.join(seed, rel), "w") as handle:
                handle.write("x\n")
        run(["git", "add", "-A"], cwd=seed)
        run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], cwd=seed)
        run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed)
        script = os.path.join(self.root, "stand_in.py")
        with open(script, "w") as handle:
            handle.write(STAND_IN)
        if WIN:
            self.claude = os.path.join(self.root, "claude.cmd")
            with open(self.claude, "w") as handle:
                handle.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
        else:
            self.claude = os.path.join(self.root, "claude")
            with open(self.claude, "w") as handle:
                handle.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
            os.chmod(self.claude, 0o755)

    def env(self, account):
        home = os.path.join(self.root, "accounts", account)
        os.makedirs(home, exist_ok=True)
        return dict(os.environ, FB_HOME=os.path.join(home, "cfg"), FB_CACHE=os.path.join(home, "cache"), FB_ACCOUNT=account, FB_CLAUDE=self.claude,
                    CHECK_LOG=self.log, CHECK_BASH=self.bash or "", PYTHONPATH=HERE, PYTHONDONTWRITEBYTECODE="1")

    def clone(self, account):
        path = os.path.join(self.root, "accounts", account, "repo")
        if not os.path.isdir(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            run(["git", "clone", "-q", self.remote, path])
            run(["git", "config", "user.name", account], cwd=path)
            run(["git", "config", "user.email", "a@b"], cwd=path)
        return path

    def fb(self, account, *args, **kw):
        return run([sys.executable, FB] + list(args), cwd=self.clone(account), env=self.env(account), **kw)

    def publish(self, where):
        draft = os.path.join(self.root, "draft-" + where)
        info = json.loads(self.fb("alpha", "draft", "--goal", "Ship it", "--accounts", "2", "--where", where, "--autopilot", "full", "--code", "CHK", "--today", "2026-11-02", "--dir", draft).stdout)
        lanes = [{"name": "core", "owns": ["src/auth/**"], "modules": ["M01"], "digits": [1, 3, 5, 7, 0, 9], "steward": True},
                 {"name": "money", "owns": ["src/pay/**"], "modules": ["M02"], "digits": [2, 4, 6, 8]}]
        with open(os.path.join(draft, "lanes.json"), "w") as handle:
            json.dump({"lanes": lanes, "shared_ok": []}, handle)
        self.fb("alpha", "render", "--draft", draft)
        for folder, names in ((os.path.join(draft, "lanes"), os.listdir(os.path.join(draft, "lanes"))), (draft, [info["plan_file"]])):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(re.sub(r"\{\{[^}]*\}\}", "filled", text))
        result = json.loads(self.fb("alpha", "publish", "--draft", draft).stdout)
        expect(result.get("ok"), "publish said: %s" % result)
        return info["run"] if "run" in info else result["run"]


def tool_versions():
    found = re.search(r"(\d+)\.(\d+)", run(["git", "--version"]).stdout)
    version = (int(found.group(1)), int(found.group(2)))
    expect(version >= (2, 31), "git %d.%d is too old to keep bus files byte for byte: 2.31 or later is needed" % version)
    return "git %d.%d, Python %s" % (version[0], version[1], sys.version.split()[0])


def real_tools():
    missing = [name for name in ("claude", "gh") if not shutil.which(name)]
    expect(not missing, "not found on PATH: %s. The listener needs them for real lanes" % ", ".join(missing))
    return shutil.which("claude")


def hook_python():
    """What a real hook will start here, run the way Claude Code runs it, since a hook that cannot start blocks nothing."""
    import fb
    bash = git_bash()
    expect(bash, "Git Bash not found. Install Git for Windows: Claude Code runs hooks with it")
    done = run([bash, "-c", fb.hook_command(FB.replace("\\", "/"), "stop")], stdin="{}")
    return "exit %d" % done.returncode


def imports():
    import fb
    expect(fb.WINDOWS == WIN, "WINDOWS is %s" % fb.WINDOWS)
    return "fb.py %s" % fb.VERSION


def text_is_utf8(world):
    import fb
    path = os.path.join(world.root, "t.json")
    fb.write_file(path, '{"note": "caf\u00e9 \u2014 ok"}\n')
    fb.write_file(path, '{"note": "caf\u00e9 \u2014 ok"}\n')  # over a file that already exists
    with open(path, "rb") as handle:
        expect(handle.read() == '{"note": "caf\u00e9 \u2014 ok"}\n'.encode("utf-8"), "bytes changed on the way to disk")
    expect(fb.read_json(path)["note"] == "caf\u00e9 \u2014 ok", "did not read back")


def locks_exclude(world):
    env = world.env("locker")
    holder = subprocess.Popen([sys.executable, "-c", "import fb, time\nb = fb.Bus(%r, 'x')\nb.lock()\nprint('held', flush=True)\ntime.sleep(60)\n" % world.remote],
                              env=env, stdout=subprocess.PIPE, universal_newlines=True)
    try:
        expect(holder.stdout.readline().strip() == "held", "the first process did not get the lock")
        tryit = "import fb\nb = fb.Bus(%r, 'x')\nb.lock_seconds = 1\ntry:\n    b.lock()\n    print('got it')\n    b.release()\nexcept fb.FbError:\n    print('busy')\n" % world.remote
        expect(run([sys.executable, "-c", tryit], env=env).stdout.strip() == "busy", "a second process got a held lock")
    finally:
        holder.kill()
        holder.wait()
    expect(run([sys.executable, "-c", tryit], env=env).stdout.strip() == "got it", "the lock did not come back after its holder died")


def timeouts_hold():
    """A timeout has to end the wait even when a grandchild is left holding the pipes, or a hook outlives its limit."""
    import fb
    code = ("import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'], stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)\n"
            "time.sleep(40)\n")  # naming the streams is what makes Windows hand them down
    began = time.time()
    try:
        fb.run_cmd([sys.executable, "-c", code], timeout=2)
    except fb.FbError:
        pass
    took = time.time() - began
    expect(took < 15, "a 2 second timeout took %d seconds" % took)
    return "%.0f seconds" % took


def process_handling():
    import fb
    code = "import subprocess, sys, time\nc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\nprint(c.pid, flush=True)\ntime.sleep(120)\n"
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, universal_newlines=True, **fb.DETACHED)
    child = int(parent.stdout.readline())
    expect(fb.alive(parent.pid) and fb.alive(child), "a running process reads as dead")
    first = fb.process_start(parent.pid)
    expect(first, "no start time for a running process")
    time.sleep(1)
    expect(fb.alive(parent.pid), "asking about a process harmed it")
    expect(fb.process_start(parent.pid) == first, "start time changed between two readings")
    fb.end_tree(parent.pid)
    end = time.time() + 15
    while time.time() < end and (parent.poll() is None or fb.alive(child)):
        time.sleep(0.1)
    expect(parent.poll() is not None, "the parent outlived end_tree")
    expect(not fb.alive(child), "its child outlived end_tree")
    expect(not fb.alive(parent.pid), "a dead process reads as alive")
    expect(fb.process_start(parent.pid) is None, "a dead process still has a start time")
    expect(not fb.alive(4000000), "a pid that never existed reads as alive")


def guard(world, run_name):
    import fb
    bash = git_bash()
    expect(bash, "Git Bash not found")
    world.fb("alpha", "join", "--run", run_name, "--lane", "core")
    clone = world.clone("alpha")
    with open(os.path.join(HERE, "..", "hooks", "hooks.json")) as handle:
        command = json.load(handle)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    env = dict(world.env("alpha"), CLAUDE_PLUGIN_ROOT=os.path.dirname(HERE))

    def verdict(target):
        payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": target}, "cwd": clone}, ensure_ascii=False)
        done = run([bash, "-c", command], cwd=clone, env=env, stdin=payload)
        return "deny" if '"deny"' in done.stdout else "allow"

    sep = os.sep
    expect(verdict(os.path.join(clone, "src", "auth", "login.ts")) == "allow", "the lane was refused its own file")
    expect(verdict(os.path.join(clone, "src", "auth", "caf\u00e9.ts")) == "allow", "the lane was refused its own file with an accent")
    expect(verdict(os.path.join(clone, "src", "pay", "charge.ts")) == "deny", "another lane's file was allowed")
    expect(verdict(clone + sep + "src" + sep + "auth" + sep + ".." + sep + "pay" + sep + "x.ts") == "deny", "another lane's file was allowed through ..")
    expect(verdict(clone.replace("\\", "/") + "/src/pay/charge.ts") == "deny", "another lane's file was allowed with forward slashes")
    expect(verdict(os.path.join(clone, ".git", "config")) == "deny", "an edit inside .git was allowed")
    expect(verdict(os.path.join(clone, ".GIT", "config")) == "deny", "an edit inside .GIT was allowed")
    expect(fb.allowed_when_halted("Bash", 'py -3 "$FB" lane-state stopped'), "the launcher form of the kit is refused")


def lane_starts(world, run_name):
    world.fb("bravo", "listen-install", "--dry-run", "--remote", world.remote, "--account", "bravo", "--routine-id", "trig_0123456789abcdefghijklmn")
    env = world.env("bravo")
    runner = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")  # what the scheduled task runs: no console, no stdout
    if WIN and os.path.exists(runner):
        # With no streams of its own, as under Task Scheduler: pipes here would give it the stdout it does not have there.
        subprocess.run([runner, FB, "listen", "--once"], cwd=world.clone("bravo"), env=env, timeout=180, check=True)
    else:
        world.fb("bravo", "listen", "--once")
    end = time.time() + 60
    while time.time() < end and not os.path.exists(world.log):
        time.sleep(0.2)
    expect(os.path.exists(world.log), "the stand-in claude never started. See %s" % os.path.join(world.env("bravo")["FB_HOME"], "logs"))
    with open(world.log, encoding="utf-8") as handle:
        record = json.load(handle)
    world.lane = record
    expect(record["account"] == "bravo", "the lane got account %s" % record["account"])
    expect(record["FB"] and os.path.exists(record["FB"]), "the lane got no FB path: %s" % record["FB"])
    brief = record["stdin"] if WIN else " ".join(record["argv"])
    expect(brief.count("\n") > 3 and run_name in brief, "the lane's brief did not arrive whole")
    if WIN:
        expect(not any("\n" in arg for arg in record["argv"]), "the brief reached a command line")
    import fb
    expect(fb.alive(record["pid"]), "the lane did not outlive the listener pass that started it")
    says = record.get("python3_says")
    expect(says is not None or not WIN, "the lane had no Git Bash to try python3 in")
    if says is None:
        return "no bash here to try python3 in"
    expect(fb.VERSION in says, "python3 in the lane's Bash said: %s" % says)
    return "python3 in its Bash ran fb.py"


def lane_stops(world):
    import fb
    record = world.lane
    env = world.env("bravo")
    children = fb.read_json(os.path.join(env["FB_HOME"], "children.json"))
    expect(children and len(children) == 1, "children.json has %s" % children)
    key, child = list(children.items())[0]
    expect(child.get("started"), "no start time was recorded for the lane")
    code = "import fb, sys\nprint(fb.child_state(fb.read_json(fb.children_path())[sys.argv[1]]))\nfb.end_child(sys.argv[1], sys.argv[2])\n"
    done = run([sys.executable, "-c", code, key, world.remote], env=env)
    expect(done.stdout.strip() == "ours", "the lane was not recognised as ours: %s" % done.stdout.strip())
    end = time.time() + 15
    while time.time() < end and (fb.alive(record["pid"]) or fb.alive(record["grandchild"])):
        time.sleep(0.1)
    expect(not fb.alive(child["pid"]) and not fb.alive(record["pid"]), "the lane outlived end_child")
    expect(not fb.alive(record["grandchild"]), "what the lane started outlived end_child")


def hook_says(world, payload):
    with open(os.path.join(HERE, "..", "hooks", "hooks.json")) as handle:
        command = json.load(handle)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    clone = world.clone("alpha")
    for folder, _, names in os.walk(os.path.join(clone, ".git")):  # the guard caches for a few seconds
        if "control-cache.json" in names:
            os.remove(os.path.join(folder, "control-cache.json"))
    env = dict(world.env("alpha"), CLAUDE_PLUGIN_ROOT=os.path.dirname(HERE))
    done = run([git_bash(), "-c", command], cwd=clone, env=env, stdin=json.dumps(dict(payload, cwd=clone)))
    return "deny" if '"deny"' in done.stdout else "allow"


def stop_reaches_guard(world, run_name):
    bash_call = {"tool_name": "Bash", "tool_input": {"command": "touch x"}}
    shell_call = {"tool_name": "PowerShell", "tool_input": {"command": "Remove-Item x"}}
    expect(hook_says(world, bash_call) == "allow", "a running lane was refused a command")
    world.fb("alpha", "stop", "--run", run_name, "--reason", "check")
    expect(hook_says(world, bash_call) == "deny", "a stopped lane's Bash call went through")
    expect(hook_says(world, shell_call) == "deny", "a stopped lane's PowerShell call went through")
    expect(hook_says(world, {"tool_name": "Bash", "tool_input": {"command": "git status"}}) == "allow", "a stopped lane cannot even read")


def out_of_usage_releases(world, run_name):
    """Through the real hook: the run is stopped by now, which changes nothing here, and the lane's work has to reach a branch."""
    clone = world.clone("alpha")
    # A person's clone on Windows: autocrlf on and CRLF files, which fb.py's own git settings must not read as changes.
    own = dict(os.environ, GIT_CONFIG_COUNT="0")
    run(["git", "config", "core.autocrlf", "true"], cwd=clone, env=own)
    run(["git", "rm", "-rq", "--cached", "."], cwd=clone, env=own)
    run(["git", "reset", "-q", "--hard"], cwd=clone, env=own)
    with open(os.path.join(clone, "src", "auth", "unfinished.ts"), "w") as handle:
        handle.write("x\n")
    with open(os.path.join(clone, ".env"), "w") as handle:
        handle.write("SECRET\n")
    before = run(["git", "status", "--porcelain"], cwd=clone).stdout
    with open(os.path.join(HERE, "..", "hooks", "hooks.json")) as handle:
        command = json.load(handle)["hooks"]["StopFailure"][0]["hooks"][0]["command"]
    env = dict(world.env("alpha"), CLAUDE_PLUGIN_ROOT=os.path.dirname(HERE))
    run([git_bash(), "-c", command], cwd=clone, env=env, stdin=json.dumps({"hook_event_name": "StopFailure", "error": "rate_limit", "cwd": clone}))
    branches = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/fast-builder/wip/"], cwd=world.remote).stdout.split()
    expect(len(branches) == 1, "expected one snapshot branch, found %s" % branches)
    changed = run(["git", "diff", "--name-only", "main", branches[0]], cwd=world.remote).stdout.split()
    expect(changed == ["src/auth/unfinished.ts"], "the snapshot changed %s, and only the unfinished file should differ: line endings would do this" % changed[:6])
    expect(run(["git", "status", "--porcelain"], cwd=clone).stdout == before, "the clone was changed")
    claim = run(["git", "cat-file", "-e", "fast-builder/bus:runs/%s/claims/core.json" % run_name], cwd=world.remote, check_exit=False)
    expect(claim.returncode != 0, "the lane was not freed")
    bus = lambda rel: run(["git", "show", "fast-builder/bus:" + rel], cwd=world.remote, check_exit=False).stdout
    expect("out_of_usage" in bus("accounts/alpha.json"), "the account was not marked")
    expect(branches[0] in bus("runs/%s/handoffs/core.md" % run_name), "the handoff does not name the snapshot")
    said = world.fb("alpha", "join", "--run", run_name, check_exit=False)
    expect(said.returncode != 0 and "ran out of usage" in said.stderr, "an account out of usage was offered a lane: %s" % said.stdout[:200])


def unreadable_bus_stops_lanes():
    """A file name Windows cannot hold, on a bus another computer pushed. Only Windows refuses to check that out."""
    if not WIN:
        return "only Windows refuses these names"
    world = World()
    try:
        name = world.publish("local")
        world.fb("alpha", "join", "--run", name, "--lane", "core")
        probe = {"tool_name": "Bash", "tool_input": {"command": "touch x"}}
        expect(hook_says(world, probe) == "allow", "a running lane was refused a command")
        phone = os.path.join(world.root, "phone")
        run(["git", "clone", "-q", "-n", "-b", "fast-builder/bus", world.remote, phone])
        run(["git", "read-tree", "HEAD"], cwd=phone)
        blob = run(["git", "hash-object", "-w", "--stdin"], cwd=phone, stdin="x\n").stdout.strip()
        run(["git", "-c", "core.protectNTFS=false", "update-index", "--add", "--cacheinfo", "100644,%s,bad:name.json" % blob], cwd=phone)
        run(["git", "-c", "core.protectNTFS=false", "-c", "user.name=p", "-c", "user.email=p@p", "commit", "-qm", "bad"], cwd=phone)
        run(["git", "push", "-q", "origin", "HEAD:fast-builder/bus"], cwd=phone)
        expect(hook_says(world, probe) == "deny", "a lane that cannot read the bus carried on")
    finally:
        force_remove(world.root)


def bus_bytes(world, run_name):
    world.fb("alpha", "send", "--run", run_name, "--to", "money", "--kind", "note", "--body", "caf\u00e9 \u2014 ok")
    bus = "fast-builder/bus"
    names = run(["git", "ls-tree", "-r", "--name-only", bus], cwd=world.remote).stdout.split()
    for name in [n for n in names if "/msgs/" in n or n.endswith("job.json") or n == "kit/fb.py"]:
        data = subprocess.run(["git", "cat-file", "blob", "%s:%s" % (bus, name)], cwd=world.remote, stdout=subprocess.PIPE, check=True).stdout
        expect(b"\r" not in data, "%s has CRLF line endings on the bus" % name)
    found = [n for n in names if "/msgs/" in n]
    expect(found, "the message never reached the bus")
    texts = [subprocess.run(["git", "cat-file", "blob", "%s:%s" % (bus, n)], cwd=world.remote, stdout=subprocess.PIPE, check=True).stdout.decode("utf-8") for n in found]
    expect(any("caf" in t for t in texts), "the message text was lost")


def real_service(world):
    """The real job, pointed at a copy of fb.py in the throwaway folder. Under Task Scheduler or systemd it has no FB_HOME, so
    it reads the real config folder, which is why this is skipped if that folder is in use."""
    import fb
    real_home = os.path.join(os.path.expanduser("~"), ".config", "fast-builder")
    made_real_home = not os.path.isdir(real_home)
    config = os.path.join(real_home, "config.json")
    if os.path.exists(config):
        return "SKIPPED: this computer already has a listener config"
    if os.path.exists(fb.service_file()) and not WIN:
        return "SKIPPED: this computer already has the service"
    if WIN:
        name = fb.task_name()
        if run(["schtasks", "/Query", "/TN", name], check_exit=False).returncode == 0:
            return "SKIPPED: this computer already has the task"
        state = lambda: run(["powershell", "-NoProfile", "-Command", "(Get-ScheduledTask -TaskName '%s').State" % name], check_exit=False).stdout.strip()
    elif sys.platform == "darwin":
        return "SKIPPED: this check does not touch launchd"
    else:
        expect(shutil.which("systemctl"), "systemctl not found")
        state = lambda: run(["systemctl", "--user", "is-active", fb.UNIT_NAME], check_exit=False).stdout.strip()
        if state() == "active":
            return "SKIPPED: this computer already has the service"
    try:
        os.makedirs(real_home, exist_ok=True)
        with open(config, "w") as handle:  # no repos, so it can start nothing: a task gets no FB_ACCOUNT of its own
            json.dump({"account": "svc", "repos": {}}, handle)
        world.fb("svc", "listen-install", "--remote", world.remote, "--account", "svc")
        time.sleep(8)
        seen = state()
        expect(seen in ("Running", "active"), "after 8 seconds the listener's state is %r" % seen)
        return "state %s after 8 seconds" % seen
    finally:
        world.fb("svc", "listen-uninstall", "--all", check_exit=False)
        if WIN:
            expect(run(["schtasks", "/Query", "/TN", name], check_exit=False).returncode != 0, "the task was not removed")
        if os.path.exists(config):
            os.remove(config)
        if made_real_home and os.path.isdir(real_home):
            shutil.rmtree(real_home, ignore_errors=True)


def main():
    say("fast-builder check on %s" % sys.platform)
    check("this Python and git", tool_versions)
    check("fb.py loads", imports)
    check("claude and gh are installed", real_tools)
    check("a hook starts Python the way Claude Code would", hook_python)
    world = World()
    say("  in %s" % world.root)
    try:
        check("files are UTF-8 with LF", lambda: text_is_utf8(world))
        check("file locks keep a second process out", lambda: locks_exclude(world))
        check("processes are seen, timed and ended with their children", process_handling)
        check("a timeout holds with a child left running", timeouts_hold)
        names = {}

        def publish_runs():
            names["local"] = world.publish("local")
            return names["local"]
        check("a run publishes to the bus", publish_runs)
        if "local" in names:
            check("the bus keeps bytes as written", lambda: bus_bytes(world, names["local"]))
            check("the guard hook enforces the lane's files", lambda: guard(world, names["local"]))
            check("the listener starts a lane with its whole brief", lambda: lane_starts(world, names["local"]))
            if hasattr(world, "lane"):
                check("a lane is recognised and stopped with what it started", lambda: lane_stops(world))
            check("a stop reaches the guard for Bash and PowerShell", lambda: stop_reaches_guard(world, names["local"]))
            check("an account out of usage frees its lane and saves its work", lambda: out_of_usage_releases(world, names["local"]))
            check("a bus Windows cannot check out stops lanes", unreadable_bus_stops_lanes)
        if REAL_SERVICE:
            check("the real background job installs, runs and uninstalls", lambda: real_service(world))
        else:
            say("  --    real background job not tried: add --real-service")
    finally:
        time.sleep(1)
        try:
            force_remove(world.root)
        except OSError as exc:
            say("  note  could not remove %s: %s" % (world.root, exc))
    say()
    say("FAILED: %s" % ", ".join(FAILED) if FAILED else "All checks passed.")
    with open("fast-builder-check-results.txt", "w", encoding="utf-8") as handle:
        handle.write("\n".join(LOG) + "\n")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
