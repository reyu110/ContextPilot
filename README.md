![ContextPilot — close the tab, keep the thread. Local repository handoffs for Claude Code.](docs/assets/hero.svg)

<p align="center">
  <a href="#quick-start"><img alt="Claude Code skill" src="https://img.shields.io/badge/Claude_Code-Skill-152936?style=flat-square&amp;labelColor=152936&amp;color=73e2c0" /></a>
  <a href="#quick-start"><img alt="Python 3.11 or newer" src="https://img.shields.io/badge/Python-3.11%2B-152936?style=flat-square&amp;labelColor=152936&amp;color=73e2c0" /></a>
  <a href="#privacy-and-trust"><img alt="Runs locally" src="https://img.shields.io/badge/Runtime-Local-152936?style=flat-square&amp;labelColor=152936&amp;color=73e2c0" /></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/License-MIT-152936?style=flat-square&amp;labelColor=152936&amp;color=73e2c0" /></a>
</p>

<p align="center">
  <strong>A small local skill for the moment between “done for today” and “where was I?”</strong>
</p>

<p align="center">
  <a href="#quick-start">Install</a> ·
  <a href="#how-it-works">Workflow</a> ·
  <a href="#estimates-at-a-glance">Estimates</a> ·
  <a href="examples/HANDOFF.md">Example handoff</a> ·
  <a href="#reference">Reference</a>
</p>

ContextPilot saves a bounded repository inventory and your explicit notes. When
you return, it checks the handoff against current file metadata and Git state so
you can spot stale information before continuing. **No account, API key, or
runtime Python dependencies.**

## Quick start

Install for Claude Code across your projects:

```sh
npx skills add reyu110/ContextPilot --skill contextpilot --agent claude-code --global --yes
```

Then use these commands in Claude Code:

| Before you leave | When you return | Just checking |
| :--- | :--- | :--- |
| `/contextpilot snapshot` | `/contextpilot resume` | `/contextpilot status` |
| Save a local handoff. | Flag changes and review the next task. | Inspect the saved snapshot. |

**Requires Python 3.11+ and Git on PATH.** Node.js/npm is only for installation.
Omit `--global` for a project-only install. Reopen Claude Code if the skill is not
listed. This public repository does not require GitHub authentication.

> Snapshot writes inside `.contextpilot/` and appends `/.contextpilot/` to
> `.gitignore`. Resume and status are read-only. Tasks never execute automatically.

## How it works

![Three steps: snapshot selected files and explicit notes; retain a local handoff and integrity record; resume with metadata and Git checks, then reconfirm tasks.](docs/assets/workflow.svg)

| What you keep | What gets checked | What stays yours |
| :--- | :--- | :--- |
| A readable handoff, repository inventory, and explicit tasks and decisions. | File metadata, Git state, handoff integrity, and coverage limits. | Your files and notes stay on your machine during skill execution. |

Use it alongside Claude Code's `/compact`. Compact manages the active
conversation; ContextPilot leaves a reviewable handoff in your working tree.
It cannot recover private chat history, infer your intentions, or measure
Claude's context-window utilization.

## Estimates at a glance

A larger scan budget means more local sampling. The numbers below make that
tradeoff visible; **they are arithmetic illustrations, not performance claims.**

![Input budgets of 16, 64, 128 and 256 KiB estimate 4,096, 16,384, 32,768 and 65,536 tokens using bytes divided by four. An illustrative full-budget scan reads 256 KiB fresh, zero selected content on a matching cache, and 256 KiB on refresh.](docs/assets/estimates.svg)

The default reads up to **120 selected files**, **256 KiB total**, and **16 KiB
per file**. Each generated scan JSON or handoff is capped at **64 KiB**. Truncation
is reported. A matching cache skips content rereads, while directory and Git
metadata checks still run.

## Privacy and trust

The skill runs locally, filters ignored and sensitive paths before sampling, and
skips symlinks. It does not run repository scripts, export source file bodies, or
make network requests. The `npx` installer downloads the skill; the installed
Python runtime does not upload your repository.

**Review before sharing.** Filenames, dependency signals and human notes can
contain private information. Secret detection is heuristic. A metadata match
cannot prove unread content is unchanged or that old tasks are still valid.

## Carry decisions, not guesses

The scanner cannot know what you discussed. Add only supplied or explicitly
confirmed information to `.contextpilot/notes.json` during an explicit snapshot
invocation. Notes survive regeneration; human claims still need reconfirmation.
See the [note contract](.claude/skills/contextpilot/references/handoff-template.md)
and the [generated example](examples/HANDOFF.md).

## Reference

The short path is above. The implementation details are here when you need them.

<details>
<summary><strong>Notes schema and validation</strong></summary>

The scanner cannot know conversations or intentions. Before an explicit snapshot,
put only supplied or confirmed information in `.contextpilot/notes.json`:

```json
{
  "objectives": [{"text": "Finish the import command", "source": "user"}],
  "decisions": [{"text": "Keep storage as local JSON", "source": "confirmed"}],
  "completed": [{"text": "Parser implementation reviewed", "source": "user"}],
  "tasks": [{"text": "Add malformed input coverage", "source": "user"}],
  "blockers": [],
  "next_steps": []
}
```

All sections are optional. Sources are `user`, `confirmed`, and `assumption`;
decisions require `confirmed`, meaning explicit human confirmation. The tool
validates this label but cannot authenticate who confirmed a note. Empty sections
remain explicitly unknown. Maximum: 32 KiB notes file, 50 notes per section,
1000 characters per note. Notes are preserved across snapshots. Edit notes rather
than the generated handoff to carry changes into the next snapshot. See the
[handoff contract](.claude/skills/contextpilot/references/handoff-template.md).

</details>

<details>
<summary><strong>Scan limits, estimate arithmetic, and cache behavior</strong></summary>

| Boundary | Default size | Rough token equivalent |
| :--- | ---: | ---: |
| Sample from one selected file | Up to 16 KiB | 4,096 |
| Total selected file content per scan | Up to 256 KiB | 65,536 |
| Each generated scan JSON or handoff | Up to 64 KiB | 16,384 |

Input samples are inspected locally; source file bodies are not exported. Output
is metadata, coverage, labels and explicit notes. The input and output estimates
describe different data and cannot establish a compression ratio or token savings.
The snapshot CLI's output estimate combines scan JSON and handoff bytes.

| Flag | Default | Meaning |
| --- | ---: | --- |
| `--max-files` | 120 | Maximum selected file read attempts; also caps Git change entries |
| `--max-bytes` | 262144 | Total selected file-content bytes read per scan |
| `--max-file-bytes` | 16384 | Maximum bytes read from each selected file |
| `--max-candidates` | 20000 | Eligible discovered file entries examined before stopping traversal |
| `--max-output-bytes` | 65536 | UTF-8 bytes per scan JSON or handoff/continuation output; minimum 4096 |

These limits apply to scanner content and generated output, not operating system
directory listings or Git subprocess metadata. Notes and previous handoffs have
separate fixed read limits. Git commands have a 15-second timeout. Very large
worktrees may still incur directory/Git inspection cost. Truncation is reported;
lower-ranked detail is dropped while handoff section headings remain present.

Every scan checks file names, sizes, nanosecond mtimes, limits and Git metadata.
If their fingerprint matches the previous snapshot, content reads are reused.
This avoids rereading unchanged files, not directory traversal. Status avoids
traversal entirely. `snapshot --refresh` forces selected content reads. Resume
does not save its fresh scan. All scans include approximate input bytes/tokens;
snapshot CLI output also reports artifact bytes and approximate output tokens.
The estimate is bytes / 4, not a tokenizer or a claim about model utilization.

</details>

<details>
<summary><strong>Architecture and local artifact layout</strong></summary>

| Mode | Reads | Writes |
| :--- | :--- | :--- |
| `snapshot` | Bounded repository selection + explicit notes | Local artifacts + root ignore rule |
| `resume` | Saved handoff + current bounded scan | Nothing |
| `status` | Saved scan metadata | Nothing |

- `scan.py` owns deterministic discovery, privacy filtering, Git inspection,
  prioritization, bounded text reads, cache fingerprints and confined artifact I/O.
- `snapshot.py` owns note validation, labeled Markdown, one-generation backup,
  resume comparison and CLI modes. No repository code is imported or executed.
- The skill bundle is self-contained, so `${CLAUDE_SKILL_DIR}` works after copying
  it globally or into another project. The project requires no Python package import.

Ranking favors README/architecture docs, manifests, filename-based entry points,
changed files, source files and tests, with path depth penalties and lexical
tie-breaking. Framework signals are dependency word matches in selected manifests;
they are not proof a framework is actually used. Architecture output is a structural
inventory, not a model-generated explanation of runtime behavior.

Snapshots include overview, architecture, important files/directories, objectives,
confirmed decisions, completed work, unfinished tasks, Git changes, blockers and
next steps. Observed metadata, user notes, confirmed notes and assumptions have
distinct labels. See the real generated [example](examples/HANDOFF.md).

### What stays on disk

```text
.contextpilot/                  # Ignored by Git
├── HANDOFF.md                  # Human-readable handoff
├── HANDOFF.previous.md         # One previous handoff, after a repeat snapshot
├── scan.json                   # Selected file metadata and scan coverage
├── snapshot.json               # Fingerprint and handoff integrity record
└── notes.json                  # Optional, explicitly supplied notes
```

</details>

<details>
<summary><strong>Manual installation and CLI commands</strong></summary>

### Install

Python 3.11+ must be available as `python3`, and Git must be on PATH, including
when inspecting a project that has never been initialized with Git.

The quick-start command installs across your projects. Omit `--global` to install
only in the current project. This repository is public; GitHub authentication is
not required to download it.

Alternatively, clone or download this repository, then copy the **entire skill
directory** using one of the commands below.

Personal installation, available across your projects:

```sh
mkdir -p ~/.claude/skills
cp -R .claude/skills/contextpilot ~/.claude/skills/
```

Project installation, available only in a selected project:

```sh
mkdir -p /path/to/project/.claude/skills
cp -R .claude/skills/contextpilot /path/to/project/.claude/skills/
```

Run these from this checkout. If the destination already contains ContextPilot,
back up any edits and replace that directory rather than nesting another copy.
On Windows, copy the same directory with Explorer or PowerShell and use `py -3`
for direct CLI commands. Claude must have permission to run Python and Git.
Reopen Claude Code if the skill is not discovered. No pip install is required
for the skill; installing the Python project alone does not install the skill.

### Use in Claude Code

```text
/contextpilot snapshot
/contextpilot resume
/contextpilot status
/contextpilot snapshot --refresh --max-files 80 --max-bytes 131072
```

Only explicit user invocation enables the skill (`disable-model-invocation:
true`). `snapshot` creates or refreshes `.contextpilot/scan.json` and
`.contextpilot/HANDOFF.md`, saves an integrity record as `snapshot.json`, and adds
`/.contextpilot/` to the project's `.gitignore`. This ignore-file append is the
sole write outside `.contextpilot/`. No application source files are modified.
An existing handoff is backed up as `HANDOFF.previous.md` before replacement;
there is one backup, not an archive. Nothing is automatically staged or committed.

`resume` reads the handoff and checks current file metadata and Git state. It
flags changed, missing or no-longer-selected files, edits to the handoff, and the
next documented unfinished task. It never updates the saved snapshot or executes
tasks. Human notes always need reconfirmation. `status` reports the saved snapshot
without scanning or writing; no arguments defaults to status in Claude Code.

The Python CLI works independently of Claude Code:

```sh
python3 .claude/skills/contextpilot/scripts/snapshot.py snapshot --root /path/to/project
python3 .claude/skills/contextpilot/scripts/snapshot.py resume --root /path/to/project
python3 .claude/skills/contextpilot/scripts/snapshot.py status --root /path/to/project
python3 .claude/skills/contextpilot/scripts/scan.py --root /path/to/project
```

The last command prints JSON to stdout and writes nothing. Errors return exit
code 2 with an explanation on stderr. Redirecting stdout is your responsibility;
the tool provides no arbitrary artifact output-path option.

</details>

<details>
<summary><strong>Privacy rules and trust boundaries</strong></summary>

Git's ignore engine honors nested `.gitignore` rules and negations, even in
non-Git directories via a temporary bare Git directory outside the project. If
Git is unavailable or ignore checking fails, discovery fails closed and surfaces
a warning rather than inspecting potentially ignored files. Tracked files that
match ignore rules are excluded too. There are no network operations, subprocess
shells, repository scripts, diffs, author identities, commit messages, or exported
file bodies. Recent commit metadata contains only short hashes and dates.

The scanner skips `.env*`, credential/secret/token filenames, SSH/private-key
files, common credential directories, dependencies, virtual environments, build
outputs, `.git`, and `.contextpilot`. It only considers known text extensions and
selected extensionless manifests, rejects sampled NUL/non-UTF-8 data, and skips
files whose sampled text resembles common credentials. Every symlink is skipped,
including internal links; symlinked artifact or `.gitignore` paths are rejected.
New artifact files have private permissions where supported. Existing `.gitignore`
content is preserved and the root ignore rule is appended once.

Repository content and notes remain untrusted data. Neither Python code nor the
skill should obey instructions embedded in a repository or handoff. Review notes
and output before sharing: no secret detector can recognize every custom secret,
and paths, language counts, dependency signals and human notes can themselves
contain private information. Git ignore files/configuration are read by Git.
The tool is not a sandbox for concurrent hostile filesystem mutation.

</details>

<details>
<summary><strong>Known limitations</strong></summary>

- Only selected UTF-8 text files are sampled. Unread portions, unknown extensions,
  excluded paths and files beyond discovery/selection limits are not understood.
- Entry points are filename candidates; framework detection is a text heuristic.
  The MVP does not parse call graphs or summarize implementation semantics.
- Metadata cache checks cannot detect content changes with restored size and
  mtime, or changes beyond a truncated inventory. Use `--refresh` when uncertain.
- Git renames show the destination path. Git change listings are capped. No Git
  executable means a warning and empty discovery, including for non-Git projects.
- A snapshot is individually atomic per artifact, not a multi-file transaction;
  interruption can leave mixed generations. Resume flags handoff integrity issues;
  rerun snapshot after reviewing notes if interrupted.
- Windows paths and process APIs are supported where practical; the current test
  run is on macOS, and Windows installations may restrict symlink creation.

</details>

<details>
<summary><strong>Development and tests</strong></summary>

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
```

On Windows use `.venv\Scripts\python.exe`. Tests use temporary repositories and
require Git. They cover discovery/ranking, Git status and renames, non-Git/empty
projects, ignore rules, secrets/binaries, symlinks, Unicode, missing files, large
inventories, snapshot notes/backups, resume/cache, limits and skill frontmatter.
No API key or network is required to run tests once pytest is installed.

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

- **Skill not listed:** copy the directory, not just SKILL.md; confirm the path is
  `~/.claude/skills/contextpilot/SKILL.md` or the project's equivalent and reopen
  Claude Code. Use an explicit invocation; automatic model invocation is disabled.
- **Git unavailable / empty results:** install Git, check PATH, and read warnings.
  Non-Git projects still need Git for faithful ignore matching. Empty snapshots
  may also result from privacy/ignore rules or unsupported text extensions.
- **Missing handoff:** explicitly run `snapshot` first. Status/resume create nothing.
- **Stale handoff:** review tasks and decisions with the user, update notes, then
  snapshot again. Use `--refresh` for fresh content reads.
- **Malformed notes / artifact:** fix the JSON using the documented schema. If a
  saved scan is damaged, `snapshot --refresh` bypasses the cached scan. Never
  delete notes without backing them up.
- **Symlink refused / permission denied:** use a real project directory and real
  artifact paths with local write permission. Do not point `.contextpilot` at
  another directory. `.gitignore` must also be a normal file.
- **Output truncated:** increase limits deliberately. Broadening discovery also
  broadens private metadata exposure. Check Coverage and size and scanner warnings.

</details>

---

Built with Python's standard library. Distributed under the [MIT license](LICENSE).
[Contributions](CONTRIBUTING.md) are welcome; keep changes small, local, and honest
about what the scanner can verify.
