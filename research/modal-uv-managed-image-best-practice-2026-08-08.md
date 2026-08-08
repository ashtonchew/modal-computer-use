# Modal + uv managed Images: best-practice research

Research date: 2026-08-08
Scope: Modal Python Image construction with `Image.uv_sync`, uv lockfiles and tool
versions, local source inclusion, named Images, Image IDs, Image Builder Version, base
and `apt` inputs, CI publication, and Sandbox runtime selection.

This is a dated, non-normative research note. It uses first-party Modal documentation,
the Modal Python SDK changelog/reference, and official uv documentation. Statements in
the sections labelled **Documented guarantee** are direct readings of those sources.
Statements in **Inference / policy** are recommendations derived from the guarantees;
they are not additional Modal or uv contracts.

A managed or heavier named Image is not part of article parity. It does not block the
version 2 optimized-default cutover and needs separate correctness, cost, provenance,
and benchmark approval before adoption.

## Short conclusion

For a production Sandbox runtime, build an Image in a separate release or CI job and
publish it under a versioned named-Image tag. The recipe should:

1. use an explicitly selected Python/base image;
2. run `Image.uv_sync(..., frozen=True, uv_version="<pinned>")` against a committed
   `pyproject.toml` and `uv.lock`;
3. add the runtime's local source with `add_local_python_source(..., copy=True)` (or
   `add_local_dir(..., copy=True)` when non-Python assets are required);
4. build eagerly, capture the resulting Modal `object_id`, and publish a tag that is
   write-once under the project's release policy; and
5. create the Sandbox with `Image.from_name("name:release")`, or with
   `Image.from_id(object_id)` when exact artifact identity matters.

The first four points are a recommended release policy, not a single Modal command. The
underlying guarantees are that `uv_sync` copies the project metadata and lockfile, its
default `frozen` mode avoids changing an existing lockfile, source copying can either be
an Image layer or a startup mount, and named-Image lookup never implicitly rebuilds.

## Primary sources

### Modal

- [Image Python SDK reference](https://modal.com/docs/sdk/py/latest/Image) —
  `uv_sync`, local source/file methods, `build`, `object_id`/`from_id`, base Images,
  `apt_install`, named-Image methods, and external registry Images.
- [Images guide](https://modal.com/docs/guide/images) — Image layer caching,
  dependency pinning advice, base-image contents, and Image Builder Version behavior.
- [Named Images guide](https://modal.com/docs/guide/named-images) — build/publish and
  `from_name` workflow, tag semantics, mutability, and Sandbox use.
- [Sandboxes guide](https://modal.com/docs/guide/sandboxes) — the recommendation to
  build in a deployment/scheduled/CI flow and use named Images at Sandbox creation.
- [Using existing Images](https://modal.com/docs/guide/existing-images) — registry
  platform/Python requirements and external-tag cache behavior.
- [Python SDK changelog](https://modal.com/docs/sdk/py/changelog) — introduction of
  `uv_sync`, Image Builder Version changes, eager `Image.build`, and named Images.
- [Workspace CLI reference](https://modal.com/docs/cli/latest/workspace) and
  [Python Workspace reference](https://modal.com/docs/sdk/py/latest/Workspace) —
  setting/listing the Image Builder Version and the current workspace settings type.
- [JavaScript ModalClient reference](https://modal.com/docs/sdk/js/latest/ModalClient) —
  the current statement that the effective Image Builder Version is environment-scoped
  when queried through that SDK surface.
- [Modal environment guide](https://modal.com/docs/guide/environments) — environment
  isolation and lookup defaults.
- [Modal runtime environment variables](https://modal.com/docs/guide/environment_variables)
  — `MODAL_IMAGE_ID` available inside a Modal container.
- [Continuous deployment guide](https://modal.com/docs/guide/continuous-deployment) —
  GitHub Actions credentials and environment selection.

### uv (Astral)

- [uv project structure and lockfile](https://docs.astral.sh/uv/concepts/projects/layout/)
  — universal lockfiles, exact resolved versions, and version-control guidance.
- [uv project configuration](https://docs.astral.sh/uv/concepts/projects/config/) —
  build-system/package-install behavior and the fact that build backends control which
  files enter a distribution.
- [uv building distributions](https://docs.astral.sh/uv/concepts/projects/build/) —
  wheel/sdist outputs and the build-backend boundary for included files.
- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/) —
  `--frozen`, `--locked`, `uv lock --check`, exact sync, groups/extras, and upgrade
  behavior.
- [uv resolution](https://docs.astral.sh/uv/concepts/resolution/) — universal
  resolution and lockfile schema compatibility across uv versions.
- [uv settings](https://docs.astral.sh/uv/reference/settings/) —
  `[tool.uv].required-version` and its runtime failure behavior.
- [uv GitHub Actions integration](https://docs.astral.sh/uv/guides/integration/github/)
  — pinning the uv version in CI and the documented `uv sync --locked` workflow.
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/) —
  requesting a specific standalone-installer version.
- [uv versioning policy](https://docs.astral.sh/uv/reference/policies/versioning/) —
  minor releases may contain breaking changes and lockfile schema bumps.

## `Image.uv_sync` semantics

### Documented guarantee

The [Modal Image reference](https://modal.com/docs/sdk/py/latest/Image#uv_sync)
defines `Image.uv_sync(uv_project_dir="./", *, force_build=False, groups=None,
extras=None, frozen=True, extra_options="", uv_version=None, ...)`. It creates a
virtual environment with `uv sync` and automatically adds the `pyproject.toml` and
`uv.lock` from `uv_project_dir` to the Image build context. `uv_project_dir` is
resolved relative to the current working directory where the Modal command is called.

The method does **not** install the project itself. Modal documents this as equivalent
to passing uv's `--no-install-project` and says that local Python source should be added
after the call with `add_local_python_source` or a similar method. Modal explicitly calls
out that this separation means source-code changes do not require reinstalling the
third-party dependency layer. uv workspaces are currently unsupported by this Modal
method.

`frozen=True` is the Modal default. When a `uv.lock` exists, Modal runs `uv sync
--frozen`, so the lockfile is not updated during the Image build. `groups` and `extras`
are forwarded as `uv sync --group` and `uv sync --extra`, respectively. `extra_options`
is appended to the generated `uv sync` invocation. `uv_version` pins the uv binary
copied from `ghcr.io/astral-sh/uv`.

The corresponding [uv sync documentation](https://docs.astral.sh/uv/concepts/projects/sync/)
says that `--frozen` uses the lockfile without checking whether it is up to date; by
contrast, `--locked` refuses to run when the lockfile is missing or stale. uv's normal
`uv sync` is an exact sync and removes packages not present in the lockfile. Extras are
not installed by default; they must be requested explicitly. Existing lockfile versions
are preferred unless an explicit upgrade is requested.

### Inference / policy

- Treat `uv.lock` as a required release input, not an optional optimization. Modal's
  `frozen=True` clause is conditional on a lockfile being present, so a release job should
  fail before Image construction if the file is absent.
- Run `uv lock --check` (or an equivalent `uv sync --locked`) in CI before invoking the
  Modal build. Then keep `frozen=True` inside the Modal recipe so the remote builder cannot
  silently rewrite the lockfile.
- Pass the same explicit `groups`/`extras` in CI validation and in `Image.uv_sync`; an
  omitted extra is a different runtime environment even when the lockfile is unchanged.
- Run the Modal build from a known repository root, or pass an explicit
  `uv_project_dir`, because Modal resolves that path relative to the caller's current
  working directory.
- `uv_sync` is a dependency-layer operation. It is not, by itself, proof that the local
  project code or package data is in the resulting Image.

## Library distribution, caller CWD, and the Image build context

### Documented guarantee

Modal documents `uv_project_dir` as a local directory resolved relative to the current
working directory where the Modal command is called. `Image.uv_sync` adds the
`pyproject.toml` and `uv.lock` from that directory to the remote Image build context;
neither the `Image` reference nor the `uv_sync` docs describe searching the installed
Modal SDK, the caller's site-packages, or an import distribution for those files. See
the [Modal `uv_sync` reference](https://modal.com/docs/sdk/py/latest/Image#uv_sync).

uv's project docs likewise define a project around a `pyproject.toml` and its adjacent
`uv.lock`. A build system determines whether the current project is packaged and which
files are included in a distribution. The [uv project-configuration docs](https://docs.astral.sh/uv/concepts/projects/config/)
say that build systems control inclusion/exclusion of distribution files, and the
[uv distribution docs](https://docs.astral.sh/uv/concepts/projects/build/) say that
`uv build` delegates those details to the configured build backend. A wheel or sdist
therefore is not documented by uv as necessarily containing the project lockfile or
Modal Image recipe.

### Inference / policy

- Treat `Image.uv_sync(".")` as a **repository/build-context API**, not a package-runtime
  API. If the Modal SDK is imported from a wheel and the caller's CWD is an application
  directory, `"."` resolves to that application directory; it should not be expected to
  discover this library's own `pyproject.toml` or `uv.lock`.
- Keep an explicit Image-runtime context under source control (for example,
  `image-runtime/`) containing its own `pyproject.toml`, `uv.lock`, and any scripts or
  assets needed in the container. CI should invoke the build from that context (or pass
  its explicit path), rather than relying on whatever CWD happened to launch the SDK.
  This is a release-policy recommendation derived from the path semantics, not a Modal
  guarantee about project layout.
- For a source checkout, the simplest release recipe is: point `uv_sync` at the known
  runtime context, then add the checkout's source and required data with
  `copy=True`. Modal explicitly says that `uv_sync` does not install the current project,
  so source inclusion must remain a separate Image step.
- For a wheel-installed consumer that must build an Image, prefer a dedicated runtime
  project whose dependencies include the released package (and whose lockfile records
  that release), then run `uv_sync` against that extracted/installed context. Because
  Modal's method passes `--no-install-project`, the runtime project itself is not the
  package to rely on; its dependencies must describe what the container needs. If the
  package is private or unavailable from its index, the build must add/install a local
  wheel through an explicit Image step. Modal and uv do not document automatic discovery
  of an installed wheel by `add_local_python_source` or by `uv_sync`.
- If the runtime context is shipped as package data, verify the wheel and sdist contents
  in CI. uv explicitly places file-inclusion decisions with the build backend; shipping
  `pyproject.toml`/`uv.lock` is therefore an artifact configuration decision, not an
  implicit consequence of using uv. A separately versioned runtime-context archive is
  another valid policy when package-data rules are too fragile.

### Inline developer Image versus published release Image

This distinction is an implementation policy, not a separate Modal object type:

| Use case | Build context and source | Identity/lifecycle | Recommended Modal shape |
| --- | --- | --- | --- |
| Inline developer Image | Local checkout; CWD and `uv_project_dir` are explicit. Startup-mounted source (`copy=False`) is useful for iteration. | May build lazily when a Sandbox is created; no durable release tag required. | Method-chained recipe, `uv_sync(..., frozen=True)` for local consistency, then `Sandbox.create`. |
| Published named release Image | CI checkout or dedicated runtime context; lockfile and source/assets are baked (`copy=True`). | Eager build, canary, captured `object_id`, and versioned named tag; consumers resolve the published assignment. | `Image.build(app).publish("name:version")`; runtime uses `Image.from_name` or a manifest ID via `Image.from_id`. |

The [Modal Sandbox guide](https://modal.com/docs/guide/sandboxes#separating-image-builds-from-sandbox-creation)
and [Named Images guide](https://modal.com/docs/guide/named-images) document the second
workflow. The first row is a convenience policy for development; do not infer from it
that an inline recipe is a reproducible release artifact.

## Frozen lockfiles and uv version pinning

### Documented guarantee

uv describes `uv.lock` as a universal (cross-platform) lockfile containing the exact
resolved versions for the project's possible Python/platform markers. uv recommends
checking it into version control for consistent installations and says it is managed by
uv rather than edited manually. See the [project layout lockfile section](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile)
and [universal resolution](https://docs.astral.sh/uv/concepts/resolution/#universal-resolution).

`uv --locked`/`uv lock --check` validates that the lockfile still matches project
metadata. `uv --frozen` skips that freshness check and uses the existing lockfile. uv
does not treat the release of a newer package as lockfile staleness; an explicit upgrade
operation is required. This distinction is important: `--locked` is a CI consistency
check, while `--frozen` is a build-time no-rewrite policy.

The [uv lockfile-versioning rules](https://docs.astral.sh/uv/concepts/resolution/#lockfile-versioning)
state that a uv version rejects a lockfile with a greater schema version. The schema is
only bumped in minor uv releases; all patch releases within a minor release are promised
full lockfile compatibility. uv's [versioning policy](https://docs.astral.sh/uv/reference/policies/versioning/)
also treats minor releases as the boundary for breaking changes.

uv supports `[tool.uv].required-version`. The [settings reference](https://docs.astral.sh/uv/reference/settings/#required-version)
says uv exits with an error when the running binary does not satisfy that PEP 440
specifier. The [GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/#installation)
calls pinning a specific uv version best practice and shows `astral-sh/setup-uv` with a
`version:` input. The standalone installer can likewise request a specific version in its
URL, as documented in [uv installation](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer).

Modal separately exposes `uv_version` on `Image.uv_sync` and documents that it pins the
binary copied from the official uv GHCR image.

### Inference / policy

- Pin uv in three places to prevent resolver/tool drift: the project's
  `[tool.uv].required-version`, the CI installation, and Modal's `uv_version` argument.
  Keep the values identical unless a deliberate migration is being tested.
- Treat a uv minor-version bump as a lockfile migration. Regenerate/validate `uv.lock`
  with the new binary, rebuild a canary Image, and record the binary version with the
  release metadata. A patch bump is schema-compatible, but still deserves normal CI
  verification because package-install behavior can change.
- Do not infer that `frozen=True` validates lock freshness. The explicit CI
  `--locked`/`uv lock --check` gate is what catches a changed `pyproject.toml`.
- A universal lockfile locks Python package choices across markers, not the Debian base,
  `apt` repository state, or arbitrary shell commands in a Modal Image. Those inputs need
  separate release controls.

## Local source: mount versus baked Image layer

### Documented guarantee

The [Modal `add_local_python_source` reference](https://modal.com/docs/sdk/py/latest/Image#add_local_python_source)
says that source modules are added under `/root`, which is on the Modal Functions'
`PYTHONPATH`. Its default `copy=False` adds files to containers at startup and does not
build them into the Image, which speeds deployment. `copy=True` copies the files into an
Image layer at build time; that can slow iteration because source changes invalidate the
layer and later steps. The same startup-mount versus Image-layer behavior is documented
for [`add_local_dir`](https://modal.com/docs/sdk/py/latest/Image#add_local_dir) and
[`add_local_file`](https://modal.com/docs/sdk/py/latest/Image#add_local_file).

`add_local_python_source` excludes dot-prefixed files/directories and `.pyc`/
`__pycache__` by default and, by default, includes only Python files. The `ignore`
argument can change that selection. For full directory control, Modal points to
`add_local_dir` with an explicit destination.

### Inference / policy

- For a release Image whose identity is meant to include application code, use
  `copy=True`. A startup mount is a separate input at Sandbox/container startup; it is
  not represented by the built Image artifact.
- Put `uv_sync` before the source-copy step. This preserves the documented dependency
  cache benefit and lets a release build run any source-dependent smoke test after the
  source is present.
- Audit package data, templates, browser assets, scripts, and other non-`.py` files. Add
  them explicitly with `add_local_dir(..., copy=True)` or adjust `ignore`; do not assume
  the Python-source helper captures the entire repository.
- Keep `copy=False` as an intentional development choice when rapid source iteration is
  more important than artifact completeness. It should not be silently treated as an
  immutable release guarantee.

## Named Images, tags, and exact object identity

### Documented guarantee

The [Named Images guide](https://modal.com/docs/guide/named-images) defines a workflow of
building an Image independently, publishing it under a name, and referencing that name
later. `Image.publish(name)` publishes an already-created Image; if no tag is supplied,
Modal uses `:latest`. `Image.from_name(name)` resolves a previously published name. Names
may include an explicit `{name}:{tag}` suffix.

Named references are mutable. Modal says a named reference is normally updated only after
a successful publish, so callers continue using the previous working Image while a new
build runs. The [Python SDK changelog](https://modal.com/docs/sdk/py/changelog) makes the
lookup failure behavior explicit: `Image.from_name` either succeeds or raises
`modal.exception.NotFoundError`; it never triggers a build. Publishing a new named Image
does not automatically update a deployed Function that references that name; the App
must be redeployed for that Function to adopt the new Image.

The [Image `build`/`from_id` reference](https://modal.com/docs/sdk/py/latest/Image#build)
documents eager `image.build(app)`. After build, the Image's `object_id` can be read and
passed to `Image.from_id(image_id)`, which returns a hydrated Image handle for that ID.
Modal does not describe `object_id` as an OCI digest or cryptographic content hash. At
runtime, Modal documents `MODAL_IMAGE_ID` as an environment variable present in every
Modal container; this is the ID of the Image used by that container (see [runtime
environment variables](https://modal.com/docs/guide/environment_variables#container-runtime-environment-variables)).

### Inference / policy

- Use a versioned tag (for example, a release number or full Git revision) for a
  write-once release convention. This makes the reference operationally stable, but it
  does not make the Modal tag intrinsically immutable; another authorized publisher can
  still update it.
- Capture `built_image.object_id` in a release manifest alongside the tag, source
  revision, lockfile hash, uv version, Python/base selection, and effective Image Builder
  Version. Use `Image.from_id` for provenance-sensitive tests or compare the container's
  `MODAL_IMAGE_ID` with the manifest value.
- Reserve `:latest` for development or an explicitly managed promotion pointer. Do not
  use it as the only production identity when rollback and artifact provenance matter.
- Treat an object ID as a Modal artifact identifier, not as a promise of byte-level
  reproducibility. If cryptographic provenance is required, maintain a project-owned
  signed manifest of source/lock/build inputs in addition to the Modal ID.

## Image Builder Version

### Documented guarantee

The [Modal Images guide](https://modal.com/docs/guide/images#image-builder-updates) says
that base definitions include details such as the Image OS, included Python, and Modal
client dependencies. Modal uses a separate Image Builder Version to update those base
definitions without automatically causing unpredictable rebuilds. The guide describes
the setting as workspace-level, says a new version is released every few months, and
warns that changing it causes Images to rebuild on the next deployment. Unpinned
third-party dependencies can resolve to newer, breaking versions after such a rebuild.

The [workspace CLI](https://modal.com/docs/cli/latest/workspace#modal-workspace-settings-set)
and [Python Workspace API](https://modal.com/docs/sdk/py/latest/Workspace#settings-set)
expose `image-builder-version` as a workspace setting, and the public
`WorkspaceSettings` type includes `image_builder_version`.

There is a current cross-SDK wording difference. The [JavaScript ModalClient reference](https://modal.com/docs/sdk/js/latest/ModalClient#getimagebuilderversion)
says the Image Builder Version is an **environment-scoped** server setting and that
`getImageBuilderVersion(environmentName)` should be called for the environment in which
the Image will be built. The Python guide/CLI continue to present the setting as
workspace-level. This note does not resolve the discrepancy; it records both first-party
surfaces.

### Inference / policy

- Treat the effective Image Builder Version as a release input scoped to the exact Modal
  environment used by the build. Query it when the SDK/API supports querying, or record
  the workspace setting and target environment together. Do not assume a build in `dev`
  and a build in `prod` share the same builder configuration.
- A builder-version change is a deliberate base/runtime migration: run a canary build,
  dependency smoke tests, and Sandbox readiness checks before promotion. Do not allow a
  Sandbox creation path to discover the change for the first time.
- Keep dependency pins/lockfiles and the builder-version value in the release manifest.
  The builder version is outside `uv.lock` and therefore cannot be reconstructed from the
  Python dependency lock alone.
- Record the Modal SDK version used by the build as well. The [Modal changelog](https://modal.com/docs/sdk/py/changelog)
  documents that Image Builder Version changes and SDK releases can alter Image behavior
  (for example, the `2025.06` builder and the introduction of uv Image methods).

## Base Images, Python, `apt`, and external registry inputs

### Documented guarantee

The [Image reference](https://modal.com/docs/sdk/py/latest/Image#debian_slim) describes
`Image.debian_slim(python_version=None)` as the standard Debian slim Python Image based
on the official `python` Docker Images. `python_version` accepts a Python series or full
version. The Images guide says that when no Python version is supplied, the default
container follows the local interpreter's minor `v3.x`; explicitly passing a version is
the way to select the remote Python series.

`Image.apt_install` installs Debian packages by package name and returns an Image with
`apt-get install` layers ([reference](https://modal.com/docs/sdk/py/latest/Image#apt_install)).
Modal's public API reference does not promise an APT snapshot, repository timestamp, or
version lock for an unqualified package name. It also does not claim that a future rebuild
of the same `apt_install("name")` recipe will produce the same package bytes.

For external registries, [Using existing Images](https://modal.com/docs/guide/existing-images)
requires the image to target `linux/amd64` and documents Python/`pip` requirements for
Modal Functions. `Image.from_registry(..., add_python="...")` can add a reproducible
standalone Python build when the external image lacks a compatible Python installation.
Modal's [Sandbox custom-Image guidance](https://modal.com/docs/guide/sandboxes#separating-image-builds-from-sandbox-creation)
says external registry tags are treated as immutable once pulled: `Image.build` returns
the cached version and does not detect upstream changes to a mutable tag such as
`:latest`. To pick up an upstream change, update the tag in the deploy script.

### Inference / policy

- Pin the Python series (or a tested full version) explicitly in the base constructor;
  do not let a developer's local minor version silently choose the production base.
- Consider unqualified `apt` package names a rebuild input, not a reproducibility lock.
  If a package update can affect behavior, capture the built Image ID and publish it;
  reproduce only through a deliberate rebuild/canary process. If the required package
  version cannot be controlled by the Modal API, use a reviewed external base or another
  project-owned mechanism that provides that control.
- Use an explicit, intentionally updated external tag rather than `:latest`; Modal's
  cache behavior means changing the tag is the documented way to request a new external
  Image. The Modal docs do not make a cryptographic digest contract for this API, so do
  not claim digest-level guarantees without separately verifying the registry input.
- Keep the target platform (`linux/amd64`) and the Python executable available on `PATH`
  in the release test; `uv_sync` cannot produce a useful runtime if the selected base does
  not provide a compatible Python environment.

## CI publication and Sandbox runtime selection

### Documented guarantee

Modal's [Sandbox guide](https://modal.com/docs/guide/sandboxes#separating-image-builds-from-sandbox-creation)
specifically recommends separating Image builds from Sandbox creation. It recommends
calling `Image.build` in a deployment flow, scheduled job, or CI pipeline, publishing the
result as a named Image, and using `Image.from_name` so Sandbox creation is guaranteed not
to block on rebuilding an invalidated Image.

The [Named Images guide](https://modal.com/docs/guide/named-images#publishing-an-image-from-a-script)
shows `image.build(app).publish("name")`, then `Sandbox.create(...,
image=Image.from_name("name"))`. Inline Image definitions can otherwise build lazily when
the Sandbox is created; the [Modal changelog](https://modal.com/docs/sdk/py/changelog)
describes eager `Image.build` as the way to force that work to complete before Sandbox
creation.

Modal's [continuous-deployment guide](https://modal.com/docs/guide/continuous-deployment)
shows GitHub Actions setting `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from repository
secrets and optionally selecting the target environment with `MODAL_ENVIRONMENT`. uv's
[GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/#syncing-and-running)
shows the lock-checked CI pattern `uv sync --locked` and recommends pinning the uv version
when installing `astral-sh/setup-uv`.

### Inference / policy

A release pipeline should have these stages:

1. **Checkout and tool gate.** Install the pinned uv version and Modal SDK. Run
   `uv lock --check` or `uv sync --locked`; fail if `uv.lock` is absent or stale. Keep
   `MODAL_TOKEN_SECRET` and other credentials in CI secret storage and out of logs.
2. **Build.** Run an independent Image-build script from the repository root. Its recipe
   should use explicit Python/base inputs, `uv_sync(frozen=True, uv_version=...)`, and
   baked local source/assets.
3. **Smoke test.** Use `image.build(app)` to get the resolved Image, start a canary
   Sandbox with that built Image or its ID, and verify the daemon health/readiness and the
   runtime's expected browser/display behavior.
4. **Record identity.** Read `built_image.object_id`; record it with the Git revision,
   lockfile hash, uv version, Modal SDK version, target environment, effective Image
   Builder Version, base/Python choice, and selected `apt` packages.
5. **Publish.** Publish only after the canary passes, using a versioned named tag. Treat
   an existing release tag as a conflict rather than overwriting it. A separate mutable
   promotion tag (if desired) should be explicit and auditable.
6. **Runtime selection.** Sandbox-launching code resolves the versioned tag with
   `Image.from_name` and passes that Image to `Sandbox.create`. For strict artifact checks,
   resolve the manifest's `object_id` with `Image.from_id` and/or compare
   `MODAL_IMAGE_ID` from the running container.

An illustrative Python shape (the version and names are project policy values) is:

```python
import modal

UV_VERSION = "<pinned-uv-version>"
RELEASE_TAG = "<write-once-release-tag>"

app = modal.App.lookup("image-builds", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="<tested-python-version>")
    .apt_install("<reviewed-package>")
    .uv_sync(
        uv_project_dir=".",
        frozen=True,
        uv_version=UV_VERSION,
        # extras=["..."], groups=["..."], when required by the runtime
    )
    .add_local_python_source("modal_computer_use", copy=True)
)

built = image.build(app)
image_id = built.object_id
built.publish(f"modal-computer-use:{RELEASE_TAG}")
# Persist image_id and the other release inputs in a manifest before promotion.
```

At runtime:

```python
app = modal.App.lookup("sandbox-app", create_if_missing=True)
image = modal.Image.from_name("modal-computer-use:<release-tag>")
sb = modal.Sandbox.create(app=app, image=image)
```

`Image.from_name` is intentionally used in the launch path because it is a lookup, not a
recipe that can trigger a rebuild. If the launch must prove one exact artifact rather than
one named assignment, use the manifest's `Image.from_id(image_id)` and keep the launch and
build in the intended Modal environment.

## Explicitly unresolved or non-guaranteed points

The primary documentation leaves these boundaries important:

- **Builder Version scope:** Python/CLI docs call it workspace-level, while the current
  JS client says environment-scoped. Record the target environment and effective value;
  do not silently assume they are interchangeable.
- **APT reproducibility:** `apt_install` documents package-name installation, not a
  repository snapshot or package-byte lock. A `uv.lock` cannot lock Debian packages.
- **Named-tag immutability:** versioned tags are a useful project convention, but Modal
  names are mutable references. Enforce write-once behavior in the publication workflow.
- **Object ID versus digest:** Modal documents an Image `object_id`, `from_id`, and the
  runtime `MODAL_IMAGE_ID`; it does not document an OCI/cryptographic digest equivalence.
- **Lock freshness versus frozen builds:** `--locked` checks metadata consistency;
  `--frozen` deliberately skips that check. Use both at the appropriate CI/build stages.
- **Source completeness:** `uv_sync` excludes project installation, and the default local
  Python-source helper excludes non-Python files. Source and data inclusion must be an
  explicit Image-layer decision.

## Release checklist

- [ ] `pyproject.toml` and `uv.lock` are present and checked in.
- [ ] CI uses the same exact uv version required by the project and passed as Modal
      `uv_version`.
- [ ] CI runs `uv lock --check` or `uv sync --locked` before remote Image build.
- [ ] Modal `uv_sync` uses `frozen=True`; required groups/extras are explicit.
- [ ] Python/base selection is explicit and tested on `linux/amd64`.
- [ ] Local source and required non-Python assets are baked with `copy=True` for release.
- [ ] Effective Image Builder Version and target environment are recorded.
- [ ] `Image.build` completes before publication; canary Sandbox checks pass.
- [ ] Built `object_id`, source revision, lock hash, tool versions, and package/base inputs
      are written to a release manifest.
- [ ] Named release tag is versioned and write-once by policy; `:latest` is not the only
      production identity.
- [ ] Sandbox launch resolves `Image.from_name` (or a manifest ID via `from_id`) and does
      not inline-build on the latency-sensitive path.
- [ ] Runtime checks can observe `MODAL_IMAGE_ID` when provenance verification is needed.
