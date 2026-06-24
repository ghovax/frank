import operator
from typing import Annotated, Any, Callable

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from typing import NotRequired, TypedDict


class StepDefinition(TypedDict):
    id: str
    agent: str
    prompt: str
    depends_on: NotRequired[list[str]]


class StepResult(TypedDict):
    id: str
    agent: str
    output: str


class OrchestrationState(TypedDict):
    steps: list[StepDefinition]
    results: Annotated[list[StepResult], operator.add]


AsyncNodeFunction = Callable[[OrchestrationState], OrchestrationState]


def compile_orchestration_graph(
    steps: list[StepDefinition],
    node_factory: Callable[[StepDefinition, int], AsyncNodeFunction],
) -> Any:
    if not steps:
        raise ValueError("orchestration requires at least one step")

    step_ids = {step["id"] for step in steps}
    outgoing_from: set[str] = set()
    incoming_edges: dict[str, list[str]] = {}

    for step in steps:
        step_id = step["id"]

        if "depends_on" not in step:
            # Chain mode — depends on the previous step (if any)
            previous_index = steps.index(step) - 1
            if previous_index >= 0:
                predecessor_id = steps[previous_index]["id"]
                incoming_edges.setdefault(step_id, []).append(predecessor_id)
                outgoing_from.add(predecessor_id)
            else:
                # First step, no explicit depends_on → root
                pass
        elif step["depends_on"]:
            # Explicit fan-in — skip references to IDs outside this graph
            for dependency_id in step["depends_on"]:
                if dependency_id in step_ids:
                    incoming_edges.setdefault(step_id, []).append(dependency_id)
                    outgoing_from.add(dependency_id)
        # else: depends_on is empty list → root step

    builder = StateGraph(OrchestrationState)

    for index, step in enumerate(steps):
        node_function = node_factory(step, index)
        builder.add_node(step["id"], node_function)

    # Connect edges
    for step in steps:
        step_id = step["id"]
        dependencies = incoming_edges.get(step_id, [])

        if not dependencies:
            builder.add_edge(START, step_id)
        else:
            for dependency_id in dependencies:
                builder.add_edge(dependency_id, step_id)

    # Steps that no step depends on connect to END
    all_step_ids = {step["id"] for step in steps}
    leaf_nodes = all_step_ids - outgoing_from
    for leaf_id in leaf_nodes:
        builder.add_edge(leaf_id, END)

    return builder.compile(checkpointer=MemorySaver())
