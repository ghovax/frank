# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "fastapi>=0.115.0",
#   "python-multipart>=0.0.20",
#   "uvicorn[standard]>=0.34.0",
#   "dots_mocr @ git+https://github.com/rednote-hilab/dots.mocr.git",
# ]
# ///

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile


app = FastAPI(title="Dots/MOCR parser endpoint")


def _check_api_key(authorization: str | None) -> None:
    expected = os.environ.get("DAISY_DOTS_OCR_SERVER_API_KEY", "")
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid Dots/MOCR API key.")


@app.get("/health")
def health():
    importable = True
    import_error = ""
    try:
        import dots_mocr.parser  # noqa: F401
    except Exception as exception:  # noqa: BLE001
        importable = False
        import_error = str(exception)

    model_server = _model_server_url()
    model_server_ready = False
    model_server_error = ""
    try:
        with urllib.request.urlopen(f"{model_server}/v1/models", timeout=2) as response:
            model_server_ready = 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exception:
        model_server_error = str(exception)

    return {
        "status": "ok" if importable and model_server_ready else "not_ready",
        "dots_mocr_importable": importable,
        "dots_mocr_import_error": import_error,
        "model_server_url": model_server,
        "model_server_ready": model_server_ready,
        "model_server_error": model_server_error,
    }


@app.post("/parse")
async def parse_document(
    file: UploadFile = File(...),
    prompt_mode: str = Form("prompt_layout_all_en"),
    model_name: str = Form("rednote-hilab/dots.mocr"),
    output_format: str = Form("dots_json"),
    authorization: str | None = Header(default=None),
):
    _check_api_key(authorization)
    if output_format != "dots_json":
        raise HTTPException(status_code=400, detail="Only dots_json output is supported.")

    try:
        from dots_mocr.parser import DotsMOCRParser
    except Exception as exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"dots_mocr is not importable: {exception}") from exception

    suffix = Path(file.filename or "document").suffix
    with tempfile.TemporaryDirectory(prefix="dots-mocr-") as temporary_directory:
        work_dir = Path(temporary_directory)
        input_path = work_dir / f"input{suffix}"
        input_path.write_bytes(await file.read())
        output_dir = work_dir / "output"
        parser = DotsMOCRParser(
            protocol=os.environ.get("DAISY_DOTS_MOCR_PROTOCOL", "http"),
            ip=os.environ.get("DAISY_DOTS_MOCR_HOST", "127.0.0.1"),
            port=int(os.environ.get("DAISY_DOTS_MOCR_PORT", "8000")),
            model_name=model_name,
            output_dir=str(output_dir),
            use_hf=os.environ.get("DAISY_DOTS_MOCR_USE_HF", "").lower() in {"1", "true", "yes"},
            num_thread=int(os.environ.get("DAISY_DOTS_MOCR_THREADS", "16")),
            dpi=int(os.environ.get("DAISY_DOTS_MOCR_DPI", "200")),
        )
        try:
            pages = parser.parse_file(str(input_path), output_dir=str(output_dir), prompt_mode=prompt_mode)
        except Exception as exception:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Dots/MOCR parsing failed: {exception}") from exception
        return {"pages": [_inline_layout_cells(page) for page in pages]}


def _inline_layout_cells(page: dict) -> dict:
    layout_path = Path(str(page.get("layout_info_path") or ""))
    if not layout_path.is_file():
        return page
    try:
        parsed = json.loads(layout_path.read_text(encoding="utf-8"))
    except Exception:
        return page
    if isinstance(parsed, list):
        return {**page, "cells": parsed}
    if isinstance(parsed, dict):
        for key in ("layout", "cells", "result", "elements"):
            cells = parsed.get(key)
            if isinstance(cells, list):
                return {**page, "cells": cells}
    return page


def _model_server_url() -> str:
    protocol = os.environ.get("DAISY_DOTS_MOCR_PROTOCOL", "http")
    host = os.environ.get("DAISY_DOTS_MOCR_HOST", "127.0.0.1")
    port = int(os.environ.get("DAISY_DOTS_MOCR_PORT", "8000"))
    return f"{protocol}://{host}:{port}"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("DAISY_DOTS_OCR_SERVER_HOST", "127.0.0.1"),
        port=int(os.environ.get("DAISY_DOTS_OCR_SERVER_PORT", "8765")),
    )
