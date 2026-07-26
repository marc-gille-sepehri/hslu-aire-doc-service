"""hslu-aire-doc-service — a small FastAPI service that converts uploaded documents
to Markdown (via Docling) and analyses spreadsheets natively. It is called
server-to-server by the AI@RE portal (never exposed publicly); a shared bearer
token (SERVICE_TOKEN) guards it."""
import os

from fastapi import FastAPI, File, Header, HTTPException, UploadFile

from .convert import EXCEL_EXTS, convert_excel, ext_of, to_markdown

app = FastAPI(title="hslu-aire-doc-service", version="0.1.0")

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "").strip()
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(30 * 1024 * 1024)))


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/convert")
async def convert(file: UploadFile = File(...), authorization: str = Header(default="")):
    # Shared-token auth (skip only if no token configured, e.g. local dev).
    if SERVICE_TOKEN and authorization != f"Bearer {SERVICE_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    name = file.filename or "datei"
    ext = ext_of(name)
    try:
        if ext in EXCEL_EXTS:
            return {"kind": "excel", "filename": name, "excel": convert_excel(data, name)}
        markdown = to_markdown(data, name)
        return {"kind": "markdown", "filename": name, "markdown": markdown}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — surface a clean message to the caller
        raise HTTPException(status_code=500, detail=f"conversion failed: {e}")
