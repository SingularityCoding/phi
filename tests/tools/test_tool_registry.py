from __future__ import annotations

from typing import Any, cast

import pytest

from phi.tools import (
    ApprovalClass,
    Injected,
    Tool,
    ToolRegistry,
    build_default_registry,
    tool,
)


def test_specs_expose_only_model_controlled_arguments() -> None:
    @tool(
        name="summarize",
        description="Summarize text.",
        approval_class=ApprovalClass.READ_ONLY,
    )
    def summarize(text: str, context: Injected[str], limit: int = 10) -> str:
        return f"{context}:{text[:limit]}"

    registry = ToolRegistry([summarize])

    specs = registry.specs()

    assert specs == [
        {
            "type": "function",
            "function": {
                "name": "summarize",
                "description": "Summarize text.",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {
                        "text": {"title": "Text", "type": "string"},
                        "limit": {"default": 10, "title": "Limit", "type": "integer"},
                    },
                    "required": ["text"],
                    "title": "SummarizeArguments",
                    "type": "object",
                },
            },
        }
    ]

    specs[0]["function"]["parameters"]["properties"].clear()
    assert registry.specs()[0]["function"]["parameters"]["properties"] == {
        "text": {"title": "Text", "type": "string"},
        "limit": {"default": 10, "title": "Limit", "type": "integer"},
    }


def test_registry_orders_specs_and_rejects_duplicate_names() -> None:
    @tool(name="zeta", description="Zeta.")
    def zeta() -> str:
        return "z"

    @tool(name="alpha", description="Alpha.")
    def alpha() -> str:
        return "a"

    registry = ToolRegistry([zeta, alpha])

    assert [spec["function"]["name"] for spec in registry.specs()] == ["alpha", "zeta"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(alpha)


def test_registry_accepts_a_remote_schema_without_a_local_argument_model() -> None:
    async def remote_handler(**arguments: object) -> dict[str, object]:
        return arguments

    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    remote = Tool(
        name="remote_search",
        description="Search a remote source.",
        handler=remote_handler,
        args_schema=schema,
        args_model=None,
        approval_class=ApprovalClass.UNCONFINED,
    )
    schema["properties"].clear()

    spec = ToolRegistry([remote]).specs()[0]

    assert spec["function"]["parameters"] == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    with pytest.raises(TypeError):
        cast(Any, remote.args_schema)["type"] = "array"


def test_default_registry_contains_all_seven_builtins() -> None:
    assert [spec["function"]["name"] for spec in build_default_registry().specs()] == [
        "bash",
        "edit",
        "find",
        "grep",
        "ls",
        "read",
        "write",
    ]
