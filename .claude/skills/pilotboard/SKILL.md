---
name: pilotboard
description: Open the ContextPilot localhost dashboard for lifetime, session and snapshot-prompt token and dollar estimates. Requires the companion contextpilot skill.
disable-model-invocation: true
argument-hint: "[--root PATH] [--port N]"
---

For an explicit `/pilotboard` invocation, start the sibling `contextpilot`
bundle's self-contained dashboard in the shell tool's background-process mode:

```sh
python3 "${CLAUDE_SKILL_DIR}/../contextpilot/scripts/pilotboard.py" --root "/path/to/project" --session-id "${CLAUDE_SESSION_ID}"
```

Use the actual current project root and current session ID; omit `--session-id`
if unavailable. Interpret `$ARGUMENTS` only as documented `--root`, `--port`, or
`--no-browser` flags, passing paths as quoted arguments. The server opens the
browser; confirm successful startup from its printed `Pilotboard:` URL. If
automatic opening fails, give the user that complete localhost URL. Do not start
another server after a confirmed launch. Stop the background job to stop serving.

For estimates and privacy details read the sibling file
`../contextpilot/references/pilotboard.md` relative to this skill directory.
Do not inspect Claude transcripts, fabricate usage or publish the board.

If `contextpilot` is missing, explain that both skills must be installed:

```sh
npx skills add reyu110/ContextPilot --skill contextpilot --skill pilotboard --agent claude-code --global --yes
```
