# Contributing

Thanks for helping improve Across Orchestrator.

## Development

```bash
python3 -m pip install -e '.[dev]'
npm install
bash scripts/check.sh
```

The Python package has no runtime dependencies. Node dependencies are
development-only and are used by strict browser E2E probes.

## Pull Requests

- Keep changes focused.
- Add or update tests for behavior changes.
- Preserve the host/plugin boundary: hosts own credentials, agent processes,
  approvals, and UI; Across Orchestrator owns task lifecycle, contracts,
  quality gates, evidence, and protocol surfaces.
- Do not commit private paths, tokens, credentials, screenshots, task scratch
  directories, generated evidence bundles, or local runtime state.
- Run `bash scripts/check.sh` before opening a PR.
- Follow the project [Code of Conduct](CODE_OF_CONDUCT.md).

## Security Issues

Please do not disclose vulnerabilities or secrets in public issues. See
[SECURITY.md](SECURITY.md) for the reporting policy.
