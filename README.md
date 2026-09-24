# Worklog

A local work journal for people using Codex and Claude Code. Worklog turns conversation activity into short Obsidian notes so you can recall what you did, why it mattered, and what remains open.

You choose what belongs in the journal. Capture sources and the client/model used to summarize are separate choices. Notes retain conversation references and PR, ticket, or document URLs supplied in the conversation.

## Set up on a Mac

You need an existing Obsidian vault and an authenticated Codex or Claude Code CLI. Automatic capture uses macOS launchd. Summarization uses your chosen hosted model through its existing login; notes and private configuration stay on your Mac.

Clone this repository, then bootstrap the skills:

```sh
cd worklog
/usr/bin/python3 install-worklog.py --skills-only
```

This installs `setup-worklog` and `update-worklog-policy` for both clients and caches the tool. It does not enable capture yet.

Ask your agent to use `setup-worklog`. If the skill has not been discovered, ask it to read `~/.codex/skills/setup-worklog/SKILL.md` or `~/.claude/skills/setup-worklog/SKILL.md`.

The setup conversation covers:

- What work to record, what to exclude, and concrete examples.
- Whether to capture Codex, Claude Code, or both.
- Which available client, model, and effort to use for summaries.
- Which Obsidian vault and folder should receive notes.

The agent previews real included and excluded conversations, shows the proposed settings, and applies the setup you approve. It preserves existing choices when you return to setup. An optional model comparison uses the same examples.

A prompt you can paste into an agent:

> Use this repository to set up Worklog on my Mac. Read the README, install the onboarding skills, and follow setup-worklog. Ask what I want recorded or excluded, which clients to capture, which model should summarize, and which Obsidian vault to use. Show real included and excluded examples before applying the setup I approve. Keep my policy and notes outside the repository. Verify the installed hooks and worker.

## How capture works

`Stop` queues a capture at most hourly after the first capture. `SessionEnd` queues remaining activity. A background worker summarizes the queued text and updates one entry per conversation per day. Related conversations on the same day share a visible topic heading while keeping their own source references and updates. Subagent activity, injected instructions, tool output, and internal reasoning are excluded from the supported transcript formats.

Codex notes link to their conversation. Claude notes include a `claude --resume` reference. Ticket and PR URLs are copied from supplied material; missing URLs are not guessed. Generated summaries can omit details, so retain your original conversations.

The worker retries only when requested or when later queued work permits recovery. Failed jobs remain available. A candidate policy or preview does not rewrite existing notes.

## Change the policy

Ask your agent to use `update-worklog-policy`. It shows a focused rule change and real previews before applying your approved revision. A one-off correction to a note does not silently become a permanent rule.

## Local files and status

| Item | Default location |
| --- | --- |
| Daily notes | Your selected vault, under `daily_notes` |
| Private policy, settings, queue, and checkpoints | `~/Library/Application Support/Worklog` |
| Cached runtime, installer, and skills | `~/.local/share/worklog` |
| Commands | `~/.local/bin/worklog` and `~/.local/bin/worklog-install` |

```sh
worklog status
worklog retry
```

Resolve the cause of a failed capture before retrying it. Inspect Worklog's `worker.log` in the state directory when needed. Respect each client's native hook trust controls.

The installer copies the tool into its permanent cache, so the checkout is not needed afterward. Run the reviewed installer again to adopt later source changes. Keep policies, previews, transcripts, credentials, and journal notes outside this repository.

## Development

No third-party Python packages are required. Run the executable tests with Python 3.9 or later:

```sh
python3 -m unittest -f test_worklog test_worklog_preview test_install_worklog
```

The tests use temporary homes and fake summarizer processes; they do not install live hooks or call hosted models. The launchd worker is macOS-specific. Concurrent edits in Obsidian during the worker's final file replacement can still race with capture.
