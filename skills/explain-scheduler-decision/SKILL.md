---
name: explain-scheduler-decision
description: Explain a persisted Scheduler decision and its local attribution chain. Use for a known Decision ID to inspect gates, evidence references, candidate ordering, or fallback attribution without rerunning or changing the Scheduler.
---

# Explain Scheduler Decision

Invoke `scheduler_attribution.read` with one validated Decision ID.

- Explain only persisted facts and referenced evidence.
- Never accept caller-supplied `actual_channel` as authoritative.
- Keep missing attribution explicit and do not reconstruct it from names.
- Never rerun routing, fallback, execution, or evidence correlation.
