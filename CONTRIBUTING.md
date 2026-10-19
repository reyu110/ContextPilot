# Contributing

Small, focused pull requests are welcome. Discuss behavior changes in an issue
before adding dependencies or new configuration surfaces. Keep Python runtime
dependencies at zero unless there is a demonstrated maintenance benefit.

Set up Python 3.11+ and Git, create a virtual environment, install `.[test]`, then
run `python -m pytest`. Read [AGENTS.md](AGENTS.md) and the README privacy contract.
Add a focused regression test for nontrivial behavior. Tests must use temporary
directories/Git repositories and never need a network, account or credentials.

Changes must preserve deterministic ordering, bounded reads/exports, faithful
ignore handling, safe paths and read-only status/resume. Do not add repository
script execution, telemetry, inferred session history, or raw source exports.
Keep skill docs and flags aligned with the bundled Python CLI. Inspect generated
handoffs and run all tests before submitting. Describe behavior, proof, and any
limitations in the PR; avoid unrelated cleanup.

For a security issue, do not include real secrets, private repository content,
or exploitable details in a public issue. Use a minimal synthetic reproduction
and ask maintainers for a private reporting channel. No hosted security contact
or service is provisioned by this MVP.

Contributions are licensed under the repository's MIT license.
