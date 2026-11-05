# Claude Skills

An old-school MMO skills panel for your Claude Code usage. It floats in the corner of
your Mac, always on top, and levels up 14 skills from your real Claude Code transcripts.

```bash
./install.sh            # build, scan history, start now and at every login
./install.sh --uninstall
```

Needs macOS, Xcode command line tools (`xcode-select --install`) and python3. Nothing
leaves your machine: it only reads `~/.claude/projects/**/*.jsonl`.

## How XP works

- **You** (blue tiles): Prompting (new messages), Steering (mid-turn messages and
  interrupts), Planning (plan mode, questions the agent asks you). Only messages Claude Code
  marks as typed by a human count; notifications, other sessions and subagents do not.
- **Your agents**: AFK Coding (minutes agents work between your messages, idle gaps capped
  at 2 min), Ultracode, Delegation, Research, Testing, Shipping, Building, Scouting, Shell,
  Artistry, Connecting.
- **Fair by design**: each skill learns what a typical active day looks like for you (median
  of your active days, frozen once it has 5). A typical day is 1,000 XP in every skill, so 4,000
  shell commands and 3 plan-mode sessions are worth the same. Past 2 typical days the gain
  turns logarithmic, and no single day is worth more than 5.
- Forked and resumed sessions copy history; every event is counted once by its id.
- Levels 1-99; level 50 takes about two months of daily use of a skill, 99 about four years.

## Customize

Edit `skills.json`: names, icons, the rules (tool-name regex, shell-command regex, prompt
kinds and keywords), the balance and the curve. The tracker rescans history on its own after
a change. Tests: `python3 -m unittest discover tests`.

Settings via environment (in the LaunchAgent plist): `CLAUDE_PROJECTS_DIR`,
`CLAUDE_SKILLS_DATA`, `CLAUDE_SKILLS_REFRESH` (seconds, default 60).
