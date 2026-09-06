@AGENTS.md

## Claude Code

- This is unmaintained legacy software (final release 0.10.1; see `docs/legacy-migration.md`). Do not resume development, bump or tag a version, publish, update Homebrew, or install/remove hooks without Bryan's explicit request — historical plans are not authority.
- Never run `om install` / `om uninstall` against this machine's `~/.claude` or `~/.codex` as part of repo work; point `HOME` or the OM config dir at a temporary directory when a test needs one.
- Focused checks: `uv run pytest tests/sync/test_filesystem_sync.py tests/sync/test_relay_transport.py tests/sync/test_store_and_materialize.py`; smoke: `OM_CLUSTER_ENABLED=0 uv run om context >/tmp/om-context.json`.
