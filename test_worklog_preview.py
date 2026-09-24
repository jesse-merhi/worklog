#!/usr/bin/env python3
"""Capture-source and onboarding-preview behavior for Worklog."""

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import worklog


SCRIPT = Path(__file__).with_name("worklog.py")


class WorklogPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.vault = self.root / "vault"
        self.policy = self.root / "candidate.txt"
        self.policy.write_text("Only customer launch work.\n", encoding="utf-8")
        self.fake = self.root / "summarizer.py"
        self.fake.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
request = json.load(sys.stdin)
Path(__file__).with_name('request.json').write_text(json.dumps(request))
if '--safe-mode' in args:
    required = ['--print', '--disable-slash-commands', '--strict-mcp-config',
                '--no-session-persistence', '--output-format', '--json-schema',
                '--permission-prompts', '--model', '--tools', '--mcp-config']
    if any(flag not in args for flag in required):
        sys.exit(2)
    if args[args.index('--tools') + 1] != '':
        sys.exit(3)
    if args[args.index('--permission-prompts') + 1] != 'none':
        sys.exit(4)
    if json.loads(args[args.index('--mcp-config') + 1]) != {'mcpServers': {}}:
        sys.exit(5)
    if os.environ.get('CLAUDECODE') or os.environ.get('CLAUDE_CODE_SESSION_ID'):
        sys.exit(6)
    mode = Path(__file__).with_name('mode.txt')
    if mode.exists() and mode.read_text() == 'invalid':
        print(json.dumps({'type': 'result', 'subtype': 'success',
                          'is_error': False, 'structured_output': {'title': 'bad'}}))
        sys.exit(0)
    if mode.exists() and mode.read_text() == 'error':
        print(json.dumps({'type': 'result', 'subtype': 'error_during_execution',
                          'is_error': True, 'structured_output':
                          {'title': 'Work', 'bullets': ['Untrusted result.']}}))
        sys.exit(0)
    output = {'type': 'result', 'subtype': 'success', 'is_error': False,
              'structured_output': {'title': 'Work', 'bullets': [
                  item['text'] for item in request['messages']]}}
    print(json.dumps(output))
else:
    if '--ignore-user-config' not in args or '--output-last-message' not in args:
        sys.exit(7)
    output = Path(args[args.index('--output-last-message') + 1])
    mode = Path(__file__).with_name('mode.txt')
    if mode.exists() and mode.read_text() == 'link':
        bullets = ['Review [PR](https://github.com/example/project/pull/42).']
    elif mode.exists() and mode.read_text() == 'skip':
        bullets = []
    else:
        bullets = [item['text'] for item in request['messages']]
    output.write_text(json.dumps({'title': 'Work', 'bullets': bullets}))
""",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)

    def cli(self, *args, stdin=None, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=env,
        )

    def write_lines(self, path, records):
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def claude_record(self, role, content, session_id="same", **extra):
        return {
            "type": role,
            "sessionId": session_id,
            "timestamp": datetime.now().astimezone().isoformat(),
            "isSidechain": False,
            "message": {"role": role, "content": content},
            **extra,
        }

    def codex_records(self, session_id="same", text="Ship launch notes."):
        now = datetime.now().astimezone().isoformat()
        return [
            {"type": "session_meta", "timestamp": now,
             "payload": {"id": session_id, "source": "cli", "thread_source": "user"}},
            {"type": "response_item", "timestamp": now,
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": text}]}},
        ]

    def hook(self, transcript, source, event_name="SessionEnd"):
        event = {"hook_event_name": event_name, "session_id": "same",
                 "transcript_path": str(transcript), "cwd": str(self.root)}
        return self.cli("hook", "--state-dir", self.state, "--source", source,
                        stdin=json.dumps(event))

    def drain(self, harness):
        return self.cli("drain", "--state-dir", self.state,
                        "--vault", self.vault, "--codex-bin", self.fake,
                        "--claude-bin", self.fake, "--harness", harness,
                        "--model", "test-model")

    def preview(self, transcript, source, output, *extra):
        return self.cli("preview", "--transcript", transcript,
                        "--source", source, "--policy-file", self.policy,
                        "--output-dir", output, "--codex-bin", self.fake,
                        "--claude-bin", self.fake, *extra)

    def test_claude_parser_skips_tools_injected_text_and_sidechains(self):
        transcript = self.root / "claude.jsonl"
        records = [
            self.claude_record("user", "Build export.\n<system-reminder>private rule</system-reminder>"),
            self.claude_record("assistant", [
                {"type": "thinking", "thinking": "internal"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "Export ready."},
            ]),
            self.claude_record("user", [{"type": "tool_result", "content": "secret"}]),
            self.claude_record("user", [{"type": "text", "text": "injected companion"}],
                               isMeta=True, sourceToolUseID="tool-1"),
            self.claude_record("assistant", [{"type": "text", "text": "sidechain"}],
                               isSidechain=True),
            self.claude_record("assistant", [{"type": "text", "text": "subagent"}],
                               agentId="agent-1"),
        ]
        self.write_lines(transcript, records)
        messages, cursor = worklog.claude_messages(transcript, 0, transcript.stat().st_size,
                                                   "claude:same")
        self.assertEqual([item["text"] for item in messages], ["Build export.", "Export ready."])
        self.assertEqual(cursor, transcript.stat().st_size)

    def test_claude_parser_keeps_text_around_reminders_and_skips_bash_envelopes(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [
            self.claude_record("user", "<system-reminder>worktree metadata</system-reminder>Plan launch."),
            self.claude_record("user", "Review <system-reminder>private rule</system-reminder>the draft."),
            self.claude_record("user", "Publish notes.<system-reminder>private rule</system-reminder>"),
            self.claude_record("user", "<bash-input>pwd</bash-input>\n<bash-stdout>/private/path</bash-stdout>\n<bash-stderr></bash-stderr>"),
            self.claude_record("user", "<local-command-caveat>command output</local-command-caveat>"),
        ])
        messages, _ = worklog.claude_messages(transcript, 0, transcript.stat().st_size,
                                              "claude:same")
        self.assertEqual([item["text"] for item in messages],
                         ["Plan launch.", "Review the draft.", "Publish notes."])

    def test_claude_later_user_activity_recovers_after_empty_stop(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [self.claude_record("assistant", "Ready for a request.")])
        self.assertEqual(self.hook(transcript, "claude", "Stop").returncode, 0)
        first = self.drain("codex")
        self.assertEqual(first.returncode, 0, first.stderr)
        checkpoint = next((self.state / "checkpoints").glob("*.json"))
        self.assertFalse(json.loads(checkpoint.read_text())["user_owned"])
        self.assertFalse((self.root / "request.json").exists())
        self.assertFalse(self.vault.exists())

        with transcript.open("a", encoding="utf-8") as handle:
            for item in (
                self.claude_record("user", "<system-reminder>worktree metadata</system-reminder>Finish launch notes."),
                self.claude_record("assistant", "Launch notes are ready."),
            ):
                handle.write(json.dumps(item) + "\n")
        self.assertEqual(self.hook(transcript, "claude", "SessionEnd").returncode, 0)
        final = self.drain("codex")
        self.assertEqual(final.returncode, 0, final.stderr)
        note = next(self.vault.rglob("*.md")).read_text()
        self.assertIn("Finish launch notes.", note)
        self.assertIn("Launch notes are ready.", note)
        self.assertNotIn("worktree metadata", note)
        self.assertTrue(json.loads(checkpoint.read_text())["user_owned"])
        self.assertEqual(list((self.state / "failed").glob("*.json")), [])

    def test_source_and_summarizer_are_independent_and_ids_do_not_collide(self):
        codex = self.root / "codex.jsonl"
        claude = self.root / "claude.jsonl"
        self.write_lines(codex, self.codex_records())
        self.write_lines(claude, [self.claude_record("user", "Finish launch guide.")])
        self.assertEqual(self.hook(codex, "codex").returncode, 0)
        self.assertEqual(self.drain("claude").returncode, 0)
        self.assertEqual(self.hook(claude, "claude").returncode, 0)
        job = json.loads(next((self.state / "pending").glob("*.json")).read_text())
        self.assertEqual(job["source"], "claude")
        self.assertEqual(job["session_id"], "claude:same")
        self.assertEqual(self.drain("codex").returncode, 0)
        note = next(self.vault.rglob("*.md")).read_text()
        self.assertIn("codex://threads/same", note)
        self.assertIn("claude --resume same", note)
        self.assertIn("Ship launch notes.", note)
        self.assertIn("Finish launch guide.", note)
        self.assertEqual(note.count("<!-- worklog:"), 4)
        self.assertEqual(len(list((self.state / "checkpoints").glob("*.json"))), 2)

    def test_claude_invalid_structured_output_fails_then_retries_snapshot(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [self.claude_record("user", "Review launch plan.")])
        self.assertEqual(self.hook(transcript, "claude").returncode, 0)
        (self.root / "mode.txt").write_text("invalid")
        env = dict(os.environ, CLAUDECODE="nested", CLAUDE_CODE_SESSION_ID="parent")
        failed = self.cli("drain", "--state-dir", self.state, "--vault", self.vault,
                          "--harness", "claude", "--claude-bin", self.fake,
                          "--model", "test-model", env=env)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(len(list((self.state / "failed").glob("*.json"))), 1)
        self.assertEqual(list((self.state / "checkpoints").glob("*.json")), [])
        self.assertFalse(self.vault.exists())
        (self.root / "mode.txt").write_text("good")
        self.assertEqual(self.cli("retry", "--state-dir", self.state).returncode, 0)
        self.assertEqual(self.drain("claude").returncode, 0)
        self.assertIn("Review launch plan.", next(self.vault.rglob("*.md")).read_text())
        self.assertEqual(list((self.state / "failed").glob("*.json")), [])

    def test_claude_result_error_is_not_written_to_note(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [self.claude_record("user", "Review launch plan.")])
        self.assertEqual(self.hook(transcript, "claude").returncode, 0)
        (self.root / "mode.txt").write_text("error")
        result = self.drain("claude")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(list((self.state / "failed").glob("*.json"))), 1)
        self.assertFalse(self.vault.exists())

    def test_invented_source_url_is_rejected(self):
        prompt = worklog.summary_prompt("", "", "", "", [
            {"role": "user", "text": "Review launch plan.", "timestamp": "2026-09-24T00:00:00+00:00",
             "date": "2026-09-24"}
        ])
        with self.assertRaisesRegex(worklog.WorklogError, "invented a source URL"):
            worklog.checked_summary(
                {"title": "Launch", "bullets": ["Review at https://example.com/ticket/1"]},
                prompt,
            )

    def test_quoted_source_url_can_be_rendered_as_markdown_link(self):
        transcript = self.root / "codex.jsonl"
        self.write_lines(transcript, self.codex_records(
            text="Review `https://github.com/example/project/pull/42` for launch."))
        (self.root / "mode.txt").write_text("link")
        self.assertEqual(self.hook(transcript, "codex").returncode, 0)
        result = self.drain("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        note = next(self.vault.rglob("*.md")).read_text()
        self.assertIn("[PR](https://github.com/example/project/pull/42)", note)
        self.assertEqual(list((self.state / "failed").glob("*.json")), [])

    def test_ordinary_quote_delimiters_do_not_change_source_url(self):
        url = "https://github.com/example/project/pull/42"
        for quoted in (f'"{url}"', f"'{url}'", f"“{url}”", f"‘{url}’"):
            with self.subTest(quoted=quoted):
                prompt = worklog.summary_prompt("", "", quoted, "", [])
                summary = worklog.checked_summary(
                    {"title": "Launch", "bullets": [f"Review [PR]({url})"]}, prompt)
                self.assertEqual(summary["bullets"], [f"Review [PR]({url})"])

    def test_claude_subagent_hook_does_not_queue_work(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [self.claude_record("user", "Subagent text.")])
        event = {"hook_event_name": "SessionEnd", "session_id": "same",
                 "transcript_path": str(transcript), "agent_id": "agent-1"}
        result = self.cli("hook", "--source", "claude", "--state-dir", self.state,
                          stdin=json.dumps(event))
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())

    def test_preview_is_private_and_uses_latest_activity_with_prior_request_as_context(self):
        transcript = self.root / "codex.jsonl"
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        today = datetime.now().astimezone()
        records = self.codex_records(text="Prepare launch memo.")
        records[1]["timestamp"] = yesterday.isoformat()
        records.append({"type": "response_item", "timestamp": today.isoformat(),
                        "payload": {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "Memo ready at https://example.com/doc/1"}]}})
        self.write_lines(transcript, records)
        before_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
        output = self.root / "preview-one"
        result = self.preview(transcript, "codex", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "included")
        self.assertEqual(receipt["date"], today.date().isoformat())
        self.assertEqual(receipt["transcript_sha256"], before_hash)
        self.assertEqual(receipt["policy_sha256"], hashlib.sha256(self.policy.read_bytes()).hexdigest())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        request = json.loads((self.root / "request.json").read_text())
        self.assertIn("Prepare launch memo.", request["conversation_requests"])
        self.assertEqual([item["text"] for item in request["messages"]],
                         ["Memo ready at https://example.com/doc/1"])
        self.assertIn("https://example.com/doc/1", (output / "preview.md").read_text())
        self.assertEqual(hashlib.sha256(transcript.read_bytes()).hexdigest(), before_hash)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.vault.exists())

    def test_policy_requires_matching_preview_and_backs_up_existing_scope(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [self.claude_record(
            "user", "Ship customer launch.", cwd=str(self.root))])
        output = self.root / "preview-one"
        result = self.preview(transcript, "claude", output, "--harness", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "request.json").read_text())[
            "working_directory"], str(self.root))
        self.assertIn("claude --resume same", (output / "preview.md").read_text())
        self.state.mkdir()
        scope = self.state / "scope.txt"
        scope.write_text("Original private scope.\n", encoding="utf-8")
        self.policy.write_text("Changed after preview.\n", encoding="utf-8")
        denied = self.cli("policy", "--file", self.policy, "--preview", output,
                          "--state-dir", self.state)
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(scope.read_text(), "Original private scope.\n")
        self.assertEqual(list(self.state.glob("scope.txt.backup-*")), [])
        self.policy.write_text("Only customer launch work.\n", encoding="utf-8")
        applied = self.cli("policy", "--file", self.policy, "--preview", output,
                           "--state-dir", self.state)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(scope.read_text(), self.policy.read_text())
        backups = list(self.state.glob("scope.txt.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "Original private scope.\n")
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_long_preview_retains_completed_work_when_later_chunk_has_no_update(self):
        transcript = self.root / "long.jsonl"
        self.write_lines(transcript, self.codex_records(
            text="Review customer deliverable. " + "Reference detail. " * 3000))
        output = self.root / "preview-long"
        with patch.object(worklog, "summarize", side_effect=[
            {"title": "Customer delivery", "bullets": ["Completed the customer launch."]},
            {"title": "Nothing new", "bullets": []},
        ]):
            worklog.preview_command(transcript, "codex", self.policy, output, None,
                                    "codex", str(self.fake), None, "test-model", None)
        preview = (output / "preview.md").read_text()
        self.assertIn("Completed the customer launch.", preview)
        self.assertIn("codex://threads/same", preview)
        self.assertEqual(json.loads((output / "receipt.json").read_text())["status"], "included")

    def test_preview_skips_non_user_claude_session_without_model_call(self):
        transcript = self.root / "claude.jsonl"
        self.write_lines(transcript, [self.claude_record("assistant", [{"type": "tool_use"}])])
        output = self.root / "preview-empty"
        result = self.preview(transcript, "claude", output, "--harness", "claude",
                              "--model", "test-model")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "skipped")
        self.assertFalse((self.root / "request.json").exists())
        self.assertFalse(self.state.exists())

    def test_skipped_previews_keep_source_reference_without_writing_notes(self):
        (self.root / "mode.txt").write_text("skip")
        for source, records, reference in (
            ("codex", self.codex_records(), "codex://threads/same"),
            ("claude", [self.claude_record("user", "Plan launch.")], "claude --resume same"),
        ):
            with self.subTest(source=source):
                transcript = self.root / f"{source}.jsonl"
                self.write_lines(transcript, records)
                output = self.root / f"preview-{source}"
                result = self.preview(transcript, source, output)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((output / "receipt.json").read_text())["status"],
                                 "skipped")
                preview = (output / "preview.md").read_text()
                self.assertIn(reference, preview)
                self.assertIn("No work matched the policy", preview)
                self.assertNotIn("<!-- worklog:", preview)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.vault.exists())

    def test_preview_skips_empty_transcript_without_model_call(self):
        transcript = self.root / "empty.jsonl"
        transcript.write_bytes(b"")
        output = self.root / "preview-empty"
        result = self.preview(transcript, "codex", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "skipped")
        self.assertIsNone(receipt["session_id"])
        self.assertEqual(receipt["transcript_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertFalse((self.root / "request.json").exists())

    def test_claude_requires_explicit_model_before_mutating_state(self):
        result = self.cli("drain", "--state-dir", self.state, "--harness", "claude",
                          "--claude-bin", self.fake)
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires --model", result.stderr)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
