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
| 🚀 Shipping | Commits, pushes and pull requests your agents make. | transcripts |

## Balance

- Each skill learns your normal day (median of your active days, frozen after 5). A normal day is 1,000 XP in every skill, so no skill outruns the others.
- Past 2 normal days the gain turns logarithmic; one day is capped at 12.
- A year of maxed-out days (about 3,000 XP per skill per day) reaches level 92; 99 takes about two years of that.
- `xp_since` in `skills.json` is the moment everyone started at level 1. Older history still teaches the panel your normal day.

## Gear

Each skill powers one piece of gear on your character: Prompting the helm, AFK Coding the armour,
Wispr Flow the amulet, Messaging the cape, Shipping the blade. A new tier unlocks every 10 levels
(Cloth, Copper, Iron, Steel, Cobalt, Jade, Obsidian, Crimson, Starmetal, Celestial) and level 99
gives the legendary item. Total level gives titles from Newcomer to Ascended. To preview later gear
without touching your data: `CLAUDE_SKILLS_PREVIEW="40,60,80,90,99" build/ClaudeSkills`.

## Customize

Edit `skills.json`: names, icons, the rules (tool-name regex, shell-command regex, prompt
kinds and keywords), the balance and the curve. The tracker rescans history on its own after
a change. Tests: `python3 -m unittest discover tests`.

Settings via environment (in the LaunchAgent plist): `CLAUDE_PROJECTS_DIR`,
`CLAUDE_SKILLS_DATA`, `CLAUDE_SKILLS_REFRESH` (seconds, default 60).
