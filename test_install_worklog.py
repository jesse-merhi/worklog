import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(__file__).with_name("install-worklog.py")
RUNTIME = INSTALLER.with_name("worklog.py")
SKILLS = ("setup-worklog", "update-worklog-policy")


class InstallWorklogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="worklog-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "original checkout"
        self.source.mkdir()
        shutil.copy2(INSTALLER, self.source / INSTALLER.name)
        shutil.copy2(RUNTIME, self.source / RUNTIME.name)
        for name in SKILLS:
            skill = self.source / "worklog-skills" / name
            (skill / "variants").mkdir(parents=True)
            (skill / "BASE.md").write_text(f"base for {name}\n")
            (skill / "SKILL.md").symlink_to("variants/gpt-6.md")
            for profile in ("gpt-6", "claude-fable-5.1", "claude-opus-5.5"):
                (skill / "variants" / f"{profile}.md").write_text(f"---\nname: {name}\n---\n{profile}\n")
        self.home = self.root / "A home with spaces"
        self.vault = self.root / "Work vault"
        self.vault.mkdir()
        self.codex = self.make_executable("codex")
        self.claude = self.make_executable("claude")
        self.codex_hooks = self.home / ".codex/hooks.json"
        self.claude_settings = self.home / ".claude/settings.json"
        self.state = self.home / "Library/Application Support/Worklog"
        self.plist = self.home / "Library/LaunchAgents/local.worklog.worker.plist"
        self.bundle = self.home / ".local/share/worklog"

    def make_executable(self, name):
        path = self.root / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def run_installer(self, *extra, script=None):
        return subprocess.run(
            [sys.executable, str(script or self.source / INSTALLER.name),
             "--home", str(self.home), "--no-load", *map(str, extra)],
            capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def install(self, *extra, script=None):
        return self.run_installer("--vault", self.vault, "--codex-bin", self.codex, *extra, script=script)

    def assert_installed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def args(self):
        return plistlib.loads(self.plist.read_bytes())["ProgramArguments"]

    def receipt(self):
        return json.loads((self.state / "installation.json").read_text())

    def make_preview(self, policy, *, source="codex", harness="codex", model="gpt-6-astra", effort="xhigh", success=True):
        path = self.root / "preview receipt.json"
        path.write_text(json.dumps({
            "success": success,
            "status": "included" if success else "skipped",
            "source": source,
            "transcript_sha256": hashlib.sha256(b"fixture transcript\n").hexdigest(),
            "policy_sha256": hashlib.sha256(policy).hexdigest(),
            "harness": harness,
            "model": model,
            "effort": effort,
        }))
        return path

    def owned_handlers(self, path, event):
        settings = json.loads(path.read_text())
        return [handler for group in settings.get("hooks", {}).get(event, [])
                for handler in group.get("hooks", [])
                if "worklog" in handler.get("command", "")]

    def home_snapshot(self):
        if not self.home.exists():
            return {}
        snapshot = {}
        for path in self.home.rglob("*"):
            if path.is_symlink():
                content = os.readlink(path)
            elif path.is_file():
                content = path.read_bytes()
            else:
                content = None
            snapshot[str(path.relative_to(self.home))] = (path.lstat().st_mode, content)
        return snapshot

    def test_fresh_worker_uses_generic_label(self):
        self.assert_installed(self.install())
        self.assertEqual([path.name for path in self.plist.parent.glob("*.plist")],
                         ["local.worklog.worker.plist"])
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["Label"], "local.worklog.worker")

    def test_ignores_malformed_unrelated_plist(self):
        self.plist.parent.mkdir(parents=True)
        unrelated = self.plist.with_name("org.example.weather.plist")
        original = b'<plist><dict><key>Label</key><string>weather & news</string></dict></plist>'
        unrelated.write_bytes(original)

        self.assert_installed(self.install())
        self.assertEqual(unrelated.read_bytes(), original)
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["Label"], "local.worklog.worker")

    def test_ignores_unrelated_plist_with_invalid_date(self):
        self.plist.parent.mkdir(parents=True)
        unrelated = self.plist.with_name("org.example.weather.plist")
        original = (b'<plist version="1.0"><dict><key>Label</key>'
                    b'<string>org.example.weather</string><key>LastRun</key>'
                    b'<date>invalid-date</date></dict></plist>')
        unrelated.write_bytes(original)

        self.assert_installed(self.install())
        self.assertEqual(unrelated.read_bytes(), original)
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["Label"], "local.worklog.worker")

    def test_rejects_malformed_default_plist_before_changes(self):
        self.plist.parent.mkdir(parents=True)
        original = b'<plist><dict><key>Label</key><string>work & log</string></dict></plist>'
        self.plist.write_bytes(original)

        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read existing Worklog worker", result.stderr)
        self.assertEqual(self.plist.read_bytes(), original)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.codex_hooks.exists())

    def test_rejects_default_plist_with_invalid_date_before_changes(self):
        self.plist.parent.mkdir(parents=True)
        original = (b'<plist version="1.0"><dict><key>Label</key>'
                    b'<string>local.worklog.worker</string><key>LastRun</key>'
                    b'<date>invalid-date</date></dict></plist>')
        self.plist.write_bytes(original)
        before = self.home_snapshot()

        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read existing Worklog worker", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.home_snapshot(), before)

    def test_reuses_owned_worker_with_different_label_and_settings(self):
        self.assert_installed(self.install("--daily-folder", "Daily work", "--model", "test-model",
                                           "--capture-sources", "codex", "claude"))
        old_path = self.plist
        worker = plistlib.loads(old_path.read_bytes())
        worker["Label"] = "local.worklog.previous"
        self.plist = old_path.with_name("local.worklog.previous.plist")
        self.plist.write_bytes(plistlib.dumps(worker))
        old_path.unlink()
        unrelated = old_path.with_name("org.example.weather.plist")
        unrelated.write_bytes(plistlib.dumps({
            "Label": "org.example.weather",
            "ProgramArguments": ["/usr/bin/python3", str(self.root / "weather.py"), "run"],
        }))
        unrelated_before = unrelated.read_bytes()

        self.assert_installed(self.run_installer())
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["Label"], "local.worklog.previous")
        self.assertEqual(self.args()[self.args().index("--state-dir") + 1], str(self.state))
        self.assertEqual(self.receipt()["vault"], str(self.vault))
        self.assertEqual(self.receipt()["model"], "test-model")
        self.assertEqual(self.receipt()["daily_folder"], "Daily work")
        self.assertEqual(self.receipt()["capture_sources"], ["codex", "claude"])
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "Stop")), 1)
        self.assertEqual(len(self.owned_handlers(self.claude_settings, "Stop")), 1)
        self.assertFalse(old_path.exists())
        self.assertEqual(unrelated.read_bytes(), unrelated_before)
        self.assertEqual(len(list(self.plist.parent.glob("*.plist"))), 2)

    def test_rejects_multiple_owned_workers_before_changes(self):
        self.plist.parent.mkdir(parents=True)
        arguments = ["/usr/bin/python3", str(self.home / ".local/bin/worklog"),
                     "drain", "--state-dir", str(self.state)]
        other = self.plist.with_name("local.worklog.other.plist")
        for path, label in ((self.plist, "local.worklog.worker"),
                            (other, "local.worklog.other")):
            command = (arguments if path == self.plist else
                       arguments[:3] + ["--model", "synthetic-model"] + arguments[3:])
            path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": command}))
        before = {path: path.read_bytes() for path in (self.plist, other)}

        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple Worklog workers", result.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.codex_hooks.exists())

    def test_rejects_conflicting_default_label_before_changes(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({
            "Label": "local.worklog.worker",
            "ProgramArguments": ["/usr/bin/python3", str(self.root / "other.py")],
        }))
        before = self.plist.read_bytes()

        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Default Worklog worker label conflicts", result.stderr)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.codex_hooks.exists())
        self.assert_installed(self.run_installer("--skills-only"))
        self.assertEqual(self.plist.read_bytes(), before)

    def test_rejects_duplicate_existing_label_before_changes(self):
        self.plist.parent.mkdir(parents=True)
        owned = self.plist.with_name("local.worklog.previous.plist")
        owned.write_bytes(plistlib.dumps({
            "Label": "local.worklog.previous",
            "ProgramArguments": ["/usr/bin/python3", str(self.home / ".local/bin/worklog"),
                                 "drain", "--state-dir", str(self.state)],
        }))
        unrelated = self.plist.with_name("org.example.weather.plist")
        unrelated.write_bytes(plistlib.dumps({
            "Label": "local.worklog.previous",
            "ProgramArguments": ["/usr/bin/python3", str(self.root / "weather.py"), "run"],
        }))
        before = {path: path.read_bytes() for path in (owned, unrelated)}

        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worker label conflicts", result.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.codex_hooks.exists())

    def test_preserves_existing_settings_and_deduplicates_codex_hooks(self):
        original = {
            "description": "My hooks", "other": {"enabled": True},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "existing-command", "timeout": 10}]}],
                "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "start-command"}]}],
            },
        }
        self.codex_hooks.parent.mkdir(parents=True)
        original_bytes = json.dumps(original).encode()
        self.codex_hooks.write_bytes(original_bytes)
        self.assert_installed(self.install())
        first = json.loads(self.codex_hooks.read_text())
        self.assertEqual(first["description"], "My hooks")
        self.assertEqual(first["other"], original["other"])
        self.assertEqual(first["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(first["hooks"]["SessionStart"], original["hooks"]["SessionStart"])
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "Stop")), 1)
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "SessionEnd")), 1)
        self.assertFalse(self.claude_settings.exists())
        backups = list((self.state / "backups").glob("codex-hooks-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_bytes)
        self.assert_installed(self.run_installer())
        self.assertEqual(json.loads(self.codex_hooks.read_text()), first)
        self.assertEqual(len(list((self.state / "backups").glob("codex-hooks-*.bak"))), 1)
        self.assertEqual(self.receipt()["capture_sources"], ["codex"])
        self.assertEqual(self.receipt()["effort"], "xhigh")
        command = self.owned_handlers(self.codex_hooks, "Stop")[0]["command"]
        self.assertEqual(shlex.split(command), ["/usr/bin/python3", str(self.home / ".local/bin/worklog"),
                                                "hook", "--state-dir", str(self.state)])
        self.assertFalse((self.home / ".local/bin/worklog").is_symlink())
        self.assertEqual((self.home / ".local/bin/worklog").read_bytes(), RUNTIME.read_bytes())

    def test_sources_and_summary_backend_are_independent_and_removal_is_selective(self):
        self.claude_settings.parent.mkdir(parents=True)
        self.claude_settings.write_text(json.dumps({
            "disableAllHooks": True,
            "permissions": {"allow": ["Read"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-command"}]}]},
        }))
        result = self.install("--capture-sources", "claude")
        self.assert_installed(result)
        self.assertIn("disableAllHooks", result.stdout)
        self.assertFalse(self.codex_hooks.exists())
        self.assertEqual(self.receipt()["harness"], "codex")
        self.assertEqual(self.receipt()["capture_sources"], ["claude"])
        self.assertIn("--codex-bin", self.args())
        claude = json.loads(self.claude_settings.read_text())
        self.assertTrue(claude["disableAllHooks"])
        self.assertEqual(claude["permissions"], {"allow": ["Read"]})
        self.assertEqual(claude["hooks"]["Stop"][0]["hooks"][0]["command"], "my-command")
        handler = self.owned_handlers(self.claude_settings, "Stop")[0]
        self.assertEqual(shlex.split(handler["command"])[-2:], ["--source", "claude"])
        self.assert_installed(self.run_installer("--capture-sources", "codex", "claude"))
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "Stop")), 1)
        self.assert_installed(self.run_installer("--capture-sources", "codex"))
        self.assertEqual(self.owned_handlers(self.claude_settings, "Stop"), [])
        self.assertEqual(json.loads(self.claude_settings.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"], "my-command")
        self.assertTrue(json.loads(self.claude_settings.read_text())["disableAllHooks"])
        self.assert_installed(self.run_installer())
        self.assertEqual(self.receipt()["capture_sources"], ["codex"])

    def test_source_removal_keeps_other_handlers_in_the_same_group(self):
        self.assert_installed(self.install("--capture-sources", "codex", "claude"))
        settings = json.loads(self.claude_settings.read_text())
        worklog = settings["hooks"]["Stop"][0]["hooks"][0]
        settings["hooks"]["Stop"] = [{"matcher": "all", "hooks": [
            {"type": "command", "command": "personal-command"}, worklog, worklog,
        ]}]
        self.claude_settings.write_text(json.dumps(settings))
        self.assert_installed(self.run_installer("--capture-sources", "codex"))
        stop = json.loads(self.claude_settings.read_text())["hooks"]["Stop"]
        self.assertEqual(stop, [{"matcher": "all", "hooks": [
            {"type": "command", "command": "personal-command"},
        ]}])
        self.assertEqual(self.owned_handlers(self.claude_settings, "SessionEnd"), [])

    def test_claude_backend_needs_explicit_model_and_only_its_executable(self):
        missing = self.root / "missing-codex"
        result = self.run_installer("--vault", self.vault, "--codex-bin", missing,
                                    "--harness", "claude", "--claude-bin", self.claude)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit --model", result.stderr)
        self.assertFalse(self.home.exists())
        self.assert_installed(self.run_installer("--vault", self.vault, "--codex-bin", missing,
                                                "--harness", "claude", "--claude-bin", self.claude,
                                                "--model", "claude-model", "--capture-sources", "codex"))
        self.assertEqual(self.receipt()["harness"], "claude")
        self.assertEqual(self.receipt()["model"], "claude-model")
        self.assertIn("--claude-bin", self.args())
        self.assertNotIn("--codex-bin", self.args())
        self.assert_installed(self.run_installer())
        self.assertEqual(self.receipt()["model"], "claude-model")

    def test_switching_summary_backend_requires_matching_preview(self):
        self.assert_installed(self.install())
        self.codex.unlink()
        denied = self.run_installer("--harness", "claude", "--model", "claude-model",
                                    "--claude-bin", self.claude)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("matching successful --preview", denied.stderr)
        self.assertEqual(self.receipt()["harness"], "codex")
        preview = self.make_preview(b"", harness="claude", model="claude-model", effort=None)
        skipped = json.loads(preview.read_text())
        skipped["status"] = "skipped"
        preview.write_text(json.dumps(skipped))
        denied = self.run_installer("--harness", "claude", "--model", "claude-model",
                                    "--claude-bin", self.claude, "--preview", preview)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("exercised the summarizer", denied.stderr)
        skipped["status"] = "included"
        preview.write_text(json.dumps(skipped))
        self.assert_installed(self.run_installer("--harness", "claude", "--model", "claude-model",
                                                "--claude-bin", self.claude, "--preview", preview))
        self.assertEqual(self.receipt()["capture_sources"], ["codex"])
        self.assertEqual(self.receipt()["harness"], "claude")
        self.assertEqual(self.receipt()["effort"], None)
        self.assertIn("--claude-bin", self.args())
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "Stop")), 1)
        self.assertFalse(self.claude_settings.exists())

    def test_policy_needs_matching_preview_and_reinstall_preserves_scope(self):
        self.assert_installed(self.install())
        old_policy = b"Keep project alpha.\n"
        scope = self.state / "scope.txt"
        scope.write_bytes(old_policy)
        candidate = self.root / "candidate scope.txt"
        candidate.write_text("Keep project beta.\n")
        wrong = self.make_preview(b"wrong policy")
        result = self.run_installer("--policy-file", candidate, "--preview", wrong)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("policy_sha256", result.stderr)
        self.assertEqual(scope.read_bytes(), old_policy)
        self.assertEqual(self.receipt()["model"], "gpt-6-astra")
        preview = self.make_preview(candidate.read_bytes())
        self.assert_installed(self.run_installer("--policy-file", candidate, "--preview", preview))
        self.assertEqual(scope.read_bytes(), candidate.read_bytes())
        backups = list(self.state.glob("scope.txt.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old_policy)
        self.assert_installed(self.run_installer())
        self.assertEqual(scope.read_bytes(), candidate.read_bytes())
        result = self.run_installer("--model", "gpt-6-new")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matching successful --preview", result.stderr)
        self.assertEqual(self.receipt()["model"], "gpt-6-astra")
        preview = self.make_preview(candidate.read_bytes(), model="gpt-6-new")
        self.assert_installed(self.run_installer("--model", "gpt-6-new", "--preview", preview))
        self.assertEqual(self.receipt()["model"], "gpt-6-new")
        self.assertEqual(scope.read_bytes(), candidate.read_bytes())

    def test_preview_must_match_source_backend_effort_and_success(self):
        self.assert_installed(self.install())
        candidate = self.root / "candidate.txt"
        candidate.write_text("Keep this work.\n")
        before = (self.state / "installation.json").read_bytes()
        for mismatch in (
            {"source": "claude"}, {"harness": "claude"}, {"model": "other"},
            {"effort": None}, {"success": False},
        ):
            with self.subTest(mismatch=mismatch):
                preview = self.make_preview(candidate.read_bytes(), **mismatch)
                result = self.run_installer("--policy-file", candidate, "--preview", preview)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.state / "scope.txt").exists())
                self.assertEqual((self.state / "installation.json").read_bytes(), before)

    def test_skills_only_bootstrap_conflict_and_owned_update(self):
        result = self.run_installer("--skills-only")
        self.assert_installed(result)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.codex_hooks.exists())
        self.assertFalse(self.claude_settings.exists())
        self.assertFalse((self.home / ".local/bin/worklog").exists())
        self.assertTrue((self.home / ".local/bin/worklog-install").is_symlink())
        self.assertEqual((self.bundle / "worklog.py").read_bytes(), RUNTIME.read_bytes())
        for name in SKILLS:
            self.assertEqual((self.home / ".codex/skills" / name / "SKILL.md").read_bytes(),
                             (self.source / "worklog-skills" / name / "variants/gpt-6.md").read_bytes())
            self.assertEqual((self.home / ".claude/skills" / name / "SKILL.md").read_bytes(),
                             (self.source / "worklog-skills" / name / "variants/claude-opus-5.5.md").read_bytes())
        variant = self.source / "worklog-skills/setup-worklog/variants/gpt-6.md"
        variant.write_text(variant.read_text() + "updated\n")
        self.assert_installed(self.run_installer("--skills-only"))
        self.assertIn("updated", (self.home / ".codex/skills/setup-worklog/SKILL.md").read_text())
        owned = self.home / ".codex/skills/setup-worklog/SKILL.md"
        owned.write_text("user change\n")
        result = self.run_installer("--skills-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)
        self.assertEqual(owned.read_text(), "user change\n")
        self.assertFalse(self.plist.exists())

    def test_skills_only_preserves_existing_runtime_policy_worker_and_hooks(self):
        self.assert_installed(self.install())
        scope = self.state / "scope.txt"
        scope.write_text("existing private policy\n")
        runtime = self.home / ".local/bin/worklog"
        runtime.write_text("existing live executable\n")
        before = {path: path.read_bytes() for path in (scope, runtime, self.plist, self.codex_hooks,
                                                     self.state / "installation.json")}
        self.assert_installed(self.run_installer("--skills-only"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_command_parent_file_rejects_both_install_modes_before_writes(self):
        bin_path = self.home / ".local/bin"
        bin_path.parent.mkdir(parents=True)
        bin_path.write_bytes(b"unrelated local file\n")
        before = self.home_snapshot()

        for options in (("--skills-only", "--dry-run"), ("--skills-only",),
                        ("--dry-run",), ()):
            with self.subTest(options=options):
                result = self.run_installer(*options) if "--skills-only" in options else self.install(*options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Expected a directory", result.stderr)
                self.assertEqual(self.home_snapshot(), before)

    def test_fresh_install_preserves_unrelated_worklog_command(self):
        command = self.home / ".local/bin/worklog"
        command.parent.mkdir(parents=True)
        command.write_bytes(b"#!/bin/sh\nprintf 'Independent journal command\\n'\n")
        command.chmod(0o755)
        before = self.home_snapshot()

        for options in (("--dry-run",), ()):
            with self.subTest(options=options):
                result = self.install(*options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not owned by Worklog", result.stderr)
                self.assertEqual(self.home_snapshot(), before)

        self.assert_installed(self.run_installer("--skills-only"))
        self.assertEqual(command.read_bytes(), b"#!/bin/sh\nprintf 'Independent journal command\\n'\n")
        before = self.home_snapshot()
        denied = self.install()
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("not owned by Worklog", denied.stderr)
        self.assertEqual(self.home_snapshot(), before)

    def test_matching_runtime_without_receipt_or_worker_can_be_installed(self):
        command = self.home / ".local/bin/worklog"
        command.parent.mkdir(parents=True)
        command.write_bytes(RUNTIME.read_bytes())
        command.chmod(0o755)

        self.assert_installed(self.install())
        self.assertEqual(command.read_bytes(), RUNTIME.read_bytes())
        self.assertEqual(self.receipt()["vault"], str(self.vault))

    def test_cached_runtime_without_receipt_or_worker_can_be_upgraded(self):
        self.assert_installed(self.run_installer("--skills-only"))
        old_runtime = (self.bundle / "worklog.py").read_bytes()
        command = self.home / ".local/bin/worklog"
        command.write_bytes(old_runtime)
        command.chmod(0o755)
        source_runtime = self.source / "worklog.py"
        source_runtime.write_bytes(old_runtime + b"\n# Synthetic runtime update.\n")

        self.assert_installed(self.install())
        self.assertEqual(command.read_bytes(), source_runtime.read_bytes())
        backups = list((self.state / "backups").glob("worklog-runtime-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old_runtime)

    def test_new_skill_collision_is_rejected_before_any_change(self):
        conflict = self.home / ".claude/skills/setup-worklog/SKILL.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("personal skill\n")
        result = self.run_installer("--skills-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)
        self.assertFalse(self.bundle.exists())
        self.assertEqual(conflict.read_text(), "personal skill\n")

    def test_cached_installer_works_after_original_checkout_is_removed(self):
        self.assert_installed(self.run_installer("--skills-only"))
        cached = self.home / ".local/bin/worklog-install"
        shutil.rmtree(self.source)
        self.assert_installed(self.run_installer("--vault", self.vault, "--codex-bin", self.codex,
                                                script=cached))
        self.assertEqual(self.receipt()["vault"], str(self.vault))
        self.assertEqual((self.home / ".local/bin/worklog").read_bytes(), (self.bundle / "worklog.py").read_bytes())
        self.assert_installed(self.run_installer(script=cached))
        self.assertTrue((self.bundle / "install-worklog.py").exists())
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "Stop")), 1)

    def test_migrates_worker_choices_without_receipt(self):
        self.assert_installed(self.install("--daily-folder", "Daily work", "--model", "older-model"))
        (self.state / "installation.json").unlink()
        self.assert_installed(self.run_installer())
        receipt = self.receipt()
        self.assertEqual(receipt["daily_folder"], "Daily work")
        self.assertEqual(receipt["model"], "older-model")
        self.assertEqual(receipt["vault"], str(self.vault))
        self.assertEqual(receipt["capture_sources"], ["codex"])

    def test_legacy_custom_state_keeps_scope_queue_hooks_and_later_reinstall(self):
        self.assert_installed(self.install())
        custom = self.home / "private worklog"
        shutil.move(str(self.state), str(custom))
        (custom / "installation.json").unlink()
        scope = custom / "scope.txt"
        scope.write_text("Only customer launch work.\n")
        old_state = str(self.state)
        worker = plistlib.loads(self.plist.read_bytes())
        worker["ProgramArguments"] = [value.replace(old_state, str(custom))
                                      for value in worker["ProgramArguments"]]
        arguments = worker["ProgramArguments"]
        worker["ProgramArguments"] = arguments[:3] + arguments[5:] + arguments[3:5]
        for key in ("QueueDirectories", "StandardOutPath", "StandardErrorPath"):
            value = worker[key]
            worker[key] = ([item.replace(old_state, str(custom)) for item in value]
                           if isinstance(value, list) else value.replace(old_state, str(custom)))
        worker["Label"] = "local.worklog.previous"
        previous_plist = self.plist.with_name("local.worklog.previous.plist")
        previous_plist.write_bytes(plistlib.dumps(worker))
        self.plist.unlink()
        self.plist = previous_plist
        hooks = json.loads(self.codex_hooks.read_text())
        for event in ("Stop", "SessionEnd"):
            for group in hooks["hooks"][event]:
                for handler in group["hooks"]:
                    handler["command"] = handler["command"].replace(
                        shlex.quote(old_state), shlex.quote(str(custom)))
        self.codex_hooks.write_text(json.dumps(hooks))
        self.codex.write_text("""#!/usr/bin/env python3
import json
from pathlib import Path
import sys

request = json.load(sys.stdin)
Path(__file__).with_name('legacy-request.json').write_text(json.dumps(request))
args = sys.argv
output = Path(args[args.index('--output-last-message') + 1])
output.write_text(json.dumps({'title': 'Customer launch',
                              'bullets': ['Captured customer launch work.']}))
""")
        self.codex.chmod(0o755)
        transcript = self.root / "legacy.jsonl"
        timestamp = datetime.now().astimezone().isoformat()
        transcript.write_text("".join(json.dumps(record) + "\n" for record in (
            {"type": "session_meta", "timestamp": timestamp,
             "payload": {"id": "legacy-custom", "source": "cli", "thread_source": "user"}},
            {"type": "response_item", "timestamp": timestamp,
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "Finish customer launch notes."}]}},
        )))
        event = {"hook_event_name": "SessionEnd", "session_id": "legacy-custom",
                 "transcript_path": str(transcript), "cwd": str(self.root)}
        queued = subprocess.run(
            [sys.executable, str(self.home / ".local/bin/worklog"), "hook",
             "--state-dir", str(custom)], input=json.dumps(event), text=True,
            capture_output=True, check=False)
        self.assertEqual(queued.returncode, 0, queued.stderr)
        self.assertEqual(len(list((custom / "pending").glob("*.json"))), 1)

        self.assert_installed(self.run_installer())
        self.assertIn(f"State: {custom}", self.run_installer("--dry-run").stdout)
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["QueueDirectories"],
                         [str(custom / "pending")])
        self.assertEqual(self.args()[self.args().index("--state-dir") + 1], str(custom))
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["Label"], "local.worklog.previous")
        self.assertFalse((self.plist.parent / "local.worklog.worker.plist").exists())
        for event_name in ("Stop", "SessionEnd"):
            commands = self.owned_handlers(self.codex_hooks, event_name)
            self.assertEqual(len(commands), 1)
            self.assertEqual(shlex.split(commands[0]["command"])[-1], str(custom))
        self.assertFalse(self.state.exists())
        self.assertEqual(scope.read_text(), "Only customer launch work.\n")

        drained = subprocess.run(self.args(), capture_output=True, text=True, check=False)
        self.assertEqual(drained.returncode, 0, drained.stderr)
        request = json.loads((self.root / "legacy-request.json").read_text())
        self.assertEqual(request["journal_scope"], "Only customer launch work.")
        self.assertIn("Captured customer launch work.",
                      next(self.vault.rglob("*.md")).read_text())
        self.assertEqual(list((custom / "pending").glob("*.json")), [])
        self.assertEqual(len(list((custom / "checkpoints").glob("*.json"))), 1)

        self.assert_installed(self.run_installer(script=self.home / ".local/bin/worklog-install"))
        self.assertEqual(self.args()[self.args().index("--state-dir") + 1], str(custom))
        self.assertEqual(json.loads((custom / "installation.json").read_text())["vault"],
                         str(self.vault))
        self.assertEqual(len(self.owned_handlers(self.codex_hooks, "Stop")), 1)
        self.assertFalse(self.state.exists())

    def test_dry_run_and_invalid_destination_do_not_mutate_home(self):
        self.assert_installed(self.install("--dry-run"))
        self.assertFalse(self.home.exists())
        invalid = self.install("--daily-folder", "../outside")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("inside the vault", invalid.stderr)
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main(failfast=True)
