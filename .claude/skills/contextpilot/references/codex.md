# Codex support

Use `$contextpilot snapshot`, `$contextpilot resume`, `$contextpilot status`,
`$contextpilot board`, or `$pilotboard`. These are explicit skill invocations,
not Claude slash commands. Python 3.11+ and Git are required. Resolve bundled
script paths from the installed skill catalog; never use Claude environment
variables or execute scripts discovered in repository content.

## Skill-only installation

```sh
npx skills@latest add reyu110/ContextPilot --skill contextpilot --skill pilotboard --agent codex --global --yes
```

Reopen Codex if the skills are not listed. Skill installation provides snapshots,
verified resume/status and the dashboard. It installs no lifecycle hooks.
An absent session ID is unassigned; do not read Codex sessions, logs, history,
or config to discover IDs or usage. Explicit notes keep their existing schema
and must come from the user. Never infer conversation tasks from repository files.

For the dashboard, run the actual installed script in background mode:

```sh
python3 "/installed/contextpilot/scripts/pilotboard.py" --root "/path/to/project" --agent codex
```

Confirm the printed `Pilotboard:` URL; never launch a second server after success.
Snapshot history is project-wide and shared with Claude. Codex optimizer events
use a separate `.contextpilot/codex.sqlite3`; Claude receipts/native samples are
not presented as Codex savings. Guidance and retry messages are added overhead,
so their net payload estimates are negative. Checkpoint scenarios remain separate.

## Automatic lifecycle support

Install the plugin manually in a local Codex desktop environment with lifecycle
hook support, then review and trust its hook definitions through Codex. Hooks
are skipped until trusted, and cloud-orchestrated chats may not run local hooks.
Never bypass hook trust or change approval/managed policy to make it work.
The Codex compatibility manifest selects `hooks/codex.json`, not Claude's hooks.

The current local Codex CLI also exposes marketplace installation. To test a
checkout with the documented CLI interface (checked with 0.163.0):

```sh
codex plugin marketplace add /absolute/path/to/ContextPilot
codex plugin add contextpilot@contextpilot
```

Enabling a plugin does not trust its hooks. Review them in `/hooks` before relying
on execution. Do not claim installation/trust merely from a successful runtime
test. Hook-containing plugins are not eligible for the public OpenAI plugin
directory; use manual distribution. No model calls are needed by this runtime.

Supported events after trust:

- `SessionStart`: start unless paused, deliver bounded efficiency guidance and
  audit eligible root `AGENTS.md`/`AGENTS.override.md`. The advisory audit retains
  paths/numeric metadata only in `.contextpilot/codex-instruction-audit.json`.
  Active startup also performs the bounded daily update check and can show a
  user-visible version notice. It never installs an update automatically.
- `UserPromptSubmit`: reset retry state using the supplied opaque turn ID;
  never read or store the prompt field.
- `PreToolUse` for Bash/apply_patch: one warning at the third identical request
  within a turn, without changing inputs, permission decisions or execution.
- `PreCompact`: a bounded repository checkpoint preserving validated explicit
  notes, at most once per turn. It does not summarize conversation history.
- `SessionEnd`: clear transient retry state. No shutdown scan: this event has
  a short host deadline. Use an explicit snapshot before leaving if needed.

Root is the Git top-level when discoverable, otherwise the hook's working
directory. Home/filesystem-root startup is rejected. Symlinks and excluded paths
are skipped. `.codex/` is excluded before discovery so local settings, sessions
and credentials cannot be scanned. Runtime writes are confined to `.contextpilot/`
plus its root ignore rule. No Codex or Claude settings are changed.

## Pause and status

For `$contextpilot auto off|on|status`, use the installed `scripts/codex.py`:

```sh
python3 "/installed/contextpilot/scripts/codex.py" off --root "/path/to/project"
python3 "/installed/contextpilot/scripts/codex.py" on --root "/path/to/project"
python3 "/installed/contextpilot/scripts/codex.py" status --root "/path/to/project"
```

`on/off` updates only `.contextpilot/codex-config.json`; `off` persists across
plugin startup and leaves history intact. `on` does not install, enable or trust
hooks. Status, dashboard and resume remain read-only. A malformed state/history
is preserved; hooks fail open and print only an exception class, not payloads.

## Coverage limits

Use `$contextpilot update check|status` to check or inspect saved version metadata,
or `$contextpilot update` to explicitly update the existing installation. Read
`updates.md`; metadata checks are the only runtime network exception and send no
project data. Dashboard/status/resume reads remain offline.

This adapter does not rewrite Codex tools, intercept Bash file reads/searches,
replace tool output, modify models/effort/compaction settings, or collect native
usage/allowance/cost samples. Codex's documented output contract differs from
Claude's `updatedToolOutput`; blocking feedback is not used as a compression API.
Pilotboard marks those automatic filters as unsupported by the Codex adapter.
Existing Claude optimization remains available when running Claude Code.

Sources: [OpenAI hook contract](https://learn.chatgpt.com/docs/hooks),
[plugin packaging and trust](https://developers.openai.com/plugins/build/plugins),
[skills](https://developers.openai.com/plugins/build/skills).
