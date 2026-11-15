# Claud AI — Architecture Overview

## Design objective

Claud AI is designed as a modular personal cognitive intelligence system. Its intended role is to support context management, reasoning, knowledge retrieval, goal-oriented workflows, and controlled interaction with tools.

The architecture is being developed incrementally. The descriptions below identify the current repository structure and intended responsibilities; they are not a certification of end-to-end capability.

## Major components

### Cognitive orchestration
Location: `core/orchestrator/`

Contains components for intent analysis, complexity assessment, risk assessment, strategy selection, and cognitive orchestration.

### Context and memory
Location: `core/context/`

Contains adapters and context-management components for conversation, memory, and knowledge.

The active system uses local persistence. Personal database contents are deliberately excluded from this portfolio.

### Knowledge intelligence
Location: `core/knowledge/`

Contains grounding, semantic retrieval, hybrid retrieval, knowledge management, and source-intelligence components.

### Reasoning
Location: `core/reasoning/`

Contains reasoning and self-critique components.

### Goal fulfillment
Location: `core/goal_fulfillment/`

Contains goal-related models, execution logic, tools, repository components, and safety-related code.

### Development and evolution
Location: `core/development/`

Contains development planning, policies, orchestration, execution-state, repository, and experimental self-development components.

Self-development is treated as a controlled engineering capability. Its presence must not be interpreted as unrestricted autonomous modification.

### Communication and integrations
Location: `core/communication/`

Contains communication adapters, an MCP server, and mentor-related tools.

### Dashboard
Location: `dashboard/`

Contains web-interface source components.

## Execution model direction

The project follows a local-first direction using a local language model and local persistence, with a future hybrid path for capabilities that may require external services or infrastructure.

The complete deployment model, operational boundaries, and integration contracts remain under development.
