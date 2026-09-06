# Observational Memory v0.10.1

Final legacy closeout, September 5, 2026. **Unmaintained:** no future feature,
compatibility or security updates are promised. See [migration](legacy-migration.md).
Existing installations are not automatically disabled by this patch release.

## Changes

- Preserve unrelated Claude hook groups and entries during installation and
  removal. Back up settings privately before changes; reject malformed or
  symlinked settings for manual review. Quote installed POSIX script paths.
- Diagnose OM hook executables rather than treating a whole command with
  arguments as a filename. Retirement guidance no longer treats missing OM
  jobs as a reason to reinstall the project.
- Include text blocks after Anthropic thinking blocks and join text-only content
  parts from compatible OpenAI responses and Batch results. Reject truncated
  Anthropic responses, including flattened proxy responses. Thanks to Timothy
  Johnson for the provider parsing contribution in #108.
- Declare HTTPX directly: OM's OAuth and retry paths import it independently of
  the provider SDKs' transport dependencies.
- Mark core, bundled Cowork metadata and distribution descriptions as legacy;
  preserve source and existing licensing.

## Verification scope

The closeout uses mocked provider responses and isolated lifecycle fixtures, not
a live paid-provider call or hosted memory import. The local full suite passed
on Python 3.12.13 with Anthropic 1.2.0, OpenAI 3.6.0 and HTTPX 0.28.1.
See the release's CI and closeout receipt for final artifact checks. No future
host/runtime compatibility is implied by those results.
