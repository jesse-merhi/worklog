#!/usr/bin/env python3
"""End-to-end tests for the local Codex worklog CLI."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import worklog as worklog_module


SCRIPT = Path(__file__).with_name("worklog.py")


class WorklogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.vault = self.root / "vault"
        self.transcript = self.root / "transcript.jsonl"
        self.fake = self.root / "fake_summarizer.py"
        self.fake.write_text(
            """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]
required = [
    "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
    "--sandbox", "read-only", "--json", "--output-schema",
    "--output-last-message", "--enable", "skip_host_skill_discovery",
]
if any(value not in arguments for value in required):
    print("missing isolated Codex invocation flag", file=sys.stderr)
    sys.exit(3)
disabled = {
    arguments[index + 1]
    for index, value in enumerate(arguments[:-1])
    if value == "--disable"
}
if not {"hooks", "apps", "plugins", "shell_tool", "multi_agent"} <= disabled:
    print("missing tool disable", file=sys.stderr)
    sys.exit(4)
schema_path = Path(arguments[arguments.index("--output-schema") + 1])
schema = json.loads(schema_path.read_text())
if schema.get("required") != ["title", "bullets"]:
    print("unexpected summary schema", file=sys.stderr)
    sys.exit(5)
output_path = Path(arguments[arguments.index("--output-last-message") + 1])
request = json.load(sys.stdin)
Path(__file__).with_name("last_request.json").write_text(json.dumps(request))
if any("[FAIL]" in message["text"] for message in request["messages"]):
    print("selected summary failure", file=sys.stderr)
    sys.exit(6)
prior = [
    line[2:]
    for line in request["existing_entry"].splitlines()
    if line.startswith("- ")
]
new = [
    "Captured " + message["role"] + ": " + " ".join(message["text"].split())
    for message in request["messages"]
]
bullets = list(dict.fromkeys(prior + new))
output_path.write_text(json.dumps({"title": "Codex work", "bullets": bullets}))
""",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)
        self.fail_fake = self.root / "failed_summarizer.py"
        self.fail_fake.write_text(
            "#!/usr/bin/env python3\nimport sys\nprint('model unavailable', file=sys.stderr)\nsys.exit(2)\n",
            encoding="utf-8",
        )
        self.fail_fake.chmod(0o755)
        self.noop_fake = self.root / "noop_summarizer.py"
        self.noop_fake.write_text(
            """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]
output_path = Path(arguments[arguments.index("--output-last-message") + 1])
output_path.write_text(json.dumps({"title": "No diary work", "bullets": []}))
""",
            encoding="utf-8",
        )
        self.noop_fake.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, arguments, stdin=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + list(arguments),
            input=stdin,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def hook(self, session_id, event_name="Stop", transcript=None):
        event = {
            "session_id": session_id,
            "transcript_path": str(transcript or self.transcript),
            "cwd": str(self.root),
            "hook_event_name": event_name,
        }
        return self.run_cli(
            ["hook", "--state-dir", str(self.state)],
            json.dumps(event),
        )

    def drain(self, codex_bin=None):
        return self.run_cli(
            [
                "drain",
                "--state-dir",
                str(self.state),
                "--vault",
                str(self.vault),
                "--daily-folder",
                "daily_notes",
                "--codex-bin",
                str(codex_bin or self.fake),
            ],
        )

    def write_transcript(self, items, append=False, path=None):
        transcript = path or self.transcript
        mode = "a" if append else "w"
        with transcript.open(mode, encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item) + "\n")

    @staticmethod
    def session_meta(session_id, source="cli"):
        return {
            "timestamp": datetime.now().astimezone().isoformat(),
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "source": source,
                "thread_source": "user",
            },
        }

    @staticmethod
    def message(role, text, timestamp=None, kinds=None, phase=None):
        payload = {
            "type": "message",
            "role": role,
            "content": [
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": text,
                }
            ],
        }
        if kinds is not None:
            payload["internal_chat_message_metadata_passthrough"] = {
                "content_item_kinds": kinds
            }
        if phase is not None:
            payload["phase"] = phase
        return {
            "timestamp": (timestamp or datetime.now().astimezone()).isoformat(),
            "type": "response_item",
            "payload": payload,
        }

    def note_path(self, timestamp=None):
        date = (timestamp or datetime.now().astimezone()).date()
        return self.vault / "daily_notes" / date.strftime("%d-%m-%Y.md")

    def pending_jobs(self):
        return list((self.state / "pending").glob("*.json"))

    def failed_jobs(self):
        return list((self.state / "failed").glob("*.json"))

    def checkpoint(self, session_id):
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        path = self.state / "checkpoints" / (key + ".json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_stop_is_hourly_session_end_flushes_and_entry_updates_in_place(self):
        session_id = "hourly-session"
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Implement a local work diary."),
            ]
        )

        def concurrent_stop(_):
            return self.hook(session_id)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(concurrent_stop, range(4)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertTrue(all(result.stdout == "{}\n" for result in results))
        self.assertEqual(len(self.pending_jobs()), 1)

        end_result = self.hook(session_id, "SessionEnd")
        self.assertEqual(end_result.stdout, "{}\n")
        self.assertEqual(len(self.pending_jobs()), 2)
        first_drain = self.drain()
        self.assertEqual(first_drain.returncode, 0, first_drain.stderr)

        note = self.note_path().read_text(encoding="utf-8")
        self.assertEqual(note.count("<!-- worklog:"), 2)
        self.assertIn("Captured user: Implement a local work diary.", note)

        self.write_transcript(
            [self.message("assistant", "The queue and note update now work.")],
            append=True,
        )
        throttled = self.hook(session_id, "Stop")
        self.assertEqual(throttled.stdout, "{}\n")
        self.assertEqual(self.pending_jobs(), [])

        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        second_drain = self.drain()
        self.assertEqual(second_drain.returncode, 0, second_drain.stderr)
        updated = self.note_path().read_text(encoding="utf-8")
        self.assertEqual(updated.count("<!-- worklog:"), 2)
        self.assertIn("Captured user: Implement a local work diary.", updated)
        self.assertIn("Captured assistant: The queue and note update now work.", updated)

    def test_preserves_unrelated_notes_groups_days_and_filters_metadata(self):
        session_id = "multi-day-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        self.write_transcript(
            [
                self.session_meta(session_id),
                {
                    "timestamp": yesterday.isoformat(),
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "secret injected text"},
                            {"type": "input_text", "text": "Resolved the queue design."},
                        ],
                    },
                    "internal_chat_message_metadata_passthrough": {
                        "content_item_kinds": ["agents_md.instructions", "user"]
                    },
                },
                {
                    "timestamp": yesterday.isoformat(),
                    "type": "event_msg",
                    "payload": {"message": "Resolved the queue design."},
                },
                self.message("assistant", "Implemented atomic note updates.", today),
                self.message("user", "<environment_context>private paths</environment_context>", today),
            ]
        )
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        checkpoint_dir = self.state / "checkpoints"
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / (key + ".json")).write_text(
            json.dumps({"session_id": session_id, "cursor": 0, "last_success": 0}),
            encoding="utf-8",
        )
        today_path = self.note_path(today)
        today_path.parent.mkdir(parents=True)
        original = "# Personal daily note\n\nUnrelated text without a trailing newline"
        today_path.write_text(original, encoding="utf-8")

        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)

        today_note = today_path.read_text(encoding="utf-8")
        yesterday_note = self.note_path(yesterday).read_text(encoding="utf-8")
        self.assertTrue(today_note.startswith(original))
        self.assertIn("Implemented atomic note updates.", today_note)
        self.assertIn("Resolved the queue design.", yesterday_note)
        combined = today_note + yesterday_note
        self.assertNotIn("secret injected text", combined)
        self.assertNotIn("private paths", combined)
        self.assertEqual(combined.count("Resolved the queue design."), 1)

    def test_linked_snapshot_survives_source_deletion(self):
        session_id = "archived-session"
        self.write_transcript(
            [self.session_meta(session_id), self.message("user", "Archive this session safely.")]
        )
        hook_result = self.hook(session_id, "SessionEnd")
        self.assertEqual(hook_result.returncode, 0)
        self.transcript.unlink()

        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Archive this session safely.",
            self.note_path().read_text(encoding="utf-8"),
        )
        self.assertEqual(list((self.state / "snapshots").iterdir()), [])

    def test_failure_keeps_cursor_and_retry_uses_same_snapshot(self):
        session_id = "retry-session"
        self.write_transcript(
            [self.session_meta(session_id), self.message("user", "Retry this summary.")]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        failed = self.drain(self.fail_fake)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("model unavailable", failed.stderr)
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual(len(self.failed_jobs()), 1)
        with self.assertRaises(FileNotFoundError):
            self.checkpoint(session_id)

        retry = self.run_cli(["retry", "--state-dir", str(self.state)])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout), {"retried": 1})
        succeeded = self.drain()
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertIn("Retry this summary.", self.note_path().read_text(encoding="utf-8"))
        self.assertGreater(self.checkpoint(session_id)["cursor"], 0)

    def test_newer_capture_recovers_failed_previous_day_before_advancing(self):
        session_id = "failed-yesterday-then-new-work"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Yesterday's unsaved result.", yesterday),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        first_job = self.pending_jobs()[0]
        first_job_data = json.loads(first_job.read_text(encoding="utf-8"))
        first_job_data["queued_at"] = yesterday.isoformat()
        first_job.write_text(json.dumps(first_job_data), encoding="utf-8")
        self.assertEqual(self.drain(self.fail_fake).returncode, 1)
        self.assertEqual(len(self.failed_jobs()), 1)

        self.write_transcript(
            [self.message("assistant", "Today's completed result.", today)],
            append=True,
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        recovered = self.drain()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

        yesterday_note = self.note_path(yesterday).read_text(encoding="utf-8")
        today_note = self.note_path(today).read_text(encoding="utf-8")
        self.assertIn("Yesterday's unsaved result.", yesterday_note)
        self.assertIn("Today's completed result.", today_note)
        self.assertEqual(self.failed_jobs(), [])
        self.assertEqual(list((self.state / "snapshots").iterdir()), [])
        self.assertEqual(self.checkpoint(session_id)["cursor"], self.transcript.stat().st_size)

    def test_retry_publication_cannot_race_worker_or_reverse_job_order(self):
        session_id = "retry-publication-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Older failed work.", yesterday),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        first_job = self.pending_jobs()[0]
        first_job_data = json.loads(first_job.read_text(encoding="utf-8"))
        first_job_data["queued_at"] = yesterday.isoformat()
        first_job.write_text(json.dumps(first_job_data), encoding="utf-8")
        self.assertEqual(self.drain(self.fail_fake).returncode, 1)

        self.write_transcript(
            [self.message("assistant", "Newer failed work.", today)], append=True
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        self.assertEqual(self.drain(self.fail_fake).returncode, 1)
        self.assertEqual(len(self.failed_jobs()), 2)

        real_replace = worklog_module.os.replace
        observed_boundary = []

        def replace_with_worker(source, destination):
            real_replace(source, destination)
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                not observed_boundary
                and source_path.parent == self.state / "failed"
                and destination_path.parent == self.state / "pending"
            ):
                drain_result = self.drain()
                observed_boundary.append(
                    (
                        drain_result.returncode,
                        len(self.pending_jobs()),
                        len(self.failed_jobs()),
                    )
                )

        output = io.StringIO()
        with mock.patch.object(worklog_module.os, "replace", replace_with_worker):
            with redirect_stdout(output):
                retry_result = worklog_module.retry_command(self.state)

        self.assertEqual(retry_result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"retried": 2})
        self.assertEqual(observed_boundary, [(0, 1, 1)])
        self.assertEqual(len(self.pending_jobs()), 2)
        self.assertEqual(self.failed_jobs(), [])

        final_drain = self.drain()
        self.assertEqual(final_drain.returncode, 0, final_drain.stderr)
        self.assertIn(
            "Older failed work.",
            self.note_path(yesterday).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Newer failed work.",
            self.note_path(today).read_text(encoding="utf-8"),
        )
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual(list((self.state / "snapshots").iterdir()), [])

    def test_failed_recovery_parks_newer_work_and_other_sessions_continue(self):
        blocked_session = "blocked-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        self.write_transcript(
            [
                self.session_meta(blocked_session),
                self.message("user", "[FAIL] Yesterday remains unsaved.", yesterday),
            ]
        )
        self.assertEqual(self.hook(blocked_session, "SessionEnd").returncode, 0)
        first_job = self.pending_jobs()[0]
        first_job_data = json.loads(first_job.read_text(encoding="utf-8"))
        first_job_data["queued_at"] = yesterday.isoformat()
        first_job.write_text(json.dumps(first_job_data), encoding="utf-8")
        self.assertEqual(self.drain().returncode, 1)

        self.write_transcript(
            [self.message("assistant", "Newer work must wait.")], append=True
        )
        self.assertEqual(self.hook(blocked_session, "SessionEnd").returncode, 0)
        other_transcript = self.root / "other.jsonl"
        other_session = "unrelated-session"
        self.write_transcript(
            [
                self.session_meta(other_session),
                self.message("user", "Unrelated work can progress."),
            ],
            path=other_transcript,
        )
        self.assertEqual(
            self.hook(other_session, "SessionEnd", other_transcript).returncode,
            0,
        )

        result = self.drain()
        self.assertEqual(result.returncode, 1)
        self.assertIn("selected summary failure", result.stderr)
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual(len(self.failed_jobs()), 2)
        today_note = self.note_path().read_text(encoding="utf-8")
        self.assertIn("Unrelated work can progress.", today_note)
        self.assertNotIn("Newer work must wait.", today_note)
        self.assertFalse(self.note_path(yesterday).exists())
        self.assertEqual(self.drain().returncode, 0)

    def test_subagent_transcript_is_an_intentional_no_op(self):
        session_id = "subagent-session"
        self.write_transcript(
            [
                self.session_meta(session_id, {"subagent": "worker"}),
                self.message("user", "Internal delegated task."),
                self.message("assistant", "Internal delegated result."),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.note_path().exists())
        checkpoint = self.checkpoint(session_id)
        self.assertFalse(checkpoint["user_owned"])
        self.assertEqual(checkpoint["cursor"], self.transcript.stat().st_size)

    def test_oversized_metadata_distinguishes_automation_from_user_threads(self):
        oversized_instructions = "x" * 83_000
        automation_session = "automation-session"
        automation_meta = self.session_meta(automation_session)
        automation_meta["payload"]["thread_source"] = "automation"
        automation_meta["payload"]["model_instructions"] = oversized_instructions
        self.write_transcript(
            [
                automation_meta,
                self.message("assistant", "Automation output is not diary work."),
            ]
        )
        self.assertGreater(len(json.dumps(automation_meta)), 65_536)
        self.assertEqual(self.hook(automation_session, "SessionEnd").returncode, 0)
        automation_result = self.drain()
        self.assertEqual(automation_result.returncode, 0, automation_result.stderr)
        self.assertFalse(self.note_path().exists())
        self.assertFalse(self.checkpoint(automation_session)["user_owned"])

        user_session = "large-user-session"
        user_meta = self.session_meta(user_session)
        user_meta["payload"]["model_instructions"] = oversized_instructions
        self.write_transcript(
            [
                user_meta,
                self.message("user", "Long user metadata still belongs to the user."),
            ]
        )
        self.assertGreater(len(json.dumps(user_meta)), 65_536)
        self.assertEqual(self.hook(user_session, "SessionEnd").returncode, 0)
        user_result = self.drain()
        self.assertEqual(user_result.returncode, 0, user_result.stderr)
        note = self.note_path().read_text(encoding="utf-8")
        self.assertIn("Long user metadata still belongs to the user.", note)
        self.assertTrue(self.checkpoint(user_session)["user_owned"])

    def test_chatter_only_summary_advances_without_empty_entry(self):
        session_id = "chatter-session"
        self.write_transcript(
            [self.session_meta(session_id), self.message("user", "Hello there.")]
        )
        self.assertEqual(self.hook(session_id, "Stop").returncode, 0)
        result = self.drain(self.noop_fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.note_path().exists())
        self.assertEqual(self.checkpoint(session_id)["cursor"], self.transcript.stat().st_size)

        self.write_transcript(
            [self.message("user", "Implemented the useful part.")], append=True
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        self.assertEqual(self.drain().returncode, 0)
        before = self.note_path().read_text(encoding="utf-8")
        self.write_transcript([self.message("user", "Thanks!")], append=True)
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        self.assertEqual(self.drain(self.noop_fake).returncode, 0)
        self.assertEqual(self.note_path().read_text(encoding="utf-8"), before)

    def test_hook_reports_snapshot_failure_without_non_json_stdout(self):
        result = self.hook(
            "missing-transcript",
            "SessionEnd",
            transcript=self.root / "missing.jsonl",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("worklog hook:", result.stderr)

    def test_local_scope_is_read_for_each_capture_without_changing_the_hook(self):
        session_id = "scoped-session"
        self.write_transcript([
            self.session_meta(session_id),
            self.message("user", "Investigate the customer support export."),
        ])
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        request_path = self.root / "last_request.json"
        self.assertEqual(json.loads(request_path.read_text())["journal_scope"], "")

        scope = "Only work for my employer; exclude personal projects."
        (self.state / "scope.txt").write_text(scope, encoding="utf-8")
        self.write_transcript([
            self.message("assistant", "Found the export failure's cause."),
        ], append=True)
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(request_path.read_text())["journal_scope"], scope)
        self.assertEqual(self.checkpoint(session_id)["cursor"], self.transcript.stat().st_size)

    def test_first_capture_starts_today_in_a_resumed_transcript(self):
        session_id = "resumed-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Old work should not be backfilled.", yesterday),
                self.message("user", "Current work belongs in the diary."),
            ]
        )
        self.assertEqual(self.hook(session_id, "Stop").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        note = self.note_path().read_text(encoding="utf-8")
        self.assertIn("Current work belongs in the diary.", note)
        self.assertNotIn("Old work should not be backfilled.", note)
        self.assertFalse(self.note_path(yesterday).exists())

    def test_new_day_capture_keeps_task_purpose_as_background_only(self):
        session_id = "work-purpose-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        purpose = "Build invoice export for the finance team's month-end close."
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", purpose, yesterday),
                self.message("user", "Private injected rules.", yesterday,
                             kinds=["agents_md"]),
                self.message("assistant", "Started the export.", yesterday),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        first_drain = self.drain()
        self.assertEqual(first_drain.returncode, 0, first_drain.stderr)
        yesterday_note = self.note_path(yesterday).read_text(encoding="utf-8")

        self.write_transcript(
            [self.message("assistant", "Export now includes invoice dates.")],
            append=True,
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads((self.root / "last_request.json").read_text())
        self.assertIn(purpose, request["conversation_requests"])
        self.assertNotIn("Private injected rules", request["conversation_requests"])
        self.assertEqual(request["existing_entry"], "")
        self.assertEqual(request["day_topics"], [])
        self.assertEqual([m["text"] for m in request["messages"]],
                         ["Export now includes invoice dates."])
        self.assertNotIn(purpose, self.note_path().read_text(encoding="utf-8"))
        self.assertEqual(self.note_path(yesterday).read_text(encoding="utf-8"),
                         yesterday_note)

    def test_progress_chatter_is_skipped_but_final_and_legacy_work_are_captured(self):
        session_id = "completed-work-session"
        self.write_transcript([
            self.session_meta(session_id),
            self.message("user", "Build invoice export."),
            self.message("assistant", "Opened files; running tests.", phase="commentary"),
            self.message("assistant", "Invoice export is ready for Finance.", phase="final_answer"),
            self.message("assistant", "Release still needs Finance approval."),
        ])
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.note_path().read_text(encoding="utf-8")
        self.assertIn("Build invoice export.", before)
        self.assertIn("Invoice export is ready for Finance.", before)
        self.assertIn("Release still needs Finance approval.", before)
        self.assertNotIn("Opened files", before)

        self.write_transcript([
            self.message("assistant", "Tweaked a filter; more tests pass.", phase="commentary"),
        ], append=True)
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain(self.fail_fake)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.note_path().read_text(encoding="utf-8"), before)
        self.assertEqual(self.checkpoint(session_id)["cursor"], self.transcript.stat().st_size)

    def test_request_context_keeps_original_and_recent_goals_with_bounded_size(self):
        messages = [
            {"role": "user", "timestamp": "2026-09-23", "text": "Build invoice export."},
            {"role": "assistant", "timestamp": "2026-09-23", "text": "Internal steps."},
            {"role": "user", "timestamp": "2026-09-23", "text": "x" * 50_000},
            {"role": "user", "timestamp": "2026-09-24", "text": "Prioritize duplicate invoices."},
        ]
        context = worklog_module.conversation_requests(messages)
        self.assertIn("Build invoice export.", context)
        self.assertIn("Prioritize duplicate invoices.", context)
        self.assertNotIn("Internal steps.", context)
        self.assertLess(len(context), 13_000)

    def test_first_stop_keeps_request_before_midnight_and_result_afterward(self):
        session_id = "midnight-result-session"
        historical = datetime.now().astimezone() - timedelta(days=3)
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Historical completed request.", historical),
                self.message("assistant", "Historical completed result.", historical),
                self.message("user", "Request started before midnight.", yesterday),
                self.message("assistant", "Result finished after midnight.", today),
            ]
        )

        self.assertEqual(self.hook(session_id, "Stop").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        yesterday_note = self.note_path(yesterday).read_text(encoding="utf-8")
        today_note = self.note_path(today).read_text(encoding="utf-8")
        self.assertIn("Request started before midnight.", yesterday_note)
        self.assertIn("Result finished after midnight.", today_note)
        combined = yesterday_note + today_note
        self.assertNotIn("Historical completed request.", combined)
        self.assertNotIn("Historical completed result.", combined)

    def test_first_session_end_keeps_interrupted_request_before_midnight(self):
        session_id = "midnight-interrupted-session"
        historical = datetime.now().astimezone() - timedelta(days=3)
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Older finished request.", historical),
                self.message("assistant", "Older finished result.", historical),
                self.message("user", "Interrupted request before midnight.", yesterday),
            ]
        )

        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        note = self.note_path(yesterday).read_text(encoding="utf-8")
        self.assertIn("Interrupted request before midnight.", note)
        self.assertNotIn("Older finished request.", note)
        self.assertFalse(self.note_path().exists())

    def test_cross_midnight_first_turn_survives_failure_and_retry(self):
        session_id = "midnight-retry-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Retry request from before midnight.", yesterday),
                self.message("assistant", "Retry result after midnight.", today),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        self.assertEqual(self.drain(self.fail_fake).returncode, 1)
        self.assertEqual(len(self.failed_jobs()), 1)

        retry = self.run_cli(["retry", "--state-dir", str(self.state)])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        recovered = self.drain()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn(
            "Retry request from before midnight.",
            self.note_path(yesterday).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Retry result after midnight.",
            self.note_path(today).read_text(encoding="utf-8"),
        )

    def test_interruption_marker_does_not_replace_prior_day_human_turn(self):
        session_id = "interrupted-marker-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        marker_text = "The user interrupted the previous turn intentionally."
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message(
                    "user",
                    "Handle generic.turn_aborted without losing human work.",
                    yesterday,
                ),
                self.message(
                    "assistant",
                    "The parser repair was still unfinished.",
                    yesterday,
                ),
                self.message(
                    "user",
                    marker_text,
                    today,
                    ["generic.turn_aborted"],
                ),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        self.assertEqual(self.drain(self.fail_fake).returncode, 1)
        self.assertEqual(len(self.failed_jobs()), 1)

        retry = self.run_cli(["retry", "--state-dir", str(self.state)])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        recovered = self.drain()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

        note = self.note_path(yesterday).read_text(encoding="utf-8")
        self.assertIn("Handle generic.turn_aborted without losing human work.", note)
        self.assertIn("The parser repair was still unfinished.", note)
        self.assertNotIn(marker_text, note)
        self.assertEqual(note.count("generic.turn_aborted"), 1)
        self.assertEqual(note.count("<!-- worklog:"), 2)
        self.assertFalse(self.note_path(today).exists())
        self.assertEqual(self.failed_jobs(), [])
        self.assertEqual(self.checkpoint(session_id)["cursor"], self.transcript.stat().st_size)

    def test_delayed_first_drain_uses_the_enqueue_date(self):
        session_id = "overnight-session"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        self.write_transcript(
            [
                self.session_meta(session_id),
                self.message("user", "Finish queued before midnight.", yesterday),
            ]
        )
        self.assertEqual(self.hook(session_id, "SessionEnd").returncode, 0)
        job_path = self.pending_jobs()[0]
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job["queued_at"] = yesterday.isoformat()
        job_path.write_text(json.dumps(job), encoding="utf-8")

        result = self.drain()
        self.assertEqual(result.returncode, 0, result.stderr)
        note = self.note_path(yesterday).read_text(encoding="utf-8")
        self.assertIn("Finish queued before midnight.", note)


if __name__ == "__main__":
    unittest.main()
