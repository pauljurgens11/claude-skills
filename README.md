# Claude Skills

An old-school MMO skills panel for your Claude Code usage. It floats in the corner of
your Mac, always on top, and levels up 5 skills from your real Claude Code transcripts.

```bash
./install.sh            # build, scan history, start now and at every login
./install.sh --uninstall
```

Needs macOS, Xcode command line tools (`xcode-select --install`) and python3. Nothing
leaves your machine: it only reads `~/.claude/projects/**/*.jsonl`.

## What each skill tracks

| Skill | Counts | Source |
|---|---|---|
| 💬 Prompting | Every message you send to Claude Code: new prompts, mid-turn corrections (interrupts count half). Longer prompts earn a bit more. Only messages Claude Code marks as typed by a human. | `~/.claude/projects` transcripts |
| 💤 AFK Coding | Minutes your agents work between your messages, summed across sessions. Idle gaps over 2 min do not count. | transcripts |
| 🎙️ Wispr Flow | Words you dictate. Reads only word counts and times, never the text. | Wispr Flow's local database, read-only |
| 📨 Messaging | Active minutes with Slack or WhatsApp in front. Only counted if you touched keyboard or mouse in the last minute. | the panel samples the front app every 15 s |
| 🚀 Shipping | Pull requests GitHub merged with you as author. | `gh` (GitHub CLI), every 10 min |

## Balance

- Each skill learns your normal day (median of your finished active days, frozen after 5). Until it freezes, a skill's level can still shift a little at midnight. A normal day is 1,000 XP in every skill, so no skill outruns the others.
- Past 2 normal days the gain turns logarithmic; one day is capped at 12.
- A year of maxed-out days (about 3,000 XP per skill per day) reaches level 92; 99 takes about two years of that.
- `xp_since` in `skills.json` is the moment everyone started at level 1. Older history still teaches the panel your normal day.

## Chests, streaks and the shop

- **Chests**: one per 2,500 XP of combined work (2 on a normal day, at any level). From level 15 every level-up gives a chest (below that, a 1 in 3 chance), and a level-up also upgrades your next meter chest; every 10th level of a skill gives a milestone chest; streaks of 7/30/100/200/365 days give golden or mythic chests. Each chest says why it came.
- **Unboxing**: a reel spins and lands on one drop: Common, Rare, Ultra Rare or Extraordinary. Drops: XP potions and elixirs, Streak Shields, pets, titles, coins, and real-life permissions (look out the window, a cookie, a pudding, an afternoon off). Permissions only come from chests.
- **Coins**: 1 per 50 XP plus coin drops (chests give no bonus coins). They buy chests and big real-life rewards in the shop.
- **Streak**: a day counts once your skills earn 500 XP together. One missed day a week is free; Streak Shields cover more.
- Everything is deterministic: the same work always drops the same chests, and nothing can be spent twice. Earned chests live in `loot.json`, your clicks in `actions.jsonl`.

How it works inside, and how to build a variation: [BUILDING.md](BUILDING.md).

## Customize

Edit `skills.json`: names, icons, the rules (tool-name regex, shell-command regex, prompt
kinds and keywords), the balance and the curve. The tracker rescans history on its own after
a change. Tests: `python3 -m unittest discover tests`.

Settings via environment (in the LaunchAgent plist): `CLAUDE_PROJECTS_DIR`,
`CLAUDE_SKILLS_DATA`, `CLAUDE_SKILLS_REFRESH` (seconds, default 60).
