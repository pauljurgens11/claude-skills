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
import shutil
import sqlite3
import subprocess
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import loot  # noqa: E402
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
        self.github = self.data / "github_prs.json"
        self.loot = self.data / "loot.json"        # chests earned; only grows, survives rescans
        self.actions = self.data / "actions.jsonl"  # what you did in the panel: open, use, equip, redeem  # merged PRs; survives rescans
        self.activity = self.data / "activity.jsonl"  # written by the panel: front app samples
        self.wispr = Path(env("CLAUDE_SKILLS_WISPR_DB")
                          or Path.home() / "Library" / "Application Support" / "Wispr Flow" / "flow.sqlite")
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
        since = cfg.get("xp_since")
        self.since = datetime.fromisoformat(since).timestamp() if since else None
        self.ts = 0.0  # timestamp of the event being counted
        self.boosts = []  # [start, end, multiplier] of XP potions and elixirs you used


    def first_time(self, key):
        if not key:
            return True
        k = key[-16:]
        if k in self.seen:
            return False
        self.seen.add(k)
        return True

    def add(self, day, skill, amount):
        if self.since is not None and self.ts < self.since:
            day = "pre:" + day  # learns your typical day, earns no XP
        amount *= max([m for a, b, m in self.boosts if a <= self.ts < b], default=1)
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

    def matching(self, key):
        for s in self.cfg["skills"]:
            for r in s["rules"]:
                if r.get(key):
                    yield s["id"], r

    def dictated(self, day, words):
        for sid, r in self.matching("wispr_words"):
            self.add(day, sid, words * r.get("weight", 1))

    def app_time(self, day, app, seconds):
        for sid, r in self.matching("app_minutes"):
            if re.search(r["app_minutes"], app):
                self.add(day, sid, seconds / 60 * r.get("weight", 1))

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
    counter.ts = ts
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


def scan_wispr(db, st, counter):
    """Words per dictation from Wispr Flow's own database, read-only; never the text."""
    if not db.exists():
        return
    since = st.get("wispr_since", "")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute(
            "SELECT transcriptEntityId, timestamp, numWords FROM History "
            "WHERE timestamp >= ? AND numWords > 0 AND status != 'no_audio' ORDER BY timestamp", (since,)).fetchall()
    finally:
        con.close()
    for tid, ts, words in rows:
        t = datetime.fromisoformat(ts.replace(" +", "+").replace(" -", "-"))
        counter.ts = t.timestamp()
        if counter.first_time("wispr:" + tid):
            counter.dictated(t.astimezone().strftime("%Y-%m-%d"), words)
    if rows:  # re-read the last day each run: a dictation can be finalised after it is stamped
        last = datetime.fromisoformat(rows[-1][1].replace(" +", "+").replace(" -", "-"))
        st["wispr_since"] = (last.astimezone(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def find_gh():
    extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin"
    return os.environ.get("CLAUDE_SKILLS_GH") or shutil.which("gh", path=os.environ.get("PATH", "") + ":" + extra)


def gh_merged(gh, author, since_day):
    """[(url, merged_at)] for PRs by `author` merged on or after since_day, in weekly windows (search caps at 1000)."""
    out, start = [], datetime.fromisoformat(since_day).date()
    today = datetime.now().date()
    while start <= today:
        end = start + timedelta(days=6)
        res = subprocess.run(
            [gh, "search", "prs", "--author", author, "--merged", "--merged-at", f"{start}..{end}",
             "--limit", "1000", "--json", "url,closedAt"],
            capture_output=True, text=True, timeout=60, check=True)
        out += [(pr["url"], pr["closedAt"]) for pr in json.loads(res.stdout or "[]")]
        start = end + timedelta(days=1)
    return out


def scan_github(st, counter, first_day, cache_path, now=time.time, fetch=gh_merged):
    """Merged pull requests per day. A local commit or a push is not shipped; a merge is.

    Merges live in their own cache file, so a rescan (rules changed) replays them instead of
    spending GitHub's search quota, which your other sessions share. GitHub is asked only for
    the last two days, at most every check_every_minutes, also after a refusal."""
    for sid, r in counter.matching("github_merged_prs"):
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cache = {"checked": 0, "since": first_day, "prs": {}}
        gh = find_gh()
        if gh and now() - cache["checked"] >= r.get("check_every_minutes", 10) * 60:
            cache["checked"] = now()
            try:
                for url, merged_at in fetch(gh, r.get("author", "@me"), cache["since"]):
                    cache["prs"][url] = merged_at
                cache["since"] = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")  # search lags
            finally:
                save_json(cache_path, cache)
        for url, merged_at in cache["prs"].items():
            t = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            counter.ts = t.timestamp()
            if counter.first_time("pr:" + url):
                counter.add(t.astimezone().strftime("%Y-%m-%d"), sid, r.get("weight", 1))


def scan_activity(path, st, counter):
    """Front-app samples the panel appends: {"t": epoch, "app": bundle id, "s": seconds}."""
    if not path.exists():
        return
    fstate = st.setdefault("activity", {})
    size, off = path.stat().st_size, fstate.get("off", 0)
    if size < off:
        off = 0
    with path.open("rb") as fh:
        fh.seek(off)
        chunk = fh.read()
    end = chunk.rfind(b"\n") + 1
    for raw in chunk[:end].splitlines():
        try:
            d = json.loads(raw)
            counter.ts = d["t"]
            day = datetime.fromtimestamp(d["t"]).astimezone().strftime("%Y-%m-%d")
            counter.app_time(day, d["app"], d["s"])
        except (ValueError, KeyError, TypeError):
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
        merged = {}
        for key, bucket in st["days"].items():
            day = merged.setdefault(key.removeprefix("pre:"), {})
            for k, v in bucket.items():
                day[k] = day.get(k, 0) + v
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        done = {k: d for k, d in merged.items() if k != today} or merged  # today is still growing
        vals = [d[sid] for d in done.values() if d.get(sid, 0) > 0]
        default = next((r["default_typical"] for r in s["rules"] if "default_typical" in r), None)
        typical = median(vals) if len(vals) >= need or default is None else default
        st["calib"][sid] = {"typical": typical or 1, "frozen": len(vals) >= need}


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
            if day.startswith("pre:"):
                continue
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
        "curve": table,
        "skills": out,
    }


def run(paths):
    cfg = load_config(paths.config)
    st = load_state(paths, cfg)
    counter = Counter(cfg, st)
    paths.data.mkdir(parents=True, exist_ok=True)
    ledger, actions = loot.load(paths.loot), loot.load_actions(paths.actions)
    counter.boosts = loot.inventory(cfg, ledger, actions, time.time())["boosts"]
    present = set()
    for path in sorted(paths.projects.rglob("*.jsonl")):
        present.add(str(path))
        try:
            scan_file(path, st["files"].setdefault(str(path), {}), counter)
        except OSError:
            continue  # deleted or unreadable mid-scan; picked up next run
    st["files"] = {k: v for k, v in st["files"].items() if k in present}
    try:
        scan_wispr(paths.wispr, st, counter)
    except sqlite3.Error:
        pass  # Wispr Flow busy or changed its schema; try again next run
    scan_activity(paths.activity, st, counter)
    days = sorted(k.removeprefix("pre:") for k in st["days"])
    try:
        scan_github(st, counter, days[0] if days else datetime.now().strftime("%Y-%m-%d"), paths.github)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass  # offline or gh not signed in; try again next run
    calibrate(cfg, st)
    save_json(paths.state, {**st, "seen": sorted(st["seen"])})
    stats = build_stats(cfg, st)
    stats["loot"], stats["streak"] = settle_loot(cfg, st, ledger, actions, stats)
    save_json(paths.loot, ledger)
    save_json(paths.stats, stats)
    return stats


def daily_totals(cfg, st, since_start=False):
    """XP per calendar day across all skills; since_start skips work from before xp_since."""
    merged = {}
    for key, bucket in st["days"].items():
        if since_start and key.startswith("pre:"):
            continue
        day = merged.setdefault(key.removeprefix("pre:"), {})
        for k, v in bucket.items():
            day[k] = day.get(k, 0) + v
    return {day: sum(day_xp(b.get(s["id"], 0), st["calib"][s["id"]]["typical"], cfg["balance"]) for s in cfg["skills"])
            for day, b in merged.items()}


def settle_loot(cfg, st, ledger, actions, stats, now=None):
    now = now or time.time()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    loot_day = cfg.get("loot_since", cfg.get("xp_since", "0000"))[:10]
    totals = {d: x for d, x in daily_totals(cfg, st, since_start=True).items() if d >= loot_day}
    need = cfg.get("streak", {}).get("min_day_xp", 500)
    active = {d for d, x in totals.items() if x >= need}
    held = loot.inventory(cfg, ledger, actions, now)["items"].get(loot.SHIELD, 0)  # shields come from chests, not coins
    if totals:
        loot.settle_streak(ledger, active, min(totals), today, held)
    streak_now = loot.streak(ledger, active, today)
    streak_now["today_xp"], streak_now["need"] = int(totals.get(today, 0)), need
    loot.earn(cfg, ledger, stats["skills"], streak_now, now)
    # coins count XP earned after loot_since: the first run after it records where XP stood
    base = ledger.setdefault("coins_base_xp", stats["total_xp"])
    coins = max(0, stats["total_xp"] - base) * cfg["loot"].get("coins_per_xp", 0)
    inv = loot.inventory(cfg, ledger, actions, now, earned_coins=coins)
    inv["chest_contents"] = {c["id"]: loot.contents(cfg, c["id"], c["tier"]) for c in inv["unopened"]}
    inv["config"] = {k: cfg["loot"][k] for k in ("tiers", "rarities", "drops")}
    inv["shop"] = loot.shop(cfg)
    inv["meter"] = ledger.get("meter", {"have": 0, "need": cfg["loot"]["xp_per_chest"], "upgrade": False})
    inv["earned_coins"] = int(coins)
    return inv, streak_now


def gift(paths, tier, reason):
    """Put a chest in the tray by hand: python3 tracker.py --gift gold "why" """
    ledger = loot.load(paths.loot)
    cid = f"gift-{int(time.time() * 1000)}"
    ledger.setdefault("chests", {})[cid] = {"tier": tier, "source": reason, "t": time.time()}
    save_json(paths.loot, ledger)
    return cid


def main():
    paths = Paths()
    paths.data.mkdir(parents=True, exist_ok=True)
    lock = open(paths.data / "tracker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if "--gift" in sys.argv:  # wait for the panel's run instead of dropping the gift
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            return  # another run is already updating the numbers
    if "--gift" in sys.argv:
        i = sys.argv.index("--gift")
        tier = sys.argv[i + 1] if len(sys.argv) > i + 1 else "gold"
        reason = sys.argv[i + 2] if len(sys.argv) > i + 2 else "A gift"
        print("gifted", gift(paths, tier, reason))
    stats = run(paths)
    if "--print" in sys.argv:
        for s in stats["skills"]:
            print(f'{s["name"]:<12} lv {s["level"]:>2}  xp {s["xp"]:>8}  today {s["today_xp"]:>5} '
                  f'(raw {s["today_raw"]})  typical/day {s["typical_day"]}')
        print("total level", stats["total_level"])


if __name__ == "__main__":
    main()
