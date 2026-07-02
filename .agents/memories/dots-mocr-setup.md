---
name: dots-mocr-setup
title: Dots/MOCR OCR server setup and configuration
description: How to run the dots.mocr VLM for document parsing, which model to use on Apple Silicon, and how the two-server architecture works.
importance: high
tags: dots-mocr, ocr, vlm, mlx, apple-silicon, document-parsing
---

## Architecture

Dots/MOCR is a two-tier OCR pipeline:

1. **Model server** (port 8000) — serves the VLM as an OpenAI-compatible API.
2. **Parser endpoint** (port 8765) — wraps the model server behind a `/parse` POST endpoint that Daisy's research evidence plane calls.

Daisy's config (`.daisy/configuration.yaml`) points at the parser endpoint (`http://127.0.0.1:8765/parse`), not the model server directly.

## Running the servers

Both must be running for OCR to work. Start them in separate terminals or background them.

**Model server (MLX, Apple Silicon):**

```sh
nohup uv run --with mlx-vlm -- python -m mlx_vlm.server \
  --model mlx-community/dots.mocr-mxfp4 \
  --port 8000 \
  --trust-remote-code \
  > /tmp/mlx_vlm_server.log 2>&1 &
```

This uses `mlx-vlm` which runs natively on Apple Silicon (no CUDA required). The model is `mlx-community/dots.mocr-mxfp4` (2B params, mxfp4 quantized, ~3.5 GB).

**Parser endpoint:**

```sh
nohup uv run scripts/dots_mocr_server.py \
  > /tmp/dots_mocr_server.log 2>&1 &
```

Listens on `127.0.0.1:8765` by default.

## Health check

```sh
curl http://127.0.0.1:8765/health
```

Returns `{"status": "ok", ...}` when the model server is reachable.

## Configuration (`.daisy/configuration.yaml`)

```yaml
dots_ocr:
  enabled: true
  mode: "local"
  endpoint: "http://127.0.0.1:8765/parse"
  api_key: ""
  model_name: "mlx-community/dots.mocr-mxfp4"
  prompt_mode: "prompt_layout_all_en"
  timeout_seconds: 900
```

Key points:
- `model_name` must match the model ID served by the model server (`mlx-community/dots.mocr-mxfp4`).
- `endpoint` points at the parser endpoint, not the model server.
- `prompt_mode` selects which prompt template to use. Templates live in `scripts/prompts/`.

## Prompt templates

Prompts are individual `.md` files in `scripts/prompts/`, loaded at server startup. Each file is named `prompt_<mode>.md`. Edit the files to tune prompts without touching code.

Available modes: `prompt_layout_all_en`, `prompt_layout_only_en`, `prompt_ocr`, `prompt_grounding_ocr`, `prompt_web_parsing`, `prompt_scene_spotting`, `prompt_image_to_svg`, `prompt_general`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DAISY_DOTS_MOCR_PROTOCOL` | `http` | Protocol for model server |
| `DAISY_DOTS_MOCR_HOST` | `127.0.0.1` | Model server host |
| `DAISY_DOTS_MOCR_PORT` | `8000` | Model server port |
| `DAISY_DOTS_MOCR_API_KEY` | `not-needed` | API key for model server |
| `DAISY_DOTS_OCR_SERVER_HOST` | `127.0.0.1` | Parser endpoint host |
| `DAISY_DOTS_OCR_SERVER_PORT` | `8765` | Parser endpoint port |
| `DAISY_DOTS_OCR_SERVER_API_KEY` | `""` | API key for parser endpoint |

## Why not vLLM?

The upstream dots.mocr README recommends vLLM, but vLLM requires CUDA (NVIDIA GPU). This machine is Apple Silicon, so we use the MLX port (`mlx-community/dots.mocr-mxfp4`) via `mlx-vlm` instead. The API is OpenAI-compatible, so the parser endpoint works unchanged.

## Files

- `scripts/dots_mocr_server.py` — parser endpoint (FastAPI)
- `scripts/prompts/*.md` — prompt templates
- `src/harness/research/dots.py` — Daisy's client for the parser endpoint
- `.daisy/configuration.yaml` — user configuration
