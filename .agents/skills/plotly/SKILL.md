---
name: plotly
title: Create and update Plotly chart artifacts
description: Use the configured Plotly MCP server to create and update interactive Plotly charts (bar, line, scatter, histogram, box, heatmap, pie, 3D, financial, and more) as rendered chart artifacts.
enabled: false
---

# Create and Update Plotly Chart Artifacts

Use this skill when the user asks for a chart, plot, or interactive data visualization — bar/line/scatter charts, histograms, box plots, heatmaps, pie/donut charts, 3D plots, candlestick/financial charts, and similar.

## MCP workflow

The configured MCP server is `plotly`. Start by discovering its current tools:

```json
{"server": "plotly"}
```

Use `call_mcp_tool` with these tools:

- `create_chart` — create a stateful chart and return an HTML artifact. Pass explicit `traces`; at least one is required.
- `update_chart` — update an existing `chart_id` (replace traces, merge layout/config, change title or height).
- `add_trace` — append a single trace to an existing chart.
- `list_charts` — inspect charts currently held by the stateful MCP subprocess.

The server is stateful, so keep and reuse the returned `chart_id` when the user asks to modify a chart. If a new turn lacks a known `chart_id`, call `list_charts` before creating a duplicate.

## Figure shape

Plotly owns the scales, axes, ticks, and rendering. Your job is to shape the data into a Plotly figure and pass it as JSON, exactly as Plotly.js expects.

- `traces` is the figure's `data`: a list of trace objects. Each trace has a `type` and its data arrays, e.g.

  ```json
  [
    {"type": "bar", "name": "Revenue", "x": ["Q1", "Q2", "Q3"], "y": [120, 145, 132]},
    {"type": "bar", "name": "Cost", "x": ["Q1", "Q2", "Q3"], "y": [90, 100, 110]}
  ]
  ```

- `layout` is the Plotly layout object: axis titles, `barmode`, legend, annotations, etc. The chart `title` is filled into the layout automatically.
- `config` is the Plotly config object, e.g. `{"displayModeBar": false}`. Responsive sizing is on by default.

Pick the trace `type` that fits the data: `scatter` (with `mode: "lines"`/`"markers"`), `bar`, `histogram`, `box`, `violin`, `heatmap`, `pie`, `scatter3d`, `surface`, `candlestick`, `ohlc`, and so on. Do not hand-build SVG or compute pixel positions — Plotly does that.

## Artifact updates

The chart tools use the harness-wide artifact update contract:

- `artifact_update_mode: "append"` renders a separate new artifact.
- `artifact_update_mode: "replace"` or `"update"` refreshes an existing artifact.
- `artifact_update_mode: "upsert"` refreshes the target when present, otherwise renders a new artifact.
- `artifact_target_id` selects the artifact to refresh; it defaults to the `chart_id` for `update_chart` and `add_trace`.

When the user asks to modify an existing chart, call `update_chart` (or `add_trace`) with the same `chart_id` and the default replacement behavior. Use `append` only when the user asks to compare charts or keep both versions visible.

## Interactivity

Charts forward point clicks back to you as a structured `widget_event` turn with `event: "point_click"` and `data.points` describing the clicked points (trace, x, y, label, value). React to it like any other input — for example, drill in by calling `update_chart` on the same `chart_id` with `artifact_update_mode="replace"`.

If a figure fails to render (malformed trace data, an invalid layout), the error comes back the same way as a `widget_event` with `event: "render_error"` and `data.message`. Read it and call `update_chart` to fix the figure.
