#!/usr/bin/python3
"""Install Worklog from a permanent local bundle, with opt-in capture hooks."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError

from worklog import DEFAULT_MODEL, WorklogError, policy_command


LABEL = "local.worklog.worker"
SOURCES = ("codex", "claude")
SKILLS = ("setup-worklog", "update-worklog-policy")
EVENTS = ("Stop", "SessionEnd")


class InstallError(ValueError):
    pass


def write_atomic(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallError(f"Expected a JSON object in {path}")
    return value


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_destination(home: Path, path: Path, *, directory: bool = False) -> None:
    current = home
    for part in path.relative_to(home).parts:
        if current.is_symlink():
            raise InstallError(f"Refusing to write through a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise InstallError(f"Expected a directory, found a file: {current}")
        current /= part
    if current.is_symlink():
        raise InstallError(f"Refusing to write through a symlink: {current}")
    if directory and current.exists() and not current.is_dir():
        raise InstallError(f"Expected a directory, found a file: {current}")


def backup_file(path: Path, state: Path, name: str) -> None:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    write_atomic(state / "backups" / f"{name}-{stamp}.bak", path.read_bytes(), 0o600)


def backup_and_write(path: Path, content: bytes, state: Path, name: str, mode: int) -> None:
    if path.exists():
        if path.read_bytes() == content:
            return
        backup_file(path, state, name)
        mode = stat.S_IMODE(path.stat().st_mode)
    write_atomic(path, content, mode)


def option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise InstallError(f"Existing worker has no value for {option}")
    return arguments[index + 1]


def read_plist(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = plistlib.loads(path.read_bytes())
    except AttributeError as error:
        raise InstallError(f"Invalid plist in {path}: {error}") from error
    if not isinstance(value, dict):
        raise InstallError(f"Expected a plist dictionary in {path}")
    return value


def find_worker(home: Path, executable: Path) -> tuple[Path, dict, str]:
    agents = home / "Library/LaunchAgents"
    default_path = agents / (LABEL + ".plist")
    command = ["/usr/bin/python3", str(executable), "drain"]
    owned = []
    labels = {}
    for path in agents.glob("*.plist"):
        try:
            plist = read_plist(path)
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
            if path == default_path:
                raise InstallError(f"Cannot read existing Worklog worker {path}: {error}") from error
            continue
        label = plist.get("Label")
        if isinstance(label, str):
            labels.setdefault(label, []).append(path)
        arguments = plist.get("ProgramArguments")
        if isinstance(arguments, list) and arguments[:3] == command:
            if not isinstance(label, str) or not label:
                raise InstallError(f"Existing Worklog worker has no valid Label in {path}")
            owned.append((path, plist, label))
        elif path == default_path or label == LABEL:
            raise InstallError(f"Default Worklog worker label conflicts with {path}")
    if len(owned) > 1:
        raise InstallError("Multiple Worklog workers found in Library/LaunchAgents")
    if owned and len(labels[owned[0][2]]) > 1:
        raise InstallError(f"Existing Worklog worker label conflicts with another LaunchAgent: {owned[0][2]}")
    return owned[0] if owned else (default_path, {}, LABEL)


def worker_state_dir(plist: dict, default: Path) -> Path:
    arguments = plist.get("ProgramArguments", [])
    if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
        raise InstallError("Existing worker ProgramArguments must be a list of strings")
    value = option_value(arguments, "--state-dir")
    if value is None:
        return default
    state = Path(value).expanduser()
    if not state.is_absolute() or ".." in state.parts or state == Path(state.anchor):
        raise InstallError("Existing worker --state-dir must be an absolute directory path")
    return state


def prior_settings(receipt_path: Path, plist: dict) -> tuple[dict, bool]:
    receipt = read_object(receipt_path)
    if receipt:
        if receipt.get("kind") != "worklog-installation" or receipt.get("version") != 1:
            raise InstallError(f"Unrecognized installation receipt: {receipt_path}")
        required = ("capture_sources", "harness", "model", "vault", "daily_folder", "codex_bin", "claude_bin")
        if any(key not in receipt for key in required):
            raise InstallError(f"Incomplete installation receipt: {receipt_path}")
        return receipt, True
    arguments = plist.get("ProgramArguments", [])
    if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
        raise InstallError("Existing worker ProgramArguments must be a list of strings")
    if not arguments:
        return {}, False
    harness = option_value(arguments, "--harness") or "codex"
    return {
        "capture_sources": ["codex"],
        "harness": harness,
        "model": option_value(arguments, "--model") or DEFAULT_MODEL,
        "effort": option_value(arguments, "--effort") or ("xhigh" if harness == "codex" else None),
        "vault": option_value(arguments, "--vault"),
        "daily_folder": option_value(arguments, "--daily-folder") or "daily_notes",
        "codex_bin": option_value(arguments, "--codex-bin"),
        "claude_bin": option_value(arguments, "--claude-bin"),
    }, True


def resolve_settings(args: argparse.Namespace, prior: dict) -> dict:
    sources = list(dict.fromkeys(args.capture_sources)) if args.capture_sources else prior.get("capture_sources", ["codex"])
    if not isinstance(sources, list) or not sources or any(source not in SOURCES for source in sources):
        raise InstallError("Installed capture_sources must contain codex or claude")
    harness = args.harness or prior.get("harness") or "codex"
    if harness not in SOURCES:
        raise InstallError("Installed harness must be codex or claude")
    if harness == "claude" and prior.get("harness") != "claude" and not args.model:
        raise InstallError("Selecting Claude summaries requires an explicit --model")
    model = args.model or (prior.get("model") if prior.get("harness") == harness else DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise InstallError("--model must be a nonempty model name")
    if args.effort is not None:
        effort = args.effort
    elif prior.get("harness") == harness:
        effort = prior.get("effort")
    else:
        effort = "xhigh" if harness == "codex" else None
    if effort is None and harness == "codex":
        effort = "xhigh"
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise InstallError("--effort must be nonempty")
    vault_value = str(args.vault) if args.vault else prior.get("vault")
    if not vault_value:
        raise InstallError("--vault is required for a new capture installation")
    vault = Path(vault_value).expanduser().resolve()
    if not vault.is_dir():
        raise InstallError(f"Obsidian vault does not exist: {vault}")
    folder = args.daily_folder if args.daily_folder is not None else prior.get("daily_folder", "daily_notes")
    if not isinstance(folder, str) or not folder or Path(folder).is_absolute() or ".." in Path(folder).parts:
        raise InstallError("--daily-folder must stay inside the vault")
    codex_value = str(args.codex_bin) if args.codex_bin else prior.get("codex_bin") or shutil.which("codex") or "/opt/homebrew/bin/codex"
    claude_value = str(args.claude_bin) if args.claude_bin else prior.get("claude_bin") or shutil.which("claude") or "/opt/homebrew/bin/claude"
    codex = Path(codex_value).expanduser().absolute()
    claude = Path(claude_value).expanduser().absolute()
    chosen = codex if harness == "codex" else claude
    if not chosen.is_file() or not os.access(chosen, os.X_OK):
        raise InstallError(f"{harness.capitalize()} executable not found: {chosen}")
    return {
        "kind": "worklog-installation",
        "version": 1,
        "capture_sources": sources,
        "harness": harness,
        "model": model,
        "effort": effort,
        "vault": str(vault),
        "daily_folder": folder,
        "codex_bin": str(codex),
        "claude_bin": str(claude),
    }


def owned_command(handler: object, executable: Path, state: Path, source: str) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    expected = ["/usr/bin/python3", str(executable), "hook", "--state-dir", str(state)]
    if source == "claude":
        expected += ["--source", "claude"]
    return parts == expected


def update_hooks(path: Path, executable: Path, state: Path, source: str, selected: bool) -> tuple[dict, bool]:
    settings = read_object(path)
    if not settings and not path.exists() and not selected:
        return settings, False
    original = copy.deepcopy(settings)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"Expected a hooks object in {path}")
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise InstallError(f"Invalid {event} hooks in {path}")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise InstallError(f"Invalid {event} hook group in {path}")
            handlers = group.get("hooks", [])
            if any(not isinstance(handler, dict) for handler in handlers):
                raise InstallError(f"Invalid {event} handler in {path}")
            remaining = [handler for handler in handlers if not owned_command(handler, executable, state, source)]
            removed_owned = len(remaining) != len(handlers)
            if removed_owned and not remaining and len(group) == 1:
                continue
            kept.append({**group, "hooks": remaining} if removed_owned else group)
        if selected:
            command = ["/usr/bin/python3", str(executable), "hook", "--state-dir", str(state)]
            if source == "claude":
                command += ["--source", "claude"]
            kept.append({"hooks": [{"type": "command", "command": shlex.join(command), "timeout": 3}]})
        if kept or event in hooks:
            hooks[event] = kept
    if selected or hooks != settings.get("hooks", {}):
        settings["hooks"] = hooks
    return settings, settings != original


def source_files(source_dir: Path) -> tuple[dict[Path, bytes], dict[str, dict[str, dict[str, bytes]]]]:
    bundle_files = {}
    for name in ("worklog.py", "install-worklog.py"):
        path = source_dir / name
        if not path.is_file():
            raise InstallError(f"Missing Worklog bundle source: {path}")
        bundle_files[Path(name)] = path.read_bytes()
    profiles = {"codex": "gpt-6.md", "claude": "claude-opus-5.5.md"}
    installed_skills = {client: {} for client in profiles}
    for name in SKILLS:
        skill = source_dir / "worklog-skills" / name
        required = ["BASE.md", "SKILL.md", "variants/gpt-6.md", "variants/claude-fable-5.1.md", "variants/claude-opus-5.5.md"]
        for relative in required:
            if not (skill / relative).is_file():
                raise InstallError(f"Missing Worklog skill source: {skill / relative}")
        files = {}
        for path in skill.rglob("*"):
            if path.is_symlink() and (
                not path.is_file() or not path.resolve().is_relative_to(skill.resolve())
            ):
                raise InstallError(f"Worklog skill link must target a file inside its skill: {path}")
            if path.is_file():
                relative = path.relative_to(skill)
                content = path.read_bytes()
                files[str(relative)] = content
                bundle_files[Path("worklog-skills") / name / relative] = content
        for client, profile in profiles.items():
            materialized = files.copy()
            materialized["SKILL.md"] = files[f"variants/{profile}"]
            installed_skills[client][name] = materialized
    return bundle_files, installed_skills


def plan_skills(home: Path, bundle: Path, skills: dict) -> tuple[dict, dict]:
    manifest_path = bundle / "skills-manifest.json"
    previous = read_object(manifest_path)
    if previous and (previous.get("version") != 1 or not isinstance(previous.get("installed_skills"), dict)):
        raise InstallError(f"Unrecognized Worklog skills manifest: {manifest_path}")
    old_skills = previous.get("installed_skills", {})
    if any(not isinstance(by_name, dict) or any(not isinstance(files, dict) for files in by_name.values())
           for by_name in old_skills.values()):
        raise InstallError(f"Invalid Worklog skills manifest: {manifest_path}")
    for by_name in old_skills.values():
        for files in by_name.values():
            if any(not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts
                   or not isinstance(digest, str) for relative, digest in files.items()):
                raise InstallError(f"Invalid Worklog skills manifest: {manifest_path}")
    manifest = {"version": 1, "installed_skills": {}}
    for client, by_name in skills.items():
        manifest["installed_skills"][client] = {}
        for name, files in by_name.items():
            target = home / f".{client}/skills" / name
            if target.is_symlink():
                raise InstallError(f"Worklog skill path is a symlink: {target}")
            old_hashes = old_skills.get(client, {}).get(name, {})
            new_hashes = {relative: file_hash(content) for relative, content in files.items()}
            for relative in set(old_hashes) | set(new_hashes):
                path = target / relative
                if path.is_symlink():
                    raise InstallError(f"Worklog skill file is a symlink: {path}")
                if path.exists():
                    actual = file_hash(path.read_bytes())
                    if actual not in (old_hashes.get(relative), new_hashes.get(relative)):
                        raise InstallError(f"Existing skill conflicts with Worklog: {path}")
            if target.exists() and not old_hashes:
                extras = [path for path in target.rglob("*") if path.is_file() and str(path.relative_to(target)) not in new_hashes]
                if extras:
                    raise InstallError(f"Existing skill conflicts with Worklog: {target}")
            manifest["installed_skills"][client][name] = new_hashes
    return previous, manifest


def validate_preview(preview: Path | None, policy: bytes, settings: dict,
                     sources: list[str], require_included: bool) -> None:
    if preview is None:
        raise InstallError("A matching successful --preview receipt is required")
    receipt = read_object(preview)
    for field, expected in (
        ("policy_sha256", file_hash(policy)),
        ("harness", settings["harness"]),
        ("model", settings["model"]),
        ("effort", settings["effort"]),
    ):
        if receipt.get(field) != expected:
            raise InstallError(f"Preview {field} does not match the selected installation")
    if receipt.get("source") not in sources:
        raise InstallError("Preview source is not among the selected capture sources")
    if (receipt.get("success") is not True or receipt.get("status") not in ("included", "skipped")
            or not isinstance(receipt.get("transcript_sha256"), str)):
        raise InstallError("Preview receipt does not report a successful preview")
    if require_included and receipt["status"] != "included":
        raise InstallError("A backend change requires an included preview that exercised the summarizer")


def install_skills(home: Path, bundle: Path, skills: dict, previous: dict, manifest: dict, state: Path) -> None:
    old_skills = previous.get("installed_skills", {})
    for client, by_name in skills.items():
        for name, files in by_name.items():
            target = home / f".{client}/skills" / name
            for relative, content in files.items():
                backup_and_write(target / relative, content, state, f"{client}-{name}-{Path(relative).name}", 0o644)
            for relative in old_skills.get(client, {}).get(name, {}):
                if relative not in files:
                    old_file = target / relative
                    if old_file.exists():
                        backup_file(old_file, state, f"{client}-{name}-{Path(relative).name}")
                        old_file.unlink()
    write_atomic(bundle / "skills-manifest.json", json_bytes(manifest), 0o600)


def install_bundle(bundle: Path, files: dict[Path, bytes]) -> None:
    for relative, content in files.items():
        target = bundle / relative
        if target.exists() and target.read_bytes() == content:
            continue
        write_atomic(target, content, 0o755 if relative.name.endswith(".py") else 0o644)


def install_cached_command(home: Path, bundle: Path) -> None:
    command = home / ".local/bin/worklog-install"
    target = bundle / "install-worklog.py"
    if command.is_symlink():
        if command.resolve() == target:
            return
        raise InstallError(f"Existing worklog-install command conflicts: {command}")
    if command.exists():
        raise InstallError(f"Existing worklog-install command conflicts: {command}")
    command.parent.mkdir(parents=True, exist_ok=True)
    command.symlink_to(target)


def worker_plist(previous: dict, label: str, settings: dict, executable: Path, state: Path) -> dict:
    arguments = [
        "/usr/bin/python3", str(executable), "drain", "--state-dir", str(state),
        "--vault", settings["vault"], "--daily-folder", settings["daily_folder"],
        "--harness", settings["harness"], "--model", settings["model"],
    ]
    if settings["harness"] == "codex":
        arguments += ["--codex-bin", settings["codex_bin"]]
    else:
        arguments += ["--claude-bin", settings["claude_bin"]]
    if settings["effort"] is not None:
        arguments += ["--effort", settings["effort"]]
    return {
        **previous,
        "Label": label,
        "ProgramArguments": arguments,
        "QueueDirectories": [str(state / "pending")],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(state / "worker.log"),
        "StandardErrorPath": str(state / "worker.log"),
        "EnvironmentVariables": previous.get("EnvironmentVariables", {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}),
    }


def parser_for_install() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap Worklog skills or install selected conversation capture hooks and summary worker.",
        epilog=("Start with --skills-only, then use setup-worklog to prepare a private scope and preview. "
                "Example: worklog-install --vault ~/Documents/Obsidian/Work --preview receipt.json "
                "--policy-file scope.txt --no-load. Reinstalling with omitted choices preserves the receipt."),
    )
    parser.add_argument("--vault", type=Path, help="Existing Obsidian vault")
    parser.add_argument("--daily-folder", help="Folder inside the vault; preserved on reinstall")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Installation home")
    parser.add_argument("--capture-sources", nargs="+", choices=SOURCES, help="Sources to hook; preserved on reinstall")
    parser.add_argument("--harness", choices=SOURCES, help="Summary backend; independent of capture sources")
    parser.add_argument("--model", help="Summary model; Claude requires an explicit model when first selected")
    parser.add_argument("--effort", help="Backend effort setting")
    parser.add_argument("--codex-bin", type=Path, help="Existing authenticated Codex executable")
    parser.add_argument("--claude-bin", type=Path, help="Existing authenticated Claude executable")
    parser.add_argument("--policy-file", type=Path, help="Private candidate scope policy")
    parser.add_argument("--preview", type=Path, help="Successful matching preview receipt")
    parser.add_argument("--skills-only", action="store_true", help="Install cached tool and both client skills only")
    parser.add_argument("--dry-run", action="store_true", help="Validate and show destinations without changes")
    parser.add_argument("--no-load", action="store_true", help="Prepare files without loading the macOS worker")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = parser_for_install()
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    source_dir = Path(__file__).resolve().parent
    bundle = home / ".local/share/worklog"
    executable = home / ".local/bin/worklog"
    state = home / "Library/Application Support/Worklog"
    bundle_files, skills = source_files(source_dir)
    previous_manifest, manifest = plan_skills(home, bundle, skills)
    cached = home / ".local/bin/worklog-install"
    if cached.exists() or cached.is_symlink():
        if not cached.is_symlink() or cached.resolve() != bundle / "install-worklog.py":
            parser.error(f"Existing worklog-install command conflicts: {cached}")
    validate_destination(home, cached.parent, directory=True)
    destinations = [bundle / relative for relative in bundle_files]
    destinations.extend(home / f".{client}/skills" / name / relative
                        for client, by_name in skills.items()
                        for name, files in by_name.items() for relative in files)
    destinations.extend((bundle / "skills-manifest.json",))
    for destination in destinations:
        validate_destination(home, destination)
        if destination.is_dir():
            parser.error(f"Expected a file, found a directory: {destination}")
    if not args.skills_only:
        validate_destination(home, executable)
    if args.skills_only:
        if any((args.vault, args.daily_folder, args.capture_sources, args.harness, args.model,
                args.effort, args.codex_bin, args.claude_bin, args.policy_file, args.preview)):
            parser.error("--skills-only cannot also configure capture or policy")
        validate_destination(home, state / "backups" / "placeholder")
        print(f"Worklog bundle: {bundle}\nSkills: {home / '.codex/skills'}, {home / '.claude/skills'}")
        if args.dry_run:
            return 0
        install_bundle(bundle, bundle_files)
        install_cached_command(home, bundle)
        install_skills(home, bundle, skills, previous_manifest, manifest, state)
        print("Worklog onboarding skills installed. Capture remains unchanged.")
        return 0
    if not args.no_load and sys.platform != "darwin":
        parser.error("Loading the worker requires macOS; use --no-load")
    try:
        plist_path, previous_plist, label = find_worker(home, executable)
        state = worker_state_dir(previous_plist, state)
        for relative in ("backups/placeholder", "pending/placeholder"):
            validate_destination(Path(state.anchor), state / relative)
        prior, installed = prior_settings(state / "installation.json", previous_plist)
        if executable.is_file() and not previous_plist and not installed:
            existing_runtime = executable.read_bytes()
            cached_runtime = bundle / "worklog.py"
            if (existing_runtime != bundle_files[Path("worklog.py")]
                    and (not cached_runtime.is_file() or existing_runtime != cached_runtime.read_bytes())):
                raise InstallError(f"Existing worklog command is not owned by Worklog: {executable}")
        settings = resolve_settings(args, prior)
        if args.preview and not args.policy_file and not installed:
            raise InstallError("--preview without --policy-file requires an existing installation")
        if args.policy_file:
            policy_path = args.policy_file.expanduser().resolve()
            policy = policy_path.read_bytes()
            policy.decode("utf-8")
            if not policy.strip():
                raise InstallError("--policy-file must contain a nonempty policy")
        else:
            policy = (state / "scope.txt").read_bytes() if (state / "scope.txt").exists() else b""
            policy.decode("utf-8")
        changed_backend = installed and any(settings[key] != prior.get(key) for key in ("harness", "model", "effort"))
        if args.policy_file or changed_backend:
            validate_preview(args.preview, policy, settings, settings["capture_sources"], changed_backend)
        elif args.preview:
            raise InstallError("--preview is only used with a policy or backend change")
        hooks = {}
        for source in SOURCES:
            path = home / (".codex/hooks.json" if source == "codex" else ".claude/settings.json")
            hooks[source] = (path, *update_hooks(path, executable, state, source, source in settings["capture_sources"]))
        plist = worker_plist(previous_plist, label, settings, executable, state)
        for path in (executable, plist_path, state / "scope.txt", state / "installation.json"):
            validate_destination(home if path.is_relative_to(home) else Path(state.anchor), path)
            if path.is_dir():
                raise InstallError(f"Expected a file, found a directory: {path}")
        for path, _, changed in hooks.values():
            if changed:
                validate_destination(home, path)
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        parser.error(str(error))
    print(f"Worklog: {executable}\nDaily notes: {Path(settings['vault']) / settings['daily_folder']}\n"
          f"Sources: {', '.join(settings['capture_sources'])}\nSummary: {settings['harness']} / {settings['model']}\n"
          f"State: {state}")
    if args.dry_run:
        print(f"Would update selected hooks, worker, receipt, and skills under {home}")
        return 0
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    state.chmod(0o700)
    (state / "pending").mkdir(mode=0o700, exist_ok=True)
    install_bundle(bundle, bundle_files)
    install_cached_command(home, bundle)
    install_skills(home, bundle, skills, previous_manifest, manifest, state)
    backup_and_write(executable, bundle_files[Path("worklog.py")], state, "worklog-runtime", 0o755)
    scope_path = state / "scope.txt"
    if args.policy_file and (not scope_path.exists() or scope_path.read_bytes() != policy):
        policy_command(policy_path, args.preview.expanduser().resolve(), state)
    for source, (path, updated, changed) in hooks.items():
        if changed:
            backup_and_write(path, json_bytes(updated), state, f"{source}-hooks", 0o600)
    backup_and_write(plist_path, plistlib.dumps(plist), state, "worker-plist", 0o600)
    backup_and_write(state / "installation.json", json_bytes(settings), state, "installation", 0o600)
    if not args.no_load:
        target = f"gui/{os.getuid()}/{label}"
        loaded = subprocess.run(["/bin/launchctl", "print", target], capture_output=True)
        if loaded.returncode == 0:
            subprocess.run(["/bin/launchctl", "bootout", target], check=True)
        subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=True)
    if "claude" in settings["capture_sources"] and hooks["claude"][1].get("disableAllHooks"):
        print("Claude disableAllHooks is set; Worklog hooks are installed but disabled by your policy.")
    print("Installed. Review and trust selected Worklog hooks in your client settings.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, WorklogError, subprocess.CalledProcessError) as error:
        print(f"Worklog installation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
