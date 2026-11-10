"""Offline tests for kanban_schedule.py (stdlib unittest; also runs under pytest)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kanban-schedule" / "scripts"))
import kanban_schedule as ks  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


class ParseWhen(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(ks.parse_when("+30m", NOW), NOW + timedelta(minutes=30))
        self.assertEqual(ks.parse_when("+2h", NOW), NOW + timedelta(hours=2))
        self.assertEqual(ks.parse_when("+1d", NOW), NOW + timedelta(days=1))

    def test_clock_time_is_next_occurrence(self):
        self.assertEqual(ks.parse_when("18:30", NOW), NOW.replace(hour=18, minute=30))
        self.assertEqual(ks.parse_when("09:00", NOW), NOW.replace(hour=9) + timedelta(days=1))

    def test_iso(self):
        self.assertEqual(ks.parse_when("2026-11-12T18:00+01:00", NOW),
                         datetime(2026, 11, 12, 17, 0, tzinfo=UTC))
        self.assertEqual(ks.parse_when("2026-11-12T17:00Z", NOW),
                         datetime(2026, 11, 12, 17, 0, tzinfo=UTC))
        self.assertIsNotNone(ks.parse_when("2026-11-12 18:00", NOW).tzinfo)

    def test_garbage(self):
        with self.assertRaises(ValueError):
            ks.parse_when("jutro rano", NOW)


class FindDue(unittest.TestCase):
    def test_newest_comment_wins(self):
        show = {"comments": [
            {"body": "SCHEDULED: SCHEDULED_UNTIL=2026-01-01T10:00+00:00", "created_at": 1},
            {"body": "SCHEDULED: SCHEDULED_UNTIL=2026-02-01T10:00+00:00", "created_at": 2},
        ]}
        self.assertEqual(ks.find_due(show), "2026-02-01T10:00+00:00")

    def test_fallback_to_summary_and_none(self):
        self.assertEqual(ks.find_due({"comments": [], "latest_summary": "SCHEDULED_UNTIL=X"}), "X")
        self.assertIsNone(ks.find_due({"comments": [{"body": "hi", "created_at": 1}]}))


class BoardFilter(unittest.TestCase):
    def test_default_all(self):
        self.assertTrue(ks.board_selected("a", {"include": [], "exclude": []}))

    def test_exclude(self):
        self.assertFalse(ks.board_selected("a", {"include": [], "exclude": ["a"]}))

    def test_include_allowlist(self):
        cfg = {"include": ["a"], "exclude": []}
        self.assertTrue(ks.board_selected("a", cfg))
        self.assertFalse(ks.board_selected("b", cfg))

    def test_exclude_beats_include(self):
        self.assertFalse(ks.board_selected("a", {"include": ["a"], "exclude": ["a"]}))


class FakeHermes:
    """Stand-in for the hermes CLI over two boards."""

    def __init__(self, due_by_task):
        self.due = due_by_task  # {(board, id): marker or None}
        self.unblocked = []
        self.fail_unblock = False

    def json(self, args):
        a = list(args)
        board = a[a.index("--board") + 1] if "--board" in a else None
        if a[:3] == ["kanban", "boards", "list"]:
            boards = {}
            for (b, _), _d in self.due.items():
                boards[b] = boards.get(b, 0) + 1
            return [{"slug": b, "counts": {"scheduled": n}} for b, n in boards.items()] + [
                {"slug": "empty", "counts": {}}]
        if "list" in a:
            return [{"id": i, "title": f"task {i}", "assignee": "x"} for (b, i) in self.due if b == board]
        if "show" in a:
            tid = a[a.index("show") + 1]
            marker = self.due[(board, tid)]
            comments = [{"body": f"SCHEDULED: {ks.MARKER}={marker}", "created_at": 1}] if marker else []
            return {"comments": comments}
        raise AssertionError(args)

    def run(self, args, timeout=90):
        self.unblocked.append(list(args))
        return (1, "", "boom") if self.fail_unblock else (0, "ok", "")


class Tick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"XDG_CONFIG_HOME": self.tmp.name, "XDG_STATE_HOME": self.tmp.name}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def tick(self, fake, *argv):
        buf = io.StringIO()
        with mock.patch.object(ks, "hermes_json", fake.json), \
             mock.patch.object(ks, "run_hermes", fake.run), \
             contextlib.redirect_stdout(buf):
            ks.main(["tick", *argv])
        return buf.getvalue()

    def iso(self, **delta):
        return (datetime.now().astimezone() + timedelta(**delta)).isoformat(timespec="seconds")

    def test_releases_only_due_tasks(self):
        fake = FakeHermes({("b1", "t_due"): self.iso(minutes=-1),
                           ("b1", "t_later"): self.iso(hours=2),
                           ("b2", "t_nomarker"): None})
        out = self.tick(fake)
        self.assertIn("Started t_due", out)
        self.assertEqual(len(fake.unblocked), 1)
        self.assertIn("t_due", fake.unblocked[0])
        self.assertIn("b1", fake.unblocked[0])

    def test_quiet_when_nothing_due(self):
        fake = FakeHermes({("b1", "t_later"): self.iso(hours=2)})
        self.assertEqual(self.tick(fake), "")
        self.assertEqual(fake.unblocked, [])

    def test_exclude_board(self):
        Path(ks.config_path()).parent.mkdir(parents=True, exist_ok=True)
        ks.config_path().write_text(json.dumps({"exclude": ["b1"]}))
        fake = FakeHermes({("b1", "t_due"): self.iso(minutes=-1),
                           ("b2", "t_due2"): self.iso(minutes=-1)})
        self.tick(fake)
        self.assertEqual(len(fake.unblocked), 1)
        self.assertIn("b2", fake.unblocked[0])

    def test_include_board(self):
        os.environ["KANBAN_SCHEDULE_INCLUDE"] = "b1"
        try:
            fake = FakeHermes({("b1", "t_a"): self.iso(minutes=-1),
                               ("b2", "t_b"): self.iso(minutes=-1)})
            self.tick(fake)
        finally:
            del os.environ["KANBAN_SCHEDULE_INCLUDE"]
        self.assertEqual(len(fake.unblocked), 1)
        self.assertIn("b1", fake.unblocked[0])

    def test_dry_run_does_not_unblock(self):
        fake = FakeHermes({("b1", "t_due"): self.iso(minutes=-1)})
        out = self.tick(fake, "--dry-run")
        self.assertIn("would start", out)
        self.assertEqual(fake.unblocked, [])

    def test_max_late_skips_and_alerts_once(self):
        os.environ["KANBAN_SCHEDULE_MAX_LATE_MINUTES"] = "10"
        try:
            fake = FakeHermes({("b1", "t_old"): self.iso(hours=-3)})
            first = self.tick(fake)
            second = self.tick(fake)
        finally:
            del os.environ["KANBAN_SCHEDULE_MAX_LATE_MINUTES"]
        self.assertIn("missed its slot", first)
        self.assertEqual(second, "")  # same alert is not repeated every minute
        self.assertEqual(fake.unblocked, [])

    def test_unblock_failure_alerts_once(self):
        fake = FakeHermes({("b1", "t_due"): self.iso(minutes=-1)})
        fake.fail_unblock = True
        self.assertIn("could not be started", self.tick(fake))
        self.assertEqual(self.tick(fake), "")

    def test_bad_marker_alerts(self):
        fake = FakeHermes({("b1", "t_bad"): "not-a-date"})
        self.assertIn("unreadable", self.tick(fake))
        self.assertEqual(fake.unblocked, [])


class Add(unittest.TestCase):
    def test_add_creates_blocked_then_schedules(self):
        calls = []

        def fake_json(args):
            calls.append(list(args))
            return {"id": "t_new"}

        def fake_run(args, timeout=90):
            calls.append(list(args))
            return 0, "Scheduled", ""

        with mock.patch.object(ks, "hermes_json", fake_json), \
             mock.patch.object(ks, "run_hermes", fake_run), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = ks.main(["add", "Post #1", "--at", "2026-11-12T18:00+01:00",
                          "--board", "b1", "--assignee", "bot", "--skill", "s1"])
        self.assertEqual(rc, 0)
        create, schedule = calls
        self.assertIn("--initial-status", create)
        self.assertEqual(create[create.index("--initial-status") + 1], "blocked")
        self.assertIn("--assignee", create)
        self.assertEqual(schedule[:3], ["kanban", "--board", "b1"])
        self.assertIn("schedule", schedule)
        self.assertIn("SCHEDULED_UNTIL=2026-11-12T18:00:00+01:00", schedule)

    def test_idempotency_key_passed_and_existing_task_untouched(self):
        calls = []

        def fake_json(args):
            calls.append(list(args))
            return {"id": "t_old", "status": "done", "created_at": 1}  # created long ago

        def fake_run(args, timeout=90):
            calls.append(list(args))
            return 0, "", ""

        out = io.StringIO()
        with mock.patch.object(ks, "hermes_json", fake_json), \
             mock.patch.object(ks, "run_hermes", fake_run), \
             contextlib.redirect_stdout(out):
            rc = ks.main(["add", "Pub", "--at", "+5m", "--idempotency-key", "pub-s06-ig"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)  # created/looked up, never scheduled
        self.assertIn("pub-s06-ig", calls[0])
        self.assertIn("Already exists", out.getvalue())

    def test_add_rejects_bad_time(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ks.main(["add", "x", "--at", "kiedyś"]), 2)


if __name__ == "__main__":
    unittest.main()
