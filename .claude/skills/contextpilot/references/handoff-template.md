# Handoff contract and explicit notes

The generator supplies these sections: Project overview, Architecture summary,
Important files and directories, Current development objectives, Confirmed
technical decisions, Work completed, Unfinished tasks, Git changes, Known bugs
and blockers, Suggested next steps, Coverage and size.

Verified observations come only from the scanner. Filename-based entry points
and dependency text matches are signals, not proof of runtime behavior. No
session history is inferred. Empty note sections say no information was recorded.

Before an explicitly requested snapshot, optionally create
`.contextpilot/notes.json` using this schema:

```json
{
  "objectives": [{"text": "Ship the local CLI", "source": "user"}],
  "decisions": [{"text": "Use JSON rather than a database", "source": "confirmed"}],
  "completed": [],
  "tasks": [{"text": "Review installation instructions", "source": "user"}],
  "blockers": [],
  "next_steps": [{"text": "A Windows smoke test may be useful", "source": "assumption"}]
}
```

All sections are optional. Sources: `user`, `confirmed`, `assumption`. Only
`confirmed` is accepted for decisions; this records explicit human confirmation,
not independent verification. Maximum: 50 notes per section, 1000 characters per
note, 32 KiB notes file. Never put credentials in notes. Edit notes to retain
information across regenerated snapshots; hand edits to HANDOFF.md are backed up
once but are not merged. Keep long-term history in your own version control.
