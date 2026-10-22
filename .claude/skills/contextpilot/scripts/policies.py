"""Small, deterministic output filters and bounded assistant guidance; no execution."""
from __future__ import annotations

import re
import shlex
import hashlib
import json


GUIDANCE = (
    "ContextPilot efficiency guidance (never overrides the user's task or project rules): "
    "search for relevant symbols before reading whole files; prefer changed code plus enough surrounding context; "
    "expand partial reads when correctness needs it. Keep exploration within the task and its dependencies. "
    "Resolve material missing requirements early; don't ask about routine choices. Run relevant tests first, "
    "then all required checks; don't rerun unchanged successful checks without a reason. "
    "Use subagents only when their benefit justifies duplicated context. Explain results concisely without "
    "omitting risks or verification. Keep reusable instructions stable. Avoid needless model switches. "
    "Use native tool discovery rather than loading unused MCP tools. Respect explicit model/effort preferences; "
    "reserve deeper reasoning for difficult or sensitive work. For unrelated tasks, suggest a fresh session "
    "with a verified handoff. Never infer chat history from repository files. "
    "If tool output says it was reduced, recover needed details explicitly; never treat a partial view as complete."
)

# The dashboard describes enforceable transforms separately from model guidance.
FEATURES = (
    ("handoffs", "Compact project handoffs", "checkpoint", "Repository checkpoints; explicit notes preserved."),
    ("bounded", "Large-file limits", "filter", "Eligible unbounded text Reads are visibly limited."),
    ("duplicate", "Duplicate Read reduction", "filter", "Exact repeats within the current prompt only."),
    ("compaction", "Native compaction", "native", "Owned default only; existing preferences take precedence."),
    ("reporting", "Pilotboard estimates", "report", "Signed payload estimates, separate from native usage."),
    ("terminal", "Terminal output compaction", "filter", "Known test/build/lint progress and color noise; machine-readable output is preserved."),
    ("search_first", "Search before reading", "guidance", "Session guidance; model compliance is not guaranteed."),
    ("diff_context", "Changed-code context", "guidance", "Prefer relevant diffs and surrounding code."),
    ("adaptive", "Adaptive file reads", "filter", "Line-density and fresh context-aware windows grow for contiguous reads; explicit limits win."),
    ("errors", "Repeated-error grouping", "filter", "Exact diagnostic/stack repeats retain a full copy, count and positional references."),
    ("retry", "Retry-loop detection", "guidance", "Warn once after repeated identical tool inputs in one prompt."),
    ("tests", "Targeted testing", "guidance", "Relevant tests first; required checks remain required."),
    ("search_duplicate", "Search and listing reduction", "filter", "Privacy-verified file/directory Grep repeats and lossless Glob/Grep path shortening."),
    ("scope", "Task scope guard", "guidance", "Keep exploration related to the requested task."),
    ("preflight", "Prompt preflight", "guidance", "Resolve material ambiguity without storing prompt text."),
    ("budget", "Context-aware tool budgets", "filter", "Fresh native pressure reduces automatic Read/Grep windows; explicit requests remain allowed."),
    ("checkpoint", "Automatic checkpoints", "checkpoint", "Bounded repository handoff before compaction and at session end."),
    ("exclusions", "Privacy and file exclusions", "filter", "Excluded paths and symlinks are never optimized or exported."),
    ("instructions", "Project instruction audit", "guidance", "Bounded root/rules audit measures size and exact repeated paragraphs; never rewrites instructions."),
    ("mcp", "Native MCP tool discovery", "native", "Claude defers tools on supported deployments; observe ToolSearch calls without changing servers or permissions."),
    ("agents", "Subagent usage guidance", "guidance", "Avoid unjustified fanout; subagent outputs pass through."),
    ("native", "Native usage feedback", "native", "Documented status-line samples only; no transcript access."),
    ("cache", "Native prompt caching", "native", "Claude manages prompt caching; report configuration hints and native cache samples without inventing savings."),
    ("concise", "Concise output", "native", "Concise default where no existing preference is found, plus session guidance."),
    ("model", "Appropriate model", "recommendation", "Preserve selected model; automatic per-task switching is unavailable."),
    ("effort", "Appropriate reasoning effort", "native", "Medium default only where no explicit preference exists; no per-task switching."),
    ("usage", "Usage visibility", "native", "Live context and reported 5-hour/7-day limits when supplied by Claude."),
    ("fresh", "Fresh-session guidance", "guidance", "Suggest handoffs for unrelated work; never force a session reset."),
)

PLATFORM_LIMITS = (
    ("Automatic main-session model/effort switching", "No verified command-hook control; preserve explicit model and effort choices."),
    ("Automatic instruction rewriting", "Project instructions allow advisory audits, not runtime rewrites of instruction files."),
    ("Deleting past chat messages", "Command hooks cannot edit previous conversation turns; Claude owns native compaction."),
    ("Exact billed savings", "Payload estimates cannot establish what an unchanged session would have cost or how subscription limits would change."),
    ("Enforcing reasoning and workflow guidance", "Hooks can advise search order, test selection and task scope, but cannot verify the model's internal reasoning or safely replace user requirements."),
)


def native_controls(preferences: dict, environment) -> dict:
    """Interpret only documented flags; return labels, never raw environment values."""
    settings_env = preferences.get("env", {})
    result = {}
    for name, key in (("ENABLE_TOOL_SEARCH","tool_search"),("DISABLE_PROMPT_CACHING","prompt_cache")):
        source = "environment" if name in environment else "settings" if name in settings_env else "platform"
        value = environment.get(name,settings_env.get(name))
        mode = "platform_default"
        if key == "tool_search" and value is not None:
            mode = {"true":"enabled","false":"disabled","auto":"automatic"}.get(value,"custom") if isinstance(value,str) else "custom"
            if isinstance(value,str) and value.startswith("auto:") and 1 <= len(value[5:]) <= 3 and value[5:].isascii() and value[5:].isdigit() and int(value[5:]) <= 100:
                mode = "automatic"
        elif key == "prompt_cache" and value is not None:
            mode = "disabled" if value == "1" else "platform_default" if value == "0" else "custom"
        result[key] = {"mode":mode,"source":source}
    return result


def terminal_command(command: object) -> bool:
    """Recognize human-oriented test/build/lint output, never execute or rewrite a command."""
    if not isinstance(command, str) or re.search(r"[\n\r;&|`$<>]", command):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    if any(re.match(r"--?(?:json[\w-]*|xml|yaml|porcelain|format|message-format|output-format|reporter|diff)(?:=|$)", part) for part in parts[1:]):
        return False
    if parts[:2] in (["uv","run"],["poetry","run"]):
        parts = parts[2:]
        if not parts:
            return False
    executable = parts[0].rsplit("/", 1)[-1]
    return (executable == "pytest" or
            executable in {"python", "python3", "python3.11", "python3.12", "python3.13", "python3.14"}
            and parts[1:3] in (["-m", "pytest"],["-m","unittest"]) or
            executable == "cargo" and parts[1:2] in (["test"],["build"],["check"],["clippy"]) or
            executable == "go" and parts[1:2] in (["test"],["build"],["vet"]) or
            executable in {"npm", "pnpm", "yarn"} and
            (parts[1:2] in (["test"],["build"],["lint"]) or parts[1:3] in (["run","test"],["run","build"],["run","lint"])) or
            executable in {"ruff","eslint","tsc"})


def read_window(width: int, pressure: float | None, continuation: bool = False) -> int:
    target = 8192
    if pressure is not None:
        target = 1024 if pressure >= 95 else 2048 if pressure >= 85 else 4096 if pressure >= 65 else target
    if continuation and (pressure is None or pressure < 65):
        target *= 2
    return max(1, min(1200, target // max(1,width)))


def diagnostic_end(lines: list[str], index: int) -> int:
    """Recognize bounded Python and JS/Java stacks; unsupported text remains verbatim."""
    line = lines[index]
    if line.strip() == "Traceback (most recent call last):":
        for end in range(index+1,min(len(lines),index+256)):
            if re.fullmatch(r"(?:[\w.]+(?:Error|Exception|Interrupt)|Exception|SystemExit)(?::[^\n]*)?\r?\n?",lines[end]):
                return end+1
        return index
    if re.match(r'^(?:[\w.$]*(?:Error|Exception)|Exception in thread\b|ERROR\b|Error:|error:|WARNING\b|Warning:|warning:|\[ERROR\]|\[WARNING\])',line):
        end = index+1
        while end < min(len(lines),index+256) and re.match(r"^(?:\s+at\s|\s*Caused by:|\s*Suppressed:|\s+\.\.\. \d+ more)",lines[end]):
            end += 1
        return end
    return index


def compact_terminal(command: object, text: str) -> tuple[str, str]:
    """Only remove recognized noise; preserve all other lines and their order."""
    if not terminal_command(command) or "\0" in text:
        return text, "terminal"
    try:
        json.loads(text)
        return text,"terminal"  # A recognized command may still emit machine-readable data.
    except (ValueError,RecursionError):
        pass
    original = text
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    if re.search(r"\x1b",text):
        return original,"terminal"  # Other terminal control sequences may carry significant structure.
    try:
        json.loads(text)
        return original,"terminal"
    except (ValueError,RecursionError):
        pass
    lines = text.splitlines(keepends=True)
    blocks: dict[int,tuple[str,str]] = {}
    counts: dict[str,int] = {}
    first: dict[str,int] = {}
    index = 0
    while index < len(lines):
        end = diagnostic_end(lines,index)
        if end > index:
            block = "".join(lines[index:end])
            digest = hashlib.sha256(block.encode()).hexdigest()
            if len(block.encode()) >= 256 and (digest in counts or len(counts) < 256):
                blocks[index] = (digest,block)
                counts[digest] = counts.get(digest,0)+1
                first.setdefault(digest,index)
            index = end
        else:
            index += 1
    output: list[str] = []
    removed = 0
    grouped = False
    index = 0
    while index < len(lines):
        line = lines[index]
        clean = line.strip()
        if re.fullmatch(r"[.sFxXE]+\s*\[\s*\d{1,3}%\]", clean) or re.fullmatch(r"(?:\[[= >#\-]+\]|[= >#\-]{8,})\s*\d{1,3}%",clean):
            removed += 1
            index += 1
            continue
        if index in blocks:
            digest, block = blocks[index]
            if counts[digest] > 1:
                label = first[digest]+1
                if first[digest] == index:
                    output.append(block)
                    output.append(f"\n[ContextPilot: diagnostic at original line {label} occurred {counts[digest]} times; complete copy above.]\n")
                else:
                    output.append(f"[ContextPilot: exact repeat of diagnostic at original line {label}; complete copy above.]\n")
                grouped = True
                index += len(block.splitlines(keepends=True))
                continue
        if clean == "Traceback (most recent call last):":
            consumed = False
            end = index + 1
            while end < min(len(lines), index + 256):
                if re.fullmatch(r"(?:[\w.]+(?:Error|Exception|Interrupt)|Exception|SystemExit)(?::[^\n]*)?\r?\n?", lines[end]):
                    end += 1
                    block = lines[index:end]
                    count = 1
                    while lines[end:end+len(block)] == block:
                        count += 1
                        end += len(block)
                    if count > 1:
                        output.extend(block)
                        output.append(f"\n[ContextPilot: preceding identical traceback occurred {count} times.]\n")
                        grouped = True
                        index = end
                        consumed = True
                    break
                end += 1
            if consumed:
                continue
        count = 1
        if re.match(r"^(?:ERROR\b|Error:|error:|WARNING\b|Warning:|warning:)", clean):
            while index + count < len(lines) and lines[index + count] == line:
                count += 1
        output.append(line)
        if count > 1:
            if not line.endswith(("\n", "\r")):
                output.append("\n")
            output.append(f"[ContextPilot: preceding identical diagnostic occurred {count} times.]\n")
            grouped = True
        index += count
    if removed:
        output.append(f"\n[ContextPilot: omitted {removed} recognized progress lines; diagnostics and summary retained.]\n")
    result = "".join(output)
    if len(original.encode()) - len(result.encode()) < 512:
        return original, "terminal"
    return result, "errors" if grouped else "terminal"
