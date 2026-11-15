# Security and Privacy Notes

## Portfolio boundary

This repository is a curated copy of the active project. It must not contain:

- Personal conversation or memory databases.
- API keys, access tokens, passwords, or private keys.
- Local model weights or model caches.
- Virtual environments.
- Local runtime logs.
- Backups and temporary files.
- Personal workspace data.
- Local-only configuration files.

## Execution safety

Tool execution and self-development features require explicit boundaries, validation, and review. The presence of safety-related modules does not by itself prove that every execution path is secure.

## Local configuration

Real machine-specific settings must remain outside version control. Any future example configuration should contain placeholders only.

## Review limitations

Pattern-based secret scanning can identify common credential formats, but it cannot guarantee that no sensitive information exists.

Before a public release:

1. Review the complete staged Git diff.
2. Inspect configuration and test fixtures.
3. Review third-party dependencies and licenses.
4. Confirm that personal data and runtime artifacts are excluded.
5. Rotate any credential that may have been exposed.

## Reporting

Do not publish private security findings, access tokens, personal records, or raw operational logs in public issues.
