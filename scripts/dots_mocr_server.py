# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "fastapi>=0.115.0",
#   "python-multipart>=0.0.20",
#   "uvicorn[standard]>=0.34.0",
#   "httpx>=0.28.0",
# ]
# ///

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

import httpx


app = FastAPI(title="Dots/MOCR parser endpoint")

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompts() -> dict[str, str]:
    """Load prompt templates from individual .md files in prompts/."""
    prompts: dict[str, str] = {}
    for path in _PROMPTS_DIR.glob("*.md"):
        key = path.stem
        prompts[key] = path.read_text(encoding="utf-8").strip()
    return prompts


PROMPTS = _load_prompts()


def _check_api_key(authorization: str | None) -> None:
    expected = os.environ.get("DAISY_DOTS_OCR_SERVER_API_KEY", "")
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid Dots/MOCR API key.")


def _model_server_url() -> str:
    protocol = os.environ.get("DAISY_DOTS_MOCR_PROTOCOL", "http")
    host = os.environ.get("DAISY_DOTS_MOCR_HOST", "127.0.0.1")
    port = int(os.environ.get("DAISY_DOTS_MOCR_PORT", "8000"))
    return f"{protocol}://{host}:{port}"


def _image_to_base64_url(image_bytes: bytes, suffix: str) -> str:
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }.get(suffix.lower(), "image/png")
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{content_type};base64,{b64}"


def _call_model_server(
    image_bytes: bytes,
    suffix: str,
    prompt: str,
    model_name: str,
    temperature: float = 0.1,
    max_tokens: int = 32768,
) -> str:
    url = f"{_model_server_url()}/v1/chat/completions"
    image_url = _image_to_base64_url(image_bytes, suffix)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    api_key = os.environ.get("DAISY_DOTS_MOCR_API_KEY", "not-needed")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    response = httpx.post(url, json=payload, headers=headers, timeout=600)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


@app.get("/health")
def health():
    model_server = _model_server_url()
    model_server_ready = False
    model_server_error = ""
    try:
        with urllib.request.urlopen(f"{model_server}/v1/models", timeout=2) as response:
            model_server_ready = 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exception:
        model_server_error = str(exception)

    return {
        "status": "ok" if model_server_ready else "not_ready",
        "model_server_url": model_server,
        "model_server_ready": model_server_ready,
        "model_server_error": model_server_error,
    }


@app.post("/parse")
async def parse_document(
    file: UploadFile = File(...),
    prompt_mode: str = Form("prompt_layout_all_en"),
    model_name: str = Form("mlx-community/dots.mocr-mxfp4"),
    output_format: str = Form("dots_json"),
    authorization: str | None = Header(default=None),
):
    _check_api_key(authorization)
    if output_format != "dots_json":
        raise HTTPException(status_code=400, detail="Only dots_json output is supported.")

    prompt_template = PROMPTS.get(prompt_mode)
    if prompt_template is None:
        raise HTTPException(status_code=400, detail=f"Unknown prompt_mode: {prompt_mode}")

    suffix = Path(file.filename or "document").suffix
    image_bytes = await file.read()

    prompt = prompt_template
    if "{width}" in prompt or "{height}" in prompt:
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(image_bytes))
            prompt = prompt.replace("{width}", str(img.width)).replace(
                "{height}", str(img.height)
            )
        except Exception:
            pass

    try:
        raw_response = _call_model_server(image_bytes, suffix, prompt, model_name)
    except httpx.HTTPStatusError as exception:
        raise HTTPException(
            status_code=502,
            detail=f"Model server returned {exception.response.status_code}: {exception.response.text}",
        ) from exception
    except Exception as exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Model server request failed: {exception}") from exception

    return _parse_model_response(raw_response, image_bytes, suffix)


def _parse_model_response(raw_response: str, image_bytes: bytes, suffix: str) -> dict:
    """Parse the model's text response into structured layout cells."""
    cells = []
    try:
        parsed = json.loads(raw_response)
        if isinstance(parsed, list):
            cells = parsed
        elif isinstance(parsed, dict):
            for key in ("layout", "cells", "result", "elements"):
                candidate = parsed.get(key)
                if isinstance(candidate, list):
                    cells = candidate
                    break
    except json.JSONDecodeError:
        pass

    return {
        "pages": [
            {
                "cells": cells,
                "raw_response": raw_response,
            }
        ]
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("DAISY_DOTS_OCR_SERVER_HOST", "127.0.0.1"),
        port=int(os.environ.get("DAISY_DOTS_OCR_SERVER_PORT", "8765")),
    )
