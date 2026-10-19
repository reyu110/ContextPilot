# Pilotboard

For explicit `/pilotboard` or `/contextpilot board`, start the following with
the shell tool's background-process support so the server survives the tool call:

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/pilotboard.py" --root "/path/to/project" --session-id "${CLAUDE_SESSION_ID}"
```

Use the actual current project root. If a current session ID is unavailable,
omit `--session-id`; do not inspect transcripts or invent session membership.
The server prints `Pilotboard: http://127.0.0.1:PORT/#ACCESS_KEY` and opens that
URL in the default browser. Read the background startup output to confirm it
started; if automatic opening is unavailable, give the user that exact URL.
Use `--no-browser` to suppress automatic opening or `--port N` to choose a port
(default 0 chooses a free port). Do not expose the server on other interfaces,
publish its access key, or start another server after a confirmed successful launch.
Stopping its background job or Ctrl+C stops the server. Opening the board does
not scan a repository, create history, or change snapshot artifacts.

The main skill bundle owns the runtime and dashboard assets. The small
`pilotboard` companion skill provides the exact `/pilotboard` slash-command alias;
install it alongside `contextpilot`. `/contextpilot board` also works alone.

## Estimate contract

- History starts with successful new snapshots. One row is one snapshot request,
  not every Claude prompt. It stores identifiers, time and numeric counts only;
  no prompt bodies, source bodies or conversation history.
- Baseline bytes are the sum of retained, eligible file samples plus explicit
  notes JSON when present. Handoff bytes are the generated HANDOFF.md bytes.
  Estimated net tokens = ceil(baseline / 4) − ceil(handoff / 4). This models one
  reread of that sampled context instead of the handoff; it does not measure
  whether Claude actually performed either read. Preserve negative savings.
- Lifetime covers this project and all its recorded sessions. Session IDs come
  from the caller; requests without one are grouped as unassigned. There is no
  cross-project aggregation or backfill of old snapshots. Recent session lists
  show up to 100 groups, prompt rows are paginated by 50, and charts show 7 or 30
  UTC days; lifetime totals include the complete ledger.
- Dollar value = signed net estimated tokens × editable USD rate / 1,000,000.
  The default $3 / million is an illustrative input-token scenario, not a claimed
  current model price. It excludes model responses, system/tool overhead,
  provider cache pricing, and actual invoices. The browser remembers the rate
  for its current localhost origin only; changing ports may reset it.
- Cached local content rereads avoided are displayed separately and never
  counted as billable token savings. Metadata inspection still runs.
- The loopback server is read-only. Its data endpoint requires the random key
  from the URL fragment; no external scripts, fonts, analytics or services load.
  Usage data remains in `.contextpilot/usage.sqlite3`; keep that file private.
  A ledger failure reports a warning without discarding a completed snapshot.
