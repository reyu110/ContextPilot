# Automatic context optimization

Requires Claude Code 2.1.303+, Python 3.11+ and Git. Runtime uses only the Python
standard library. Installed plugin hooks invoke bundled code, never repository
scripts. A standalone skill has no startup lifecycle: it keeps explicit controls.

## Installation and commands

```sh
claude plugin install contextpilot --marketplace reyu110/ContextPilot
```

Restart Claude Code. Plugin hooks bootstrap each project automatically. On the
first startup, missing native defaults are registered in local settings; restart
once to load those reliably. Filters and session guidance work in the first
supported session. Plugin commands are `/contextpilot:contextpilot` and
`/contextpilot:pilotboard`; standalone commands keep their short names. To test
an unpublished checkout use `claude --plugin-dir /absolute/path/to/ContextPilot`.

For explicit `auto on|off|status`, invoke the installed runtime:

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/optimizer.py" on --root "/path/to/project"
python3 "${CLAUDE_SKILL_DIR}/scripts/optimizer.py" off --root "/path/to/project"
python3 "${CLAUDE_SKILL_DIR}/scripts/optimizer.py" status --root "/path/to/project"
```

Standalone `on` registers project-local hooks. Plugin startup migrates exact owned
local hooks to plugin delivery, avoiding double optimization. `off` removes exact
owned settings and unchanged defaults, keeps history, and records a pause honored
by later plugin startups. `on` resumes. Restart after disabling to clear loaded
settings. Disable with the bundled runtime **before uninstalling the plugin**;
Claude has no verified uninstall callback for cleaning up project-local defaults.
Uninstall alone stops plugin hooks but can leave those local defaults behind.

## Configuration ownership

Only `.claude/settings.local.json`, its ignore rule, and `.contextpilot/` artifacts
are written. Preserve unrelated hooks, permissions, environment and settings.
Plugin startup adds missing Concise output, medium effort, a bundled numeric-only
status line and a 70% auto-compact threshold. Existing personal/project/local
preferences, per-model effort settings and exported overrides take precedence.
The selected model is never changed. Managed policy and launch overrides may
prevent an installed default from taking effect; registration is not proof of
activation. The previous status line is never replaced or executed by this code.

Settings are bounded, validated and merged atomically. Only ownership metadata
is retained, never a backup of settings containing credentials. A stale config
lock after a crash requires verifying no configure operation is running before
removal. Malformed ownership/configuration and corrupt history are preserved for
inspection. Runtime hooks fail open and never grant permissions.

## Filters

- Eligible unbounded text Reads over 32 KiB use sampled line density and fresh
  native context pressure. At fresh context usage of 65% or higher, the file-size
  threshold falls to 4 KiB. Target text windows are 8 KiB normally, 4 KiB at 65%
  context, 2 KiB at 85%, and 1 KiB at 95%, clamped to 1–1200 lines. A contiguous
  follow-up in the same prompt can double the normal target below 65%. An initial
  bounded sample and later verified Read output supply density; only hashes and
  numeric profiles are stored. Native readings older than two minutes are ignored.
  Line windows are approximate, never exact byte/token caps. Preserve explicit
  limits, offsets, page reads and `allow_large`; always include a partial-view
  notice when a limit is added. Expand when correctness needs it.
- Exact repeated Read output of at least 2 KiB within a prompt becomes a short
  pointer retaining the official tool schema. Changed ranges/content are fresh.
- Unbounded Grep requests get a visible head limit: 200 entries normally, 100 at
  fresh 65% context, 50 at 85%. Explicit limits, including unlimited `0`, win.
  Exact repeated Grep content/count text of at least 2 KiB on eligible files or
  directories can become a pointer. Directory matches require unambiguous,
  privacy-verified filename/line prefixes (or filename/count pairs); unknown or
  truncated output passes through. A different explicit offset/head limit is a
  fresh request. Absolute filenames in Glob/Grep lists can become paths relative
  to the stated working directory, retaining every match, order, count and flag.
- Recognized direct pytest/unittest, uv/poetry-run tests, cargo/go test/build/check,
  npm/pnpm/yarn test/build/lint and ruff/eslint/tsc commands can lose recognized
  progress and ANSI color codes. Exact diagnostics and Python/JS/Java stacks can
  be grouped even when separated: retain the complete first copy and total count,
  with a reference at each later position. Unique diagnostics,
  summaries, stderr, flags and execution metadata remain intact. Shell
  pipelines, arbitrary commands, explicit machine formats, unknown terminal control
  sequences, credential-like text, images, background/timed-out tasks and structured
  output pass through. A text/list reduction must remove at least
  512 bytes after its notice. Commands are never changed or silently rerun.
- Never compact diffs or arbitrary machine-readable output. Full diagnostic
  blocks are not heuristically summarized. Explicitly request needed detail;
  the runtime stores no recoverable raw terminal output.

Privacy and Git ignore rules apply to file content before inspection. Skip
symlinks, excluded/outside paths, non-text files and subagent results. Input is
bounded to 8 MiB. Duplicate caches and retry hashes are bounded to 256 entries and
reset at session/prompt/compaction boundaries. Density profiles are bounded to 256
files/session; they retain no source/path bodies, and continuation requires the
same prompt and unchanged file metadata. Late results cannot seed new context.
Some Claude versions use Bash searches instead of Grep/Glob; arbitrary shell
searches are not rewritten or claimed as covered.

## Guidance and checkpoints

Session-start guidance covers search-first navigation, relevant diffs, scope,
prompt preflight, targeted tests without skipping required checks, justified
subagents, concise answers, stable reusable context, MCP tool discovery, model/
effort recommendations and fresh sessions for unrelated work. It is delivered
once per SessionStart, not repeated on every prompt. Session startup also audits
eligible `CLAUDE.md`, `AGENTS.md`, `.claude/CLAUDE.md` and `.claude/rules/**/*.md`.
The audit checks privacy/ignore rules first, examines at most 32 files, reads at
most 256 KiB total and 64 KiB/file, and visits at most 256 rule candidates/directories.
It measures size and exact repeated paragraphs; the saved
`.contextpilot/instruction-audit.json` contains paths and numeric metadata, never
instruction bodies. Partial coverage and stale metadata are labeled. It is
advisory: no instructions or MCP configuration are edited. Audit failures do not
disable independent filters. Dashboard/status read the saved audit without writes.
At the third identical tool input in a prompt, add one retry warning without
blocking. A fresh native reading at 65% context or higher adds at most one budget
notice per prompt. Guidance does not guarantee model compliance.

Before compaction and at session end, the existing bounded snapshot runtime
creates a repository checkpoint, preserving validated explicit notes. It never
reads transcripts, infers objectives or records chat decisions. Checkpoint failures
are best effort and cannot block Claude. Snapshot scenario entries remain separate
from optimizer receipts. Resume/status remain read-only.

## Native tool discovery and caching

Claude already supplies MCP tool search and prompt caching on supported models
and deployments. ContextPilot uses that native behavior instead of replacing it
or changing servers, tool permissions, TTLs or caching preferences. Tool-search
defaults vary by host/model; some deployments reject deferral and load tools
upfront. Setting a flag cannot override provider support.

Successful main-session `ToolSearch` completion hooks record only an anonymous
event with zero before/after bytes. No query, discovered schema or result body is
saved. Late completions cannot cross prompt boundaries; duplicate delivery is
idempotent. Observation is not a token-savings measurement.

Read-only status interprets `ENABLE_TOOL_SEARCH` (`true`, `false`, `auto`,
`auto:N`) and `DISABLE_PROMPT_CACHING` (`1` disables, `0` keeps the native default)
as configuration hints. Existing flags remain unchanged. Unknown values are
labeled custom without exposing them. Sources are saved preferences and this
process's environment; running-Claude, managed and launch policy are not inspected.
Malformed preferences show unavailable hints. No absent flag is proof of activation.

## Measurements and honest limits

Active SessionStart hooks may show a cached update notice, using the bounded
metadata-only check in `updates.py` at most once per project per day. That check
is separate from filters and never uploads project data or installs software.
Read `updates.md` for explicit update commands and offline behavior. Existing
native-default restart messages are preserved; update notices are user-visible
system messages, not model context or claimed savings.

Optimizer history retains hashes, opaque IDs, numeric counts/timestamps and
documented numeric native status-line samples. It stores no prompt, command,
source or transcript bodies. Private snapshot artifacts retain their existing
bounded repository handoff contract. Everything remains local.

Signed net payload estimate = sum of ceil(before bytes / 4) minus ceil(after
bytes / 4). Record added guidance/notice text as overhead; preserve negative
results. A bounded read has no invented full-read baseline. These are requested
text-field reductions, not acknowledged tokenizer/billing savings. Other client,
system and tool overhead is excluded. Earlier ledger rows have no fabricated date;
they remain in lifetime totals but not dated charts. Dashboard reads never migrate
history; the next authorized hook writer does that transactionally.

The native status line may supply current-window input/output/cache components,
context percentage/capacity, session cost field, 5-hour/7-day allowance/reset
readings, and numeric codes for observed output style/effort. These
are latest samples, not cumulative token usage or an invoice. Missing/null fields
mean unavailable, never zero. Existing custom status lines prevent automatic
sampling; preserve them and show that limitation. Pilotboard shows these details,
configuration sources and cache-read share separately from reduction estimates.
Cache-read share is not a savings calculation. Never follow `transcript_path`.

Do not add optimizer estimates, native observations and snapshot scenarios together.
Dollar equivalents use the editable scenario rate. Smaller reads can require more
calls; compaction and guidance consume tokens; caching changes cost. No guaranteed
extension to subscription allowances or per-task model/effort switching is claimed.

The dashboard also lists unsupported behavior and its reason. There is no verified
command-hook control for automatic main-session model/effort switching or deleting
past conversation turns. Native compaction remains Claude's responsibility.
Runtime instruction rewrites are outside the project's permitted artifact writes.
Workflow/semantic advice cannot be enforced by deterministic hooks without
substituting guessed requirements for the user's task. These are boundaries,
not unfinished implementations advertised as complete.

Official contracts: [plugin components](https://code.claude.com/docs/en/plugins/components),
[hooks](https://code.claude.com/docs/en/hooks),
[tool schemas](https://code.claude.com/docs/en/agent-sdk/typescript),
[status line](https://code.claude.com/docs/en/statusline),
[settings](https://code.claude.com/docs/en/settings-reference).
See also [environment variables](https://code.claude.com/docs/en/env-vars) and
[native tool search](https://code.claude.com/docs/en/agent-sdk/tool-search).
