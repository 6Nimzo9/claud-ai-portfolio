"""Explicit registry of callable goal tools and a workspace-scoped read adapter."""

from pathlib import Path
from typing import Any, Callable

from .models import RiskTier, ToolDefinition


class ToolRegistry:
    def __init__(self):
        self._items: dict[str, tuple[ToolDefinition, Callable[..., Any]]] = {}

    def register(self, definition: ToolDefinition, function: Callable[..., Any]) -> None:
        if not definition.name.strip() or not callable(function):
            raise ValueError("A named tool and callable implementation are required")
        if not definition.description.strip() or not definition.output_description.strip():
            raise ValueError("Registered tools must declare their purpose and output")
        if not definition.permission.strip() or not isinstance(definition.input_schema, dict):
            raise ValueError("Registered tools must declare permission scope and an input schema")
        if definition.name in self._items:
            raise ValueError(f"Tool is already registered: {definition.name}")
        self._items[definition.name] = (definition, function)

    def get(self, name: str) -> tuple[ToolDefinition, Callable[..., Any]] | None:
        return self._items.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [item[0] for item in self._items.values()]

    def validate_inputs(self, name: str, arguments: dict[str, Any]) -> None:
        entry = self.get(name)
        if entry is None:
            raise KeyError(f"Tool is not registered: {name}")
        if not isinstance(arguments, dict):
            raise TypeError("Registered tool inputs must be an object")
        schema = entry[0].input_schema
        if not schema:
            return
        unexpected = set(arguments) - set(schema)
        if unexpected:
            raise ValueError("Unexpected tool input fields: " + ", ".join(sorted(unexpected)))
        for field, declaration in schema.items():
            if isinstance(declaration, dict):
                expected_type = str(declaration.get("type", "any")).lower()
                optional = not bool(declaration.get("required", True))
            else:
                text = str(declaration).lower()
                expected_type = text.split("(", 1)[0].strip().split()[0] if text.strip() else "any"
                optional = "optional" in text
            if field not in arguments:
                if not optional:
                    raise ValueError(f"Required tool input is missing: {field}")
                continue
            value = arguments[field]
            checks = {
                "string": lambda item: isinstance(item, str),
                "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
                "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
                "boolean": lambda item: isinstance(item, bool),
                "object": lambda item: isinstance(item, dict),
                "array": lambda item: isinstance(item, list),
            }
            if expected_type in checks and not checks[expected_type](value):
                raise TypeError(f"Tool input {field} must be {expected_type}")

    @classmethod
    def with_workspace_read(cls, workspace_root: str | Path) -> "ToolRegistry":
        root = Path(workspace_root).resolve()
        registry = cls()

        def read_text(path: str, max_chars: int = 100_000) -> dict[str, Any]:
            requested = Path(path)
            target = (root / requested).resolve() if not requested.is_absolute() else requested.resolve()
            if not target.is_relative_to(root):
                raise PermissionError("Read refused: path is outside the project workspace")
            if not target.is_file():
                raise FileNotFoundError("Requested workspace file does not exist")
            if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 100_000:
                raise ValueError("max_chars must be an integer from 1 to 100000")
            text = target.read_text(encoding="utf-8")
            return {"path": target.relative_to(root).as_posix(), "text": text[:max_chars], "truncated": len(text) > max_chars}

        registry.register(
            ToolDefinition(
                name="workspace.read_text",
                description="Read a UTF-8 text file inside the project workspace.",
                input_schema={"path": "string", "max_chars": "integer (optional; max 100000)"},
                output_description="Workspace relative path, text, and truncation flag.",
                risk_tier=RiskTier.READ_ONLY,
                permission=f"read files under {root}",
            ),
            read_text,
        )
        return registry
