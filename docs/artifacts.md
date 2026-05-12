# Artifacts

Artifacts are rooted under `/home/desktop/artifacts` by default.

Standard layout:

```text
manifest.ndjson
screenshots/
recordings/
logs/
downloads/
traces/actions.ndjson
```

Public artifact APIs only accept safe relative paths. The daemon appends manifest entries for writes.

Default artifact storage is ephemeral sandbox filesystem state. To persist artifacts, mount a
Modal Volume at `/home/desktop/artifacts` or your configured artifact root. For Modal Volume v2,
set `StorageConfig(persist_artifacts=True)` and call `computer.artifacts.sync()`; the daemon runs
`sync <artifacts_dir>` inside the sandbox and reports `ok=true` only after the mountpoint commit
succeeds. Local orchestration can then verify files with `Volume.read_file`, `Volume.iterdir`, or
the `modal volume` CLI.

Modal Volume v1 is not a supported immediate-sync target for this package. v1 mounts may rely on
Modal's background/final commit behavior, but `artifacts.sync()` is only a release-quality
visibility contract for v2. Already-running readers must reload their Volume view with
`Volume.reload()` or `Sandbox.reload_volumes()`. Avoid concurrent writers to the same file; Modal
Volume conflict resolution is last-writer-wins, so use run-scoped artifact prefixes in production.
