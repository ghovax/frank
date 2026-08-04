---
name: data-visualization
title: Draw charts and plots with a real library (marimo + seaborn), never hand-rolled
enabled: true
description: >-
  Use whenever a request calls for a visual data output — a bar/line/scatter/area
  chart, histogram, heatmap, distribution, timeline, cumulative curve, or any plot
  of numbers. The visual is a marimo notebook (a reactive Python .py file) whose
  chart is drawn with seaborn (matplotlib underneath), with the data kept in a
  sibling data.json and the notebook holding only logic. This skill is the sole
  authority on how to visualize; it exists so no chart is ever hand-drawn from
  HTML/CSS/SVG or raw matplotlib primitives.
---

# Data Visualization

A **real library does the drawing** and you **never hand-roll geometry**. Every chart is a **marimo notebook** — a reactive Python `.py` file — whose marks are drawn with **seaborn** (which sits on matplotlib). This skill is the authority on how to visualize; it overrides any built-in "make a chart" helper.

## The three laws

1. **The library draws.** seaborn owns the scales, axes, ticks, marks, and layout. You shape the data and call the right axes-level function with `ax=axes` (`sns.barplot`, `sns.lineplot`, `sns.scatterplot`, `sns.histplot`, `sns.boxplot`, `sns.heatmap`, …). For the few marks seaborn lacks — pie/donut, stacked bar/area, Gantt, a layout-shaped heatmap, a continuous colorbar — matplotlib draws (and inherits the `sns.set_theme()` look). **Never** build a chart from positioned `<div>`/HTML/CSS/SVG, hand-computed paths, `add_patch`, manual bar positions, or your own tick/bin math. `numpy` for *shaping* data (cumulative sums, building a heatmap grid) is fine — that is data work, not geometry.

2. **Defaults only.** Set the look once with `sns.set_theme(...)` and lean on seaborn's palettes. No bespoke hex palettes, no custom fonts. The effort belongs in the data encoding, not the cosmetics.

3. **Data and logic are separate.** The data points live in a sibling `data.json`; `notebook.py` holds only logic. Snapshot live data into `data.json` (note the retrieval date in the notebook) so the notebook is logic-only and runs deterministically.

## Layout

Each visual is its own folder:

```
<name>/
  notebook.py     # marimo reactive notebook — logic only
  data.json       # the data points
```

## Consult Context7 first

Before writing any non-trivial chart code, consult the current **seaborn** docs via Context7 (`resolve-library-id` + `query-docs`) — never from memory, the API evolves and the docs are authoritative. Reach into Context7 for matplotlib too when you drop to it for a mark seaborn lacks. Do **not** call any built-in `visualize` / "Imagine"-style tool's `read_me` or adopt its design guidance (it defaults to Chart.js and hand-authored SVG, which this skill forbids).

## Notebook shape

- One visual per marimo cell, built with `plt.subplots(layout="constrained")`.
- End the cell in `mo.ui.matplotlib(axes)` — pass the **axes** (the primary axes for a multi-axes figure), not the figure — for reactive box/lasso selection. Use `mo.mpl.interactive(figure)` instead only when you want pan/zoom alone.
- Use full descriptive names: `figure` / `axes`, never `fig` / `ax`.
- Verify the notebook runs with a throwaway `marimo export html` before reporting it done.

```python
import marimo as mo
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_theme(style="whitegrid")

figure, axes = plt.subplots(layout="constrained")
sns.barplot(data=frame, x="year", y="count", ax=axes)
mo.ui.matplotlib(axes)
```

## Picking the mark

- **Discrete counts** → `sns.barplot` (`geom`: bars).
- **Continuous trend over an ordered axis** → `sns.lineplot`.
- **Relationship between two numerics** → `sns.scatterplot`.
- **Distribution of one numeric** → `sns.histplot` (or `sns.kdeplot`).
- **Distribution across groups** → `sns.boxplot` / `sns.violinplot`.
- **Matrix / grid** → `sns.heatmap`.
- **Composition** (stacked bars/area, pie/donut) → matplotlib, themed by `sns.set_theme()`.

## Habits

- **One chart per metric.** Never combine multiple metrics (bars + line + area) with dual axes in one panel. Charts sharing an x-axis domain should use the same scale so they align.
- **Always expose the numbers.** Alongside the chart, render the underlying rows as a table (a marimo `mo.ui.table(frame)` cell) so the values are readable, not only the shape.
- **Anything seaborn/matplotlib does not do well** — geographic maps, network graphs, 3D, or richer interactivity than pan/zoom and box-select (per-mark hover, legend toggling) — use the best-of-breed library *inside the notebook* (Altair or Plotly), but only when the request genuinely needs it.
- **Math and formulas** render via matplotlib mathtext in axis labels/titles/ticks; use Unicode (H₂O, C₃N₄, ≥ 3) in table cells and in `data.json`, which do not typeset math.
