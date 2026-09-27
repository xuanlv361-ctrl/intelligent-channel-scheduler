---
name: review-cost-configuration
description: Review bounded local cost, budget, and configuration evidence. Use to identify missing limits, inconsistent policy, or cost guard status without changing weights, budgets, credentials, routing, or execution settings.
---

# Review Cost Configuration

Invoke only `cost_configuration.read`.

- Report configured values, source, version, and missing data explicitly.
- Never reveal API keys, cookies, authorization headers, or session material.
- Do not apply recommendations or mutate configuration.
- Do not claim unlimited provider capacity from a local `null` limit.
