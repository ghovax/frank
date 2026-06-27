import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


mcp = FastMCP("plotly")

TEMPLATE_PATH = Path(__file__).with_name("chart.html")
CHARTS: dict[str, dict[str, Any]] = {}
ARTIFACT_UPDATE_MODES = {"append", "replace", "update", "upsert"}


def _normalize_traces(traces: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized = []
    for index, trace in enumerate(traces or []):
        if not isinstance(trace, dict):
            raise ValueError(f"Trace {index} must be a JSON object of Plotly trace properties.")
        normalized.append(trace)
    if not normalized:
        raise ValueError("At least one trace is required.")
    return normalized


def _trace_summary(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for index, trace in enumerate(traces):
        summary.append({
            "index": index,
            "type": str(trace.get("type") or "scatter"),
            "name": str(trace.get("name") or f"trace {index}"),
        })
    return summary


def _json_for_template(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


def _render_html(chart_data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text()
    payload = {
        "chart_id": chart_data["chart_id"],
        "traces": chart_data["traces"],
        "layout": chart_data["layout"],
        "config": chart_data["config"],
    }
    return template.replace("__CHART_DATA_JSON__", _json_for_template(payload))


def _summarize(chart_data: dict[str, Any]) -> dict[str, Any]:
    traces = _trace_summary(chart_data["traces"])
    return {
        "chart_id": chart_data["chart_id"],
        "title": chart_data["title"],
        "trace_count": len(traces),
        "traces": traces,
    }


def _artifact_mode(value: str, default: str) -> str:
    normalized = (value or default).strip().lower()
    if normalized == "new":
        return "append"
    return normalized if normalized in ARTIFACT_UPDATE_MODES else default


def _artifact(
    chart_data: dict[str, Any],
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
) -> dict[str, Any]:
    summary = _summarize(chart_data)
    target_id = artifact_target_id.strip()
    mode = _artifact_mode(artifact_update_mode, "append")
    return {
        "context": {
            **summary,
            "summary": f"{chart_data['title']}: {summary['trace_count']} trace(s).",
        },
        "artifacts": [
            {
                "artifact_id": chart_data["chart_id"],
                "artifact_target_id": target_id or chart_data["chart_id"],
                "artifact_update_mode": mode,
                "type": "html",
                "title": chart_data["title"],
                "html": _render_html(chart_data),
                "height": chart_data["height"],
                "summary": "Interactive Plotly chart.",
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
async def create_chart(
    traces: list[dict[str, Any]],
    title: str = "Plotly chart",
    layout: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    height: int = 460,
    chart_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Create a stateful interactive Plotly chart and return its artifact.

    Plotly draws the chart; you only shape the data. Pass the figure as JSON
    exactly as Plotly.js expects it.

    Args:
        traces: List of Plotly trace objects (the figure's `data`). Each is a
            JSON object such as {"type": "bar", "x": [...], "y": [...],
            "name": "Revenue"}. Supports every Plotly trace type (scatter, bar,
            line, histogram, box, heatmap, pie, scatter3d, candlestick, ...).
        title: Caption shown above the chart artifact. For an in-figure title,
            set `layout.title` yourself.
        layout: Plotly layout object (axes, legend, barmode, annotations, ...).
        config: Plotly config object (e.g. {"displayModeBar": false}). Merged
            over the responsive defaults.
        height: Rendered height in pixels (200-900).
        chart_id: Optional stable identifier; generated when omitted.
        artifact_update_mode: append to render a new artifact, replace/update to
            refresh a target artifact, or upsert to replace when present.
        artifact_target_id: Existing artifact id to refresh; defaults to chart_id.
    """
    await _progress(context, 1, 3, "Validating chart traces")
    normalized_traces = _normalize_traces(traces)
    identifier = chart_id.strip() or f"chart-{uuid.uuid4().hex[:10]}"
    CHARTS[identifier] = {
        "chart_id": identifier,
        "title": title,
        "traces": normalized_traces,
        "layout": dict(layout or {}),
        "config": dict(config or {}),
        "height": max(200, min(900, int(height))),
    }
    await _progress(context, 2, 3, f"Rendering {identifier}")
    result = _artifact(CHARTS[identifier], artifact_update_mode, artifact_target_id)
    await _progress(context, 3, 3, "Chart ready")
    return result


@mcp.tool()
async def update_chart(
    chart_id: str,
    traces: list[dict[str, Any]] | None = None,
    title: str | None = None,
    layout: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    height: int | None = None,
    artifact_update_mode: str = "replace",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Update an existing chart and return the refreshed artifact.

    Only the provided fields change. Pass `traces` to replace the data, `layout`
    to merge layout changes, and so on.
    """
    if chart_id not in CHARTS:
        raise ValueError(f"Unknown chart_id: {chart_id}")
    await _progress(context, 1, 3, f"Loading {chart_id}")
    chart_data = dict(CHARTS[chart_id])
    if traces is not None:
        chart_data["traces"] = _normalize_traces(traces)
    if title is not None:
        chart_data["title"] = title
    if layout is not None:
        chart_data["layout"] = {**chart_data["layout"], **layout}
    if config is not None:
        chart_data["config"] = {**chart_data["config"], **config}
    if height is not None:
        chart_data["height"] = max(200, min(900, int(height)))
    await _progress(context, 2, 3, "Applying chart updates")
    CHARTS[chart_id] = chart_data
    result = _artifact(chart_data, artifact_update_mode, artifact_target_id or chart_id)
    await _progress(context, 3, 3, "Updated chart ready")
    return result


@mcp.tool()
async def add_trace(
    chart_id: str,
    trace: dict[str, Any],
    artifact_update_mode: str = "replace",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Append a single trace to an existing chart and return the refreshed artifact."""
    if chart_id not in CHARTS:
        raise ValueError(f"Unknown chart_id: {chart_id}")
    await _progress(context, 1, 2, f"Adding trace to {chart_id}")
    chart_data = dict(CHARTS[chart_id])
    chart_data["traces"] = [*chart_data["traces"], *_normalize_traces([trace])]
    CHARTS[chart_id] = chart_data
    result = _artifact(chart_data, artifact_update_mode, artifact_target_id or chart_id)
    await _progress(context, 2, 2, "Updated chart ready")
    return result


@mcp.tool()
def list_charts() -> dict[str, Any]:
    """List charts currently held by this stateful MCP server process."""
    return {
        "charts": [_summarize(chart_data) for chart_data in CHARTS.values()],
        "count": len(CHARTS),
    }


if __name__ == "__main__":
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    mcp.run(transport)
