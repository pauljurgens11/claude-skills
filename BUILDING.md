# How Prompt Quest works and how it was built

Read this before you build a variation. The README covers using it. This file covers the
pieces, the decisions behind them, and where to change things.

## The pieces

```
~/.claude/projects/**/*.jsonl ─┐
Wispr Flow sqlite (read-only) ─┤
gh search prs (merged, @me) ───┼─> tracker.py ──> stats.json ──> panel (Swift + WebView)
activity.jsonl (front app) ────┘        │                           │
                                     loot.py <── actions.jsonl <────┘ (your clicks)
```

| File | Lines | Job |
|---|---|---|
| `tracker.py` | ~540 | Reads every source, counts raw events per day per skill, turns them into XP and levels, settles chests and streaks, writes `stats.json`. Runs once a minute. |
| `loot.py` | ~220 | Pure functions: which chests you earned, what is inside each one, the shop, your inventory and the streak. No I/O except two loaders. |
| `skills.json` | ~360 | All the tuning: skills and their rules, balance, curve, chest odds, drop table, shop prices. |
| `panel/main.swift` | ~200 | A borderless, always-on-top, non-activating macOS panel that hosts a WebView. Runs the tracker every 60 s, samples the front app every 15 s, makes the window jump on level-ups. |
| `panel/index.html` | ~600 | The skills grid, hover info box, level-up takeover, sounds. |
| `panel/loot.js` | ~290 | Chest tray, unboxing reel, bag (items, collection, shop). |
| `install.sh` | ~50 | Compiles the Swift file with `swiftc`, runs a first scan, installs a LaunchAgent (start at login, restart if it dies). |
| `tests/` | 22 tests | The rules that would cheat or rob you if they broke. |

No dependencies: Python standard library, AppKit, WebKit and plain JS. No server, no network
except the optional `gh` call.

## How each skill is measured

The hard problem was separating what **you** do from what your agents do.

- **Prompting (you).** A user line in a transcript counts only if Claude Code marks it
  `origin.kind == "human"`. That excludes tool results, task notifications, hook output and
  messages from other sessions, which also arrive as "user" lines. Since Claude Code
  2.1.255 every typed line carries the marker, so an unmarked line from those versions is
  the app's own (resume summaries, slash commands, session handoffs) and never counts. Messages typed while the
  agent works arrive as `queued_command` attachments, so those count too. Interrupts count
  half. Longer prompts earn a small bonus.
- **AFK Coding (agents).** The time between consecutive transcript events while a turn is
  running, summed across sessions. A gap after an assistant message with stop_reason
  end_turn is waiting, not work, and counts 0 whatever wakes the agent next (a notification,
  another session). A gap inside a turn (a long build, a subagent) counts in full, capped at
  30 minutes for a laptop that slept mid-turn.
- **Wispr Flow (you).** Word counts from Wispr's local sqlite, opened read-only. The text
  is never read.
- **Messaging (you).** The Swift panel writes the front app to `activity.jsonl` every 15 s,
  only when the keyboard or mouse was used in the last 60 s. Time with Slack or WhatsApp in
  front counts.
- **Shipping (outcome).** Pull requests GitHub merged with you as author, via
  `gh search prs --author @me --merged`. Checked every 10 minutes and cached, so a rescan
  never loses them. Counting commits or `git push` in transcripts was tried first and was
  wrong (agents push all day; a push is not shipping).

Correctness details that matter:

- Transcripts are read incrementally by byte offset, so a minute's run costs almost nothing.
- Forked and resumed sessions copy old history into new files. Every event is deduplicated
  by its id (tool_use id, message uuid), or a resumed session would double your XP.
- A file lock stops two tracker runs from overlapping.
- Any change to `skills.json` or `tracker.py` changes a hash, which triggers a full rescan.

## Balance: why it is fair

Raw counts differ wildly (thousands of dictated words vs a handful of merged PRs), so each
skill calibrates to **your** normal day:

1. Take the median raw amount over your finished active days for that skill (never today:
   today is still growing, and working more today must not raise the bar for past days). Freeze it after 5 days
   so it stops chasing you.
2. A normal day is worth 1,000 XP in every skill.
3. Past 2 normal days, extra work counts logarithmically. One day is capped at 12 normal days.
4. Levels follow a geometric curve (each level costs 7.8% more). Tuned so a third of a normal
   day reaches level 4, one normal day level 9, and a year of maxed-out days level 92.

`xp_since` in `skills.json` is when everyone starts at level 1. Older history still teaches
the calibration. **Change it to your own start time** (or delete it to count all history).
Same for `loot_since`, which starts coins and the streak.

## Chests and loot: why nothing can cheat

- **Deterministic.** A chest's id is something like `xp:12` (the 12th 2,500 XP chunk). Its tier
  and contents come from a sha1 of that id. The same work always gives the same chests, and
  a rescan cannot reroll them.
- **Ledger only grows.** Earned chests are written to `loot.json` once. If XP briefly drops
  after a rescan, no chest disappears and none is minted twice.
- **Inventory is replayed.** Your clicks (open, use, equip, buy, claim) are appended to
  `actions.jsonl`. The inventory is rebuilt from the ledger plus that log every time, so a
  chest opens once, a voucher is used once, and coins never go negative.
- **Rates:** one chest per 2,500 XP (about 2 on a normal day), the same at every level. A
  level-up upgrades the next chest one tier. Milestone chests come every 10th level and at
  streak days 7/30/100/200/365. From level 15 every level-up gives a chest; below 15 a
  level-up has a 1 in 3 chance (a roll of the chest id). Levels you had before this rule
  existed give nothing.
- **Coins:** 1 per 50 XP plus coin drops. They buy chests and real-life rewards in the shop.
  Real-life permissions (look out the window, a cookie) only come from chests.
- **Streak:** a day counts at 500 XP. One missed day per week is free; Streak Shields cover
  more, and a spent shield stays spent.

## The panel

- `NSPanel` with `.nonactivatingPanel`, `.floating` level and `canJoinAllSpaces`: it stays on
  top on every desktop and never steals focus from what you are typing in.
- The UI is HTML in a `WKWebView`; Swift pushes `stats.json` in and receives clicks back
  through a script message handler. That split kept iteration fast: UI changes need no
  rebuild, only a reload.
- Level-up: the page asks Swift to hop the window (a 60 fps parabola: 34, 20, 9 px), plays a
  synthesized fanfare (Web Audio, no sound files), and covers the panel for 6 seconds.
- It keeps itself on screen after display changes, and kills a tracker run that hangs over 5
  minutes.

## How it was built

Built with Claude Code in two sessions (about 18 commits over 14 hours), driven by short
prompts and screenshots. The order:

1. Look and feel first: a stone panel with a pixel font and a skills grid modelled on the
   old-school MMO style, floating in the corner, started at login.
2. Measurement: working out which transcript lines are really human, then dedupe, then
   checking the counts against a known session by hand.
3. Balance: per-skill calibration, then fitting the curve to a target ("a year of hard days
   is level 90-something").
4. Fixes from real use: Shipping counted shell text (switched to merged PRs), Wispr gave too
   much XP (calibration plus curve refit), hover box clipping.
5. Motivation layer: streaks, chests with an unboxing reel and rarities, real-life rewards,
   a shop. Tuned down twice after a day of use (fewer coins, rarer chests).
6. Rewarding level-ups: full-panel takeover and the window jump.

Each change was checked in the running app (screenshots, and a window-position sampler for
the jump), and every test was made to fail against broken code before it was kept.

## Making a variation

- **Different skills.** Edit `skills.json` `skills`. Rule types available: prompt kinds and
  keywords, tool-name regex, shell-command regex, `afk_minutes`, `wispr_words`,
  `app_minutes` (front-app regex), `github_merged_prs`. A new data source means a new
  `scan_*` function in `tracker.py` that feeds `Counter.add(day, skill, amount)`.
- **Different rewards.** `skills.json` `loot.drops` and `rewards`. Odds per tier are
  `[common, rare, ultra, extraordinary]`.
- **Pace.** `balance.xp_per_typical_day`, `curve`, `loot.xp_per_chest`, `loot.coins_per_xp`.
- **Look.** `panel/index.html` and `panel/loot.js`. Size: `CLAUDE_SKILLS_SCALE` (default 1.5).
- **Not on a Mac.** Keep `tracker.py`, `loot.py` and the HTML; replace `main.swift` with
  Electron, Tauri or a browser tab that polls `stats.json`.

Try things without touching your real data:

```bash
CLAUDE_SKILLS_DATA=/tmp/cs-test python3 tracker.py --print     # scan into a scratch dir
CLAUDE_SKILLS_DATA=/tmp/cs-test python3 tracker.py --gift gold "testing"  # drop a chest
python3 -m unittest discover tests                               # 22 tests, a few seconds
```

Dev hooks for the panel: `CLAUDE_SKILLS_DEMO_JS` (run JS on load, e.g.
`testLevelUp()` or `testChest()`), `CLAUDE_SKILLS_HOVER`, `CLAUDE_SKILLS_REFRESH`.
