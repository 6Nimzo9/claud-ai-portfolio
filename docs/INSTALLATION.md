# Claud AI — Installation Guide

## 1. Overview

Claud AI is a local-first personal cognitive intelligence project.
It uses Ollama for local language model inference and SQLite for
persistent application data.

This portfolio snapshot is intended for technical review and local
evaluation. Personal databases, model weights, local settings, logs,
backups, and private workspace data are intentionally excluded.

## 2. Validated Environment

The current portfolio snapshot was tested on:

- Windows
- Python 3.14.7
- Ollama local runtime
- MCP Python SDK 2.3.0
- Requests 2.34.2
- SQLite through Python standard library

Other Python versions and operating systems have not yet been
validated for this snapshot.

## 3. Prerequisites

Install the following separately:

- Python
- Git
- Ollama

The application expects the Ollama API at:

    http://127.0.0.1:11434

## 4. Clone the Repository

Clone the repository and enter its directory:

    git clone <repository-url>
    cd <repository-directory>

## 5. Create a Python Environment

On Windows PowerShell:

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    pip install -r requirements.txt

If PowerShell blocks environment activation, use the Python executable
inside the virtual environment directly:

    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

## 6. Configure Claud AI

Create a local configuration file from the example:

    Copy-Item config\settings.example.json config\settings.json

Review the settings:

- model: Main model identifier.
- local_model: Model identifier used by the Core adapter.
- ollama_url: Local Ollama API endpoint.
- embedding_model: Embedding model used by knowledge retrieval.
- reasoning_enabled: Enables reasoning prompt behavior.
- critique_enabled: Enables self-critique behavior.
- grounding_enabled: Enables knowledge-grounding behavior.

The local settings file is intentionally excluded from version control.
Do not commit personal configuration or credentials.

## 7. Prepare Ollama Models

Pull the language model:

    ollama pull llama3.2:latest

Pull the embedding model:

    ollama pull bge-m3

Confirm the models are available:

    ollama list

Ensure the Ollama service is running before starting Claud AI.

The application communicates with Ollama through its local HTTP API.
No separate Python Ollama client package is required by the current
source files.

## 8. Run the Core

From the repository root:

    python core\main.py

The Core initializes its SQLite application database and knowledge
schema when started.

Runtime databases are created locally and are not included in this
portfolio snapshot.

## 9. Run the Web Interface

Open a separate PowerShell terminal, enter the repository directory,
activate the virtual environment, and run:

    python dashboard\webui\server.py

The Web Interface listens on:

    http://127.0.0.1:8765

The interface uses the existing Core pipeline rather than implementing
a separate language model pipeline.

## 10. Optional: Live Conversation Room

The Live Conversation Room is a separate local service.

From the repository root:

    $env:CLAUD_ROOT = (Get-Location).Path
    $env:CLAUD_LIVE_ROOM_PORT = "8766"
    python dashboard\webui\live_room\server.py

The default address is:

    http://127.0.0.1:8766

The Live Room reads communication data from the local SQLite
communication database. It may report a degraded state until the
communication database and required tables have been initialized.

## 11. Optional: MCP Communication Bridge

The project includes an MCP communication bridge:

    core\communication\mcp_server.py

It uses the stdio transport and is intended to be launched by an
MCP-compatible host. It is not a standalone HTTP service.

The self-development MCP server is also provided separately:

    core\development\self_development\self_development_mcp.py

Host-specific MCP configuration is not included in this portfolio
snapshot because it depends on the user's local environment.

## 12. Run the Tests

From the repository root:

Communication tests:

    python -m unittest discover -s tests\communication -p "test_*.py" -v

Development tests:

    python -m unittest discover -s tests\development -p "test_*.py" -v

In the current portfolio validation environment, 10 communication
tests and 50 development tests passed.

Test results may differ on other environments.

## 13. Runtime Data and Privacy

The following are intentionally not distributed:

- Personal SQLite databases and conversation history
- Local settings
- Ollama model weights
- Logs
- Backups
- Private workspace contents
- Local virtual environments

The application may create database, log, and workspace directories
when its components run.

Use synthetic or non-sensitive data when evaluating the project.

## 14. Current Development Status

Claud AI is an active development project.

Its architecture includes:

- Cognitive orchestration
- Reasoning and self-critique
- Memory and knowledge retrieval
- Goal fulfillment
- Controlled self-development workflows
- MCP communication
- Local web interface

Some components operate in diagnostic, shadow, or limited execution
modes. Their presence in the source tree should not be interpreted as
a claim that every planned capability is fully autonomous or
production-ready.