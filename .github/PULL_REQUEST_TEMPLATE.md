## Related issue

<!-- Required: Closes #<issue>. Post status and acceptance-criteria coverage as issue comments; keep the original issue unchanged. -->

## Implementation

<!-- What changed and which relevant decisions were made. -->

## Validation

<!-- Local checks performed, then automatic CI status reported separately. -->

- [ ] The related issue's acceptance criteria are covered and the evidence is posted in an issue comment.
- [ ] Unit tests, `ruff check`, `ruff format --check`, and `git diff --check` pass.
- [ ] `hermes plugins doctor . --ci` and `hermes plugins validate .` passed with a disposable profile when the plugin contract or packaging changed.
- [ ] Tests use only fake keys and mocked gateways; no real OAuth or paid model calls were made.
- [ ] Secrets, profile settings, approval policy, and README effect descriptions were reviewed.
- [ ] `plugin.yaml` and `pyproject.toml` versions were incremented together for a release, or the reason it does not apply is stated.

## Risks and deviations

<!-- Residual risks and any differences from the issue. Enter "None" when there are none. -->
