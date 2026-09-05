# Implementation plans

Plans record design history and ongoing experiments. They do not define current behavior.
Use the [architecture](../docs/architecture.md), [API contract](../docs/api.md), and
[product specification](../docs/spec/product-spec.md) for the maintained implementation.

The numbered plans 001–005 and optimized-default research were superseded during the version 2
cutover. Their original contents remain in Git history. The primary SDK now uses a placed borrowed
trajectory and `computer.step()`; the [migration guide](../docs/migration-v2.md) records that contract.

Retained design and experiment records:

- [Raw screenshot design](raw-screenshot-default-design-research.md): binary response design research.
- [X11 shared-memory implementation](006-cut-over-x11-shared-memory-screenshots.md): completed
  implementation and rejected default promotion.
- [X11 shared-memory promotion](007-promote-x11-shared-memory-default.md): ongoing experiment plan;
  MSS remains the production default.
