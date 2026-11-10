---
name: kanban-schedule
description: Start kanban tasks at a set time via a per-minute cron.
version: 0.1.0
author: JJ (lelek1612), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, scheduling, cron, automation]
    related_skills: []
---

# Kanban Schedule Skill

Hermes kanban can park a task as `scheduled` ("waiting on time") but has no timer that
releases it. This skill adds one: a task carries a `SCHEDULED_UNTIL=<ISO8601>` marker, and a
`no_agent` cron job runs `tick` every minute and `unblock`s every task whose time has come.
The dispatcher then starts the assignee as usual. Standard library only, zero LLM tokens.

It does not create recurring tasks (use `cronjob` for "every day at 9") and it never touches
tasks that have no marker.

## When to Use

- "Start this task tomorrow at 18:00", "run this card at 09:30", scheduling posts or reports on a board.
- Showing what is waiting for its time (`list`).
- Choosing which boards the scheduler watches (`config`).

Don't use for: recurring jobs, or tasks that should wait for a human (use `kanban block`).

## Prerequisites

- Hermes with `hermes kanban` and a running gateway (it hosts the dispatcher).
- Python 3.9+.
- `SKILL_DIR` below = the directory of this skill (`skill_view` returns it as `skill_dir`).

## Quick Reference

```
python3 SKILL_DIR/scripts/kanban_schedule.py add "Title" --at WHEN --board B --assignee P [--body ...] [--skill S] [--priority N] [--idempotency-key KEY]
python3 SKILL_DIR/scripts/kanban_schedule.py list
python3 SKILL_DIR/scripts/kanban_schedule.py tick [--dry-run]
python3 SKILL_DIR/scripts/kanban_schedule.py config [--include a,b] [--exclude c] [--max-late-minutes N]
python3 SKILL_DIR/scripts/kanban_schedule.py install [--create-cron] [--deliver telegram]
```

`WHEN` accepts `+30m` / `+2h` / `+1d`, `HH:MM` (next occurrence), `YYYY-MM-DD HH:MM`, or full ISO 8601.
Naive times are the machine's local timezone.

## Procedure

1. Install once. `terminal(command="python3 SKILL_DIR/scripts/kanban_schedule.py install --create-cron --deliver telegram")`.
   Done when `hermes cron list` shows `kanban-schedule-tick` with `every 1m` and mode `no-agent`.
2. Optional board filter. Default is every board. `config --exclude board-x` skips a board;
   `config --include a,b` watches only those (exclude always wins; `--include ""` resets).
   Env overrides: `KANBAN_SCHEDULE_INCLUDE`, `KANBAN_SCHEDULE_EXCLUDE`.
   Done when the printed JSON shows the intended lists.
3. Schedule a task with `add`. Done when it prints `Scheduled t_... for <time>` and `list` shows it.
4. Existing task? `hermes kanban --board B schedule <id> "SCHEDULED_UNTIL=2026-11-12T18:00:00+01:00"`.
   Re-scheduling = another `schedule` call; the newest marker wins.
5. Verify with `tick --dry-run` (prints what would start, changes nothing).

## Pitfalls

- Needs the gateway dispatcher; without it the task becomes `ready` but nobody picks it up.
- Resolution is 1-2 minutes (cron cadence plus dispatcher latency).
- `--idempotency-key KEY` makes `add` safe to repeat: if a live task with that key exists it is left
  untouched (never re-scheduled) and `add` says `Already exists`.
- A task with an unfinished parent goes to `todo` on release and starts when the parent completes.
- `--max-late-minutes N` makes `tick` leave tasks more than N minutes overdue (e.g. after downtime)
  scheduled and alert once instead of starting a stale job. Default: always start.
- `tick` prints only when it started something or hit a problem; each problem is reported once, not every minute.
- Config lives in `~/.config/hermes-kanban-schedule/config.json`, alert state in
  `~/.local/state/hermes-kanban-schedule/`; both survive skill updates.

## Verification

Create a test task without an assignee on any board: `add "test" --at +1m --board default`, wait
~70 s, run `tick`; expect `Started t_...` and `hermes kanban --board default show <id>` status `ready`.
Archive it afterwards.
