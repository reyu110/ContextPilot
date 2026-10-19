# Automatic context optimization

Use only for explicit `/contextpilot auto on`, `auto off`, or `auto status`.
The installed runtime requires Claude Code 2.1.303+ and Python 3.11+ with Git.

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/optimizer.py" on --root "/path/to/project"
python3 "${CLAUDE_SKILL_DIR}/scripts/optimizer.py" off --root "/path/to/project"
python3 "${CLAUDE_SKILL_DIR}/scripts/optimizer.py" status --root "/path/to/project"
```

Enabling registers argument-vector command hooks in `.claude/settings.local.json`
for this project, preserving unrelated settings, permission rules, hooks and
environment variables. This is an explicit configuration write in addition to
the normal `.contextpilot/` artifacts; ignore rules for `.contextpilot/` and the
local settings file are appended once. The hooks invoke
only the installed skill runtime, never scripts discovered in the repository.
Do not copy settings into artifacts: they may contain credentials. Only ownership
metadata and hashes/numeric statistics are retained in `.contextpilot/`.

Tell the user to restart Claude Code after enabling so the hooks and environment
are loaded reliably. The hooks then run automatically for subsequent prompts in
that project. Enabling does not rewrite an existing chat or read transcripts.
Off removes the exact owned entries and the default threshold only if still
unchanged; it preserves subsequent user edits and usage history. Restart after
disabling to ensure the previous environment is cleared. A missing or damaged
ownership record needs inspection; never remove unrelated hooks by guesswork.

## Behavior

- An unbounded text `Read` for an eligible file larger than 32 KiB gets an
  explicit 400-line limit and a partial-read notice. Explicit limits, page reads,
  and `allow_large` requests are preserved. Read further offsets whenever the
  task needs more content. No permission decision is returned or bypassed.
- Exact repeated text Read results of at least 2 KiB within the same prompt are
  replaced by a short reference to the first tool result. The response retains
  the official Read output shape. A different explicit offset/limit returns a
  fresh copy. Changed content, changed ranges, images/PDFs, unknown response
  schemas, other tools and subagent events pass through unchanged.
- Session/prompt starts and compaction reset duplicate state, so results from
  earlier or compacted context are not treated as available. Session-end cleanup
  and a 24-hour stale-cache sweep discard temporary hashes. Each prompt retains
  at most 256 cache entries. Prompt text, source bodies and transcripts are never
  stored. The hook input includes prompt text on prompt events; it is ignored.
- Existing privacy and Git ignore rules apply before inspecting a tool result.
  Symlinks, excluded paths and files outside the project are not optimized.
  Duplicate suppression also skips credential-like content. Git failures,
  malformed input, unsupported shapes, inputs
  above 8 MiB or runtime errors preserve original behavior instead of blocking.
- If no local or exported compaction override exists, enabling requests native compaction
  at 70% of Claude's auto-compact window. Existing overrides and disabled
  compaction are respected. Claude Code owns summarization and may lose detail;
  this skill never claims exact context usage or triggers recursive model calls.
  Managed policies and higher-priority settings can prevent hooks from running;
  ContextPilot never overrides disabled-hook or permission controls.

## Reporting and limits

Pilotboard displays requested duplicate-text byte reductions and their rough
bytes/4 token equivalent separately from snapshot scenarios. These are hook
requests, not confirmations of a provider's tokenizer or invoice. Bounded-read
counts do not estimate how many tokens a full read would have used. Dollar
figures use the dashboard's editable scenario rate. Do not add these totals to
snapshot savings or claim guaranteed savings: smaller reads can require more
tool calls, and native compaction itself can consume tokens.

Local settings may be formatted again when updated. Generated configuration
uses absolute Python/runtime paths. Disable with the original installed runtime
before moving the bundle, then enable from its new location. Rerun `auto on`
after changing Python. A stale `.contextpilot/optimizer-config.lock` after a
crash requires checking that no configuration command is running before removal.
History corruption must be preserved for inspection; disabling can still remove
the owned hooks without deleting that history.

Official contracts:
[hooks](https://code.claude.com/docs/en/hooks#posttooluse-decision-control),
[Read output schema](https://code.claude.com/docs/en/agent-sdk/typescript#read-2),
[compaction environment setting](https://code.claude.com/docs/en/env-vars).
