# Contributing to Observational Memory

Thanks for your interest in improving Observational Memory (`om`).

## Development setup

```bash
make install-dev
make check   # ruff check, ruff format --check, pytest
```

CI runs the same checks on Python 3.11, 3.12, and 3.13. Please run `make check`
before opening a pull request.

## Project direction

Read `CLAUDE.md` and the active documents in `plans/` before large changes.
Issues and small, focused pull requests are the easiest to review.

## Licensing and contributor terms

The `om` core in this repository is and will remain MIT licensed.

Observational Memory is stewarded by Intertwine AI, LLC, which also builds
separately licensed commercial add-ons on top of the core's public plugin
interfaces. So that the project can keep a single, unambiguous licensing
story, contributions are accepted under the following terms:

1. **You certify the Developer Certificate of Origin (DCO),
   <https://developercertificate.org/>**: the contribution is your own work
   (or you otherwise have the right to submit it) and you have the right to
   submit it under this project's license. Sign each commit with
   `git commit -s`, which adds a `Signed-off-by:` line.
2. **You license your contribution under the MIT license** of this
   repository.
3. **You additionally grant Intertwine AI, LLC a perpetual, worldwide,
   non-exclusive, irrevocable, royalty-free right to use, reproduce, modify,
   sublicense, and distribute your contribution under other license terms,
   including commercial terms.** This is what lets the steward ship a
   supported commercial distribution without ever re-contacting every
   contributor; your contribution always also remains available to everyone
   under MIT in this repository.

Opening a pull request constitutes agreement to these terms. If you are
contributing on behalf of an employer, please make sure you are authorized to
agree to them.

## Security

Do not open public issues for security problems. Report them privately to
<bryan@intertwinesys.com>.
