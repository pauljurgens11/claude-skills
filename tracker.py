#!/usr/bin/env python3
"""Turn Claude Code transcripts into skill XP.

Reads <projects>/**/*.jsonl incrementally (byte offsets), counts raw events per
local day per skill with the rules in skills.json, and writes stats.json.

What counts as YOU: user lines and mid-turn queued messages whose
origin.kind == "human" (older transcripts without an origin fall back to a text
check). Everything else (tool calls, agent time) is your agents' work.

Forked and resumed sessions copy history into new files, so every event is
deduplicated by its id (tool_use id, message uuid, queued source_uuid).

Balance: each skill is calibrated so a typical active day of it is worth the
same XP; past grind_soft_cap typical days the gain is logarithmic, and one day
can never be worth more than max_day_multiple typical days.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = Path.home() / "Library" / "Application Support" / "ClaudeSkills"

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
PASTED_TAG = re.compile(r"</?pasted_content[^>]*>")
NOT_HUMAN = (
    "<task-notification", "<side-sessions-event", "<ci-monitor-event", "<local-command",
    "<cross-session-message", "<agent-message", "Another Claude session", "Caveat:",
    "The app was quit", "Continue from where you left off", "[Image: source:",
    "Background (from the session that started this one)",
)
INTERRUPT = "[Request interrupted by user"


class Paths:
    def __init__(self, config=None, projects=None, data=None):
        env = os.environ.get
        self.config = Path(config or env("CLAUDE_SKILLS_CONFIG") or HERE / "skills.json")
        self.projects = Path(projects or env("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
        self.data = Path(data or env("CLAUDE_SKILLS_DATA") or DEFAULT_DATA)
        self.state = self.data / "state.json"
        self.stats = self.data / "stats.json"


def load_config(path):
    raw = path.read_bytes()
    cfg = json.loads(raw)
    # rules or counting logic changed -> rescan everything
    cfg["_hash"] = hashlib.sha1(raw + Path(__file__).read_bytes()).hexdigest()
    for s in cfg["skills"]:
        for r in s["rules"]:
            for key in ("tool", "bash", "contains"):
                if key in r:
                    r["_" + key] = re.compile(r[key])
            if r.get("prompt") is True:
                r["prompt"] = ["turn", "midturn", "interrupt"]
    return cfg


def load_state(paths, cfg):
    try:
        st = json.loads(paths.state.read_text())
        if st.get("config_hash") == cfg["_hash"]:
            st["seen"] = set(st["seen"])
            return st
    except (OSError, ValueError, KeyError):
        pass
    return {"config_hash": cfg["_hash"], "files": {}, "days": {}, "calib": {}, "seen": set()}


def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def local_day(ts):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t.astimezone().strftime("%Y-%m-%d"), t.timestamp()


def human_text(text):
    """Strip injected reminders; what is left is what you sent."""
    return PASTED_TAG.sub("", REMINDER.sub("", text)).strip()


def classify_user_line(d):
    """Return (kind, text) for a line you wrote, else None. kind: turn | interrupt."""
    if d.get("isSidechain") or d.get("isMeta"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        has_image = any(isinstance(b, dict) and b.get("type") == "image" for b in c)
        raw = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    elif isinstance(c, str):
        has_image, raw = False, c
    else:
        return None
    text = human_text(raw)
    if text.startswith(INTERRUPT):
        return ("interrupt", "")
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    if not (text or has_image) or text.startswith(NOT_HUMAN):
        return None
    return ("turn", text)


def classify_queued(d):
    """Mid-turn messages you typed while the agent was working."""
    a = d.get("attachment") or {}
    if a.get("type") != "queued_command" or d.get("isSidechain"):
        return None
    if (a.get("origin") or {}).get("kind") != "human" or not isinstance(a.get("prompt"), str):
        return None
    return a.get("source_uuid") or d.get("uuid"), human_text(a["prompt"])


class Counter:
    def __init__(self, cfg, st):
        self.cfg, self.days, self.seen = cfg, st["days"], st["seen"]

    def first_time(self, key):
        if not key:
            return True
        k = key[-16:]
        if k in self.seen:
            return False
        self.seen.add(k)
        return True

    def add(self, day, skill, amount):
        if amount > 0:
            bucket = self.days.setdefault(day, {})
            bucket[skill] = round(bucket.get(skill, 0) + amount, 4)

    def prompt(self, day, kind, text):
        for s in self.cfg["skills"]:
            for r in s["rules"]:
                if kind not in (r.get("prompt") or ()):
                    continue
                if "_contains" in r and not r["_contains"].search(text):
                    continue
                bonus = 0
                if r.get("chars_bonus_per"):
                    bonus = min(len(text) / r["chars_bonus_per"], r.get("chars_bonus_max", 0))
                self.add(day, s["id"], r.get("weight", 1) + bonus)
                break

    def tool(self, day, name, inp):
        cmd = inp.get("command", "") if name == "Bash" and isinstance(inp, dict) else None
        for s in self.cfg["skills"]:
            for r in s["rules"]:
                if ("_tool" in r and r["_tool"].search(name)) or (
                    "_bash" in r and cmd is not None and r["_bash"].search(cmd)
                ):
                    return self.add(day, s["id"], r.get("weight", 1))

    def busy(self, day, seconds):
        for s in self.cfg["skills"]:
            for r in s["rules"]:
                if r.get("afk_minutes"):
                    self.add(day, s["id"], min(seconds, r.get("gap_cap_seconds", 120)) / 60)


def handle(d, fstate, counter):
    if not d.get("timestamp"):
        return
    typ = d.get("type")
    day, ts = local_day(d["timestamp"])
    if typ == "attachment":
        q = classify_queued(d)
        if q and counter.first_time(q[0]):
            counter.prompt(day, "midturn", q[1])
        return
    if typ == "user":
        who = classify_user_line(d)
        if who:
            if counter.first_time(d.get("uuid")):
                counter.prompt(day, *who)
            fstate["prev_ts"] = ts  # your typing time is not agent time
            return
    # agent time: main-thread events only (subagents run in parallel), each line once
    if not d.get("isSidechain"):
        prev = fstate.get("prev_ts")
        if prev is not None and ts > prev and counter.first_time(d.get("uuid")):
            counter.busy(day, ts - prev)
        fstate["prev_ts"] = ts
    if typ == "assistant":
        for b in (d.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and counter.first_time(b.get("id")):
                counter.tool(day, b.get("name", ""), b.get("input") or {})


MARKERS = (b'"type":"user"', b'"type":"assistant"', b'"queued_command"')


def scan_file(path, fstate, counter):
    size = path.stat().st_size
    off = fstate.get("off", 0)
    if size < off:
        off, fstate["prev_ts"] = 0, None
    if size == off:
        return
    with path.open("rb") as fh:
        fh.seek(off)
        chunk = fh.read()
    end = chunk.rfind(b"\n") + 1
    for raw in chunk[:end].splitlines():
        if not any(m in raw for m in MARKERS):
            continue
        try:
            handle(json.loads(raw), fstate, counter)
        except (ValueError, TypeError, AttributeError):
            continue
    fstate["off"] = off + end


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] + xs[(n - 1) // 2]) / 2 if n else 0


def calibrate(cfg, st):
    """Freeze each skill's typical active day once it has enough history."""
    need = cfg["balance"]["calibration_min_days"]
    for s in cfg["skills"]:
        sid = s["id"]
        if (st["calib"].get(sid) or {}).get("frozen"):
            continue
        vals = [d[sid] for d in st["days"].values() if d.get(sid, 0) > 0]
        st["calib"][sid] = {"typical": median(vals) or 1, "frozen": len(vals) >= need}


def day_xp(raw, typical, bal):
    r = raw / typical
    cap = bal["grind_soft_cap"]
    if r > cap:
        r = cap + math.log1p(r - cap)
    return bal["xp_per_typical_day"] * min(r, bal["max_day_multiple"])


def curve(cfg):
    c = cfg["curve"]
    base = c["xp_at_max"] / (c["ratio"] ** (c["max_level"] - 1) - 1)
    return [0] + [round(base * (c["ratio"] ** (lv - 1) - 1)) for lv in range(1, c["max_level"] + 1)]


def level_for(xp, table):
    lv = 1
    while lv + 1 < len(table) and xp >= table[lv + 1]:
        lv += 1
    return lv


def build_stats(cfg, st):
    table, bal = curve(cfg), cfg["balance"]
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    out = []
    for s in cfg["skills"]:
        sid, typical = s["id"], st["calib"][s["id"]]["typical"]
        xp = today_xp = 0.0
        for day, bucket in st["days"].items():
            gained = day_xp(bucket.get(sid, 0), typical, bal)
            xp += gained
            if day == today:
                today_xp = gained
        lv = level_for(xp, table)
        out.append({
            "id": sid, "name": s["name"], "icon": s["icon"], "group": s.get("group", ""),
            "about": s.get("about", ""), "xp": int(xp), "level": lv, "level_xp": table[lv],
            "next_xp": table[lv + 1] if lv + 1 < len(table) else None,
            "today_xp": int(today_xp), "today_raw": round(st["days"].get(today, {}).get(sid, 0), 1),
            "typical_day": round(typical, 2),
        })
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "max_level": cfg["curve"]["max_level"],
        "total_level": sum(s["level"] for s in out),
        "total_xp": sum(s["xp"] for s in out),
        "skills": out,
    }


def run(paths):
    cfg = load_config(paths.config)
    st = load_state(paths, cfg)
    counter = Counter(cfg, st)
    present = set()
    for path in sorted(paths.projects.rglob("*.jsonl")):
        present.add(str(path))
        try:
            scan_file(path, st["files"].setdefault(str(path), {}), counter)
        except OSError:
            continue  # deleted or unreadable mid-scan; picked up next run
    st["files"] = {k: v for k, v in st["files"].items() if k in present}
    calibrate(cfg, st)
    paths.data.mkdir(parents=True, exist_ok=True)
    save_json(paths.state, {**st, "seen": sorted(st["seen"])})
    stats = build_stats(cfg, st)
    save_json(paths.stats, stats)
    return stats


def main():
    paths = Paths()
    paths.data.mkdir(parents=True, exist_ok=True)
    lock = open(paths.data / "tracker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another run is already updating the numbers
    stats = run(paths)
    if "--print" in sys.argv:
        for s in stats["skills"]:
            print(f'{s["name"]:<12} lv {s["level"]:>2}  xp {s["xp"]:>8}  today {s["today_xp"]:>5} '
                  f'(raw {s["today_raw"]})  typical/day {s["typical_day"]}')
        print("total level", stats["total_level"])


if __name__ == "__main__":
    main()
