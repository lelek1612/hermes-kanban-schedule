# hermes-kanban-schedule

A [Hermes Agent](https://hermes-agent.nousresearch.com) skill that starts kanban tasks at a chosen time.

Hermes kanban has a `scheduled` status ("waiting on time, not on a human") but no timer that
releases it. This skill adds one:

1. A task is parked as `scheduled` with a `SCHEDULED_UNTIL=<ISO8601>` marker in its reason.
2. A `no_agent` cron job runs `tick` every minute. It scans the boards, and for every task whose
   time has come it runs `hermes kanban unblock`.
3. The gateway dispatcher picks the task up and starts the assignee as usual.

No LLM tokens are spent, stdlib Python only, and `tick` is silent unless it started something or
hit a problem. Typical use: scheduling social posts, reports or reminders as kanban cards.

## Install

```bash
hermes skills install <login>/hermes-kanban-schedule/kanban-schedule
```

Then, once:

```bash
python3 <skill_dir>/scripts/kanban_schedule.py install --create-cron --deliver telegram
```

This copies the tick script to `<HERMES_HOME>/scripts/` and creates the `kanban-schedule-tick`
cron job (`every 1m`, `--no-agent`). Drop `--create-cron` to just print the command.

## Use

```bash
# new task that starts tomorrow at 18:00 on board "social"
python3 kanban_schedule.py add "Post #12" --at "2026-11-12 18:00" --board social --assignee poster

# existing task
hermes kanban --board social schedule t_abc123 "SCHEDULED_UNTIL=2026-11-12T18:00:00+01:00"

python3 kanban_schedule.py list            # what is waiting
python3 kanban_schedule.py tick --dry-run  # what would start now
```

`--at` accepts `+30m`, `+2h`, `+1d`, `HH:MM` (next occurrence), `YYYY-MM-DD HH:MM` or ISO 8601.

## Choosing boards

By default every board is watched. To change that:

```bash
python3 kanban_schedule.py config --exclude some-board   # skip a board
python3 kanban_schedule.py config --include social,notes          # watch only these
python3 kanban_schedule.py config --include ""                    # back to all boards
python3 kanban_schedule.py config --max-late-minutes 30           # don't start stale jobs
```

Exclude always beats include. Environment overrides: `KANBAN_SCHEDULE_INCLUDE`,
`KANBAN_SCHEDULE_EXCLUDE`, `KANBAN_SCHEDULE_MAX_LATE_MINUTES`. Config is stored in
`~/.config/hermes-kanban-schedule/config.json`.

## Limits

- Needs a running gateway (it hosts the kanban dispatcher).
- Accuracy is about one minute.
- Only tasks with a `SCHEDULED_UNTIL` marker are ever released.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Offline, no Hermes needed (the CLI is mocked).

## License

MIT
