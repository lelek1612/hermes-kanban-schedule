#!/usr/bin/env python3
"""kanban_schedule: start Hermes kanban tasks at a chosen time.

Hermes kanban has a `scheduled` status ("waiting on time") but no timer that
releases it. This tool adds one:

  add     create a task that is parked as `scheduled` with a
          `SCHEDULED_UNTIL=<ISO8601>` marker in its reason/comment
  tick    scan boards, `unblock` every scheduled task whose time has come
          (run it from a `no_agent` cron job every minute; silent when idle)
  list    show scheduled tasks and their due times
  config  choose which boards tick looks at (default: all boards)
  install copy this file into <HERMES_HOME>/scripts and print/create the cron job

Standard library only. Talks to Hermes exclusively through the `hermes kanban`
CLI, so it works with any board layout.

Run without arguments (as cron does) = `tick`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

MARKER = "SCHEDULED_UNTIL"
MARKER_RE = re.compile(MARKER + r"=(\S+)")
CRON_NAME = "kanban-schedule-tick"
WRAPPER_NAME = "kanban_schedule_tick.py"
TOOL_DIR = "hermes-kanban-schedule"


class HermesError(RuntimeError):
    pass


# --------------------------------------------------------------------------- paths


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / TOOL_DIR / "config.json"


def state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / TOOL_DIR / "state.json"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def find_hermes() -> str:
    candidates = [
        os.environ.get("HERMES_BIN"),
        shutil.which("hermes"),
        str(Path.home() / ".local" / "bin" / "hermes"),
        str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise HermesError("hermes binary not found (set HERMES_BIN)")


# --------------------------------------------------------------------------- hermes CLI


def run_hermes(args: list[str], timeout: int = 90) -> tuple[int, str, str]:
    proc = subprocess.run(
        [find_hermes(), *args], capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, proc.stdout, proc.stderr


def hermes_json(args: list[str]):
    rc, out, err = run_hermes(args)
    if rc != 0:
        raise HermesError(f"hermes {' '.join(args)} failed: {(err or out).strip()[:300]}")
    starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
    if not starts:
        raise HermesError(f"hermes {' '.join(args)}: no JSON in output")
    return json.loads(out[min(starts):])


# --------------------------------------------------------------------------- time


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """Parse '+30m', 'HH:MM', 'YYYY-MM-DD HH:MM' or ISO 8601 into an aware datetime.

    Naive values are interpreted in the machine's local timezone. 'HH:MM' means the
    next occurrence of that time.
    """
    now = now or datetime.now().astimezone()
    s = text.strip()
    m = re.fullmatch(r"\+(\d+)([smhd])", s)
    if m:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return now + timedelta(**{unit: int(m.group(1))})
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as e:
        raise ValueError(f"cannot parse time {text!r}: use +30m, HH:MM or ISO 8601") from e
    return dt.astimezone() if dt.tzinfo is None else dt


def find_due(show: dict) -> str | None:
    """Return the newest SCHEDULED_UNTIL value of a `kanban show --json` payload."""
    comments = sorted(show.get("comments") or [], key=lambda c: c.get("created_at") or 0)
    for c in reversed(comments):
        m = MARKER_RE.search(c.get("body") or "")
        if m:
            return m.group(1)
    m = MARKER_RE.search(show.get("latest_summary") or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- config


def load_config() -> dict:
    cfg = {"include": [], "exclude": [], "max_late_minutes": None}
    try:
        cfg.update(json.loads(config_path().read_text()))
    except (OSError, ValueError):
        pass
    for key, env in (("include", "KANBAN_SCHEDULE_INCLUDE"), ("exclude", "KANBAN_SCHEDULE_EXCLUDE")):
        if os.environ.get(env):
            cfg[key] = [x.strip() for x in os.environ[env].split(",") if x.strip()]
    if os.environ.get("KANBAN_SCHEDULE_MAX_LATE_MINUTES"):
        cfg["max_late_minutes"] = int(os.environ["KANBAN_SCHEDULE_MAX_LATE_MINUTES"])
    return cfg


def board_selected(slug: str, cfg: dict) -> bool:
    """Empty include = every board. Exclude always wins."""
    if slug in cfg.get("exclude", []):
        return False
    include = cfg.get("include", [])
    return not include or slug in include


# --------------------------------------------------------------------------- alert state


def _load_alerts() -> dict:
    try:
        return json.loads(state_path().read_text()).get("alerts", {})
    except (OSError, ValueError):
        return {}


def _save_alerts(alerts: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"alerts": alerts}, indent=2))


# --------------------------------------------------------------------------- commands


def scheduled_tasks(cfg: dict):
    """Yield (board_slug, task, due_str_or_None) for selected boards."""
    for b in hermes_json(["kanban", "boards", "list", "--json"]):
        slug = b["slug"]
        if not board_selected(slug, cfg):
            continue
        if not (b.get("counts") or {}).get("scheduled"):
            continue
        for t in hermes_json(["kanban", "--board", slug, "list", "--status", "scheduled", "--json"]):
            show = hermes_json(["kanban", "--board", slug, "show", t["id"], "--json"])
            yield slug, t, find_due(show)


def cmd_tick(args) -> int:
    cfg = load_config()
    now = datetime.now().astimezone()
    lines: list[str] = []
    alerts: dict[str, str] = {}
    try:
        for slug, t, due_s in scheduled_tasks(cfg):
            key = f"{slug}:{t['id']}"
            if due_s is None:
                continue  # parked for some other reason; not ours
            try:
                due = parse_when(due_s, now)
            except ValueError:
                alerts[key] = f"[{slug}] {t['id']} has an unreadable {MARKER}={due_s}"
                continue
            if due > now:
                continue
            late = cfg.get("max_late_minutes")
            if late is not None and now - due > timedelta(minutes=late):
                alerts[key] = (
                    f"[{slug}] {t['id']} \"{t['title']}\" missed its slot "
                    f"({due.isoformat(timespec='minutes')}, over {late} min late) - left scheduled"
                )
                continue
            if args.dry_run:
                lines.append(f"[dry-run] would start {t['id']} [{slug}] {t['title']}")
                continue
            rc, out, err = run_hermes(
                ["kanban", "--board", slug, "unblock", t["id"],
                 "--reason", f"Scheduled time reached ({due.isoformat(timespec='minutes')})"]
            )
            if rc != 0:
                alerts[key] = f"[{slug}] {t['id']} could not be started: {(err or out).strip()[:200]}"
            else:
                lines.append(f"Started {t['id']} [{slug}] {t['title']} (due {due.isoformat(timespec='minutes')})")
    except (HermesError, subprocess.TimeoutExpired, OSError) as e:
        alerts["tick"] = f"kanban-schedule tick failed: {e}"

    if not args.dry_run:
        known = _load_alerts()
        lines += [msg for k, msg in alerts.items() if known.get(k) != msg]
        _save_alerts(alerts)  # cleared automatically once the problem is gone
    else:
        lines += [f"[dry-run] alert: {m}" for m in alerts.values()]
    if lines:
        print("\n".join(lines))
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    rows = []
    try:
        for slug, t, due_s in scheduled_tasks(cfg):
            rows.append((due_s or "(no marker)", slug, t["id"], t.get("assignee") or "-", t["title"]))
    except HermesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("No scheduled tasks on the selected boards.")
        return 0
    for due, slug, tid, who, title in sorted(rows):
        print(f"{due}  [{slug}] {tid} -> {who}: {title}")
    return 0


def cmd_add(args) -> int:
    try:
        due = parse_when(args.at)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    cmd = ["kanban"]
    if args.board:
        cmd += ["--board", args.board]
    cmd += ["create", args.title, "--initial-status", "blocked", "--json"]
    for flag, val in (("--assignee", args.assignee), ("--body", args.body),
                      ("--body-file", args.body_file), ("--priority", args.priority),
                      ("--workspace", args.workspace), ("--project", args.project),
                      ("--tenant", args.tenant), ("--max-runtime", args.max_runtime)):
        if val is not None:
            cmd += [flag, str(val)]
    for s in args.skill or []:
        cmd += ["--skill", s]
    for p in args.parent or []:
        cmd += ["--parent", p]
    try:
        task = hermes_json(cmd)
    except HermesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    reason = f"{MARKER}={due.isoformat(timespec='seconds')}"
    sched = ["kanban"] + (["--board", args.board] if args.board else []) + ["schedule", task["id"], reason]
    rc, out, err = run_hermes(sched)
    if rc != 0:
        print(f"error: task {task['id']} created but not scheduled (left blocked): {(err or out).strip()}",
              file=sys.stderr)
        return 1
    print(f"Scheduled {task['id']} on board {args.board or '(current)'} for {due.isoformat(timespec='minutes')}")
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    changed = False
    for key in ("include", "exclude"):
        val = getattr(args, key)
        if val is not None:
            cfg[key] = [x.strip() for x in val.split(",") if x.strip()]
            changed = True
    if args.max_late_minutes is not None:
        cfg["max_late_minutes"] = args.max_late_minutes or None
        changed = True
    if changed:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2))
    print(json.dumps(cfg, indent=2))
    print(f"(config file: {config_path()})")
    return 0


def cmd_install(args) -> int:
    dest_dir = hermes_home() / "scripts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / WRAPPER_NAME
    shutil.copyfile(Path(__file__).resolve(), dest)
    cron = [find_hermes(), "cron", "create", "every 1m", "--name", CRON_NAME,
            "--no-agent", "--script", WRAPPER_NAME, "--deliver", args.deliver]
    print(f"Copied tick script to {dest}")
    if args.create_cron:
        rc, out, err = run_hermes(cron[1:])
        print((out or err).strip())
        return rc
    print("Create the cron job with:\n  " + " ".join(cron))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Start Hermes kanban tasks at a chosen time.")
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("tick", help="release scheduled tasks that are due (default)")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(func=cmd_tick)

    sub.add_parser("list", help="show scheduled tasks").set_defaults(func=cmd_list)

    a = sub.add_parser("add", help="create a task that starts at a given time")
    a.add_argument("title")
    a.add_argument("--at", required=True, help="+30m | HH:MM | 'YYYY-MM-DD HH:MM' | ISO 8601")
    a.add_argument("--board")
    a.add_argument("--assignee")
    a.add_argument("--body")
    a.add_argument("--body-file")
    a.add_argument("--priority")
    a.add_argument("--workspace")
    a.add_argument("--project")
    a.add_argument("--tenant")
    a.add_argument("--max-runtime")
    a.add_argument("--skill", action="append")
    a.add_argument("--parent", action="append")
    a.set_defaults(func=cmd_add)

    c = sub.add_parser("config", help="show/set board filter")
    c.add_argument("--include", help="comma list; only these boards (empty string = all)")
    c.add_argument("--exclude", help="comma list; never these boards")
    c.add_argument("--max-late-minutes", type=int,
                   help="do not start tasks more than N minutes overdue (0 = always start)")
    c.set_defaults(func=cmd_config)

    i = sub.add_parser("install", help="copy tick script to HERMES_HOME/scripts and show cron command")
    i.add_argument("--create-cron", action="store_true", help="also run `hermes cron create`")
    i.add_argument("--deliver", default="local", help="cron delivery target (e.g. telegram)")
    i.set_defaults(func=cmd_install)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv or ["tick"])
    if not getattr(args, "func", None):
        args = parser.parse_args(["tick"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
