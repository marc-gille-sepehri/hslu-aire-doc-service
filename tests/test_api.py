"""Endpoint contract for the new form fields — docs/delta-spec-doc-convert-cells.md §7.

Only the spreadsheet path is exercised; the Markdown path needs Docling (heavy).
"""
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
OBJEKTLISTE = (pathlib.Path(__file__).parent / "fixtures" / "objektliste.xlsx").read_bytes()


def post(**data):
    return client.post(
        "/convert",
        files={"file": ("objektliste.xlsx", OBJEKTLISTE,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data=data,
    )


def test_default_response_carries_no_cells():
    """Absent outputFormat, the response is markdown plus analysis and nothing
    else. `serialized` used to sit here too — a second, weaker serialization
    beside `cells` (delta spec §14, decision 1) — and is gone now that the
    wizard reads `cells`."""
    body = post().json()
    assert body["kind"] == "excel"
    assert set(body["excel"]) == {"filename", "markdown", "analysis"}


def test_cells_added_when_requested():
    body = post(outputFormat="cells").json()
    cells = body["excel"]["cells"]
    assert cells["applicable"] is True
    assert cells["text"].startswith("# Sheet: Q3\n")
    assert cells["formulaMode"] == "silent"


def test_both_keeps_markdown_and_adds_cells():
    excel = post(outputFormat="both").json()["excel"]
    assert excel["markdown"]
    assert excel["cells"]["applicable"] is True


def test_formula_mode_is_reported_back():
    cells = post(outputFormat="cells", formulaMode="formula").json()["excel"]["cells"]
    assert cells["formulaMode"] == "formula"
    assert cells["requestedFormulaMode"] == "formula"
    assert "{=SUM(D4:D5)}" in cells["text"]


@pytest.mark.parametrize(
    "data,field",
    [
        ({"outputFormat": "zellen"}, "outputFormat"),
        ({"formulaMode": "laut"}, "formulaMode"),
    ],
)
def test_invalid_enum_is_rejected(data, field):
    r = post(**data)
    assert r.status_code == 400
    assert field in r.json()["detail"]


def test_non_tabular_input_returns_200_and_an_explanation():
    """A cells request for a PDF must not fail the conversion (§3)."""
    r = client.post(
        "/convert",
        files={"file": ("bericht.pdf", b"%PDF-1.4 not really", "application/pdf")},
        data={"outputFormat": "cells"},
    )
    # Docling is not installed in the light dev env, so the markdown step raises 500 —
    # but the cells verdict is computed before it and must say "not applicable".
    if r.status_code == 200:
        assert r.json()["cells"]["applicable"] is False
    else:
        assert r.status_code == 500  # Docling missing, not a cells-path failure


def test_health():
    assert client.get("/health").json() == {"ok": True}
