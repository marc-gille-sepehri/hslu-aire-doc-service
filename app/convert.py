"""Conversion logic: Docling → Markdown for documents, native pandas analysis for
spreadsheets. Docling is imported lazily so the service can run (and the Excel
path can be tested) without the heavy Torch/model stack installed."""
import io
import math
from typing import Any

# Spreadsheets get a rich, descriptive analysis instead of flat Markdown.
EXCEL_EXTS = {"xlsx", "xls", "xlsm"}
# Everything else Docling handles → Markdown.
DOC_EXTS = {"pdf", "docx", "pptx", "html", "htm", "md", "txt", "csv", "png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"}


def ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _num(v: Any):
    """Make a numpy/py number JSON-safe (NaN/Inf → None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    # keep ints as ints
    return int(v) if float(v).is_integer() else round(f, 4)


def _jsonable(obj: Any) -> Any:
    """Recursively coerce values (numpy scalars, NaN, Timestamps) to JSON-safe."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is None:
        return None
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (int, str, bool)):
        return obj
    # numpy scalars, pandas Timestamps, etc.
    try:
        import numpy as np  # noqa
        if isinstance(obj, np.generic):
            return _jsonable(obj.item())
    except Exception:
        pass
    return str(obj)


def analyze_excel(data: bytes, filename: str) -> dict:
    """Native spreadsheet analysis: per sheet, per column dtype/null/unique + numeric
    stats or top categorical values, plus a small sample. Richer than Markdown."""
    import pandas as pd

    xls = pd.ExcelFile(io.BytesIO(data))
    sheets = []
    for name in xls.sheet_names:
        df = xls.parse(name)
        columns = []
        for c in df.columns:
            s = df[c]
            col: dict = {
                "name": str(c),
                "dtype": str(s.dtype),
                "nonNull": int(s.notna().sum()),
                "nulls": int(s.isna().sum()),
                "unique": int(s.nunique(dropna=True)),
            }
            if pd.api.types.is_numeric_dtype(s) and s.notna().any():
                col["stats"] = {
                    "min": _num(s.min()),
                    "max": _num(s.max()),
                    "mean": _num(s.mean()),
                    "sum": _num(s.sum()),
                }
            else:
                top = s.dropna().astype(str).value_counts().head(3)
                col["top"] = [{"value": str(k), "count": int(v)} for k, v in top.items()]
            columns.append(col)
        head = df.head(5)
        sample = _jsonable(head.astype(object).where(pd.notna(head), None).to_dict(orient="records"))
        sheets.append({"name": str(name), "rows": int(len(df)), "columns": columns, "sample": sample})
    return {"filename": filename, "sheets": sheets}


def to_markdown(data: bytes, filename: str) -> str:
    """Convert a document to Markdown with Docling (lazy import — heavy stack)."""
    from docling.document_converter import DocumentConverter
    from docling.datamodel.base_models import DocumentStream

    converter = DocumentConverter()
    source = DocumentStream(name=filename, stream=io.BytesIO(data))
    result = converter.convert(source)
    return result.document.export_to_markdown()
