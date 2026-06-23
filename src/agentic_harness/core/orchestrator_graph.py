import operator
from typing import Annotated, Any, Callable

from langgraph.graph import StateGraph, START, END
from typing import TypedDict


class StepDefinition(TypedDict):
    id: str
    agent: str
    prompt: str


class StepResult(TypedDict):
    id: str
    agent: str
    output: str


class OrchestrationState(TypedDict):
    steps: list[StepDefinition]
    results: Annotated[list[StepResult], operator.add]
    accumulated: str
    current_step_index: int


AsyncNodeFunction = Callable[[OrchestrationState], OrchestrationState]


def compile_orchestration_graph(
    steps: list[StepDefinition],
    node_factory: Callable[[StepDefinition, int], AsyncNodeFunction],
) -> Any:
    if not steps:
        raise ValueError("orchestration requires at least one step")

    builder = StateGraph(OrchestrationState)
    previous_node: str | None = None

    for index, step in enumerate(steps):
        node_function = node_factory(step, index)
        builder.add_node(step["id"], node_function)

        if previous_node is None:
            builder.add_edge(START, step["id"])
        else:
            builder.add_edge(previous_node, step["id"])

        previous_node = step["id"]

    builder.add_edge(previous_node, END)
    return builder.compile()
