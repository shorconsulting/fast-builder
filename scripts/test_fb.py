#!/usr/bin/env python3
"""End-to-end tests. Each account is a separate FB_HOME and FB_CACHE (and clone) against a local bare remote, so what
passes here passed through real git pushes and real races. A shim stands in for the claude binary."""
import concurrent.futures
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FB = os.path.join(HERE, "fb.py")
sys.path.insert(0, HERE)
import fb  # noqa: E402

CLAUDE_SHIM = """#!/bin/sh
echo "$PWD :: $*" >> "$FB_SHIM_LOG"
echo '{"result": "200"}'
exit 0
"""


def sh(args, cwd=None, env=None, check=True):
    proc = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise AssertionError("%s: %s %s" % (args, proc.stdout, proc.stderr))
    return proc


class World(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fbtest-")
        self.remote = os.path.join(self.root, "owner", "app.git")
        os.makedirs(self.remote)
        sh(["git", "init", "-q", "--bare", "-b", "main", self.remote])
        seed = os.path.join(self.root, "seed")
        sh(["git", "clone", "-q", self.remote, seed])
        for rel in ("src/auth/login.ts", "src/pay/charge.ts", "src/ui/home.ts", "docs/a.md", "README.md"):
            path = os.path.join(seed, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("x\n")
        sh(["git", "add", "-A"], cwd=seed)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], cwd=seed)
        sh(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed)
        self.seed = seed
        self.shim = os.path.join(self.root, "claude-shim")
        with open(self.shim, "w") as handle:
            handle.write(CLAUDE_SHIM)
        os.chmod(self.shim, os.stat(self.shim).st_mode | stat.S_IEXEC)
        self.shim_log = os.path.join(self.root, "shim.log")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, account):
        home = os.path.join(self.root, "accounts", account)
        os.makedirs(home, exist_ok=True)
        return dict(os.environ, FB_HOME=os.path.join(home, "cfg"), FB_CACHE=os.path.join(home, "cache"), FB_ACCOUNT=account, FB_CLAUDE=self.shim, FB_SHIM_LOG=self.shim_log, HOME=home)

    def clone(self, account):
        path = os.path.join(self.root, "accounts", account, "repo")
        if not os.path.isdir(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            sh(["git", "clone", "-q", self.remote, path])
            sh(["git", "config", "user.name", account], cwd=path)
            sh(["git", "config", "user.email", "a@b"], cwd=path)
        return path

    def fb(self, account, *args, check=True, stdin=None):
        proc = subprocess.run([sys.executable, FB] + list(args), cwd=self.clone(account), env=self.env(account), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, input=stdin if stdin is not None else "")
        if check and proc.returncode != 0:
            raise AssertionError("fb %s: %s %s" % (args, proc.stdout, proc.stderr))
        return proc

    def j(self, account, *args, **kw):
        return json.loads(self.fb(account, *args, **kw).stdout)

    def publish(self, accounts=2, where="cloud", account="alpha", autopilot="full", today="2026-10-30"):
        info = self.j(account, "draft", "--goal", "Ship it", "--accounts", str(accounts), "--where", where, "--autopilot", autopilot, "--code", "HPH", "--today", today, "--dir", os.path.join(self.root, "draft-" + today + where + str(accounts)))
        draft = info["draft"]
        lanes = [{"name": "core", "owns": ["src/auth/**", "docs/**"], "modules": ["M01"], "digits": [1, 3, 5, 7, 0, 9], "steward": True},
                 {"name": "money", "owns": ["src/pay/**"], "modules": ["M02"], "digits": [2, 4, 6, 8]},
                 {"name": "ui", "owns": ["src/ui/**"], "modules": ["M03"], "digits": []}][:accounts]
        with open(os.path.join(draft, "lanes.json"), "w") as handle:
            json.dump({"lanes": lanes, "shared_ok": ["package-lock.json"]}, handle)
        self.fb(account, "render", "--draft", draft)
        for name in os.listdir(os.path.join(draft, "lanes")) + [info["plan_file"]]:
            path = os.path.join(draft, "lanes", name) if name.endswith(".md") and "plan" not in name else os.path.join(draft, name)
            with open(path) as handle:
                text = handle.read()
            import re
            with open(path, "w") as handle:
                handle.write(re.sub(r"\{\{[^}]*\}\}", "filled", text))
        result = self.j(account, "publish", "--draft", draft)
        self.assertTrue(result["ok"], result)
        return info, draft, result


class Names(unittest.TestCase):
    def test_plan_name_matches_the_ask(self):
        import datetime
        run, plan = fb.run_names("hph", datetime.date(2026, 10, 30), set())
        self.assertEqual(plan, "HPH-10-30-26-orchestration-plan.md")
        self.assertEqual(run, "hph-10-30-26")
        run, plan = fb.run_names("HPH", datetime.date(2026, 10, 30), {"hph-10-30-26"})
        self.assertEqual(plan, "HPH-10-30-26-2-orchestration-plan.md")

    def test_globs(self):
        self.assertTrue(fb.matches("src/a/b.ts", ["src/**"]))
        self.assertTrue(fb.matches("a/b/auth/c.ts", ["**/auth/**"]))
        self.assertTrue(fb.matches("auth/c.ts", ["**/auth/**"]))
        self.assertFalse(fb.matches("src/a/b.ts", ["src/*.ts"]))
        self.assertTrue(fb.matches("src/b.ts", ["src/*.ts"]))
        self.assertTrue(fb.matches("src/x/y", ["src/x/"]))
        self.assertTrue(fb.matches(".env", ["**/.env*"]))
        self.assertTrue(fb.matches("a/b.ts", ["./a/b.ts"]))
        self.assertTrue(fb.matches("src/a.ts", ["/src/**"]))

    def test_globs_meet(self):
        yes = [("*.md", "README*"), ("src/*/x.ts", "src/a/**"), ("**/auth/**", "src/**"), ("src/new/**", "src/new/sub/**"), ("a/", "a/b/**"), ("src/*.ts", "src/a*"), ("**", "x")]
        no = [("src/a/**", "src/b/**"), ("*.md", "*.ts"), ("src/*.ts", "src/*/x.ts"), ("a/b", "a/c")]
        for a, b in yes:
            self.assertTrue(fb.globs_meet(a, b), (a, b))
            self.assertTrue(fb.globs_meet(b, a), (b, a))
        for a, b in no:
            self.assertFalse(fb.globs_meet(a, b), (a, b))

    def test_simple_commands(self):
        for good in ['git status', 'python3 "$FB" send --body "a; b | c"']:
            self.assertTrue(fb.is_simple(good), good)
        for bad in ['echo a#b; rm -rf src', 'git log --grep=x#y && git push', 'git status && git push', 'echo x > f', 'cat a; rm -rf src', 'cat $(rm x)', 'echo `rm x`', "echo 'unterminated"]:
            self.assertFalse(fb.is_simple(bad), bad)
        self.assertFalse(fb.allowed_when_halted("Bash", "rm -rf src # fb.py"))
        self.assertTrue(fb.allowed_when_halted("Bash", 'python3 "$FB" lane-state stopped'))
        self.assertTrue(fb.allowed_when_halted("Bash", "python3 /tmp/x/fb.py wait"))
        self.assertFalse(fb.allowed_when_halted("Agent", ""))

    def test_push_is_seen_however_it_is_spelled(self):
        for text in ["git push", "git -C . push origin x", "git -c a=b push", "gh pr create", "cd x && git push"]:
            self.assertTrue(fb.PUSH_BASH.search(text), text)
        self.assertFalse(fb.PUSH_BASH.search("git status"))

    def test_trailing_newline_is_not_a_name(self):
        self.assertFalse(fb.NAME_RE.match("core\n"))
        self.assertFalse(fb.RUN_RE.match("a\n"))
        self.assertFalse(fb.matches("srcx/y", ["src/**"]))

    def test_digits_never_repeat(self):
        for n in range(1, 11):
            rows = fb.allocate_digits(n)
            flat = [d for r in rows for d in r]
            self.assertEqual(len(rows), n)
            self.assertEqual(len(flat), len(set(flat)), n)

    def test_size_picks_fewest_that_fit(self):
        r = fb.size(100, 48, 4, 12)
        self.assertEqual(r["accounts"], 4)
        self.assertTrue(r["fits_deadline"] is False or r["table"][3]["hours_to_finish"] <= 48)
        r = fb.size(20, 48, 4, 12)
        self.assertEqual(r["accounts"], 1)
        r = fb.size(1000, 1, 3, 12)
        self.assertEqual(r["accounts"], 3)
        self.assertFalse(r["fits_deadline"])

    def test_partition_keeps_shared_paths_together_and_balances(self):
        items = [{"id": "a", "paths": ["x"], "weight": 5}, {"id": "b", "paths": ["x", "y"], "weight": 5}, {"id": "c", "paths": ["z"], "weight": 6}, {"id": "d", "paths": ["w"], "weight": 4}]
        bins = fb.partition(items, 2)
        self.assertEqual(sorted(b["weight"] for b in bins), [10.0, 10.0])
        together = [b for b in bins if "a" in b["items"]][0]
        self.assertIn("b", together["items"])
        paths = [set(b["paths"]) for b in bins]
        self.assertFalse(paths[0] & paths[1])

    def test_routing_follows_the_table(self):
        s = fb.SENSITIVE_DEFAULTS
        self.assertEqual(fb.route("build", ["src/ui/a.ts"], s)["model"], "sonnet")
        self.assertEqual(fb.route("build", ["src/auth/a.ts"], s)["model"], "opus")
        self.assertEqual(fb.route("rereview", ["src/auth/a.ts"], s)["model"], "opus")
        self.assertEqual(fb.route("rereview", ["src/ui/a.ts"], s)["model"], "sonnet")
        self.assertEqual(fb.route("poll", ["src/auth/a.ts"], s)["model"], "haiku")
        self.assertEqual(fb.route("build", ["src/ui/a.ts"], s, changes_verdicts=2)["model"], "opus")
        self.assertEqual(fb.route("build", ["x"], s, stalled=True)["model"], "opus")
        for kind in fb.ROUTES:
            self.assertNotEqual(fb.route(kind, [], s)["model"], "fable")


class Validate(World):
    def test_overlap_is_refused(self):
        info, draft, _ = self.publish()
        path = os.path.join(draft, "lanes.json")
        doc = json.load(open(path))
        doc["lanes"][1]["owns"] = ["src/**"]
        doc["lanes"][0]["steward"] = False
        doc["lanes"].append({"name": "steward", "owns": [], "steward": True})
        json.dump(doc, open(path, "w"))
        job = json.load(open(os.path.join(draft, "job.json")))
        job["accounts"] = 3
        json.dump(job, open(os.path.join(draft, "job.json"), "w"))
        errors, _ = fb.validate(draft, ["src/auth/login.ts"])
        self.assertTrue(any("overlap" in e for e in errors), errors)
        self.assertTrue(any("two lanes" in e for e in errors), errors)

    def test_hostile_job_fields_are_refused(self):
        for field, value in (("plan_file", "../../secret"), ("lane_model", "opus --dangerously-skip-permissions"), ("base", "--upload-pack=x")):
            self.assertTrue(fb.job_errors({"plan_file": "a.md", field: value}), field)

    def test_future_paths_compare_without_files(self):
        info, draft, _ = self.publish()
        path = os.path.join(draft, "lanes.json")
        doc = json.load(open(path))
        doc["lanes"][0]["owns"] = ["src/new/**"]
        doc["lanes"][1]["owns"] = ["src/new/sub/**"]
        doc["lanes"][0]["steward"] = False
        doc["lanes"][1]["steward"] = True
        json.dump(doc, open(path, "w"))
        errors, _ = fb.validate(draft, [])
        self.assertEqual(errors, [])  # the steward is exempt: its globs yield to any lane's
        doc["lanes"].append({"name": "third", "owns": ["src/new/sub/**"]})
        json.dump(doc, open(path, "w"))
        errors, _ = fb.validate(draft, [])
        self.assertTrue(any("accounts" in e for e in errors))

    def test_over_max_and_placeholders_refused(self):
        info = self.j("alpha", "draft", "--goal", "g", "--accounts", "3", "--max-accounts", "2", "--dir", os.path.join(self.root, "d2"))
        draft = info["draft"]
        doc = {"lanes": [{"name": "a", "owns": ["a/**"], "steward": True}, {"name": "b", "owns": ["b/**"]}, {"name": "c", "owns": ["c/**"]}]}
        json.dump(doc, open(os.path.join(draft, "lanes.json"), "w"))
        self.fb("alpha", "render", "--draft", draft)
        result = self.fb("alpha", "publish", "--draft", draft, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("over the maximum", result.stdout)
        self.assertIn("placeholders", result.stdout)

    def test_defaults_are_cloud_and_full_autopilot(self):
        info = self.j("alpha", "draft", "--goal", "g", "--dir", os.path.join(self.root, "d3"))
        self.assertEqual(info["where"], "cloud")
        self.assertEqual(info["autopilot"], "full")


class BusTests(World):
    def phone(self):
        path = tempfile.mkdtemp(dir=self.root)
        sh(["git", "clone", "-q", "-b", "fast-builder/bus", self.remote, path])
        return path

    def push_phone(self, path):
        sh(["git", "add", "-A"], cwd=path)
        sh(["git", "-c", "user.name=p", "-c", "user.email=p@p", "commit", "-qm", "edit"], cwd=path)
        sh(["git", "push", "-q", "origin", "HEAD:fast-builder/bus"], cwd=path)

    def test_publish_puts_everything_on_the_bus_and_nothing_on_main(self):
        info, _, result = self.publish()
        files = sh(["git", "ls-tree", "-r", "--name-only", "refs/heads/fast-builder/bus"], cwd=self.remote).stdout.split()
        self.assertIn("runs/hph-10-30-26/HPH-10-30-26-orchestration-plan.md", files)
        self.assertIn("runs/hph-10-30-26/lanes/core.md", files)
        self.assertIn("kit/fb.py", files)
        main = sh(["git", "ls-tree", "-r", "--name-only", "refs/heads/main"], cwd=self.remote).stdout.split()
        self.assertFalse([f for f in main if "orchestration" in f or f.startswith("runs/")])
        self.assertNotIn(".github", " ".join(files))

    def test_only_one_account_wins_a_lane(self):
        self.publish(accounts=2)
        names = ["a%d" % i for i in range(6)]
        for name in names:
            self.clone(name)

        def go(name):
            proc = self.fb(name, "join", "--run", "hph-10-30-26", "--lane", "core", check=False)
            return proc.returncode == 0

        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            wins = list(pool.map(go, names))
        self.assertEqual(sum(wins), 1, wins)

    def test_parallel_writers_lose_nothing(self):
        self.publish(accounts=2)
        names = ["w%d" % i for i in range(6)]
        for name in names:
            self.clone(name)
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda n: self.fb(n, "send", "--run", "hph-10-30-26", "--to", "all", "--body", n), names))
        got = self.j("alpha", "inbox", "--run", "hph-10-30-26")
        self.assertEqual(sorted(m["body"] for m in got), sorted(names))

    def test_stop_reaches_every_lane(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "join", "--run", "hph-10-30-26", "--lane", "money")
        self.assertEqual(self.j("bravo", "tick")["action"], "continue")
        self.fb("alpha", "stop", "--reason", "enough")
        tick = self.j("bravo", "tick")
        self.assertEqual(tick["action"], "stop")
        self.assertEqual(self.fb("bravo", "tick", "--exit-code", check=False).returncode, 20)
        self.fb("alpha", "resume")
        self.assertEqual(self.j("bravo", "tick")["action"], "continue")

    def test_a_stop_file_from_the_github_page_works(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        phone = self.phone()
        open(os.path.join(phone, "STOP"), "w").close()
        sh(["git", "add", "-A"], cwd=phone)
        sh(["git", "-c", "user.name=p", "-c", "user.email=p@p", "commit", "-qm", "stop"], cwd=phone)
        sh(["git", "push", "-q", "origin", "HEAD:fast-builder/bus"], cwd=phone)
        self.assertEqual(self.j("alpha", "tick")["action"], "stop")

    def test_messages_arrive_once(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "join", "--run", "hph-10-30-26", "--lane", "money")
        self.fb("alpha", "send", "--to", "money", "--kind", "request", "--body", "need the charge API")
        first = self.j("bravo", "tick")
        self.assertEqual([m["body"] for m in first["inbox"]], ["need the charge API"])
        self.assertEqual(self.j("bravo", "tick")["inbox"], [])

    def test_quiet_lane_is_freed_and_taken_over_and_the_old_account_is_told(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "join", "--run", "hph-10-30-26", "--lane", "money")
        report = self.j("alpha", "conduct", "--run", "hph-10-30-26", "--stale-minutes", "0")
        self.assertIn("money", report["freed"])
        self.fb("charlie", "join", "--run", "hph-10-30-26", "--lane", "money")
        self.assertEqual(self.j("bravo", "tick")["action"], "lost")

    def test_resume_puts_stopped_lanes_back_on_offer(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("alpha", "stop")
        self.fb("alpha", "lane-state", "stopped")
        self.assertEqual(self.j("alpha", "conduct", "--run", "hph-10-30-26")["run"], "hph-10-30-26")
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "stopped")
        self.fb("bravo", "resume", "--run", "hph-10-30-26", "--remote", self.remote)
        lanes = {r["lane"]: r for r in self.j("bravo", "status", "--remote", self.remote)["runs"][0]["lanes"]}
        self.assertIsNone(lanes["core"]["account"])
        self.fb("charlie", "join", "--run", "hph-10-30-26", "--lane", "core")

    def test_pause_then_wait_wakes_on_resume(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "pause", "--run", "hph-10-30-26", "--remote", self.remote)
        self.assertEqual(self.j("alpha", "tick")["action"], "pause")
        waiter = subprocess.Popen([sys.executable, FB, "wait", "--until-resumed", "--seconds", "30", "--poll", "0.3"], cwd=self.clone("alpha"), env=self.env("alpha"), stdout=subprocess.PIPE, universal_newlines=True)
        time.sleep(1)
        self.fb("bravo", "resume", "--run", "hph-10-30-26", "--remote", self.remote)
        output, _ = waiter.communicate(timeout=20)
        self.assertIn("resumed", output)

    def test_a_blocked_lane_cannot_keep_the_run_open_forever(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "join", "--run", "hph-10-30-26", "--lane", "money")
        self.fb("alpha", "lane-state", "done")
        self.fb("bravo", "lane-state", "blocked")
        self.assertTrue(self.j("alpha", "conduct", "--run", "hph-10-30-26")["finished"])

    def test_resuming_one_run_does_not_cancel_a_global_stop(self):
        self.publish()
        self.publish(accounts=2, today="2026-10-31")
        phone = self.phone()
        open(os.path.join(phone, "STOP"), "w").close()
        self.push_phone(phone)
        result = self.j("alpha", "resume", "--run", "hph-10-30-26")
        self.assertIn("warning", result)
        states = {r["run"]: r["state"] for r in self.j("alpha", "status")["runs"]}
        self.assertEqual(set(states.values()), {"stopping"})

    def test_resume_all_clears_the_stop_file(self):
        self.publish()
        phone = self.phone()
        open(os.path.join(phone, "STOP"), "w").close()
        self.push_phone(phone)
        self.j("alpha", "stop", "--run", "hph-10-30-26")
        self.j("alpha", "resume", "--all")
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "running")

    def test_resume_clears_a_run_level_stop_file(self):
        self.publish()
        phone = self.phone()
        open(os.path.join(phone, "runs/hph-10-30-26/STOP"), "w").close()
        self.push_phone(phone)
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "stopping")
        self.j("alpha", "resume", "--run", "hph-10-30-26")
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "running")

    def test_removing_a_global_stop_restores_runs_as_they_were(self):
        self.publish()
        phone = self.phone()
        open(os.path.join(phone, "STOP"), "w").close()
        self.push_phone(phone)
        self.j("alpha", "conduct", "--run", "hph-10-30-26")
        phone = self.phone()
        os.remove(os.path.join(phone, "STOP"))
        self.push_phone(phone)
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "running")

    def test_resume_all_leaves_a_run_stopped_by_hand_alone(self):
        self.publish()
        self.fb("alpha", "stop", "--run", "hph-10-30-26")
        self.j("alpha", "conduct", "--run", "hph-10-30-26", "--stale-minutes", "0")
        self.publish(accounts=2, today="2026-10-31")
        self.fb("alpha", "pause", "--run", "hph-10-31-26")
        result = self.j("alpha", "resume", "--all")
        self.assertEqual(result["runs"], ["hph-10-31-26"])
        self.assertEqual(result["not_resumed_stopped"], ["hph-10-30-26"])

    def test_wait_reports_a_lost_lane_and_a_lost_lane_can_still_leave(self):
        self.publish()
        self.fb("alpha", "join", "--lane", "core", "--run", "hph-10-30-26")
        other = os.path.join(self.root, "accounts", "alpha", "second")
        sh(["git", "clone", "-q", self.remote, other])
        sh([sys.executable, FB, "join", "--lane", "core", "--run", "hph-10-30-26"], cwd=other, env=self.env("alpha"))
        waiter = subprocess.Popen([sys.executable, FB, "wait", "--seconds", "20", "--poll", "0.2"], cwd=self.clone("alpha"), env=self.env("alpha"), stdout=subprocess.PIPE, universal_newlines=True)
        time.sleep(1.5)
        self.fb("bravo", "send", "--run", "hph-10-30-26", "--to", "all", "--body", "x", "--remote", self.remote)
        output, _ = waiter.communicate(timeout=30)
        self.assertIn('"lost"', output.replace("woke", ""))
        self.assertNotEqual(self.fb("alpha", "lane-state", "stopped", check=False).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.clone("alpha"), ".git/fast-builder/session.json")))

    def test_plain_resume_refuses_to_guess(self):
        self.publish()
        self.publish(accounts=2, today="2026-10-31")
        self.fb("alpha", "stop", "--run", "hph-10-30-26")
        self.fb("alpha", "stop", "--run", "hph-10-31-26")
        self.assertNotEqual(self.fb("alpha", "resume", check=False).returncode, 0)

    def test_pause_never_softens_a_stop_and_a_finished_run_stays_finished(self):
        self.publish()
        self.fb("alpha", "stop", "--run", "hph-10-30-26")
        self.assertEqual(self.j("alpha", "pause", "--run", "hph-10-30-26")["runs"], [])
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "stopping")
        self.fb("alpha", "resume", "--run", "hph-10-30-26")
        self.fb("alpha", "join", "--lane", "core", "--run", "hph-10-30-26")
        self.fb("bravo", "join", "--lane", "money", "--run", "hph-10-30-26")
        self.fb("alpha", "lane-state", "done")
        self.fb("bravo", "lane-state", "done")
        self.j("alpha", "conduct", "--run", "hph-10-30-26")
        self.assertEqual(self.j("alpha", "resume", "--run", "hph-10-30-26")["runs"], [])
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "finished")

    def test_a_dead_lane_cannot_hold_a_stopping_run_open(self):
        self.publish()
        self.fb("alpha", "join", "--lane", "core", "--run", "hph-10-30-26")
        self.fb("bravo", "join", "--lane", "money", "--run", "hph-10-30-26")
        self.fb("alpha", "stop", "--run", "hph-10-30-26")
        self.j("alpha", "conduct", "--run", "hph-10-30-26", "--stale-minutes", "0")
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "stopped")

    def test_joining_a_named_lane_never_silently_gives_another(self):
        self.publish(accounts=3)
        self.fb("alpha", "join", "--lane", "core", "--run", "hph-10-30-26")
        proc = self.fb("alpha", "join", "--lane", "money", "--run", "hph-10-30-26", check=False)
        self.assertNotEqual(proc.returncode, 0)

    def test_a_replaced_session_cannot_touch_the_lane(self):
        self.publish()
        self.fb("alpha", "join", "--lane", "core", "--run", "hph-10-30-26")
        self.fb("bravo", "join", "--lane", "money", "--run", "hph-10-30-26")
        other = os.path.join(self.root, "accounts", "alpha", "second")
        sh(["git", "clone", "-q", self.remote, other])
        env = self.env("alpha")
        sh([sys.executable, FB, "join", "--lane", "core", "--run", "hph-10-30-26"], cwd=other, env=env)
        self.assertEqual(self.j("alpha", "tick")["action"], "lost")
        self.assertNotEqual(self.fb("alpha", "lane-state", "done", check=False).returncode, 0)

    def test_a_late_arriving_older_message_is_still_delivered(self):
        self.publish()
        self.fb("alpha", "join", "--lane", "core", "--run", "hph-10-30-26")
        self.fb("bravo", "send", "--run", "hph-10-30-26", "--to", "core", "--body", "new", "--remote", self.remote)
        self.assertEqual(len(self.j("alpha", "tick")["inbox"]), 1)
        phone = self.phone()
        with open(os.path.join(phone, "runs/hph-10-30-26/msgs/20200101T000000Z-x-0000.json"), "w") as handle:
            json.dump({"from": "x", "to": "core", "kind": "note", "body": "old", "at": "2020-01-01T00:00:00Z"}, handle)
        self.push_phone(phone)
        self.assertEqual([m["body"] for m in self.j("alpha", "tick")["inbox"]], ["old"])

    def test_a_symlink_on_the_bus_is_refused(self):
        self.publish()
        phone = self.phone()
        os.symlink("/etc", os.path.join(phone, "runs/hph-10-30-26/msgs"))
        self.push_phone(phone)
        self.assertNotEqual(self.fb("alpha", "send", "--run", "hph-10-30-26", "--to", "all", "--body", "x", check=False).returncode, 0)

    def test_a_hostile_run_argument_is_refused(self):
        self.publish()
        self.assertNotEqual(self.fb("alpha", "stop", "--run", "../../..", check=False).returncode, 0)

    def test_concurrent_writers_on_one_account_lose_nothing(self):
        self.publish()
        repo = self.clone("alpha")
        procs = [subprocess.Popen([sys.executable, FB, "send", "--run", "hph-10-30-26", "--to", "all", "--body", "m%d" % i], cwd=repo, env=self.env("alpha"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for i in range(8)]
        self.assertEqual([p.wait() for p in procs], [0] * 8)
        self.assertEqual(len(self.j("bravo", "inbox", "--run", "hph-10-30-26")), 8)

    def test_conductor_lease_fails_over(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.j("alpha", "tick")
        bravo = self.j("bravo", "conduct", "--run", "hph-10-30-26")
        self.assertEqual(bravo["holder"], "alpha")

    def test_run_finishes_when_every_lane_is_done(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "join", "--run", "hph-10-30-26", "--lane", "money")
        self.fb("alpha", "lane-state", "done")
        self.fb("bravo", "lane-state", "done")
        self.assertTrue(self.j("alpha", "conduct", "--run", "hph-10-30-26")["finished"])
        self.assertEqual(self.j("alpha", "status")["runs"][0]["state"], "finished")

    def test_decisions_reach_the_report(self):
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("alpha", "decide", "--what", "use JWT", "--why", "simple", "--undo", "revert #1")
        self.fb("alpha", "send", "--to", "human", "--kind", "needs-human", "--body", "rotate the key")
        text = self.fb("alpha", "report", "--run", "hph-10-30-26").stdout
        self.assertIn("Decided without you", text)
        self.assertIn("use JWT", text)
        self.assertIn("rotate the key", text)


class Guard(World):
    def setUp(self):
        super().setUp()
        self.publish()
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "money")
        self.repo = self.clone("alpha")

    def guard(self, tool, tool_input):
        payload = json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": self.repo})
        return self.fb("alpha", "hook", "guard", stdin=payload).stdout

    def clear_cache(self):
        path = os.path.join(self.repo, ".git", "fast-builder", "control-cache.json")
        if os.path.exists(path):
            os.remove(path)

    def test_edit_in_lane_passes_and_out_of_lane_is_denied(self):
        self.assertEqual(self.guard("Edit", {"file_path": os.path.join(self.repo, "src/pay/charge.ts")}), "")
        out = json.loads(self.guard("Write", {"file_path": os.path.join(self.repo, "src/auth/login.ts")}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("core", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_steward_owns_the_unclaimed(self):
        out = json.loads(self.guard("Edit", {"file_path": os.path.join(self.repo, "README.md")}))
        self.assertIn("core", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_shared_files_are_open_to_every_lane(self):
        self.assertEqual(self.guard("Edit", {"file_path": os.path.join(self.repo, "package-lock.json")}), "")

    def test_files_outside_the_repo_are_not_its_business(self):
        self.assertEqual(self.guard("Write", {"file_path": "/tmp/scratch/x.md"}), "")

    def test_stop_blocks_tools_but_lets_the_handoff_through(self):
        self.fb("bravo", "stop", "--run", "hph-10-30-26", "--remote", self.remote)
        self.clear_cache()
        out = json.loads(self.guard("Edit", {"file_path": os.path.join(self.repo, "src/pay/charge.ts")}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("deny", self.guard("Bash", {"command": "npm test"}))
        self.assertIn("deny", self.guard("Agent", {"prompt": "go"}))
        self.assertEqual(self.guard("Bash", {"command": 'python3 "$FB" handoff /tmp/x'}), "")
        self.assertIn("deny", self.guard("Bash", {"command": "git status && git push"}))
        self.assertEqual(self.guard("Read", {"file_path": "x"}), "")

    def test_drain_refuses_new_agents_only(self):
        self.fb("bravo", "stop", "--drain", "--run", "hph-10-30-26", "--remote", self.remote)
        self.clear_cache()
        self.assertIn("deny", self.guard("Agent", {"prompt": "go"}))
        self.assertEqual(self.guard("Edit", {"file_path": os.path.join(self.repo, "src/pay/charge.ts")}), "")

    def test_pushing_a_branch_that_left_its_lane_is_denied(self):
        with open(os.path.join(self.repo, "src/auth/login.ts"), "w") as handle:
            handle.write("y\n")
        sh(["git", "checkout", "-qb", "fb/x"], cwd=self.repo)
        sh(["git", "commit", "-qam", "sneak"], cwd=self.repo)
        sh(["git", "fetch", "-q", "origin"], cwd=self.repo)
        self.assertIn("deny", self.guard("Bash", {"command": "git push origin fb/x"}))
        self.assertNotEqual(self.fb("alpha", "check-diff", check=False).returncode, 0)

    def test_a_finished_run_leaves_the_clone_usable(self):
        self.fb("alpha", "lane-state", "done")
        self.fb("bravo", "join", "--run", "hph-10-30-26", "--lane", "core", "--remote", self.remote)
        self.fb("bravo", "lane-state", "done")
        self.j("alpha", "conduct", "--run", "hph-10-30-26")
        self.clear_cache()
        self.assertEqual(self.guard("Bash", {"command": "npm test"}), "")

    def test_leave_turns_the_hooks_off(self):
        self.fb("alpha", "leave")
        self.assertEqual(self.guard("Write", {"file_path": os.path.join(self.repo, "src/auth/login.ts")}), "")

    def test_a_rename_out_of_another_lane_is_seen(self):
        sh(["git", "checkout", "-qb", "fb/y"], cwd=self.repo)
        sh(["git", "mv", "src/auth/login.ts", "src/pay/stolen.ts"], cwd=self.repo)
        sh(["git", "commit", "-qm", "move"], cwd=self.repo)
        self.assertNotEqual(self.fb("alpha", "check-diff", check=False).returncode, 0)

    def test_a_diff_that_cannot_run_is_not_a_pass(self):
        session = os.path.join(self.repo, ".git", "fast-builder", "session.json")
        doc = json.load(open(session))
        doc["base"] = "no-such-branch"
        json.dump(doc, open(session, "w"))
        self.assertNotEqual(self.fb("alpha", "check-diff", check=False).returncode, 0)

    def test_sibling_and_nested_worktrees_are_checked(self):
        sibling = os.path.join(self.root, "sibling")
        sh(["git", "worktree", "add", "-q", "-b", "w1", sibling], cwd=self.repo)
        self.assertIn("deny", self.guard("Write", {"file_path": os.path.join(sibling, "src/auth/login.ts")}))
        self.assertEqual(self.guard("Write", {"file_path": os.path.join(sibling, "src/pay/charge.ts")}), "")
        nested = os.path.join(self.repo, ".claude", "worktrees", "n")
        sh(["git", "worktree", "add", "-q", "-b", "w2", nested], cwd=self.repo)
        self.assertIn("deny", self.guard("Write", {"file_path": os.path.join(nested, "src/auth/login.ts")}))

    def test_a_lane_cannot_rewrite_its_own_rules(self):
        self.assertIn("deny", self.guard("Write", {"file_path": os.path.join(self.repo, ".GIT/fast-builder/lanes.json")}))
        self.assertIn("deny", self.guard("Write", {"file_path": os.path.join(self.repo, ".git/fast-builder/lanes.json")}))
        self.assertIn("deny", self.guard("Edit", {"file_path": os.path.join(self.repo, ".git/hooks/pre-push")}))

    def test_a_halted_lane_can_still_write_its_handoff_outside_the_repo(self):
        self.fb("bravo", "stop", "--run", "hph-10-30-26", "--remote", self.remote)
        self.clear_cache()
        self.assertEqual(self.guard("Write", {"file_path": os.path.join(self.root, "handoff.md")}), "")
        self.assertIn("deny", self.guard("Write", {"file_path": os.path.join(self.repo, "src/pay/handoff.md")}))

    def test_a_halted_lane_cannot_write_a_submodule(self):
        sub = os.path.join(self.repo, "vendor", "lib")
        sh(["git", "init", "-q", sub])
        self.fb("bravo", "stop", "--run", "hph-10-30-26", "--remote", self.remote)
        self.clear_cache()
        self.assertIn("deny", self.guard("Write", {"file_path": os.path.join(sub, "a.txt")}))

    def test_a_held_lock_fails_open_inside_the_hook_timeout(self):
        import fcntl
        bus_dir = glob.glob(os.path.join(self.env("alpha")["FB_CACHE"], "bus", "*.lock"))[0]
        self.clear_cache()
        with open(bus_dir, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.fb("bravo", "send", "--run", "hph-10-30-26", "--to", "all", "--body", "move the bus", "--remote", self.remote)
            start = time.time()
            self.guard("Edit", {"file_path": os.path.join(self.repo, "src/pay/charge.ts")})
            self.assertLess(time.time() - start, 8)

    def test_network_failure_stops_nothing(self):
        session = os.path.join(self.repo, ".git", "fast-builder", "session.json")
        doc = json.load(open(session))
        doc["remote"] = "/nonexistent/repo.git"
        json.dump(doc, open(session, "w"))
        self.clear_cache()
        self.assertEqual(self.guard("Edit", {"file_path": os.path.join(self.repo, "src/pay/charge.ts")}), "")

    def test_outside_a_lane_the_hook_is_silent(self):
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/x"}, "cwd": self.seed})
        self.assertEqual(self.fb("alpha", "hook", "guard", stdin=payload).stdout, "")

    def test_stop_hook_keeps_autopilot_going_but_is_capped(self):
        payload = json.dumps({"cwd": self.repo})
        outs = [self.fb("alpha", "hook", "stop", stdin=payload).stdout for _ in range(fb.STOP_BLOCK_CAP + 1)]
        self.assertIn("block", outs[0])
        self.assertEqual(outs[-1], "")
        self.fb("alpha", "lane-state", "done")
        self.j("alpha", "tick")
        self.assertEqual(self.fb("alpha", "hook", "stop", stdin=payload).stdout, "")


class Listener(World):
    def register(self, account, permission="auto", routine="trig_123"):
        args = ["listen-install", "--dry-run", "--remote", self.remote, "--permission-mode", permission]
        self.fb(account, *(args + (["--routine-id", routine] if routine else [])))

    def calls(self):
        return [line for line in open(self.shim_log).read().splitlines() if " :: " in line] if os.path.exists(self.shim_log) else []

    def test_a_cloud_build_wakes_routines_and_they_claim_from_the_cloud(self):
        self.register("bravo")
        self.register("charlie")
        self.fb("bravo", "listen", "--once")
        self.assertEqual(self.calls(), [], "an idle listener must not start claude")
        self.publish(accounts=3)
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "listen", "--once")
        self.fb("charlie", "listen", "--once")
        calls = self.calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(all("trigger_id trig_123" in c and "--model haiku" in c and "--allowedTools RemoteTrigger" in c for c in calls))
        self.assertEqual([r["account"] for r in self.j("alpha", "status")["runs"][0]["lanes"]], ["alpha", None, None])
        self.assertIn("lane=money", self.fb("bravo", "poll").stdout)
        self.assertIn("lane=ui", self.fb("charlie", "poll").stdout)

    def test_a_cloud_build_with_no_routine_tells_a_person_and_claims_nothing(self):
        self.register("bravo", routine=None)
        self.publish(accounts=2)
        self.fb("bravo", "listen", "--once")
        self.assertEqual(self.calls(), [])
        self.assertTrue(any("no cloud routine" in m["body"] for m in self.j("alpha", "inbox", "--run", "hph-10-30-26", "--who", "human")))

    def test_a_clean_exit_is_not_a_wake(self):
        """The bug the real cloud test found: another login's routine answers 404 and claude still exits 0."""
        with open(self.shim, "w") as handle:
            handle.write("#!/bin/sh\necho '{\"result\": \"404\"}'\nexit 0\n")
        self.register("bravo")
        self.publish(accounts=2)
        self.fb("bravo", "listen", "--once")
        got = [m["body"] for m in self.j("alpha", "inbox", "--run", "hph-10-30-26", "--who", "human")]
        self.assertTrue(any("did not start (404)" in b and "claude auth status" in b for b in got), got)
        self.assertNotEqual(self.fb("bravo", "routine-check", "--routine-id", "trig_1", check=False).returncode, 0)

    def test_a_failed_wake_tells_a_person(self):
        with open(self.shim, "w") as handle:
            handle.write("#!/bin/sh\nexit 3\n")
        self.register("bravo")
        self.publish(accounts=2)
        self.fb("bravo", "listen", "--once")
        self.assertTrue(any("could not start its routine" in m["body"] for m in self.j("alpha", "inbox", "--run", "hph-10-30-26", "--who", "human")))

    def test_local_build_uses_the_chosen_permissions_and_its_own_clone(self):
        self.register("bravo", "acceptEdits")
        self.publish(accounts=2, where="local")
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "listen", "--once")
        line = open(self.shim_log).read()
        self.assertIn("--permission-mode acceptEdits", line)
        self.assertIn("--permission-prompts none", line)
        self.assertIn("hph-10-30-26-money", line)
        self.assertNotIn("--cloud", line)

    def test_a_stopped_run_starts_nothing(self):
        self.register("bravo")
        self.publish(accounts=2)
        self.fb("alpha", "stop", "--run", "hph-10-30-26")
        self.fb("bravo", "listen", "--once")
        self.assertFalse(os.path.exists(self.shim_log))

    def test_one_listener_never_carries_a_whole_fresh_run(self):
        self.register("bravo")
        self.publish(accounts=3, where="local")
        self.fb("bravo", "listen", "--once")
        self.fb("bravo", "listen", "--once")
        held = [r["lane"] for r in self.j("bravo", "status", "--remote", self.remote)["runs"][0]["lanes"] if r["account"] == "bravo"]
        self.assertEqual(held, ["core"])

    def test_a_listener_in_the_run_takes_over_a_freed_lane(self):
        self.register("bravo")
        self.publish(accounts=2, where="local")
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "listen", "--once")
        self.j("bravo", "conduct", "--run", "hph-10-30-26", "--stale-minutes", "0")
        cfg = os.path.join(self.env("bravo")["FB_HOME"], "children.json")
        self.fb("bravo", "listen", "--once")  # takes one of the two lanes a zero stale limit just freed
        self.fb("bravo", "listen", "--once")
        held = {r["lane"]: r["account"] for r in self.j("alpha", "status")["runs"][0]["lanes"]}
        self.assertEqual(set(held.values()), {"bravo"}, held)
        self.assertTrue(os.path.exists(cfg))

    def test_a_failed_local_start_gives_the_lane_back_and_tells_a_person(self):
        self.register("bravo")
        self.publish(accounts=2, where="local")
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        os.remove(self.shim)
        self.fb("bravo", "listen", "--once")
        lanes = {r["lane"]: r for r in self.j("alpha", "status")["runs"][0]["lanes"]}
        self.assertIsNone(lanes["money"]["account"])
        self.assertTrue(any("Could not start" in m["body"] for m in self.j("alpha", "inbox", "--run", "hph-10-30-26", "--who", "human")))

    def test_a_routine_in_the_run_takes_a_freed_lane_but_no_fresh_one(self):
        self.publish(accounts=3)
        self.assertIn("lane=core", self.fb("bravo", "poll").stdout)
        self.assertEqual(self.fb("bravo", "poll").stdout.strip(), "NOTHING")
        self.j("bravo", "conduct", "--run", "hph-10-30-26", "--stale-minutes", "0")  # bravo took the lease in poll
        self.assertIn("lane=", self.fb("bravo", "poll").stdout)

    def test_local_lanes_never_run_the_buss_copy_of_the_tool(self):
        self.register("bravo")
        self.publish(accounts=2, where="local")
        self.fb("alpha", "join", "--run", "hph-10-30-26", "--lane", "core")
        self.fb("bravo", "listen", "--once")
        text = open(self.shim_log).read()
        self.assertIn(os.path.join(self.env("bravo")["FB_HOME"], "fb.py"), text)
        self.assertNotIn("kit/", text)
        self.assertIn("--plugin-dir " + os.path.join(self.env("bravo")["FB_HOME"], "plugin"), text)
        self.assertTrue(os.path.exists(os.path.join(self.env("bravo")["FB_HOME"], "plugin", "hooks", "hooks.json")))

    def test_broken_json_on_the_bus_does_not_kill_the_listener(self):
        self.register("bravo")
        self.publish(accounts=2)
        phone = tempfile.mkdtemp(dir=self.root)
        sh(["git", "clone", "-q", "-b", "fast-builder/bus", self.remote, phone])
        with open(os.path.join(phone, "runs/hph-10-30-26/lanes.json"), "w") as handle:
            handle.write("[1, 2")
        with open(os.path.join(phone, "runs/hph-10-30-26/job.json"), "w") as handle:
            handle.write('{"lane_model": 7}')
        sh(["git", "add", "-A"], cwd=phone)
        sh(["git", "-c", "user.name=p", "-c", "user.email=p@p", "commit", "-qm", "bad"], cwd=phone)
        sh(["git", "push", "-q", "origin", "HEAD:fast-builder/bus"], cwd=phone)
        self.assertEqual(self.fb("bravo", "listen", "--once").returncode, 0)

    def test_the_no_routine_notice_is_posted_once(self):
        self.register("bravo", routine=None)
        self.publish(accounts=2)
        self.fb("bravo", "listen", "--once")
        self.fb("bravo", "listen", "--once")
        self.assertEqual(len([m for m in self.j("alpha", "inbox", "--run", "hph-10-30-26", "--who", "human") if "no cloud routine" in m["body"]]), 1)

    def test_a_restart_keeps_the_lanes_work(self):
        path = fb.work_clone  # the real function, on a real clone
        os.environ["FB_CACHE_TEST"] = "1"
        cache = os.path.join(self.root, "wc")
        old = fb.CACHE_DIR
        fb.CACHE_DIR = cache
        try:
            clone = path(self.remote, "r", "l", "main")
            sh(["git", "checkout", "-qb", "wip"], cwd=clone)
            with open(os.path.join(clone, "dirty"), "w") as handle:
                handle.write("x")
            self.assertEqual(path(self.remote, "r", "l", "main"), clone)
            self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone).stdout.strip(), "wip")
            self.assertTrue(os.path.exists(os.path.join(clone, "dirty")))
        finally:
            fb.CACHE_DIR = old

    def test_a_clone_with_a_bad_base_leaves_nothing_behind(self):
        old = fb.CACHE_DIR
        fb.CACHE_DIR = os.path.join(self.root, "wc2")
        try:
            with self.assertRaises(fb.FbError):
                fb.work_clone(self.remote, "r", "l", "no-such-base")
            self.assertFalse(os.path.exists(os.path.join(fb.CACHE_DIR, "work")) and glob.glob(os.path.join(fb.CACHE_DIR, "work", "*", "*")))
        finally:
            fb.CACHE_DIR = old

    def test_a_lane_that_cannot_start_does_not_churn_the_bus(self):
        self.register("bravo")
        self.publish(accounts=2, where="local")
        os.remove(self.shim)
        heads = {}
        env = self.env("bravo")
        script = "import fb, sys\nh = {}\ncfg = fb.load_config()['repos']['owner/app']\nfor i in range(3):\n    fb.poll_repo(cfg['remote'], cfg, 'bravo', h)\n"
        sh([sys.executable, "-c", script], cwd=HERE, env=dict(env, PYTHONDONTWRITEBYTECODE="1"))
        count = sh(["git", "log", "--oneline", "--grep=release", "refs/heads/fast-builder/bus"], cwd=self.remote).stdout.splitlines()
        self.assertEqual(len(count), 1, count)
        self.assertEqual(heads, {})

    def test_routine_poll_claims_or_says_nothing(self):
        self.assertEqual(self.fb("bravo", "poll").stdout.strip(), "NOTHING")
        self.publish(accounts=2)
        self.assertIn("lane=core", self.fb("bravo", "poll").stdout)
        self.assertIn("lane=money", self.fb("charlie", "poll").stdout)
        self.assertEqual(self.fb("delta", "poll").stdout.strip(), "NOTHING")
        self.assertIn("python3 \"$FB\" join --run hph-10-30-26 --lane money --account charlie", self.fb("charlie", "prompt", "--run", "hph-10-30-26", "--lane", "money", "--account", "charlie").stdout)

    def test_a_hostile_lane_name_on_the_bus_starts_nothing(self):
        self.publish(accounts=2, where="local")
        phone = os.path.join(self.root, "phone")
        sh(["git", "clone", "-q", "-b", "fast-builder/bus", self.remote, phone])
        path = os.path.join(phone, "runs/hph-10-30-26/lanes.json")
        doc = json.load(open(path))
        doc["lanes"][0]["name"] = "x; rm -rf ~"
        json.dump(doc, open(path, "w"))
        sh(["git", "add", "-A"], cwd=phone)
        sh(["git", "-c", "user.name=p", "-c", "user.email=p@p", "commit", "-qm", "evil"], cwd=phone)
        sh(["git", "push", "-q", "origin", "HEAD:fast-builder/bus"], cwd=phone)
        self.register("bravo")
        sh(["git", "rm", "-q", "--cached", "-r", "kit"], cwd=phone, check=False)
        self.fb("bravo", "listen", "--once", check=False)
        log = open(self.shim_log).read() if os.path.exists(self.shim_log) else ""
        self.assertNotIn("rm -rf", log)


class CloudBootstrap(World):
    def test_prompt_bootstraps_from_nothing_but_git(self):
        """What a cloud session does: no plugin, only a clone and the pasted prompt's commands."""
        self.publish()
        prompt = fb.lane_prompt("hph-10-30-26", "money", "bravo")
        commands = [line for line in prompt.splitlines() if line.startswith(("git ", "mkdir ", "for ", "export ", "python3 "))]
        script = "set -e\n" + "\n".join(commands) + "\npython3 \"$FB\" tick\n"
        repo = self.clone("bravo")
        env = dict(self.env("bravo"))
        env.pop("FB_ACCOUNT")
        proc = sh(["sh", "-c", script], cwd=repo, env=env)
        self.assertIn("JOINED run=hph-10-30-26 lane=money", proc.stdout)
        self.assertIn('"action": "continue"', proc.stdout)
        self.assertEqual(sh(["git", "status", "--porcelain"], cwd=repo).stdout, "")


if __name__ == "__main__":
    unittest.main()
