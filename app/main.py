"""hslu-aire-doc-service — a small FastAPI service that converts uploaded documents
to Markdown (via Docling) and analyses spreadsheets natively. It is called
server-to-server by the AI@RE portal (never exposed publicly); a shared bearer
token (SERVICE_TOKEN) guards it."""
import os

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

from .cells import FORMULA_MODES, OUTPUT_FORMATS, build_cells, wants
from .convert import EXCEL_EXTS, convert_excel, ext_of, to_markdown

app = FastAPI(title="hslu-aire-doc-service", version="0.1.0")

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "").strip()
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(30 * 1024 * 1024)))


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    outputFormat: str = Form(default="markdown"),
    formulaMode: str = Form(default="silent"),
    authorization: str = Header(default=""),
):
    # Shared-token auth (skip only if no token configured, e.g. local dev).
    if SERVICE_TOKEN and authorization != f"Bearer {SERVICE_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

    if outputFormat not in OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail=f"invalid outputFormat: {outputFormat}")
    if formulaMode not in FORMULA_MODES:
        raise HTTPException(status_code=400, detail=f"invalid formulaMode: {formulaMode}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    name = file.filename or "datei"
    ext = ext_of(name)
    try:
        # Absent/markdown outputFormat must behave exactly as before — no extra keys.
        cells = build_cells(data, name, ext, formulaMode) if wants(outputFormat, "cells") else None

        if ext in EXCEL_EXTS:
            body = {"kind": "excel", "filename": name, "excel": convert_excel(data, name)}
            if cells is not None:
                body["excel"]["cells"] = cells
            return body

        body = {"kind": "markdown", "filename": name, "markdown": to_markdown(data, name)}
        if cells is not None:
            body["cells"] = cells
        return body
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — surface a clean message to the caller
        raise HTTPException(status_code=500, detail=f"conversion failed: {e}")
