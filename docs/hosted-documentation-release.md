# Hosted documentation release system

Audience: documentation and release maintainers.

This record defines how the project previews, publishes, and rolls back the hosted
documentation. It does not publish content.

The details below were verified on 2026-08-26.

## System of record

- **Repository:** [`ashtonchew/modal-computer-use-docs`](https://github.com/ashtonchew/modal-computer-use-docs)
- **Default and deployment branch:** `main`
- **Content root:** the repository root, with navigation in `docs.json`
- **Production site:** [modal-computer-use.mintlify.app](https://modal-computer-use.mintlify.app)
- **Production owner:** Ashton Chew, [`@ashtonchew`](https://github.com/ashtonchew)
- **Deployment service:** the Mintlify GitHub App
- **Continuous integration:** `.github/workflows/docs.yml` in the documentation repository

The application repository owns code, API contracts, OpenAPI, and benchmark evidence. The
documentation repository owns the public site structure and content.

`@ashtonchew` owns the documentation repository and has administrator access. Mintlify builds the
site after a change reaches `main`. The production owner must also retain administrator access to
the Mintlify deployment and its Git settings.

The documentation repository publishes from protected `main`. Keep its documentation check and
pull-request gate enabled; do not use a direct push to bypass a failed release check.

## Current production baseline

- **Released SDK described by the checks:** `modal-computer-use[modal]==2.0.2`
- **Production documentation revision:** `fb40bf051b359095ff36076a5982c77d5630c734`
- **Successful deployment record:** [GitHub deployment 6285236375](https://api.github.com/repos/ashtonchew/modal-computer-use-docs/deployments/6285236375)
- **Production verification:** `2026-09-05`
- **Verified production URL:** `https://modal-computer-use.mintlify.app`

## Retained rollback baselines

The retained patch documentation baseline is `73461e8ef563e4ab372c6f051f8f142d7c5e84f2`,
which describes `modal-computer-use[modal]==2.0.0`. Keep this revision available for patch rollback.
The operator-selected SDK rollback remains `modal-computer-use==2.0.0`.
The annotated `docs-v1.1.0-last-known-good` tag remains the major-version documentation rollback
baseline. Record any change to a selected rollback baseline in a reviewed pull request.

## Preview a change

Use the local preview first.

1. Check out a branch in the documentation repository.
2. Run `npm ci`.
3. Run `npm run dev`.
4. Open `http://localhost:3000`.
5. Run `npm run check`.
6. Run `python3 scripts/check_python_examples.py`.
7. Install the released package version required by the branch.
8. Run `python3 scripts/check_api_contract.py`.

Open a pull request against `main` after the local checks pass. The `Docs` GitHub Actions workflow
runs for the pull request. It validates the site, links, accessibility, dependencies, Python
examples, the released SDK contract, and the repository's Vale rules.

The Mintlify GitHub App creates a hosted preview for a pull request that targets the deployment
branch. The preview updates when the branch changes. Review the preview URL that the Mintlify bot
adds to the pull request. Check every changed route, version entry, redirect, and executable
example. Preview URLs are public unless the Mintlify owner restricts them.

Mintlify documents the preview behavior in its
[preview deployment guide](https://www.mintlify.com/docs/deploy/preview-deployments).

## Publish

The production owner controls publication. Use this order for the optimized-path release.

1. Publish and verify any required runtime artifacts.
2. Publish and verify the Python package.
3. Update the documentation checks to install that exact package version.
4. Complete the local and pull-request preview checks.
5. Record the production owner's explicit approval in the documentation pull request.
6. Squash-merge the pull request into `main`.
7. Wait for the `Docs` workflow and the Mintlify deployment to report success.
8. Verify the production quickstart, provider guides, API reference, version selector, redirects,
   and `llms.txt`.
9. Record the new production revision and its GitHub deployment ID.

Do not merge documentation for an unavailable package. Do not publish from a feature branch. Do
not use a manual Mintlify deployment to bypass a failed pull-request check. A dashboard redeploy is
appropriate only when the verified `main` revision is correct and the automatic deployment failed.

Mintlify describes the GitHub integration in its
[GitHub deployment guide](https://www.mintlify.com/docs/deploy/github).

## Version navigation

Production `docs.json` defines `navigation.versions`: `2.x` is tagged `Latest`, while `1.x` is
tagged `Previous` and retained below the stable `v1/` path. Patch releases update `2.x` in place;
they do not rewrite or redirect the preserved `1.x` pages.

The version selector is a release requirement, not an automatic copy of Git history. The
documentation cutover task must preserve and test the `1.x` page tree. It must also test links and
navigation in both versions. Mintlify documents this configuration in its
[navigation guide](https://www.mintlify.com/docs/organize/navigation#versions).

## Roll back

Use a new commit on `main` for rollback. Do not force-push the deployment branch. Do not delete the
Mintlify deployment.

1. Stop further documentation merges.
2. Identify the failed production revision and deployment ID.
3. Create a rollback branch from the current `main` branch.
4. For a patch rollback, revert the cutover or restore the retained patch baseline in a new
   commit. Use `docs-v1.1.0-last-known-good` for a major-version documentation rollback.
5. Keep the version selector truthful for the package versions that remain available.
6. Run the complete local gate.
7. Open a pull request against `main` and review its Mintlify preview.
8. Get explicit approval from the production owner.
9. Squash-merge the rollback pull request.
10. Wait for both the `Docs` workflow and the Mintlify deployment to succeed.
11. Verify the production site, redirects, version selector, and `llms.txt`.
12. Record the rollback revision, deployment ID, reason, and verification result.

A documentation rollback changes what the site recommends. It must not make a new SDK silently
select an old runtime. Keep released package and runtime compatibility decisions explicit.
