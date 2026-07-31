# Modal upstream audit for `modal-computer-use` spec v8

- **Cutoff:** 2026-07-30
- **Source policy:** first-party Modal documentation and SDK reference only for Modal claims.
- **Scope:** upstream facts that materially change, qualify, or sharpen the archived
  [`modal_computer_use_spec_v7.md`](../docs/archive/spec/modal_computer_use_spec_v7.md).

## Executive finding

The canonical v8 should be based on Modal Python SDK 1.5.x, not the Modal surface that
existed when v7 was prepared on 2026-05-14. The repository already declares
`modal~=1.5.2`, which admits patch releases through `<1.6`. At the start of this audit,
`uv.lock` resolved 1.5.2; the v8 branch updates it to 1.5.3. Modal 1.5.3 was released on
2026-07-23 and is the latest documented stable release as of the cutoff. Its
Sandbox-relevant change is an approximately 2.5x improvement for large writes through
`sandbox.filesystem.copy_from_local`, `write_bytes`, and `write_text`.
([Modal Python SDK changelog](https://modal.com/docs/sdk/py/changelog);
[`pyproject.toml`](../pyproject.toml);
[`uv.lock`](../uv.lock))

The largest v7-to-v8 upstream changes are:

1. Connect Tokens can now be explicitly scoped to a container port.
2. `Sandbox.create` now has stable readiness probes, creation-time tags, inbound CIDR
   controls, outbound domain controls, and a clearer `idle_timeout` contract.
3. Modal's newer `sandbox.filesystem` API supersedes the old `Sandbox.open`, `ls`,
   `mkdir`, `rm`, and `watch` methods.
4. Filesystem and directory snapshots now default to a 30-day TTL, and snapshot types
   have sharply different maturity and restore semantics.
5. Named Images decouple image publication from Sandbox creation, but names/tags are
   mutable references.
6. Volumes v2 make explicit in-Sandbox `sync <mountpoint>` possible, but remain Beta
   and are not recommended by Modal as the sole store for mission-critical data.
7. V2 Sandboxes and VM Sandboxes are relevant experiments, not safe canonical
   defaults for this project.

## Priority correction matrix

| Priority | v7-era statement or omission | Current first-party fact | v8 action |
|---|---|---|---|
| P0 | Modal SDK baseline is implicit and the spec predates Modal 1.5. | Latest stable is 1.5.3 (2026-07-23); the repo's compatible-release constraint admits it, while the lock was 1.5.2 when this audit began. | Date-stamp the audit, state the tested SDK/lock, update the lock for the merge-ready branch, and do not claim later SDK behavior without a pinning test. |
| P0 | Connect Token use is described generically. | `Sandbox.create_connect_token(user_metadata=None, port=8080)` can scope credentials to an explicit port; Connect Tokens support HTTP and WebSocket ingress. | Require `port=8080` at every daemon token creation site. Treat omission as relying on an upstream default, not as an intentional security boundary. |
| P0 | v7 speaks about snapshots generically. | Filesystem and directory snapshots are Images with a default 30-day TTL; memory snapshots are Alpha, expire after 7 days, close TCP connections, terminate the source Sandbox, and cannot be used with GPUs. | Split the spec into filesystem, directory, and memory snapshot contracts. Keep memory snapshots out of the stable core roadmap. |
| P0 | Volume v2 persistence is presented mainly as a supported sync mechanism. | `sync <mountpoint>` is correct for v2, but Volumes v2 remain Beta and Modal says they are not recommended for mission-critical data. | Preserve honest sync receipts, but state the durability/maturity caveat and require an external/durable copy for irreplaceable artifacts. |
| P1 | Readiness is mostly package-owned polling. | Modal supports `readiness_probe=Probe.with_tcp(...)` or `Probe.with_exec(...)` and `wait_until_ready()`. Modal's own warm-pool example uses an exec probe that checks the HTTP service before admission. | Preserve two readiness layers, but consider an exec probe of unauthenticated `/readyz` so Modal-level readiness means “desktop usable,” not only “TCP listener open.” |
| P1 | Network policy does not distinguish protocol behavior. | `outbound_domain_allowlist` is Beta and only allows TLS on port 443; non-TLS traffic also needs a CIDR allowlist. `inbound_cidr_allowlist` applies to tunnels and Connect Tokens. | Document protocol limits, wildcard semantics, and incompatibility with `block_network=True`. |
| P1 | Image building is part of Sandbox startup architecture. | Named Images (`Image.publish` / `Image.from_name`) never trigger an implicit build on lookup, but name/tag references are mutable. | Treat named Images as the preferred cold-path artifact and full Git SHA tags as a repository convention, not an immutability guarantee supplied by Modal. |
| P1 | Native Sandbox file access is described without API generation. | The Beta `sandbox.filesystem` API replaces deprecated `Sandbox.open`, `ls`, `mkdir`, `rm`, and `watch`. | Name the new API explicitly and keep the daemon artifact API as the untrusted-user boundary; Modal filesystem access is an orchestration privilege. |
| P2 | Warm pools are “example-only.” | Modal still publishes a Queue-backed warm Sandbox pool example with readiness checking and expiry, while this repository now ships richer pool orchestration. | Cite the official example only as an upstream primitive pattern; truth-up shipped repository behavior separately. |
| P2 | The project could adopt the fastest Sandbox backend without qualification. | V2 Sandboxes are Beta and optimized for high creation throughput / concurrency, but use experimental creation/list/name APIs, are omitted from `Sandbox.list()`, lack GPUs, and do not support `modal shell`. | Keep V2 behind an explicit experimental flag and separate registry/attach compatibility tests. |
| P2 | A full VM may appear to be a natural “more capable” desktop target. | VM Sandboxes are Beta, CPU-only, do not support `reload_volumes()` or memory snapshots, and are selected through `experimental_options={"vm_runtime": True}`. | Do not make VM runtime the desktop default; evaluate only for a workload that demonstrably needs a real kernel. |

## Detailed findings and recommended spec language

### 1. Modal SDK baseline and release discipline

Modal's current changelog identifies 1.5.3 (2026-07-23) as latest. Modal 1.5.0 is a
minor release with breaking changes, and Modal's 1.0 release policy recommends pinning
to a minor series (`modal~=1.Y.Z`) when outstanding deprecations may matter. The
repository's `modal~=1.5.2` constraint follows that policy.
([Modal changelog: 1.5.3 and 1.5.x](https://modal.com/docs/sdk/py/changelog))

Recommended v8 contract:

- “The tested Modal SDK line is 1.5.x; the release lock is part of the spec evidence.”
- Distinguish “supported by current Modal” from “implemented and pinned by this
  repository.”
- Record the exact lock version in the v8 implementation truth table.
- Treat 1.6 as a deliberate compatibility event, not an automatic dependency update.

### 2. Connect Tokens: explicitly scope every credential to port 8080

The stable signature is
`Sandbox.create_connect_token(user_metadata=None, port=8080)`. Modal routes the token
to that port and forwards an unspoofable `X-Verified-User-Data` header containing the
JSON-serialized metadata. Metadata must be JSON-serializable, must serialize to fewer
than 512 characters, is encoded into the token, and must not contain secrets.
([Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#create_connect_token);
[Sandbox networking guide](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets))

Modal accepts the token in an `Authorization` header, `_modal_connect_token` query
parameter, or cookie. The package's stricter bearer-header-only policy is therefore a
project security decision, not a limitation of Modal. Query/cookie flows are useful
for browsers but create URL/history/cookie handling obligations; the v8 spec should
continue to prefer the Authorization header for SDK traffic and say why.
([Sandbox networking guide](https://modal.com/docs/guide/sandbox-networking#connecting-to-sandboxes-with-http-and-websockets))

Recommended v8 contract:

```python
creds = sandbox.create_connect_token(
    user_metadata={"sdk": "modal-computer-use", "version": __version__},
    port=8080,
)
```

- Add `port=8080` to every sync and async creation/reattach/borrow path.
- Keep the metadata bounded, non-secret, and schema-validated.
- Do not put Connect Tokens or token-bearing URLs in tags, traces, logs, queue entries,
  or durable session metadata.
- State that a noVNC token on port 6080 would be a separate credential if that design
  is adopted; a daemon token should not silently authorize a second port.

### 3. Ingress and egress controls are now richer and need protocol-accurate wording

`Sandbox.create` supports `block_network`, `outbound_cidr_allowlist`,
`outbound_domain_allowlist`, and `inbound_cidr_allowlist`. The old `cidr_allowlist`
name is deprecated in favor of `outbound_cidr_allowlist`.
([Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#create);
[Modal changelog 1.4.3](https://modal.com/docs/sdk/py/changelog))

Important semantics:

- By default, outbound access to public IPs is allowed.
- `block_network=True` drops outbound traffic and cannot be combined with outbound or
  inbound allowlists.
- Domain allowlists are Beta and permit TLS on port 443 only. A bare domain matches
  itself, while `*.example.com` matches the parent and subdomains. Non-TLS traffic is
  blocked unless a CIDR allowlist independently permits it.
- CIDR and domain allowlists combine additively.
- `inbound_cidr_allowlist` restricts connections through both Tunnels and Connect
  Tokens.
([Sandbox networking guide](https://modal.com/docs/guide/sandbox-networking))

Modal also exposes an Alpha runtime policy replacement method. The new policy takes
effect immediately and terminates established connections that it no longer permits,
but each policy dimension must have been enabled at creation time. This is useful for
an install-then-lock-down agent lifecycle, but it is not a stable core dependency.
([runtime network-policy section](https://modal.com/docs/guide/sandbox-networking#updating-the-network-policy-at-runtime))

Recommended v8 contract:

- Ship static creation-time policy as the stable surface.
- Mark runtime policy narrowing experimental and test connection teardown/recovery
  before exposing it publicly.
- Define provider/browser egress profiles in docs, but do not pretend a domain list
  covers HTTP, raw TCP, UDP, certificate pinning, or application-level prompt
  injection.

### 4. Modal readiness and daemon readiness should be deliberately layered

Modal readiness probes support TCP and exec checks; `Sandbox.wait_until_ready()` only
works when a readiness probe was supplied. Modal's warm Sandbox pool example uses
`Probe.with_exec("curl", "-sf", "http://localhost:PORT/")`, waits for readiness, then
enqueues the Sandbox.
([Sandbox guide: readiness probes](https://modal.com/docs/guide/sandboxes#readiness-probes);
[official warm-pool example](https://modal.com/docs/examples/sandbox_pool);
[Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#wait_until_ready))

The repository currently uses `Probe.with_tcp(8080)` and then separately calls its
daemon `/readyz`. That is safe and correctly distinguishes listener readiness from
desktop usability. For v8, either:

- preserve this explicit two-stage contract; or
- switch Modal's probe to an exec probe of `http://127.0.0.1:8080/readyz`, while
  retaining the SDK check as defense in depth.

Do not describe a TCP probe as proof that X11, the window manager, screenshot capture,
or browser startup is usable.

### 5. `idle_timeout` is not the package's idle budget

Modal considers a Sandbox active when it has an active `sb.exec(...)` command, stdin is
being written, or a Tunnel has an open TCP connection. `timeout` remains the maximum
Sandbox lifetime. These are Modal lifecycle controls, separate from the daemon's
action/idle budget.
([Sandbox guide: idle timeouts](https://modal.com/docs/guide/sandboxes#idle-timeouts);
[Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#create))

Recommended v8 wording:

- “Modal `timeout` is an infrastructure lifetime ceiling.”
- “Modal `idle_timeout` uses Modal-observed activity; the daemon idle budget uses
  application-observed actions.”
- Do not claim that a short HTTP request, Connect Token issuance, or package heartbeat
  resets Modal idle time unless a pinning integration test proves it.
- The current warm-pool restriction against explicit `idle_timeout` is conservative
  and justified because Modal does not expose remaining idle lifetime.

### 6. Use the new Sandbox filesystem API; keep it privileged

Modal's Beta `sandbox.filesystem` API is the current file-transfer surface. It provides
`copy_from_local`, `copy_to_local`, `write_text`, `write_bytes`, `read_text`,
`read_bytes`, `list_files`, `stat`, `make_directory`, `remove`, and `watch`. Modal says
it improves reliability over the pre-1.4 API. The old `Sandbox.open`, `ls`, `mkdir`,
`rm`, and `watch` are deprecated. `stat` reports a symlink itself rather than following
its target. Reads support files up to 5 GB; writes have no documented size limit.
([Filesystem Access guide](https://modal.com/docs/guide/sandbox-files);
[Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#filesystem))

Security/architecture implication:

- The Modal filesystem API is a control-plane capability with broad Sandbox file
  access. It must not replace the daemon's path-normalized artifact API for untrusted
  callers.
- It is appropriate for SDK-owned bootstrap, recovery, snapshots, and bounded artifact
  transfer after authorization.
- v8 should explicitly say which file operations are daemon-native and which are
  privileged Modal orchestration operations.

### 7. Snapshot types must not be conflated

Modal currently documents three distinct snapshot types:

| Type | Maturity / retention | What is restored | Important limits |
|---|---|---|---|
| Filesystem snapshot | Stable API; default 30-day TTL | An Image usable as a new Sandbox root filesystem | Filesystem only, not live processes |
| Directory snapshot | Beta; default 30-day TTL | An Image mountable with `mount_image`, and now also usable as a root image | Directory-scoped; handle TTL expiry |
| Memory snapshot | Alpha; 7-day retention | Memory plus filesystem through experimental APIs | Snapshot terminates source; TCP closes; no GPU; no active `Sandbox.exec`; restored instance type must match |

([Sandbox snapshots guide](https://modal.com/docs/guide/sandbox-snapshots);
[Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#snapshot_filesystem))

In Modal 1.5, both filesystem and directory snapshot methods take `ttl=` and default to
30 days; `snapshot_directory` also gained a 55-second default timeout. Pass `ttl=None`
for indefinite retention. This was a breaking default for filesystem snapshots, which
previously persisted indefinitely.
([Modal 1.5.0 changelog](https://modal.com/docs/sdk/py/changelog);
[snapshot retention](https://modal.com/docs/guide/sandbox-snapshots#snapshot-retention))

Recommended v8 contract:

- Preserve explicit `timeout` and `ttl` arguments in the package wrappers.
- State that filesystem/directory snapshots do not preserve GUI process state.
- Treat `NotFoundError` on restore as a normal expired-retention failure mode.
- Store snapshot IDs outside the Sandbox if they are needed for recovery.
- Keep memory snapshot APIs experimental and outside the release criteria.
- Modal says there is no API to list all created snapshots; track Image IDs explicitly.

### 8. Named Images are the right cold path, with a mutability warning

Modal 1.5 introduced `Image.publish(name[:tag])` and `Image.from_name(name[:tag])`.
Lookup never triggers an implicit build, which makes named Images well suited to a
latency-sensitive Sandbox creation path. If no tag is supplied, `:latest` is assumed.
The reference behind a name/tag is mutable.
([Named Images guide](https://modal.com/docs/guide/named-images);
[Image SDK reference](https://modal.com/docs/sdk/py/latest/Image#from_name))

Recommended v8 contract:

- Publish images in a separate build/promotion workflow.
- Use a full source commit SHA as the tag in release examples.
- Say explicitly that “write-once SHA tag” is repository policy; Modal does not make
  the named reference immutable.
- Keep inline image construction as a documented rollback/development path.
- Record the resolved image identity in non-secret operational metadata.

### 9. Volume v2 sync is correct but still Beta

For Volumes v2, running `sync /path/to/mountpoint` inside a Sandbox explicitly persists
pending filesystem and metadata changes. Background commits still occur every few
seconds and a final commit occurs on Sandbox shutdown. Other already-running containers
must reload before observing committed changes; `Sandbox.reload_volumes(timeout=55)`
now blocks until reload completes or raises `modal.exception.TimeoutError`, although
the reload may still finish in the background.
([Filesystem Access guide](https://modal.com/docs/guide/sandbox-files#committing-volume-changes-with-sync-v2-only);
[Volumes guide](https://modal.com/docs/guide/volumes);
[Sandbox SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox#reload_volumes))

Volumes v2 remain Beta. Modal says they may still lose data and does not recommend them
for mission-critical data. Concurrent writes to distinct files scale, but a particular
file still has last-write-wins risks. `Volume.with_mount_options` can enforce
`read_only=True` and/or a per-session `sub_path`, which is useful for tenant isolation.
([Volumes v2 overview](https://modal.com/docs/guide/volumes#volumes-v2-overview);
[Volume mount options](https://modal.com/docs/guide/volumes#mount-options))

Recommended v8 contract:

- Preserve the current “verified mount + checked `sync` exit status” rule.
- Define sync success as “Modal accepted the v2 mountpoint commit,” not as a backup or
  end-to-end durability guarantee.
- Prefer one writer per artifact/manifest path.
- Recommend per-run `sub_path` mounts where multiple users share a Volume.
- Do not use read-only mounts for sessions expected to write artifacts.
- Keep `modal.NetworkFileSystem` out of core: Modal marks it deprecated and says it
  will be removed.
  ([NetworkFileSystem deprecation](https://modal.com/docs/guide/network-file-systems))

### 10. Tags, names, attach, and detach

Sandbox tags can be assigned at creation and used as a conjunctive filter in
`Sandbox.list(tags=...)`; `set_tags` replaces the entire tag set rather than updating
it. Named Sandboxes are unique within an App while running, and duplicate creation
raises `AlreadyExistsError`. A name is reusable after the Sandbox stops. Modal
recommends `detach()` after interaction; after detach, operations on that handle are
not guaranteed to work, so reattach through `Sandbox.from_id`.
([Sandbox tagging guide](https://modal.com/docs/guide/sandboxes#tagging);
[named Sandboxes](https://modal.com/docs/guide/sandboxes#named-sandboxes);
[detach lifecycle](https://modal.com/docs/guide/sandboxes#detaching-from-sandboxes))

Recommended v8 contract:

- Keep attach-by-ID as the least ambiguous recovery path.
- Treat tag snapshots as eventually observed orchestration metadata, not a lock.
- Any compare-and-set lifecycle transition still needs the repository's own lock and
  post-write verification.
- If `set_tags` is used, always write the complete intended set.

### 11. V2 Sandboxes: valuable experiment, not the canonical backend yet

Modal's V2 Sandbox backend is Beta and targets higher creation throughput, lower
time-to-interactive, and greater concurrency; Modal specifically recommends evaluating
it above 20 creates/second or 10,000 concurrent Sandboxes. It uses
`Sandbox._experimental_create`.
([V2 Sandboxes guide](https://modal.com/docs/guide/sandbox-v2))

As of the cutoff, V2 supports Connect Tokens, tunnels, filesystem APIs, filesystem and
directory snapshots, Volumes, volume reload, readiness probes, tags, names, and i6pn.
However:

- GPUs are not supported.
- `modal shell` is not supported.
- V2 Sandboxes are not returned by stable `Sandbox.list()`; they use
  `_experimental_list`.
- Name lookup/set APIs are experimental.
- The documentation is explicitly under active development.
([V2 feature matrix](https://modal.com/docs/guide/sandbox-v2#feature-support))

Implication: do not silently switch the default. An opt-in needs separate tests for
creation, Connect ingress, tags, attach/recovery, pool reconciliation, snapshots,
filesystem access, and termination. The repository's current stable `Sandbox.list`
recovery path is not backend-neutral.

### 12. VM Sandboxes: no default benefit for the current desktop stack

VM Sandboxes are Beta and provide a real Linux kernel rather than gVisor. They are
useful for Docker, systemd, eBPF, cgroups, and workloads requiring kernel behavior.
They are selected with `experimental_options={"vm_runtime": True}`.
([VM Sandboxes guide](https://modal.com/docs/guide/vm-sandboxes))

Current limits include no GPU support, no `Sandbox.reload_volumes()`, no memory
snapshots, and a 512 GiB root-image limit. Therefore the v8 spec should keep the
existing gVisor-backed Sandbox as canonical unless a measured desktop/browser failure
requires VM semantics.
([VM Sandbox limitations](https://modal.com/docs/guide/vm-sandboxes#limitations))

### 13. i6pn is a private, regional data-plane option

Modal's i6pn networking is workspace-private IPv6 container-to-container networking.
It is region-scoped: both peers must be in the same region. Public access still
requires a Tunnel. This makes it useful for a Modal Function runner colocated with a
target Sandbox, but not a replacement for Connect Tokens for an external SDK.
([Modal cluster networking guide](https://modal.com/docs/guide/private-networking))

Recommended v8 contract:

- Pin runner and target to the same explicit region before claiming direct i6pn
  connectivity.
- Authenticate the daemon at the application layer even on workspace-private i6pn.
- Keep Connect Token or tunnel ingress as the external-controller path.
- Treat region, ingress mode, and HTTP version as configuration identity for reuse and
  warm-pool claims.

### 14. Official computer-use and warm-pool examples are references, not security specs

Modal's first-party Anthropic computer-use example launches the provider's prebuilt
image, exposes Streamlit and noVNC through encrypted ports, polls public tunnel URLs,
and terminates after a fixed timeout. It proves Modal is a viable computer-use
substrate, but it is not a daemon auth, artifact isolation, trace redaction, or
multi-tenant policy reference.
([official Anthropic computer-use example](https://modal.com/docs/examples/anthropic_computer_use))

Modal's official warm-pool example stores Sandbox ID, URL, and expiry in a Modal Queue,
waits on a readiness probe, health-checks before use, and keeps the control App
separate from the Sandbox App.
([official Sandbox pool example](https://modal.com/docs/examples/sandbox_pool))

The v8 spec should cite these as upstream orchestration precedents while making clear
that the repository's stronger admission, tag verification, secret handling, and
lease semantics are project-owned.

## Concrete v8 checklist

Items checked below were completed by the v8 branch after this audit. Unchecked items remain
follow-up decisions rather than hidden requirements for the specification document.

- [x] Record Modal 1.5.3 as the audited latest stable release and refresh `uv.lock`.
- [x] Add explicit `port=8080` to every sync and async Connect Token call.
- [x] Keep bearer-header-only SDK auth and describe it as stricter than Modal's
      header/query/cookie transport options.
- [ ] Truth-up `Sandbox.create` fields: `tags`, `idle_timeout`, readiness probe,
      outbound CIDR, outbound domain, inbound CIDR, and deprecated `cidr_allowlist`.
- [ ] Decide whether the Modal probe stays TCP-only or checks daemon `/readyz`; do not
      conflate the two readiness levels.
- [x] Name the `sandbox.filesystem` API and list the old deprecated methods.
- [x] Split snapshot claims by filesystem, directory, and memory type; include TTL and
      maturity.
- [x] Preserve explicit snapshot `timeout`/`ttl`; document expiry handling.
- [x] Describe named Image references as mutable and commit-SHA immutability as
      repository policy.
- [x] Retain Volume v2 sync verification while adding the Beta/durability caveat and
      per-run `sub_path` recommendation.
- [x] Keep NetworkFileSystem prohibited.
- [x] Mark V2 and VM Sandbox backends experimental and out of canonical release
      criteria.
- [x] Keep i6pn private, region-pinned, and application-authenticated.
- [x] Separate Modal upstream guarantees from repository-owned security invariants in
      the implementation truth table.

## Primary sources reviewed

1. [Modal Python SDK changelog](https://modal.com/docs/sdk/py/changelog)
2. [Sandbox Python SDK reference](https://modal.com/docs/sdk/py/latest/Sandbox)
3. [Sandboxes guide](https://modal.com/docs/guide/sandboxes)
4. [Sandbox networking and security](https://modal.com/docs/guide/sandbox-networking)
5. [Sandbox filesystem access](https://modal.com/docs/guide/sandbox-files)
6. [Sandbox snapshots](https://modal.com/docs/guide/sandbox-snapshots)
7. [Named Images](https://modal.com/docs/guide/named-images)
8. [Image Python SDK reference](https://modal.com/docs/sdk/py/latest/Image)
9. [Volumes](https://modal.com/docs/guide/volumes)
10. [Network file systems (deprecated)](https://modal.com/docs/guide/network-file-systems)
11. [V2 Sandboxes](https://modal.com/docs/guide/sandbox-v2)
12. [VM Sandboxes](https://modal.com/docs/guide/vm-sandboxes)
13. [Cluster/private networking](https://modal.com/docs/guide/private-networking)
14. [Official Sandbox warm-pool example](https://modal.com/docs/examples/sandbox_pool)
15. [Official Anthropic computer-use example](https://modal.com/docs/examples/anthropic_computer_use)
