# Native X11 capture packaging review

**Date:** 2026-08-08
**Review target:** `/private/tmp/modal-computer-use-candidate-d-sdk`, branch `feat/native-screenshot-capture`
**Scope:** delivery and packaging of the PyO3/X11 shared-memory screenshot extension. This is a packaging review; it does not certify the capture algorithm or change product files.

## Decision

Keep the public `modal-computer-use` artifact a Hatchling-built universal Python package (one `py3-none-any` wheel plus one sdist). Bundle the native Cargo source and lockfile as package data, then compile that source once while each managed Modal Image is built. Copy the resulting `modal_computer_use._native_capture` shared object into the baked package layer and make the native backend a build-time smoke-tested capability of the image.

Use this same private Image composition helper from `default_image()` and every managed named-image recipe (standard, Firefox, and Chromium). Publish immutable, full-Git-revision named Images before switching the default selector. Inline Images use the helper too, with the expected first-build latency. A caller-supplied custom Image is outside the managed guarantee and must either call the helper explicitly or report native capture unavailable; it must not silently claim the default capability.

This is the smallest production-safe change under the current release contract. It leaves client installs platform-neutral and offline-capable, while ensuring that a daemon started from a managed Image has the extension before serving requests. A companion native wheel is a sound second-stage optimization, but introducing it now would require a second artifact channel and release ordering that the repository's one-wheel/one-sdist workflow does not have.

## What the repository does today

- `pyproject.toml` uses Hatchling and the release checker expects exactly one wheel and one `.tar.gz` in `dist/release`.
- `src/modal_computer_use/image.py` installs the project and adds Python source, but does not compile or install `_native_capture` for normal or named Images. The benchmark runner is the only current Cargo build path.
- `add_local_python_source()` is used with its default `copy=False` in the inline recipe and `copy=True` in the named recipe. Neither is a native build step.
- The crate lives at `native/native_capture`, with `Cargo.lock` tracked and a PyO3 `cdylib`; the benchmark manually builds it and copies `lib_native_capture.so` into the package.

The native selector must therefore remain lazy in core Python. Importing the client, SDK, or daemon modules on a laptop must not import the extension, require Modal credentials, or require a compiler/X11 libraries. The managed Linux image is the place where the capability is materialized.

## Constraints from primary packaging sources

The Python Packaging User Guide describes one sdist per release and explains that compiled extensions require wheels tagged for the Python ABI, operating system, and CPU architecture, while a pure-Python project can ship one universal wheel. Its wheel examples and `py3-none-any` definition are at [Package formats](https://packaging.python.org/en/latest/discussions/package-formats/), [The packaging flow](https://packaging.python.org/en/latest/flow/), and [Platform compatibility tags](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/).

Consequences for this repository:

1. Putting the `.so` in the root release wheel would turn the artifact into a platform/ABI matrix (at minimum Linux x86_64/Python 3.12, and potentially every supported interpreter), contradicting the current universal-client and exactly-one-wheel release checks.
2. A native `.so` must not be checked into the sdist or wheel as a guessed host binary. Wheel `RECORD` hashes and platform tags describe the binary that was actually built; a macOS developer build cannot represent the Linux Modal runtime.
3. A source-only sdist remains valid and reproducible if the native source and `Cargo.lock` are included, but normal client installation must not run a compiler implicitly.

## Maturin and PyO3 facts

[Maturin's mixed Python/Rust layout](https://www.maturin.rs/project_layout.html) supports a Python package containing a Rust extension and uses `python-source`/`module-name` to place the module. That is technically suitable for a root mixed wheel, but the resulting wheel is platform-tagged. [Maturin's distribution guide](https://www.maturin.rs/distribution.html) also requires a manylinux-compatible build strategy (or an equivalent controlled target) for portable Linux wheels; a local `cargo build` does not provide that guarantee. [Maturin's configuration reference](https://www.maturin.rs/config) documents locked Cargo builds and include/exclude controls.

[PyO3's building and distribution guide](https://pyo3.rs/main/building-and-distribution) states that extension modules are compiled separately for operating system, architecture, and Python version. Its default build targets the host interpreter ABI. The `abi3` option reduces the Python-version matrix but does not remove the operating-system/architecture tag and can restrict the API available to the extension. Therefore `abi3` would not restore a `py3-none-any` root wheel or make a Linux X11 module installable on a macOS client.

The candidate's explicit target is Linux `x86_64` with Python 3.12 in Modal. The Image build must use that interpreter and a pinned Linux toolchain, not the developer's host Python or host Rust.

## Modal Image facts

Modal documents that [`Image.add_local_python_source`](https://modal.com/docs/sdk/py/latest/Image#add_local_python_source) adds Python source, defaults to a startup mount (`copy=False`), and excludes non-Python files; `copy=True` bakes the source into an image layer and is required before later build steps can read it. For Cargo files and a shared object, use [`Image.add_local_dir`](https://modal.com/docs/guide/images) or an explicit package-data copy with `copy=True`, not a Python-source mount alone.

Modal's [image guide](https://modal.com/docs/guide/images) explains that image layers are cached and that changing an earlier layer invalidates later layers. Put the stable OS/toolchain/dependency layers before the source/Cargo build layer; keep the native build after those layers so source changes do not redownload apt/Python dependencies. [`Image.run_commands`](https://modal.com/docs/sdk/py/latest/Image#run_commands) is the build-time hook for this command sequence.

[Named Images](https://modal.com/docs/guide/named-images) are published references and are not implicitly rebuilt when selected with `Image.from_name`. [Separating image builds from Sandbox creation](https://modal.com/docs/guide/sandboxes#separating-image-builds-from-sandbox-creation) recommends this pattern for lower startup latency. A full-revision name therefore gives us an auditable, rollbackable native image; it does not make an unbuilt inline or custom Image native-capable automatically.

## Option comparison

| Option | Universal root install | Managed daemon guarantee | Release/operational cost | Decision |
| --- | --- | --- | --- | --- |
| Root Maturin mixed wheel | No; every wheel is platform/ABI tagged | Good only for published target matrix | Replaces one-wheel release with matrix; manylinux/cross-build work; client install gets native artifact | Reject for current contract |
| Companion native wheel | Yes; root stays universal | Good after exact native wheel is installed in Image | Requires a second immutable artifact feed, version/order policy, and target wheel build/publish | Good later, not smallest now |
| Cargo source in package, compile in Modal Image | Yes; source data is portable | Good for every managed Image after build smoke test | Adds one cached compiler/build layer and build provenance | **Recommend now** |
| Multi-stage Dockerfile / named Image only | Yes | Good for named images, but does not define inline/custom composition by itself | More Docker/context and image publishing machinery; useful to shrink runtime later | Follow-up optimization |
| Tracked prebuilt `.so` | No reliable universal install or provenance | Accidental host/ABI coupling | Stale binary, unreproducible rollback, repository bloat and security review burden | Reject |

## Exact implementation plan

### 1. Keep the root artifact universal

Keep Hatchling as the root build backend and keep `dist/release` to the existing one wheel plus one sdist. Do not add `maturin` to the root `build-system` and do not put a compiled `.so` under `src/modal_computer_use` in source control.

Move or mirror the native crate into a package-data directory, for example:

```
src/modal_computer_use/_native_capture_src/Cargo.toml
src/modal_computer_use/_native_capture_src/Cargo.lock
src/modal_computer_use/_native_capture_src/src/lib.rs
```

Use the same crate contents and PyO3 module name (`modal_computer_use._native_capture`). Add explicit Hatch include/force-include entries for those files in both the wheel and sdist targets (the files are source data, not Python modules). Do not include `target/`, `.so`, rustup state, or generated metadata. The build test below must inspect both archives, so a packaging-rule typo cannot silently drop the source.

Keeping the authoritative crate under `native/native_capture` is also possible, but then the build configuration must copy that directory into the package-data destination for both artifacts. A single authoritative path is preferable: it prevents the Image helper, sdist, and Cargo lockfile from drifting.

### 2. Add one managed Image helper

Add a private function in `src/modal_computer_use/image.py` (for example `_with_native_capture(image)`) and call it from `default_image()` and `_named_image_recipe()` after the stable Python/desktop dependencies are installed and before the final Python-source layer.

The helper should:

1. Install the runtime and build inputs explicitly. Runtime: `libxcb1` and `libxcb-shm0` (plus the existing X11 packages). Build: `build-essential`, `libxcb1-dev`, and `libxcb-shm0-dev`; pin the Debian base/Python image already selected by the project.
2. Copy the package-data Cargo directory into a fixed build path with `copy=True` (for example `/opt/modal-computer-use/native_capture`). Do not depend on a default Python-source mount for Cargo files.
3. Install or select a pinned Rust toolchain (the candidate benchmark uses Rust 1.91.0; keep that version or record an intentional replacement). Run `cargo build --locked --release --manifest-path /opt/.../Cargo.toml` with `PYO3_PYTHON` pointing at the Image's Python 3.12 interpreter and the target set to `x86_64-unknown-linux-gnu`.
4. Copy the produced `lib_native_capture.so` to the installed package as `_native_capture.so`, using the exact extension suffix expected by the Image interpreter. Fail the Image build if the file is absent, has the wrong architecture/ABI, or cannot be imported. Remove Cargo target/build caches from the final layer if the Modal builder does not already discard them.
5. Run a build-time smoke command that imports `modal_computer_use._native_capture`, reports a stable native capability marker, starts the same Xvfb/MIT-SHM prerequisites used by the daemon, captures one small frame, and checks PNG signature and dimensions. A failed smoke command must fail Image publication; never publish an image that silently falls back to `mss` while claiming native-by-default.

The helper must be shared by inline and named recipes. `copy=True` is required for the Cargo source because later build commands must read it; the `.so` produced by `run_commands` is baked into that build layer. Keep the final `.add_local_python_source("modal_computer_use")` layer for Python updates, but ensure the native helper runs after the wheel/package install and that the source overlay cannot hide the baked extension.

### 3. Managed profiles, custom Images, and selection

Apply the helper to standard, Firefox, and Chromium recipes. Build/publish all three named variants from one clean Git revision, and select by their full revision tag. Inline `default_image()` remains available for development and will pay the native compilation cost on its first build; subsequent Modal layer cache hits should reuse the toolchain/dependency layers.

`profile="custom"` is not a managed recipe today. Document and test an explicit composition function that custom-image owners can call, or expose a readiness/capability flag that says native capture is unavailable. Do not attempt to mutate an arbitrary caller image behind their back and do not advertise “every custom Image” as covered.

## Why a companion wheel is not the first change

A companion wheel is the cleanest long-term boundary if native capture becomes a separately released product. Its shape would be:

```
native/native_capture/
  Cargo.toml
  Cargo.lock
  pyproject.toml
  python/modal_computer_use_native_capture/__init__.py
  src/lib.rs
```

Build it with Maturin in a pinned Linux `x86_64`/Python 3.12 builder (`cargo build --locked` through `maturin build --release`, with an explicit manylinux compatibility target), publish the exact wheel to a private immutable artifact store, and install that wheel in the Image. Keep it out of `dist/release` until the release workflow grows a second artifact channel. The root package can lazy-load the companion module and retain a pure-Python fallback for client use.

This removes Cargo/rustup from the runtime Image and gives a smaller, faster, more cacheable install layer, but it introduces: (a) a second version and provenance record, (b) an upload/retention policy, (c) a release ordering or rollback rule when root and native versions differ, and (d) a supply-chain trust boundary. Those are worthwhile once a native artifact registry exists; they are larger than the requested one-workflow change.

## Provenance, cache, image size, and rollback

Record a machine-readable manifest next to each named-image publication containing:

- root Git SHA and package version;
- native source tree hash and `Cargo.lock` hash;
- Rust toolchain (`rustc --version`, `cargo --version`), Python version, target triple, and base-image digest;
- apt package versions relevant to XCB/XCB-Shm;
- root wheel and sdist SHA-256 values;
- native capability marker and smoke-test result;
- Modal Image name, immutable revision tag, and returned image/object identity.

Build stable apt/Python layers first, then Rust toolchain, then the native source/build layer, then the small final copy/smoke layer. Cargo registry downloads are network-dependent during Image construction; they are not allowed during daemon startup. A named Image built once amortizes that cost and lets Sandbox creation use the cached result. If the environment is intentionally offline, select a previously published native named Image; a fresh source build should fail clearly rather than silently reverting the default backend.

Publish a new full-revision name, canary it, and switch the default only after smoke and daemon readiness checks pass. Roll back by selecting the previous immutable revision; never retag a broken image. Keep the root wheel release independent so client rollback and image rollback can be performed separately.

## Required tests and release gates

1. **Root artifact:** run `uv build` in a clean checkout; assert exactly one wheel and one sdist in `dist/release`, wheel tag `py3-none-any`, no `.so`/`target`/rustup files, and Cargo source plus lockfile present in both archives. Install each archive with network disabled and import core/client modules without Modal credentials or X11 libraries.
2. **Recipe shape:** unit-test every managed profile (inline standard/browser/browser-gpu and named standard/Firefox/Chromium) to assert the native helper, `copy=True` data copy, pinned toolchain, target triple, and smoke command are present. Test that an unmanaged custom Image is either explicitly composed with the helper or reports native unavailable.
3. **Linux target Image:** on Modal's `x86_64` Python 3.12 Image, assert import, marker, `ldd`/architecture, Xvfb startup, MIT-SHM capture, PNG signature, and dimensions. Test missing XCB-Shm, wrong Python ABI, and wrong architecture as hard build failures.
4. **Daemon contract:** start a daemon from each published named Image and verify health/readiness/capabilities expose native capture before accepting screenshot requests; verify the selected backend is native by default and that fallback is explicit.
5. **Provenance/rollback:** verify the manifest fields above, named revisions are immutable, and selecting the previous revision restores its native marker and smoke result.

## Final recommendation

Implement package-data source plus one cached, pinned, build-time Modal Image helper now. Keep the root Hatchling wheel/sdist universal and offline-safe; build no native code on client installation or daemon startup. Use the helper in all managed inline/named profiles, publish revision-tagged named Images, and make custom Images opt-in/explicit. Revisit a companion Maturin wheel only after an immutable native artifact channel and two-artifact release/rollback policy are available. Reject a root mixed wheel, Docker-only indirection, and tracked prebuilt binaries for this release contract.
