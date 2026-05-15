# Release 0.6.1

Released May 15, 2026.

## Summary

`v0.6.1` is a hardening release for `v0.6.0` OM Cluster and startup context work. It keeps cluster sync opt-in and keeps local Markdown as the readable view.

## User-Facing Changes

- `om context` now emits a bounded startup pack by default.
- `om context` accepts `--cwd`, `--task`, `--for`, and `--budget-chars`.
- `om recall` can expand startup handles or search deeper memory.
- Reflection metadata now carries richer fields for actionability, sensitivity, confidence, freshness, and scope.
- Generated profile output can be narrowed with `OM_PROFILE_INCLUDE_IDENTITY` and `OM_PROFILE_SECTIONS`.
- OM Cluster can run a stdlib relay with `om-relay` or `om cluster relay serve`.
- `om cluster relay health` checks relay reachability and scans relay artifacts for obvious secret leaks.
- `om cluster status --json` reports transport diagnostics, review artifacts, and remediation text.
- Approved peers are cleaned out of `pending_peers` after convergence.
- Public validation steps now live in [om-cluster-validation.md](om-cluster-validation.md).

## Compatibility Notes

- Cluster sync remains disabled until the user runs `om cluster init` or `om cluster join`.
- The base package still avoids heavy relay/P2P server dependencies.
- Relay access is not cluster trust. Local nodes still verify signatures, membership, revocation, key epochs, tombstones, and payload hashes.
- Hosted platform memory export remains review-based. `om` does not silently write ChatGPT or Claude Managed Agents memory.

## Validation

Release validation:

```bash
make check
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Recommended extra checks for local release validation:

```bash
OM_CLUSTER_ENABLED=0 om context
om recall --query "current work" --limit 3
om cluster relay health --artifact-dir /tmp/om-relay --json
```

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.6.1
om install
om doctor
```

For maintainer release steps, see [MAINTAINERS.md](MAINTAINERS.md).
