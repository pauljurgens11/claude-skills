"""Loot and streak rules. Each test guards a way the game could cheat you or rob you."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import loot  # noqa: E402

CFG = json.loads((ROOT / "skills.json").read_text())


def skill(sid, xp, level=1):
    return {"id": sid, "name": sid, "xp": xp, "level": level}


NO_STREAK = {"days": 0, "start": None}


def first_with(ledger, kind=None, rarity=None):
    for cid, c in ledger["chests"].items():
        d = loot.contents(CFG, cid, c["tier"])["drop"]
        if (kind is None or d["kind"] == kind) and (rarity is None or d["rarity"] == rarity):
            return cid, d
    return None, None


class LootTest(unittest.TestCase):
    def big_ledger(self):
        ledger = {}
        loot.earn(CFG, ledger, [skill("prompting", 2_000_000)], NO_STREAK, now=1)
        return ledger

    def test_chests_never_reroll(self):
        a, b = {}, {}
        loot.earn(CFG, a, [skill("prompting", 50_000)], NO_STREAK, now=1)
        loot.earn(CFG, b, [skill("prompting", 50_000)], NO_STREAK, now=2)
        self.assertEqual(a["chests"].keys(), b["chests"].keys())
        for cid, c in a["chests"].items():
            self.assertEqual(loot.contents(CFG, cid, c["tier"]), loot.contents(CFG, cid, b["chests"][cid]["tier"]))
        before = dict(a["chests"])
        loot.earn(CFG, a, [skill("prompting", 10_000)], NO_STREAK, now=3)  # XP shrank after a rescan
        loot.earn(CFG, a, [skill("prompting", 50_000)], NO_STREAK, now=4)  # and came back
        self.assertEqual(a["chests"], before)  # no new rolls for chunks already rolled

    def test_every_rarity_and_kind_can_actually_drop(self):
        ledger = self.big_ledger()
        seen = {(loot.contents(CFG, cid, c["tier"])["drop"]["rarity"]) for cid, c in ledger["chests"].items()}
        self.assertEqual(seen, {"common", "rare", "ultra", "extraordinary"})

    def test_a_chest_opens_once_and_gold_cannot_be_spent_twice(self):
        ledger = self.big_ledger()
        cid, d = first_with(ledger, kind="gold")
        got = loot.contents(CFG, cid, ledger["chests"][cid]["tier"])
        gold = d["amount"] + got["bonus_gold"]
        inv = loot.inventory(CFG, ledger, [{"action": "open", "chest": cid}] * 3, now=0)
        self.assertEqual(inv["gold"], gold)
        cheapest = min(loot.shop(CFG), key=lambda r: r["cost"])
        n = gold // cheapest["cost"]
        inv = loot.inventory(CFG, ledger, [{"action": "open", "chest": cid}]
                             + [{"action": "redeem", "reward": cheapest["id"]}] * (n + 3), now=0)
        self.assertEqual(len(inv["redeemed"]), n)  # the extra redeems are refused, never negative gold
        self.assertGreaterEqual(inv["gold"], 0)

    def test_coins_buy_chests_not_permissions_and_never_go_negative(self):
        self.assertFalse([r for r in loot.shop(CFG) if r["id"].startswith("v_")])  # permissions only from chests
        wooden = next(r for r in loot.shop(CFG) if r.get("tier") == "wooden")
        acts = [{"action": "redeem", "reward": wooden["id"], "t": 10 + i} for i in range(5)]
        inv = loot.inventory(CFG, {}, acts, now=0, earned_coins=wooden["cost"] * 2 + 1)
        self.assertEqual((len(inv["unopened"]), inv["gold"]), (2, 1))
        bought = inv["unopened"][0]["id"]
        again = loot.inventory(CFG, {}, acts + [{"action": "open", "chest": bought}], now=0, earned_coins=wooden["cost"] * 2 + 1)
        self.assertEqual(again["opened"], [bought])  # a bought chest opens, once, with fixed contents

    def test_vouchers_and_potions_are_used_up(self):
        ledger = self.big_ledger()
        vid_chest, v = first_with(ledger, kind="voucher")
        pot_chest, p = next((c, d) for c, d in (first_with(ledger, kind="item", rarity=r) for r in ("common", "rare"))
                            if d and d["id"] in CFG["loot"]["boosts"])
        acts = [{"action": "open", "chest": vid_chest}, {"action": "claim", "voucher": v["id"]},
                {"action": "claim", "voucher": v["id"]},
                {"action": "open", "chest": pot_chest}, {"action": "use", "item": p["id"], "t": 100},
                {"action": "use", "item": p["id"], "t": 200}]
        inv = loot.inventory(CFG, ledger, acts, now=150)
        self.assertEqual(len(inv["redeemed"]), 1)
        self.assertEqual(len(inv["boosts"]), 1)
        self.assertEqual(inv["boost_active"][0], 100)

    def test_you_can_only_equip_what_you_own_in_its_own_slot(self):
        ledger = self.big_ledger()
        cid, pet = first_with(ledger, kind="pet")
        inv = loot.inventory(CFG, ledger, [{"action": "equip", "slot": "pet", "id": pet["id"]}], now=0)
        self.assertIsNone(inv["equipped"]["pet"])  # not opened yet
        inv = loot.inventory(CFG, ledger, [{"action": "open", "chest": cid},
                                           {"action": "equip", "slot": "aura", "id": pet["id"]},
                                           {"action": "equip", "slot": "pet", "id": pet["id"]}], now=0)
        self.assertEqual(inv["equipped"], {"pet": pet["id"], "aura": None, "title": None})


class StreakTest(unittest.TestCase):
    # Mon 2 Nov 2026 .. Sun 15 Nov 2026
    def days(self, *dd):
        return {f"2026-11-{d:02d}" for d in dd}

    def test_one_free_rest_day_per_week_then_shields_then_it_breaks(self):
        ledger = {}
        active = self.days(2, 4, 6, 8, 9, 10, 12)  # gaps: 3, 5, 7 (week 1) and 11 (week 2)
        loot.settle_streak(ledger, active, "2026-11-02", "2026-11-13", shields_held=1)
        cov = ledger["streak"]["covered"]
        self.assertEqual(cov, {"2026-11-03": "rest", "2026-11-05": "shield", "2026-11-11": "rest"})
        s = loot.streak(ledger, active, "2026-11-13")  # 13th not done yet: count up to the 12th
        self.assertEqual((s["days"], s["start"]), (4, "2026-11-08"))  # day 7 broke it: no rest, no shield left

    def test_a_spent_shield_stays_spent(self):
        ledger = {}
        active = self.days(2, 4, 6)
        loot.settle_streak(ledger, active, "2026-11-02", "2026-11-07", shields_held=1)
        loot.settle_streak(ledger, active, "2026-11-02", "2026-11-07", shields_held=1)  # the panel runs every minute
        spent = [d for d, how in ledger["streak"]["covered"].items() if how == "shield"]
        self.assertEqual(spent, ["2026-11-05"])

    def test_today_without_enough_xp_does_not_break_the_streak_yet(self):
        ledger = {}
        active = self.days(2, 3, 4)
        loot.settle_streak(ledger, active, "2026-11-02", "2026-11-05", shields_held=0)
        s = loot.streak(ledger, active, "2026-11-05")
        self.assertEqual((s["days"], s["today_done"]), (3, False))


if __name__ == "__main__":
    unittest.main()
