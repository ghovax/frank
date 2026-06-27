import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


mcp = FastMCP("mermaid")

TEMPLATE_PATH = Path(__file__).with_name("diagram.html")
DIAGRAMS: dict[str, dict[str, Any]] = {}
ARTIFACT_UPDATE_MODES = {"append", "replace", "update", "upsert"}
THEMES = {"default", "neutral", "dark", "forest", "base"}


def _normalize_definition(definition: str) -> str:
    text = (definition or "").strip()
    if not text:
        raise ValueError("A non-empty Mermaid definition is required.")
    return text


def _normalize_theme(theme: str) -> str:
    candidate = (theme or "default").strip().lower()
    return candidate if candidate in THEMES else "default"


def _json_for_template(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


def _render_html(diagram_data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text()
    payload = {
        "diagram_id": diagram_data["diagram_id"],
        "definition": diagram_data["definition"],
        "theme": diagram_data["theme"],
    }
    return template.replace("__DIAGRAM_DATA_JSON__", _json_for_template(payload))


def _summarize(diagram_data: dict[str, Any]) -> dict[str, Any]:
    definition = diagram_data["definition"]
    first_line = definition.split("\n", 1)[0].strip()
    return {
        "diagram_id": diagram_data["diagram_id"],
        "title": diagram_data["title"],
        "theme": diagram_data["theme"],
        "kind": first_line,
        "line_count": len(definition.splitlines()),
    }


def _artifact_mode(value: str, default: str) -> str:
    normalized = (value or default).strip().lower()
    if normalized == "new":
        return "append"
    return normalized if normalized in ARTIFACT_UPDATE_MODES else default


def _artifact(
    diagram_data: dict[str, Any],
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
) -> dict[str, Any]:
    summary = _summarize(diagram_data)
    target_id = artifact_target_id.strip()
    mode = _artifact_mode(artifact_update_mode, "append")
    return {
        "context": {
            **summary,
            "summary": f"{diagram_data['title']}: Mermaid {summary['kind']} diagram.",
        },
        "artifacts": [
            {
                "artifact_id": diagram_data["diagram_id"],
                "artifact_target_id": target_id or diagram_data["diagram_id"],
                "artifact_update_mode": mode,
                "type": "html",
                "title": diagram_data["title"],
                "html": _render_html(diagram_data),
                "height": diagram_data["height"],
                "summary": "Mermaid diagram.",
                "sandbox": "allow-scripts allow-popups",
            }
        ],
    }


async def _progress(context: Context | None, step: int, total: int, message: str) -> None:
    if context is None:
        return
    await context.report_progress(step, total, message)
    await asyncio.sleep(0.03)


@mcp.tool()
async def create_diagram(
    definition: str,
    title: str = "Mermaid diagram",
    theme: str = "default",
    height: int = 460,
    diagram_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Create a stateful Mermaid diagram and return its rendered artifact.

    Mermaid draws the diagram from a Markdown-inspired text definition; you only
    write the definition.

    Args:
        definition: The Mermaid source, e.g. "graph TD\\n  A[Start] --> B[End]".
            Supports flowchart, sequenceDiagram, classDiagram, stateDiagram,
            erDiagram, gantt, journey, pie, mindmap, gitGraph, and more.
        title: Title shown above the diagram artifact.
        theme: One of default, neutral, dark, forest, base.
        height: Rendered height in pixels (200-900).
        diagram_id: Optional stable identifier; generated when omitted.
        artifact_update_mode: append to render a new artifact, replace/update to
            refresh a target artifact, or upsert to replace when present.
        artifact_target_id: Existing artifact id to refresh; defaults to diagram_id.
    """
    await _progress(context, 1, 2, "Validating Mermaid definition")
    normalized_definition = _normalize_definition(definition)
    identifier = diagram_id.strip() or f"diagram-{uuid.uuid4().hex[:10]}"
    DIAGRAMS[identifier] = {
        "diagram_id": identifier,
        "title": title,
        "definition": normalized_definition,
        "theme": _normalize_theme(theme),
        "height": max(200, min(900, int(height))),
    }
    result = _artifact(DIAGRAMS[identifier], artifact_update_mode, artifact_target_id)
    await _progress(context, 2, 2, "Diagram ready")
    return result


@mcp.tool()
async def update_diagram(
    diagram_id: str,
    definition: str | None = None,
    title: str | None = None,
    theme: str | None = None,
    height: int | None = None,
    artifact_update_mode: str = "replace",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Update an existing diagram and return the refreshed artifact.

    Only the provided fields change.
    """
    if diagram_id not in DIAGRAMS:
        raise ValueError(f"Unknown diagram_id: {diagram_id}")
    await _progress(context, 1, 2, f"Loading {diagram_id}")
    diagram_data = dict(DIAGRAMS[diagram_id])
    if definition is not None:
        diagram_data["definition"] = _normalize_definition(definition)
    if title is not None:
        diagram_data["title"] = title
    if theme is not None:
        diagram_data["theme"] = _normalize_theme(theme)
    if height is not None:
        diagram_data["height"] = max(200, min(900, int(height)))
    DIAGRAMS[diagram_id] = diagram_data
    result = _artifact(diagram_data, artifact_update_mode, artifact_target_id or diagram_id)
    await _progress(context, 2, 2, "Updated diagram ready")
    return result


@mcp.tool()
def list_diagrams() -> dict[str, Any]:
    """List diagrams currently held by this stateful MCP server process."""
    return {
        "diagrams": [_summarize(diagram_data) for diagram_data in DIAGRAMS.values()],
        "count": len(DIAGRAMS),
    }


if __name__ == "__main__":
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    mcp.run(transport)
