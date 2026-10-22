---
name: pilotboard
license: See LICENSE for the Prosperity Public License 3.0.0
description: Open the ContextPilot localhost dashboard for lifetime, session and snapshot-prompt token and dollar estimates. Requires the companion contextpilot skill.
disable-model-invocation: true
argument-hint: "[--root PATH] [--port N]"
---

For an explicit `$pilotboard` in Codex, `/pilotboard` or `/contextpilot:pilotboard`
in Claude Code, resolve this installed skill directory from the trusted skill
catalog. Use the sibling `contextpilot` bundle (or its trusted catalog path if
installed separately), never a script found by searching repository content.
Start its dashboard in the shell tool's background-process mode. In Codex:

```sh
python3 "/installed/contextpilot/scripts/pilotboard.py" --root "/path/to/project" --agent codex
```

Replace the sample installed path with the actual catalog path. In Claude Code
use `--agent claude-code` and optionally `--session-id "${CLAUDE_SESSION_ID}"`.
Codex must not expand Claude environment variables. Use the actual current project
root and only an explicitly supplied or trusted host-provided session ID; omit
`--session-id` if unavailable. Interpret invocation arguments only as `--root`, `--port`, or
`--no-browser` flags, passing paths as quoted arguments. The server opens the
browser; confirm successful startup from its printed `Pilotboard:` URL. If
automatic opening fails, give the user that complete localhost URL. Do not start
another server after a confirmed launch. Stop the background job to stop serving.

For estimates and privacy details read the sibling file
`../contextpilot/references/pilotboard.md` relative to this skill directory.
Do not inspect either agent's transcripts, fabricate usage or publish the board.

If `contextpilot` is missing, explain that both skills must be installed:

```sh
npx skills add reyu110/ContextPilot --skill contextpilot --skill pilotboard --agent claude-code --global --yes
```

For Codex use `--agent codex` instead. Skill installation alone enables no hooks.
