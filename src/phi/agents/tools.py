from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from phi.agents.definition import AgentDefinition
from phi.agents.registry import AgentRuntime, DelegationContext
from phi.tools import ApprovalClass, Injected, Tool, tool

type NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type CheckTimeout = Annotated[
    float,
    Field(gt=0, le=30, allow_inf_nan=False),
]


def build_agent_tools(
    definitions: dict[str, AgentDefinition],
) -> tuple[Tool, ...]:
    """Build the Model-visible Delegation Tool set."""

    catalog = "\n".join(
        f"- `{definition.name}`: {definition.description}"
        for definition in sorted(definitions.values(), key=lambda item: item.name)
        if not definition.disable_model_invocation
    )
    catalog_suffix = f"\n\nAvailable Agent Definitions:\n{catalog}" if catalog else ""

    @tool(
        name="spawn_agent",
        description=(
            f"Start one isolated Subagent and return its Agent ID immediately.{catalog_suffix}"
        ),
        approval_class=ApprovalClass.UNCONFINED,
    )
    async def spawn_agent(
        task: NonEmptyText,
        runtime: Injected[AgentRuntime],
        context: Injected[DelegationContext],
        agent_type: NonEmptyText | None = None,
        model: NonEmptyText | None = None,
    ) -> object:
        return await runtime.spawn(context, task, agent_type, model)

    @tool(
        name="check_agent",
        description="Check one direct Subagent, optionally waiting for a bounded interval.",
        timeout_parameter="timeout_seconds",
    )
    async def check_agent(
        agent_id: NonEmptyText,
        runtime: Injected[AgentRuntime],
        context: Injected[DelegationContext],
        timeout_seconds: CheckTimeout | None = None,
    ) -> object:
        return await runtime.check(context, agent_id, timeout_seconds)

    @tool(
        name="steer_agent",
        description="Queue a message for a running direct Subagent's next Step boundary.",
    )
    async def steer_agent(
        agent_id: NonEmptyText,
        message: NonEmptyText,
        runtime: Injected[AgentRuntime],
        context: Injected[DelegationContext],
    ) -> object:
        return await runtime.steer(context, agent_id, message)

    @tool(name="list_agents", description="List direct Subagents in deterministic order.")
    async def list_agents(
        runtime: Injected[AgentRuntime],
        context: Injected[DelegationContext],
    ) -> object:
        return await runtime.list_agents(context)

    @tool(name="close_agent", description="Cancel and await a direct Subagent and its descendants.")
    async def close_agent(
        agent_id: NonEmptyText,
        runtime: Injected[AgentRuntime],
        context: Injected[DelegationContext],
    ) -> object:
        return await runtime.close_agent(context, agent_id)

    return spawn_agent, check_agent, steer_agent, list_agents, close_agent
