"""Chests, gold, items, pets, the real-life shop and streaks.

Everything a chest holds is decided by a hash of its id, so the same work always drops the
same loot and a rescan can never re-roll it. What you have earned lives in loot.json, which
only grows; what you did with it (open, use, equip, redeem) lives in actions.jsonl, written by
the panel. Your inventory is recomputed from both on every run, so nothing can be spent twice.
"""
import hashlib
import json
from datetime import date, timedelta

SHIELD = "shield"


def roll(key):
    """Deterministic number in [0, 1) for a key."""
    return int(hashlib.sha1(key.encode()).hexdigest()[:13], 16) / 16 ** 13


def pick(key, weighted):
    """Pick from [(item, weight)] deterministically."""
    total = sum(w for _, w in weighted)
    x = roll(key) * total
    for item, w in weighted:
        x -= w
        if x < 0:
            return item
    return weighted[-1][0]


# ---- earning chests --------------------------------------------------------------------

def earn(cfg, ledger, skills, streak_now, now):
    """Add chests for new XP chunks, level milestones and streak milestones. Returns new ids."""
    lc = cfg["loot"]
    chests = ledger.setdefault("chests", {})
    new = []

    def add(cid, tier, source):
        if cid in chests:
            chests[cid]["source"] = source if cid.startswith("streak-") else chests[cid]["source"]
        else:
            chests[cid] = {"tier": tier, "source": source, "t": now}
            new.append(cid)

    tiers = [(t["id"], t["weight"]) for t in lc["tiers"]]
    order = [t["id"] for t in lc["tiers"]]
    better = lambda t: order[min(order.index(t) + 1, len(order) - 1)]
    levels = {s["id"]: s["level"] for s in skills}
    last_levels = ledger.setdefault("levels_at_last_chest", dict(levels))
    total = sum(s["xp"] for s in skills)
    per = lc["xp_per_chest"]
    k = ledger.get("meter_chests", 0)
    last = ledger.get("meter_last_xp", k * per)  # XP where the last meter chest dropped
    while total >= last + per:  # counts from the last chest, so changing the rate never jumps back
        k, last = k + 1, last + per
        cid = f"meter-{k}"
        tier, why = pick(cid + ":tier", tiers), f"Your work filled the chest meter ({per:,} XP)"
        ups = [s["name"] for s in skills if levels[s["id"]] > last_levels.get(s["id"], 1)]
        if ups:  # levelled up since the last chest: this one is a tier better
            tier, why = better(tier), f"{why}; upgraded because {', '.join(ups)} levelled up"
            last_levels.update(levels)
        add(cid, tier, why)
    ledger["meter_chests"], ledger["meter_last_xp"] = k, last
    for s in skills:
        for lv in range(lc["level_chest_every"], s["level"] + 1, lc["level_chest_every"]):
            add(f"milestone-{s['id']}-{lv}", better(pick(f"milestone-{s['id']}-{lv}", tiers)),
                f"{s['name']} reached level {lv} (milestone)")
    every, lu = lc["level_chest_every"], lc["level_up_chest"]
    start = ledger.setdefault("levelup_from", {})  # no chests for level-ups before this rule existed
    for s in skills:
        for lv in range(start.setdefault(s["id"], s["level"]) + 1, s["level"] + 1):
            cid = f"levelup-{s['id']}-{lv}"
            if lv % every == 0 or (lv < lu["always_from"] and roll(cid) >= lu["chance_below"]):
                continue  # milestone levels have their own chest; below always_from, a 1-in-3 roll
            add(cid, pick(cid + ":tier", tiers), f"{s['name']} reached level {lv}")
    ledger["meter"] = {"have": int(total - last), "need": per,
                       "upgrade": any(levels[i] > last_levels.get(i, 1) for i in levels)}
    if streak_now["days"]:
        for m in lc["streak_milestones"]:
            if streak_now["days"] >= m:
                add(f"streak-{streak_now['start']}-{m}", "mythic" if m >= 100 else "gold", f"You kept a {m}-day streak")
    return new


def contents(cfg, cid, tier_id):
    """What is inside a chest: one featured drop (rarity by the chest's odds) plus bonus gold.
    A pure function of the chest id, so it can never be re-rolled."""
    lc = cfg["loot"]
    tier = next(t for t in lc["tiers"] if t["id"] == tier_id)
    rarity = pick(cid + ":rarity", [(r["id"], w) for r, w in zip(lc["rarities"], tier["odds"])])
    pool = [d for d in lc["drops"] if d["rarity"] == rarity]
    drop = dict(pick(cid + ":drop", [(d, d["weight"]) for d in pool]))
    if drop["kind"] == "gold":
        lo, hi = drop["gold"]
        drop["amount"] = int(lo + roll(cid + ":amount") * (hi - lo + 1))
    lo, hi = tier["bonus_gold"]
    return {"drop": drop, "bonus_gold": int(lo + roll(cid + ":bonus") * (hi - lo + 1))}


# ---- spending: replay the panel's action log --------------------------------------------

COLLECTIBLE = ("pet", "title")


def shop(cfg):
    """What coins buy: chests, the few treats marked with a price, then big real-life rewards."""
    lc = cfg["loot"]
    chests = [{"id": "chest:" + t["id"], "name": t["name"], "icon": "🎁", "cost": lc["chest_prices"][t["id"]],
               "kind": "chest", "tier": t["id"]} for t in lc["tiers"] if t["id"] in lc.get("chest_prices", {})]
    treats = [{"id": d["id"], "name": d["name"], "icon": d["icon"], "cost": d["price"], "kind": "treat"}
              for d in lc["drops"] if "price" in d]
    return chests + treats + [dict(r, kind="reward") for r in cfg.get("rewards", [])]


def inventory(cfg, ledger, actions, now, earned_coins=0):
    """Replay actions in order against what was earned. Invalid actions are ignored.
    Passive coins only ever grow, so counting today's total up front never validates a
    purchase the panel would have refused at the time."""
    lc = cfg["loot"]
    rewards = {r["id"]: r for r in shop(cfg)}
    dup_gold = {r["id"]: r["duplicate_gold"] for r in lc["rarities"]}
    drops = {d["id"]: d for d in lc["drops"]}
    inv = {"gold": int(earned_coins), "items": {}, "owned": [], "equipped": {"pet": None, "title": None},
           "vouchers": {}, "opened": [], "boosts": [], "redeemed": []}
    chests = dict(ledger.get("chests", {}))  # plus chests bought below
    for a in actions:
        kind, t = a.get("action"), a.get("t", 0)
        if kind == "open" and a.get("chest") in chests and a["chest"] not in inv["opened"]:
            inv["opened"].append(a["chest"])
            got = contents(cfg, a["chest"], chests[a["chest"]]["tier"])
            d = got["drop"]
            inv["gold"] += got["bonus_gold"]
            if d["kind"] == "gold":
                inv["gold"] += d["amount"]
            elif d["kind"] == "item":
                inv["items"][d["id"]] = inv["items"].get(d["id"], 0) + 1
            elif d["kind"] == "voucher":
                inv["vouchers"][d["id"]] = inv["vouchers"].get(d["id"], 0) + 1
            elif d["id"] in inv["owned"]:
                inv["gold"] += dup_gold[d["rarity"]]  # a collectible you already own turns into gold
            else:
                inv["owned"].append(d["id"])
        elif kind == "use" and a.get("item") in lc["boosts"] and inv["items"].get(a["item"], 0) > 0:
            b = lc["boosts"][a["item"]]
            inv["items"][a["item"]] -= 1
            inv["boosts"].append([t, t + b["minutes"] * 60, b["multiplier"]])
        elif kind == "equip" and a.get("slot") in inv["equipped"] and (
                a.get("id") is None or (a["id"] in inv["owned"] and drops[a["id"]]["kind"] == a["slot"])):
            inv["equipped"][a["slot"]] = a.get("id")
        elif kind == "claim" and inv["vouchers"].get(a.get("voucher"), 0) > 0:
            inv["vouchers"][a["voucher"]] -= 1
            inv["redeemed"].append({"name": drops[a["voucher"]]["name"], "t": t, "free": True})
        elif kind == "adjust" and isinstance(a.get("coins"), (int, float)):  # a manual correction
            inv["gold"] = max(0, inv["gold"] + int(a["coins"]))
        elif kind == "redeem" and a.get("reward") in rewards and inv["gold"] >= rewards[a["reward"]]["cost"]:
            r = rewards[a["reward"]]
            inv["gold"] -= r["cost"]
            if r["kind"] == "chest":  # a bought chest; its id is fixed at purchase so it cannot be re-rolled
                chests[f"bought-{int(t * 1000)}"] = {"tier": r["tier"], "source": "Bought with coins", "t": t}
            else:
                inv["redeemed"].append({"name": r["name"], "t": t, "free": False})
    spent = len([1 for how in ledger.get("streak", {}).get("covered", {}).values() if how == "shield"])
    inv["items"]["shield"] = inv["items"].get("shield", 0) - spent
    inv["unopened"] = [{"id": cid, **c} for cid, c in sorted(chests.items(), key=lambda kv: kv[1]["t"])
                       if cid not in inv["opened"]]
    active = [b for b in inv["boosts"] if b[0] <= now < b[1]]
    inv["boost_active"] = max(active, key=lambda b: b[2]) if active else None
    return inv


# ---- streaks -----------------------------------------------------------------------------

def _d(s):
    return date.fromisoformat(s)


def settle_streak(ledger, active_days, first_day, today, shields_held):
    """Decide, once and for good, how each finished day without enough XP is covered.

    One rest day per ISO week is free. After that a held Streak Shield is spent. Otherwise
    the day stays uncovered and the streak breaks there. Each finished day is settled once,
    so a shield spent stays spent."""
    st = ledger.setdefault("streak", {"settled_until": None, "covered": {}})
    covered = st["covered"]
    day = _d(st["settled_until"]) + timedelta(days=1) if st["settled_until"] else _d(first_day)
    end = _d(today) - timedelta(days=1)
    while day <= end:
        key = day.isoformat()
        if key not in active_days:
            week = day.isocalendar()[:2]
            rest_used = any(how == "rest" and _d(d).isocalendar()[:2] == week for d, how in covered.items())
            if not rest_used:
                covered[key] = "rest"
            elif shields_held > 0:
                covered[key] = "shield"
                shields_held -= 1
        day += timedelta(days=1)
    if end >= _d(first_day):
        st["settled_until"] = end.isoformat()


def streak(ledger, active_days, today):
    """Current streak: active days back from today (or yesterday, if today is not done yet)."""
    covered = ledger.get("streak", {}).get("covered", {})
    day = _d(today)
    today_done = today in active_days
    if not today_done:
        day -= timedelta(days=1)
    count, start = 0, None
    while day.isoformat() in active_days or day.isoformat() in covered:
        if day.isoformat() in active_days:
            count += 1
            start = day.isoformat()
        day -= timedelta(days=1)
    return {"days": count, "start": start, "today_done": today_done}


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def load_actions(path):
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out
