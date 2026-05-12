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
