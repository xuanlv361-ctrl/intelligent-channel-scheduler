# Intelligent LLM Gateway

An offline-first orchestration platform for routing language-model workloads across models and channels. The project combines capability-aware model selection, health-aware channel routing, multi-agent execution, recovery controls, evaluation, and observability in one auditable workflow.

> Portfolio note: this repository demonstrates engineering architecture and deterministic offline workflows. Included metrics and fixtures are synthetic or sanitised unless explicitly labelled. It does not claim production benchmark performance, and real API execution is disabled by default.

![Routing Quality Console overview](docs/screenshots/final_acceptance/overview-desktop.png)

## Why this project

LLM applications often need more than a single model call. They must select an appropriate model, choose a healthy delivery channel, recover safely from transient failures, explain routing decisions, and retain evidence for operational review. This project explores that full decision path as a testable software system.

## Key capabilities

- **Task and prompt analysis** — deterministic classification and complexity scoring before routing.
- **Capability- and cost-aware model selection** — configurable ranking across quality, cost, and latency preferences.
- **Channel routing** — candidate resolution, confidence-aware selection, health-aware routing, and deterministic tie-breaking.
- **Multi-agent orchestration** — planning and offline execution for research, coding, reasoning, writing, and review roles.
- **Tool-aware agents** — a registry and controlled mock executors for calculation, code analysis, file analysis, and offline search.
- **Reliability controls** — retry policies, ordered fallback, execution guards, and complete attempt traces.
- **Evaluation and reflection** — rule-based output evaluation, reflection, and bounded self-correction loops.
- **Observability** — React/FastAPI operational console plus a multilingual Streamlit dashboard (English, Chinese, and Japanese).
- **Safety by default** — no credentials in source control, no network dependency for simulations, and explicit separation between mock, planned UAT, and measured evidence.

## Architecture

```text
User request
    |
    v
Task classifier + prompt complexity analyser
    |
    v
Capability / cost-aware model router
    |
    v
Model-channel resolver + health monitor
    |
    v
Routing strategy + execution guard
    |
    v
Agent runtime / tool execution / fallback
    |
    v
Evaluation + reflection + decision logging
    |
    v
React console / Streamlit dashboard
```

The routing, simulation, and agent modules use deterministic configuration files so decisions can be reproduced and tested without external model APIs.

## Technology stack

| Area | Technologies |
| --- | --- |
| Backend | Python, FastAPI, Pydantic, Uvicorn |
| Frontend | React 18, TypeScript, Vite, TanStack Query, Recharts |
| Dashboard | Streamlit, Pandas |
| Testing | pytest, Vitest, Testing Library, Playwright |
| Storage | JSON/CSV fixtures and local SQLite runtime state |

## Run locally

### Prerequisites

- Python 3.11+
- Node.js 20+
- PowerShell 7 or Windows PowerShell

### Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

cd web
npm ci
npm run build
cd ..
```

### Start the local console

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1 -OpenBrowser
```

The local console is served at `http://127.0.0.1:5174`.

### Run the offline dashboard

```powershell
python -m pip install -r requirements-dashboard.txt
streamlit run dashboard/app.py
```

Use the language selector at the top of the home page to switch between English, Chinese, and Japanese.

## Tests

```powershell
python -m pytest tests -q

cd web
npm test
npm run build
```

The suite covers routing behaviour, safety boundaries, deterministic simulations, fallback traces, agent/tool execution, dashboard parsing, and UI components.

## Repository boundaries

This public portfolio edition intentionally excludes credentials, browser sessions, local databases, raw UAT evidence, and runtime logs. Networked model calls are not required for the demonstrated flows. Configuration values representing cost, latency, or capability are engineering assumptions for offline comparison, not claims about commercial providers.

## Skills demonstrated

- Backend and API design
- React and TypeScript application development
- Reliability engineering and observability
- Deterministic simulation and statistical testing
- AI routing and agent orchestration
- Secure configuration and evidence-aware workflows

