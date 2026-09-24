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
mode = Path(__file__).with_name('mode.txt')

def shared_summary():
    title = next((topic for topic in request['day_topics']
                  if topic.startswith('Nimbus telemetry ')),
                 'Nimbus telemetry storage and access review')
    prior = [line[2:] for line in request['existing_entry'].splitlines()
             if line.startswith('- ')]
    bullets = list(dict.fromkeys(prior + [item['text'] for item in request['messages']]))
    return {'title': title, 'bullets': bullets}

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
    if mode.exists() and mode.read_text() == 'invalid':
        print(json.dumps({'type': 'result', 'subtype': 'success',
                          'is_error': False, 'structured_output': {'title': 'bad'}}))
        sys.exit(0)
    if mode.exists() and mode.read_text() == 'error':
        print(json.dumps({'type': 'result', 'subtype': 'error_during_execution',
                          'is_error': True, 'structured_output':
                          {'title': 'Work', 'bullets': ['Untrusted result.']}}))
        sys.exit(0)
    summary = shared_summary() if mode.exists() and mode.read_text() == 'shared' else {
        'title': 'Work', 'bullets': [item['text'] for item in request['messages']]}
    output = {'type': 'result', 'subtype': 'success', 'is_error': False,
              'structured_output': summary}
    print(json.dumps(output))
else:
    if '--ignore-user-config' not in args or '--output-last-message' not in args:
        sys.exit(7)
    output = Path(args[args.index('--output-last-message') + 1])
    if mode.exists() and mode.read_text() == 'link':
        bullets = ['Review [PR](https://github.com/example/project/pull/42).']
    elif mode.exists() and mode.read_text() == 'skip':
        bullets = []
    else:
        bullets = [item['text'] for item in request['messages']]
    summary = shared_summary() if mode.exists() and mode.read_text() == 'shared' else {
        'title': 'Work', 'bullets': bullets}
    output.write_text(json.dumps(summary))
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

    def test_related_sources_share_heading_and_keep_independent_updates(self):
        (self.root / "mode.txt").write_text("shared")
        codex = self.root / "codex.jsonl"
        claude = self.root / "claude.jsonl"
        self.write_lines(codex, self.codex_records(
            text="Nimbus telemetry storage and access review: map storage."))
        self.write_lines(claude, [self.claude_record(
            "user", "Nimbus telemetry access review: confirm access.")])

        self.assertEqual(self.hook(codex, "codex").returncode, 0)
        first = self.drain("codex")
        self.assertEqual(first.returncode, 0, first.stderr)
        note_path = next(self.vault.rglob("*.md"))
        manual = "\nManual note: keep this paragraph in place.\n"
        note_path.write_text(note_path.read_text() + manual, encoding="utf-8")

        self.assertEqual(self.hook(claude, "claude").returncode, 0)
        second = self.drain("claude")
        self.assertEqual(second.returncode, 0, second.stderr)
        request = json.loads((self.root / "request.json").read_text())
        self.assertEqual(request["day_topics"],
                         ["Nimbus telemetry storage and access review"])
        self.assertEqual(request["existing_entry"], "")
        self.assertNotIn("map storage.", json.dumps(request))
        self.assertNotIn("codex://threads/same", json.dumps(request))
        note = note_path.read_text()
        self.assertEqual(note.count("## Nimbus telemetry storage and access review"), 1)
        self.assertNotIn("## Nimbus telemetry access review", note)
        self.assertIn("codex://threads/same", note)
        self.assertIn("claude --resume same", note)
        self.assertIn("map storage.", worklog.existing_entry(note, "same"))
        self.assertNotIn("confirm access.", worklog.existing_entry(note, "same"))
        self.assertIn("confirm access.", worklog.existing_entry(note, "claude:same"))
        self.assertNotIn("map storage.", worklog.existing_entry(note, "claude:same"))
        self.assertEqual(note.count(manual), 1)

        with codex.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.codex_records(
                text="Nimbus telemetry: verify storage retention.")[1]) + "\n")
        self.assertEqual(self.hook(codex, "codex").returncode, 0)
        updated = self.drain("codex")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        note = note_path.read_text()
        self.assertEqual(note.count("## Nimbus telemetry storage and access review"), 1)
        self.assertIn("verify storage retention.", worklog.existing_entry(note, "same"))
        self.assertIn("confirm access.", worklog.existing_entry(note, "claude:same"))

        with claude.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.claude_record(
                "assistant", "Nimbus telemetry: document access controls.")) + "\n")
        self.assertEqual(self.hook(claude, "claude").returncode, 0)
        updated = self.drain("claude")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        request = json.loads((self.root / "request.json").read_text())
        self.assertIn("## Nimbus telemetry storage and access review",
                      request["existing_entry"])
        note = note_path.read_text()
        self.assertEqual(note.count("## Nimbus telemetry storage and access review"), 1)
        self.assertIn("verify storage retention.", worklog.existing_entry(note, "same"))
        self.assertIn("document access controls.",
                      worklog.existing_entry(note, "claude:same"))
        self.assertEqual(note.count(manual), 1)
        self.assertEqual(len(list((self.state / "checkpoints").glob("*.json"))), 2)
        self.assertEqual(json.loads(worklog.checkpoint_path(
            self.state, "same").read_text())["cursor"], codex.stat().st_size)
        self.assertEqual(json.loads(worklog.checkpoint_path(
            self.state, "claude:same").read_text())["cursor"], claude.stat().st_size)

    def test_unrelated_capture_preserves_colliding_legacy_topics(self):
        billing = worklog.render_entry("billing", {
            "title": "Nimbus", "bullets": ["Prepared the billing CSV export."],
        })
        retention = worklog.render_entry("retention", {
            "title": "Nimbus", "bullets": ["Reviewed diagnostic retention."],
        })
        note_path = self.vault / "daily_notes" / datetime.now().astimezone().strftime("%d-%m-%Y.md")
        note_path.parent.mkdir(parents=True)
        old_note = billing + "\n\n" + retention + "\n"
        note_path.write_text(old_note, encoding="utf-8")
        transcript = self.root / "codex.jsonl"
        self.write_lines(transcript, self.codex_records(text="Prepare the Orion launch guide."))

        self.assertEqual(self.hook(transcript, "codex").returncode, 0)
        result = self.drain("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads((self.root / "request.json").read_text())
        self.assertEqual(request["day_topics"], [])
        self.assertEqual(request["existing_entry"], "")
        note = note_path.read_text(encoding="utf-8")
        self.assertTrue(note.startswith(old_note))
        self.assertEqual(note.count("## Nimbus\n"), 2)
        self.assertEqual(note.count("## Work\n"), 1)
        self.assertIn("Prepare the Orion launch guide.", note)

        revised_billing = worklog.render_entry("billing", {
            "title": "Nimbus", "bullets": ["Exported the billing CSV."],
        })
        updated = worklog.upsert_entry(note, "billing", revised_billing)
        self.assertEqual(updated.count("## Nimbus\n"), 2)
        self.assertIn(retention, updated)
        third = worklog.render_entry("third", {
            "title": "Nimbus", "bullets": ["Reviewed launch pricing."],
        })
        ambiguous = worklog.upsert_entry(updated, "third", third)
        self.assertEqual(ambiguous.count("## Nimbus\n"), 3)
        self.assertIn("Exported the billing CSV.", ambiguous)
        self.assertIn("Reviewed diagnostic retention.", ambiguous)

    def test_known_group_survives_member_updates_and_title_changes(self):
        def rendered(source, title, detail):
            return worklog.render_entry(source, {"title": title, "bullets": [detail]})

        title = "Nimbus telemetry storage and access review"
        first = rendered("first", title, "Mapped storage.")
        second = rendered("second", title, "Confirmed access.")
        third = rendered("third", title, "Checked retention.")
        note = worklog.upsert_entry("", "first", first)
        note = worklog.upsert_entry(note, "second", second)
        note = worklog.upsert_entry(note, "third", third)
        self.assertEqual(note.count("## " + title), 1)

        updated = rendered("second", title, "Confirmed access policy.")
        note = worklog.upsert_entry(note, "second", updated)
        self.assertEqual(note.count("## " + title), 1)
        self.assertIn("Confirmed access policy.", worklog.existing_entry(note, "second"))
        self.assertIn("Checked retention.", worklog.existing_entry(note, "third"))

        changed = rendered("second", "Nimbus diagnostics", "Reviewed diagnostics.")
        note = worklog.upsert_entry(note, "second", changed)
        self.assertEqual(note.count("## " + title), 1)
        self.assertEqual(note.count("## Nimbus diagnostics"), 1)
        self.assertIn("Checked retention.", worklog.existing_entry(note, "third"))
        note = worklog.upsert_entry(note, "second", updated)
        self.assertEqual(note.count("## " + title), 1)
        self.assertIn("Confirmed access policy.", worklog.existing_entry(note, "second"))

        changed = rendered("first", "Nimbus billing export", "Prepared records.")
        note = worklog.upsert_entry(note, "first", changed)
        self.assertEqual(note.count("## " + title), 1)
        self.assertEqual(note.count("## Nimbus billing export"), 1)
        self.assertIn("Confirmed access policy.", worklog.existing_entry(note, "second"))
        self.assertIn("Checked retention.", worklog.existing_entry(note, "third"))
        self.assertIn("Prepared records.", worklog.existing_entry(note, "first"))

        changed = rendered("second", "Nimbus diagnostics", "Reviewed diagnostics.")
        note = worklog.upsert_entry(note, "second", changed)
        self.assertEqual(note.count("## " + title), 1)
        self.assertIn("Checked retention.", worklog.existing_entry(note, "third"))
        self.assertIn("Reviewed diagnostics.", worklog.existing_entry(note, "second"))

    def test_changed_legacy_title_joins_only_unambiguous_topic_in_same_region(self):
        def rendered(source, title, detail):
            return worklog.render_entry(source, {"title": title, "bullets": [detail]})

        canonical = "Nimbus telemetry storage and access review"
        first = rendered("first", canonical, "Mapped storage.")
        second = rendered("second", "Nimbus telemetry access review", "Confirmed access.")
        note = first + "\n\n" + second + "\n"
        note = worklog.upsert_entry(note, "second", rendered("second", canonical, "Confirmed access."))
        self.assertEqual(note.count("## " + canonical), 1)
        self.assertIn("Mapped storage.", worklog.existing_entry(note, "first"))
        self.assertIn("Confirmed access.", worklog.existing_entry(note, "second"))

        manual = "\n\nManual decision between workstreams.\n\n"
        separated = first + manual + second + "\n"
        unchanged = worklog.upsert_entry(
            separated, "second", rendered("second", canonical, "Confirmed access."))
        self.assertIn(manual, unchanged)
        self.assertEqual(unchanged.count("## " + canonical), 2)
        self.assertLess(unchanged.index("Mapped storage."), unchanged.index(manual))
        self.assertGreater(unchanged.index("Confirmed access."), unchanged.index(manual))

    def test_manual_section_restores_heading_for_updated_hidden_group(self):
        title = "Nimbus access review"
        note = ""
        for source, detail in (("first", "Mapped access."), ("second", "Checked readers."),
                               ("third", "Confirmed retention.")):
            note = worklog.upsert_entry(note, source, worklog.render_entry(source, {
                "title": title, "bullets": [detail],
            }))
        marker = worklog.marker_pair("second")[0]
        manual = "## Handwritten planning\n\nKeep this decision.\n\n"
        note = note.replace(marker, manual + marker, 1)
        update = worklog.render_entry("third", {
            "title": title, "bullets": ["Confirmed retention policy."],
        })

        updated = worklog.upsert_entry(note, "third", update)
        self.assertEqual(updated.count("## " + title + "\n"), 2)
        self.assertEqual(updated.count(manual), 1)
        self.assertLess(updated.index(manual), updated.rindex("## " + title))
        self.assertIn("Mapped access.", worklog.existing_entry(updated, "first"))
        self.assertIn("Checked readers.", worklog.existing_entry(updated, "second"))
        self.assertIn("Confirmed retention policy.", worklog.existing_entry(updated, "third"))
        self.assertEqual(worklog.upsert_entry(updated, "third", update), updated)

    def test_distinct_topics_and_old_blocks_remain_updateable(self):
        first = worklog.render_entry("first", {
            "title": "Nimbus telemetry storage and access review",
            "bullets": ["Mapped telemetry storage."],
        })
        unrelated = worklog.render_entry("unrelated", {
            "title": "Nimbus billing export", "bullets": ["Prepared billing records."],
        })
        manual = "\n\nManual note between workstreams.\n\n"
        old_note = first + manual + unrelated + "\n"
        self.assertEqual(worklog.day_topics(old_note), [
            "Nimbus telemetry storage and access review", "Nimbus billing export"])

        related = worklog.render_entry("second", {
            "title": "Nimbus telemetry storage and access review",
            "bullets": ["Confirmed telemetry access."],
        })
        note = worklog.upsert_entry(old_note, "second", related)
        self.assertEqual(note.count("## Nimbus telemetry storage and access review"), 1)
        self.assertEqual(note.count("## Nimbus billing export"), 1)
        self.assertIn(manual, note)
        self.assertIn("Prepared billing records.", note)
        self.assertIn("Confirmed telemetry access.", worklog.existing_entry(note, "second"))

        revised = worklog.render_entry("first", {
            "title": "Nimbus telemetry storage and access review",
            "bullets": ["Mapped telemetry storage and retention."],
        })
        note = worklog.upsert_entry(note, "first", revised)
        self.assertEqual(note.count("## Nimbus telemetry storage and access review"), 1)
        self.assertIn("Confirmed telemetry access.", worklog.existing_entry(note, "second"))
        self.assertIn("Mapped telemetry storage and retention.", note)
        self.assertIn(manual, note)

        contiguous = first + "\n \n" + unrelated + "\n\n" + related + "\n"
        grouped = worklog.upsert_entry(contiguous, "second", related)
        self.assertEqual(grouped.count("## Nimbus telemetry storage and access review"), 2)
        self.assertEqual(grouped.count("## Nimbus billing export"), 1)
        self.assertGreater(grouped.index("Confirmed telemetry access."),
                           grouped.index("## Nimbus billing export"))
        self.assertEqual(grouped.count("\n \n"), 1)

    def test_malformed_markers_do_not_replace_note(self):
        entry = worklog.render_entry("new", {
            "title": "Nimbus telemetry", "bullets": ["Reviewed storage."],
        })
        start, end = worklog.marker_pair("old")
        for note in (start + "\n## Old\n", end, start + "\n## Old\n" + start + end,
                     "<!-- worklog:bad:start -->"):
            with self.subTest(note=note):
                with self.assertRaises(worklog.WorklogError):
                    worklog.upsert_entry(note, "new", entry)

        transcript = self.root / "codex.jsonl"
        self.write_lines(transcript, self.codex_records(text="Review Nimbus telemetry."))
        note_path = self.vault / "daily_notes" / datetime.now().astimezone().strftime("%d-%m-%Y.md")
        note_path.parent.mkdir(parents=True)
        damaged = "Manual note stays.\n\n" + start + "\n## Old\n"
        note_path.write_text(damaged, encoding="utf-8")
        self.assertEqual(self.hook(transcript, "codex").returncode, 0)
        result = self.drain("codex")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(note_path.read_text(encoding="utf-8"), damaged)
        self.assertEqual(list((self.state / "checkpoints").glob("*.json")), [])
        self.assertEqual(len(list((self.state / "failed").glob("*.json"))), 1)

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
        ], ["Other topic at https://example.com/unrelated"])
        with self.assertRaisesRegex(worklog.WorklogError, "invented a source URL"):
            worklog.checked_summary(
                {"title": "Launch", "bullets": ["Review at https://example.com/unrelated"]},
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
