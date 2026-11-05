"""Each test guards a bug that would silently skew the levels. Run: python3 -m unittest discover tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tracker  # noqa: E402

os.environ["CLAUDE_SKILLS_GH"] = "/nonexistent/gh"  # tests never call the real GitHub

TS = "2026-11-05T10:00:{:02d}.000Z"


def line(i, **kw):
    base = {"timestamp": TS.format(i), "uuid": f"uuid-{kw.pop('uid', i):04d}-xxxxxxxxxxxx", "isSidechain": False}
    base.update(kw)
    return json.dumps(base, separators=(",", ":"))


def user(i, content, origin="human", **kw):
    o = {"kind": origin} if origin else None
    return line(i, type="user", origin=o, message={"role": "user", "content": content}, **kw)


def tool(i, name, inp=None, tid=None, uid=None):
    block = {"type": "tool_use", "id": tid or f"toolu_{i:020d}", "name": name, "input": inp or {}}
    return line(i, type="assistant", message={"content": [block]}, uid=i if uid is None else uid)


def queued(i, prompt, origin="human"):
    return line(i, type="attachment", attachment={
        "type": "queued_command", "prompt": prompt, "source_uuid": f"src-{i:04d}-xxxxxxxxxxxxxx",
        "origin": {"kind": origin}})


SESSION = [
    # the real first prompt arrives behind an injected reminder; it must still count
    user(0, [{"type": "text", "text": "<system-reminder>\nworktree\n</system-reminder>"},
             {"type": "text", "text": "build me a skills panel"}]),
    tool(1, "Bash", {"command": "git commit -m x"}),
    tool(2, "Bash", {"command": "python3 -m pytest tests/test_x.py"}),
    tool(3, "Bash", {"command": "ls"}),
    tool(4, "Workflow"),
    queued(5, "make it customizable"),                       # you, mid-turn
    queued(6, "<task-notification>done</task-notification>", origin="task-notification"),
    user(7, "<task-notification>agent finished</task-notification>", origin="task-notification"),
    user(8, "Another Claude session sent a message: hi", origin="peer"),
    user(9, [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}], origin=None),
    user(10, [{"type": "text", "text": "[Request interrupted by user]"}], origin=None),
    tool(11, "Read"),
    user(12, "now ship it with ultracode"),
    user(13, "Background (from the session that started this one):\nbrief"),  # agent-written brief
]


class Rig:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.projects = self.dir / "projects"
        self.projects.mkdir()
        cfg = json.loads((ROOT / "skills.json").read_text())
        cfg.pop("xp_since", None)  # fixtures are dated 2026-11-05; count all of them
        (self.dir / "skills.json").write_text(json.dumps(cfg))
        self.paths = tracker.Paths(config=self.dir / "skills.json", projects=self.projects, data=self.dir / "data")
        self.paths.wispr = self.dir / "flow.sqlite"  # never the real one

    def write(self, rel, lines, mode="w"):
        p = self.projects / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode) as fh:
            fh.write("".join(x + "\n" for x in lines))
        return p

    def raw(self):
        tracker.run(self.paths)
        st = json.loads(self.paths.state.read_text())
        return st["days"].get("2026-11-05", {})

    def close(self):
        shutil.rmtree(self.dir)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()

    def tearDown(self):
        self.rig.close()

    def test_only_your_messages_count_as_you(self):
        self.rig.write("p/s1.jsonl", SESSION)
        raw = self.rig.raw()
        # two real turns (+ length bonus), not the notification / peer / tool_result lines
        # two new prompts + one mid-turn message (+ length bonus) + an interrupt at half weight;
        # not the notification / peer / tool_result / agent-brief lines
        self.assertAlmostEqual(raw["prompting"], 3 + 0.5 + (len("build me a skills panel")
                               + len("now ship it with ultracode") + len("make it customizable")) / 400, places=3)

    def test_tools_land_in_the_right_skill_and_the_rest_earn_nothing(self):
        self.rig.write("p/s1.jsonl", SESSION)
        raw = self.rig.raw()
        # a local `git commit` (or a script that merely mentions it) is not shipping
        self.assertEqual(set(raw), {"prompting", "afk"})

    def test_forked_session_copy_counts_once(self):
        self.rig.write("p/s1.jsonl", SESSION)
        once = self.rig.raw()
        fresh = Rig()
        try:
            fresh.write("p/s1.jsonl", SESSION)
            fresh.write("q/fork-of-s1.jsonl", SESSION)  # resume/fork copies history verbatim
            self.assertEqual(fresh.raw(), once)
        finally:
            fresh.close()

    def test_incremental_scan_with_half_written_line_matches_full_scan(self):
        full = Rig()
        try:
            full.write("p/s1.jsonl", SESSION)
            expected = full.raw()
        finally:
            full.close()
        p = self.rig.write("p/s1.jsonl", SESSION[:6])
        with p.open("a") as fh:
            fh.write(SESSION[6][:25])  # Claude Code is mid-write when we scan
        self.rig.raw()
        with p.open("a") as fh:
            fh.write(SESSION[6][25:] + "\n" + "".join(x + "\n" for x in SESSION[7:]))
        self.assertEqual(self.rig.raw(), expected)

    def test_agent_time_skips_idle_gaps_and_your_typing(self):
        lines = [
            user(0, "go"),
            tool(30, "Read"),            # 30s of agent work
            tool(59, "Read"),            # +29s
            user(0, "next", uid=99).replace(TS.format(0), "2026-11-05T11:00:00.000Z"),  # an hour later: you
            tool(0, "Read", tid="toolu_late", uid=100).replace(TS.format(0), "2026-11-05T11:10:00.000Z"),  # gap capped at 120s
        ]
        self.rig.write("p/s1.jsonl", lines)
        self.assertAlmostEqual(self.rig.raw()["afk"], (30 + 29 + 120) / 60, places=3)

    def test_grinding_one_day_cannot_outweigh_regular_days(self):
        bal = json.loads((ROOT / "skills.json").read_text())["balance"]
        self.assertEqual(tracker.day_xp(10, 10, bal), bal["xp_per_typical_day"])
        self.assertLessEqual(tracker.day_xp(10_000, 10, bal), bal["max_day_multiple"] * bal["xp_per_typical_day"])
        self.assertGreater(tracker.day_xp(40, 10, bal), tracker.day_xp(30, 10, bal))  # more still helps a bit


    def test_work_before_the_start_date_earns_no_xp_but_still_calibrates(self):
        cfg = json.loads(self.rig.paths.config.read_text())
        cfg["xp_since"] = "2026-11-05T10:00:03+00:00"  # after the first prompt (0s), before the mid-turn message (5s)
        self.rig.paths.config.write_text(json.dumps(cfg))
        self.rig.write("p/s1.jsonl", SESSION)
        stats = tracker.run(self.rig.paths)
        st = json.loads(self.rig.paths.state.read_text())
        # the first prompt is before the start; the mid-turn (5s), interrupt (10s) and second prompt (12s) after
        self.assertAlmostEqual(st["days"]["pre:2026-11-05"]["prompting"], 1 + len("build me a skills panel") / 400, places=3)
        self.assertAlmostEqual(st["days"]["2026-11-05"]["prompting"], 2 + 0.5 + (len("make it customizable")
                               + len("now ship it with ultracode")) / 400, places=3)
        typical = st["calib"]["prompting"]["typical"]  # learned from the whole day, both sides
        self.assertAlmostEqual(typical, st["days"]["pre:2026-11-05"]["prompting"] + st["days"]["2026-11-05"]["prompting"], places=3)
        bal = cfg["balance"]
        xp = next(s for s in stats["skills"] if s["id"] == "prompting")["xp"]
        self.assertEqual(xp, int(tracker.day_xp(st["days"]["2026-11-05"]["prompting"], typical, bal)))  # post-start only

    def test_wispr_words_count_once_even_when_reread(self):
        import sqlite3
        con = sqlite3.connect(self.rig.paths.wispr)
        con.execute("CREATE TABLE History (transcriptEntityId TEXT, timestamp TEXT, numWords INT, status TEXT, formattedText TEXT)")
        con.executemany("INSERT INTO History VALUES (?,?,?,?,?)", [
            ("a", "2026-11-05 10:00:00.000 +00:00", 40, "formatted", "secret words"),
            ("b", "2026-11-05 10:05:00.000 +00:00", 0, "raw_transcript", ""),      # still being dictated
            ("c", "2026-11-05 10:06:00.000 +00:00", 12, "no_audio", ""),
        ])
        con.commit()
        self.assertEqual(self.rig.raw()["wispr"], 40)
        con.execute("UPDATE History SET numWords = 25, status = 'formatted' WHERE transcriptEntityId = 'b'")  # finalised later
        con.commit()
        con.close()
        self.assertEqual(self.rig.raw()["wispr"], 65)
        self.assertEqual(self.rig.raw()["wispr"], 65)  # third run: nothing new, nothing doubled

    def test_messaging_counts_only_slack_and_whatsapp_time(self):
        self.rig.paths.data.mkdir(parents=True, exist_ok=True)
        t = 1793872800  # 2026-11-05 10:00 UTC
        samples = [("com.tinyspeck.slackmacgap", 15)] * 4 + [("net.whatsapp.WhatsApp", 15)] * 2 + [("com.apple.Safari", 15)] * 8
        with self.rig.paths.activity.open("w") as fh:
            for i, (app, sec) in enumerate(samples):
                fh.write(json.dumps({"t": t + 15 * i, "app": app, "s": sec}) + "\n")
        self.assertAlmostEqual(self.rig.raw()["messaging"], 1.5)  # 6 samples x 15s

    def test_shipping_counts_each_merged_pr_once_across_rereads(self):
        cfg = tracker.load_config(self.rig.paths.config)
        st = {"days": {}, "seen": set()}
        counter = tracker.Counter(cfg, st)
        merged = [("https://github.com/o/r/pull/1", "2026-11-05T10:00:00Z"),
                  ("https://github.com/o/r/pull/2", "2026-11-05T11:00:00Z")]
        clock = [1_000_000.0]
        run = lambda: tracker.scan_github(st, counter, "2026-11-01", now=lambda: clock[0], fetch=lambda gh, a, since: merged)
        os.environ["CLAUDE_SKILLS_GH"] = "/usr/bin/true"
        try:
            run()
            self.assertEqual(st["days"]["2026-11-05"]["shipping"], 2)
            clock[0] += 60
            merged.append(("https://github.com/o/r/pull/3", "2026-11-05T12:00:00Z"))
            run()  # within 10 minutes: GitHub is not asked again
            self.assertEqual(st["days"]["2026-11-05"]["shipping"], 2)
            clock[0] += 600
            run()  # re-read returns 1 and 2 again plus the new 3
            self.assertEqual(st["days"]["2026-11-05"]["shipping"], 3)
        finally:
            os.environ["CLAUDE_SKILLS_GH"] = "/nonexistent/gh"

    def test_rate_limited_github_is_not_hammered(self):
        cfg = tracker.load_config(self.rig.paths.config)
        st, calls, clock = {"days": {}, "seen": set()}, [], [1_000_000.0]
        def refuse(gh, author, since):
            calls.append(since)
            raise subprocess.CalledProcessError(1, "gh", "HTTP 403: secondary rate limit")
        counter = tracker.Counter(cfg, st)
        os.environ["CLAUDE_SKILLS_GH"] = "/usr/bin/true"
        try:
            for _ in range(5):  # five panel refreshes, a minute apart
                try:
                    tracker.scan_github(st, counter, "2026-11-01", now=lambda: clock[0], fetch=refuse)
                except subprocess.CalledProcessError:
                    pass
                clock[0] += 60
            self.assertEqual(len(calls), 1)
        finally:
            os.environ["CLAUDE_SKILLS_GH"] = "/nonexistent/gh"

    def test_a_year_of_maxed_out_days_reaches_the_low_90s_not_99(self):
        cfg = json.loads((ROOT / "skills.json").read_text())
        table = tracker.curve(cfg)
        self.assertIn(tracker.level_for(365 * 3000, table), (91, 92, 93))
        self.assertLess(tracker.level_for(365 * 1000, table), 85)  # a normal year is well short
        self.assertLess(tracker.level_for(2 * 365 * 3000, table), 99 + 1)


if __name__ == "__main__":
    unittest.main()
