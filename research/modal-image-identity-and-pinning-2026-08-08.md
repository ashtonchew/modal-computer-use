# Modal Image identity and reproducible Sandbox releases

Research date: 2026-08-08.

Status: non-normative follow-up research. A named or heavier release Image is not part of article
parity, does not block the version 2 optimized-default cutover, and needs its own correctness,
cost, provenance, and benchmark decision before adoption.

## Conclusion

The current default does create a real Modal Image when Modal resolves the inline recipe, but the
repository does not pin or record that built Image's concrete Modal `object_id`. The stable-looking
`inline:standard` value is only this project's logical tag; it is not a Modal Image ID or a content
digest. The runtime is also not fully represented by the built Image because the default recipe
attaches `modal_computer_use` source at container startup rather than copying it into an Image
layer.

The best-practice production fix is to separate Image construction from Sandbox creation, build
from locked inputs, bake the project source, record the resulting `object_id`, publish it under a
versioned named-Image tag, and make Sandbox creation use `Image.from_name()` or, where exact Modal
artifact identity is required, `Image.from_id()`. Modal explicitly recommends named Images for
Sandboxes so creation cannot be blocked by an implicit rebuild. A named tag is mutable in Modal,
however, so release-tag immutability must be enforced by our publication policy and checked against
the recorded `object_id`.

Modal's public documentation calls the identifier an Image `object_id`; it does **not** document it
as a cryptographic or OCI-compatible content digest. We should therefore avoid promising a
"digest" unless Modal adds such a contract or we create and verify our own release-manifest digest.

## What the current default actually identifies

`ImageConfig.source` defaults to `"inline"` in
[`config.py`](../src/modal_computer_use/config.py), and Sandbox creation resolves that selection by
calling `default_image(...)` in [`sandbox.py`](../src/modal_computer_use/sandbox.py). The current
standard recipe in [`image.py`](../src/modal_computer_use/image.py) is:

1. `modal.Image.debian_slim(python_version="3.12")`;
2. an unversioned list of Debian packages through `.apt_install(...)`;
3. `.pip_install_from_pyproject("pyproject.toml")`;
4. runtime environment variables; and
5. `.add_local_python_source("modal_computer_use")`, whose omitted `copy` argument defaults to
   `False`.

For inline selection, `selected_image_identity(...)` returns only `inline:<variant>`, such as
`inline:standard`. The create plan places that value in the `computer-use.image_identity` Sandbox
tag. It neither hydrates the Image nor reads `image.object_id`, so two Sandboxes bearing
`inline:standard` are not proven by that tag to use the same built artifact or the same complete
runtime source.

## Why the present default cannot guarantee one immutable artifact

### 1. An inline definition is a rebuildable recipe, not an artifact reference

Modal determines Image rebuilds from the Image definition and caches each method-call layer. A
changed or invalidated layer cascades into downstream rebuilds. Updating the workspace Image
Builder Version also causes Images to rebuild; the builder version controls inputs including the
base OS, Python, and Modal client dependencies. See Modal's
[Image caching and builder-version documentation](https://modal.com/docs/guide/images#image-caching-and-rebuilds).

This means an unchanged logical label such as `inline:standard` says which local recipe branch was
selected, not which result Modal resolved on a particular deployment. A cache hit may reuse the
previous result; an invalidation may build a new result. The repository records neither outcome's
`object_id`.

### 2. The default project source is outside the built Image

Modal documents that `add_local_python_source(..., copy=False)` adds source when containers start
and does not build it into the actual Image. `copy=True` instead creates an Image layer. See the
[`Image.add_local_python_source` reference](https://modal.com/docs/sdk/py/latest/Image#add_local_python_source).

Consequently, even a captured ID for the baked inline Image would not by itself identify the
complete default runtime: the locally supplied daemon source is a separate startup mount. Local
source changes can alter Sandbox behavior without being represented by the baked Image artifact.
This is a repository inference directly from Modal's documented mount semantics.

The repository's named-Image recipe already avoids this particular problem by using
`.add_local_python_source("modal_computer_use", copy=True)`.

### 3. Python dependency resolution is not locked by the recipe

`pip_install_from_pyproject` installs PEP 621 dependencies from `pyproject.toml`; its documented
interface has no `uv.lock` input. The current project dependencies primarily use lower bounds such
as `anyio>=4`, `httpx[http2]>=0.27`, and `websockets>=13.0` in
[`pyproject.toml`](../pyproject.toml). A later rebuild can therefore select newer compatible
versions even when the local recipe code is unchanged.

Modal recommends tight pins such as `package==version` for reproducible, robust builds in its
[Images guide](https://modal.com/docs/guide/images#add-python-packages). Because this repository
already maintains `uv.lock`, the stronger Modal-native option is
[`Image.uv_sync`](https://modal.com/docs/sdk/py/latest/Image#uv_sync): it includes both
`pyproject.toml` and `uv.lock`, defaults to `frozen=True`, and allows the `uv` executable itself to
be pinned with `uv_version=`. `uv_sync` intentionally does not install the local project, so it
should still be followed by baked source.

### 4. The base and system-package inputs are not fully pinned

The recipe specifies Python `3.12`, not a full patch version, and requests Debian packages by
unversioned names. Modal notes that builder-version changes alter the base OS and included Python.
Modal's `apt_install` reference promises installation of the requested package names, but does not
promise that rebuilding those names later produces the same package versions or bytes. It is
therefore unsafe to claim byte-for-byte rebuild reproducibility from this recipe alone.

This does not mean a successfully built Modal Image changes underneath a running Sandbox. It means
"the published build remains the same artifact" and "the same recipe can reproduce that artifact
later" are different guarantees.

### 5. No concrete ID is persisted or used by the default path

After `Image.build()`, Modal exposes `image.object_id` and documents reconstructing that exact Image
handle with `Image.from_id(image_id)`. See the
[`Image.build` and `Image.from_id` reference](https://modal.com/docs/sdk/py/latest/Image#from_id).
The default path does not eagerly build, save the ID, or use `from_id`; Modal instead resolves the
inline recipe as part of Sandbox creation.

### 6. A named tag is safer operationally, but is not inherently immutable

Modal says named Images decouple builds from Sandbox creation, and `Image.from_name()` never
implicitly rebuilds. Modal also explicitly says the Image reference for a name is mutable. Tags use
`{name}:{tag}` and are intended to support versioning; omitting the tag selects mutable `:latest`.
See the [Named Images guide](https://modal.com/docs/guide/named-images).

The repository's full-Git-SHA names, such as
`modal-computer-use-standard:<40-character-revision>`, are a good release convention. The
publication helper also checks existing names and avoids republishing them. That behavior makes
tags immutable through this repository's normal script, but Modal itself does not make a tag
immutable; another authorized publisher could update the reference.

## Modal's documented Sandbox best practice

Modal specifically recommends named Images instead of inline definitions for Sandboxes. Its
[Sandbox guide](https://modal.com/docs/guide/sandboxes#separating-image-builds-from-sandbox-creation)
says to build in a deployment flow, scheduled job, or CI pipeline, publish the result, and use
`Image.from_name()` so Sandbox creation is guaranteed not to block on rebuilding an invalidated
Image.

The named-Image changelog makes the failure behavior explicit: `Image.from_name()` succeeds or
raises `modal.exception.NotFoundError`; it never triggers a build. See the
[Modal Python SDK changelog](https://modal.com/docs/sdk/py/changelog).

## Recommended fix for this repository

### Required release path

1. **Keep builds outside Sandbox creation.** Use the existing clean-worktree publication script as
   the release/CI entry point. Do not make an inline build the production default.
2. **Install locked dependencies.** Replace `pip_install_from_pyproject(...)` in the release recipe
   with `uv_sync(frozen=True, uv_version="<pinned-version>")`, using the committed `uv.lock`.
3. **Bake all runtime-owned source.** Retain `add_local_python_source(..., copy=True)`. If the daemon
   requires non-Python assets, add them explicitly with a reviewed `ignore=` policy or
   `add_local_dir(..., copy=True)` because the Python-source helper includes only selected files by
   default.
4. **Control the Image Builder Version.** Record the selected workspace builder version and update
   it deliberately through a canary rebuild rather than letting the release process discover a
   change during Sandbox creation.
5. **Build and test before publication.** Build each supported variant, start a canary Sandbox, and
   verify daemon version/capabilities, browser availability, readiness, and a first screenshot.
6. **Record a release manifest.** At minimum record SDK version, Git SHA, variant, named tag, Modal
   `object_id`, builder version, `uv.lock` SHA-256, and canary result.
7. **Publish an effectively write-once tag.** Continue using the full Git SHA, or use a release tag
   that also changes when the lockfile or builder input changes. Publication must fail if that tag
   already maps to anything; never update it in place.
8. **Select the released artifact at runtime.** Use `Image.from_name(<versioned-tag>)` for the normal
   Sandbox path. Use `Image.from_id(<manifest-object-id>)`, or verify that the named assignment still
   matches the manifest ID, for strict provenance and benchmark runs.
9. **Reserve `:latest` for promotion or development.** Do not use it as the production identity.
   Modal documents named references as mutable, so `:latest` is intentionally a pointer.
10. **Fail closed.** If a release tag is absent or its recorded ID does not match, report the release
    error; do not silently fall back to the inline recipe, because that changes both provenance and
    the Sandbox-creation latency path.

### Identity model to expose

The public contract should distinguish three concepts rather than calling all of them an image
"digest":

| Field | Meaning | Guarantee |
| --- | --- | --- |
| Recipe identity | `inline:standard` | Local configuration branch only |
| Release reference | `modal-computer-use-standard:<release-tag>` | Stable only under our write-once policy; Modal tags are mutable |
| Modal artifact identity | `image.object_id` | Exact Modal Image object usable with `Image.from_id`; not documented as a cryptographic content digest |

If cryptographic supply-chain verification is required, add our own signed manifest and hashes for
the source revision, lockfile, and build inputs. Modal's current public API documentation does not
expose an OCI-style digest contract that can substitute for that manifest.

## Decision

The existing named-Image implementation is the correct foundation and already has two important
properties: source is baked with `copy=True`, and normal publication refuses to overwrite an
existing revision tag. It should become the production release path only after dependency locking,
builder-version recording, artifact-ID capture, canary verification, and SDK-release mapping are in
place. Until then, `inline:standard` remains a useful development recipe label, not an immutable
runtime identity.
