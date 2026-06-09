# Security Policy

## Reporting Security Issues

Please do not open a public issue for sensitive security reports. Use GitHub
private vulnerability reporting when it is available for this repository.

If private reporting is not available yet, open a minimal public issue that says
you have a security report to share, without including secrets, exploit details,
tokens, credentials, private project data, or local file paths.

## Local-First Runtime

Across Orchestrator stores task state locally by default under
`~/.across/data/across-orchestrator` and runtime sidecar metadata under
`~/.across/run/across-orchestrator`.

The package does not own model credentials, host UI approvals, local agent
installation, or macOS privacy permissions. Host applications must keep those
surfaces explicit and observable.

## Sensitive Data

Tasks, evidence bundles, and quality reports can describe generated files,
commands, and local project context. Users and contributors should avoid storing
or posting:

- API keys, tokens, passwords, cookies, or credentials
- private screenshots or raw production logs
- local paths from private projects
- exploit details in public issues

Release-candidate changes should run `bash scripts/check.sh` and pass the
Security workflow before publication.
