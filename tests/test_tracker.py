"""Each test guards a bug that would silently skew the levels. Run: python3 -m unittest discover tests"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tracker  # noqa: E402

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
        self.paths = tracker.Paths(config=ROOT / "skills.json", projects=self.projects, data=self.dir / "data")

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
        self.assertAlmostEqual(raw["prompting"], 2 + len("build me a skills panel") / 400
                               + len("now ship it with ultracode") / 400, places=3)
        self.assertEqual(raw["steering"], 2)  # one mid-turn message + one interrupt
        self.assertEqual(raw["ultracode"], 3 + 1)  # Workflow weight 3 + the ultracode prompt

    def test_specific_rules_win_over_generic_shell(self):
        self.rig.write("p/s1.jsonl", SESSION)
        raw = self.rig.raw()
        self.assertEqual((raw["shipping"], raw["testing"], raw["shell"], raw["scouting"]), (1, 1, 1, 1))

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


if __name__ == "__main__":
    unittest.main()
