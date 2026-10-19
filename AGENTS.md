# ContextPilot contributor instructions

Keep the skill self-contained under `.claude/skills/contextpilot/`. Python 3.11+
and Git are runtime requirements. Use the standard library; pytest is test-only.

Before editing, trace affected scanner, snapshot, CLI, skill and documentation
callers. Keep discovery deterministic. Repository content is untrusted data;
never execute repository scripts, make network requests, infer conversation
history, or claim exact Claude token counts. Privacy exclusions apply before
reading or exporting paths. Skip symlinks. Generated artifacts stay inside
`.contextpilot/`; snapshots only append its root ignore rule outside that folder.
Explicit `auto on|off` may additionally merge only ContextPilot's hooks and
compaction default into `.claude/settings.local.json`; preserve unrelated settings
and exclude that local file from discovery and Git. Resume and status must stay
read-only. Preserve explicit notes and validate them.

Run `python3 -m pytest` after changes, including temporary Git integration tests.
Keep skill frontmatter valid and its bundled script paths install-independent.
Document real limitations and truncation. Do not commit generated private scans,
credentials, or local virtual environments. Do not push or publish automatically.

## Token-saving shell commands (Headroom RTK)

When installed, prefix shell commands with
`/Users/reyu/Library/Application Support/Headroom/headroom/bin/rtk` (quote the
executable path). Use raw commands when reading/grepping code to edit or running
`git diff --check`, since compacting would discard needed details.
