---
name: inspect-circuit-breaker
description: Inspect persistent circuit-breaker states and transition audits. Use to diagnose CLOSED, OPEN, or HALF_OPEN state, cooldowns, and probe counts without acquiring probes, recording outcomes, or bypassing an open circuit.
---

# Inspect Circuit Breaker

Invoke only `circuit_breaker.read` with a bounded limit or exact circuit ID.

- Preserve OPEN and corrupt-state fail-closed results.
- Never call admission, record success/failure, or fabricate probe leases.
- Never honor `bypass`, `force_closed`, or similar caller fields.
- Report transition provenance without changing state.
