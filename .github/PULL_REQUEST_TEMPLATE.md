## Summary

Describe the problem and the change.

## Verification

List the local checks that you ran.

- [ ] `uv run python scripts/export_openapi.py --check`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy src`
- [ ] `uv run pytest`
- [ ] Not applicable checks have an explanation below.

## Safety and compatibility

- [ ] I added or updated tests for behavior changes.
- [ ] I updated documentation and generated files when required.
- [ ] I did not add secrets or sensitive user data.
- [ ] I kept provider-owned model loops outside the core package.
- [ ] I described compatibility or migration effects below.

## Related work

Link related issues or pull requests. Add migration notes and reviewer context when they apply.
