# Claud AI

**A personal cognitive intelligence project — local-first, hybrid-ready, and under active development.**

Claud AI is an experimental personal AI system intended to extend and amplify human thinking. The goal is to move beyond a conventional chatbot toward a system that can organize context, reason across information, learn through controlled development workflows, and use tools under explicit safety constraints.

This repository is a curated engineering portfolio snapshot. It is separate from the active development environment.

## Project status

Claud AI is an ongoing prototype. Its architecture and individual components are evolving. The presence of a module in this repository does not mean that the corresponding end-to-end capability is complete, production-ready, or autonomous.

## Architecture areas

| Area | Repository location | Scope |
|---|---|---|
| Cognitive orchestration | `core/orchestrator/` | Intent, complexity, risk, and strategy components |
| Context management | `core/context/` | Conversation, memory, and knowledge adapters |
| Knowledge intelligence | `core/knowledge/` | Grounding and semantic/hybrid retrieval components |
| Reasoning | `core/reasoning/` | Reasoning and self-critique components |
| Goal fulfillment | `core/goal_fulfillment/` | Goal execution models, tools, and safety components |
| Development engine | `core/development/` | Planning, policies, orchestration, and experimental self-development |
| Communication | `core/communication/` | MCP communication and mentor-related tools |
| User interface | `dashboard/` | Web interface components |
| Automated tests | `tests/` | Component-level test suite |

## Technology direction

The current development direction includes:

- Python-based application and orchestration components.
- Ollama with a local Llama 3.2 model for local language-model inference.
- SQLite-based local persistence.
- A hybrid architecture direction, with local-first operation as the initial priority.

The production readiness and compatibility of the complete stack have not yet been established.

## Five foundational pillars

1. Cognitive Intelligence.
2. Autonomous Learning & Evolution.
3. Computer Control Engine in Hybrid Mode.
4. Economic Intelligence with High Autonomy.
5. Multi-Mentor Intelligence.

These are long-term architectural pillars, not claims that all five capabilities are currently complete. See `docs/FIVE_PILLARS.md`.

## Security and privacy

This portfolio intentionally excludes personal databases, local model weights, virtual environments, runtime logs, backups, workspace data, and local settings.

No claim is made that a pattern-based security scan proves the absence of every possible secret. Review is required before every public release.

See `docs/SECURITY_MODEL.md`.

## Evaluation

The repository contains an automated test suite. Test results, reproducible benchmarks, and quantified performance outcomes must be established against this isolated portfolio snapshot before any reliability or performance claims are made.

See `docs/EVALUATION.md`.

## Development status

Claud AI is actively being developed. Some capabilities are experimental, some are foundational components, and some remain roadmap objectives.

This repository is intended to communicate the engineering direction and implementation honestly, including limitations and unfinished work.
