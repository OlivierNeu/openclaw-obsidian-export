"""Smoke tests for the /convert and /health endpoints.

Run against a running container:

    docker build -t openclaw-obsidian-export:dev .
    docker run --rm -d --name oe-test -p 18080:8080 openclaw-obsidian-export:dev
    pytest -xvs tests/test_convert.py
    docker stop oe-test

Or against a local uvicorn dev server (binary must be on PATH):

    pip install -r requirements.txt
    uvicorn app:app --port 18080 &
    pytest -xvs tests/test_convert.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import requests

BASE_URL = os.environ.get("OE_TEST_URL", "http://127.0.0.1:18080")
FIXTURES = Path(__file__).parent / "fixtures"


def _post(filename: str) -> dict:
    path = FIXTURES / filename
    with path.open("rb") as fh:
        resp = requests.post(
            f"{BASE_URL}/convert",
            files={"file": (filename, fh, "text/markdown")},
            timeout=10,
        )
    resp.raise_for_status()
    return resp.json()


def test_health() -> None:
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    resp.raise_for_status()
    data = resp.json()
    assert data["status"] == "ok"
    assert "obsidian-export" in data["obsidian_export_version"].lower()


def test_simple_note_is_pass_through() -> None:
    data = _post("note-simple.md")
    assert "bullet 1" in data["content"]
    assert data["frontmatter"] is None
    assert data["wikilinks_out"] == []
    assert data["tags_inline"] == []
    assert data["stats"]["input_bytes"] > 0
    assert data["stats"]["output_bytes"] > 0


def test_frontmatter_is_parsed() -> None:
    data = _post("note-frontmatter.md")
    assert data["frontmatter"] is not None
    assert data["frontmatter"]["type"] == "kusen"
    assert data["frontmatter"]["tags"] == ["zen", "kusen"]
    assert "tags: zen, kusen" in data["frontmatter_text"]
    assert data["tags_inline"] == ["#observation"]


def test_wikilinks_are_extracted_and_normalised() -> None:
    data = _post("note-wikilinks.md")
    # Outgoing wikilinks (deduplicated, fragments stripped)
    assert "Base 5" in data["wikilinks_out"]
    assert "Base 8" in data["wikilinks_out"]
    assert "Base 2" in data["wikilinks_out"]
    assert "MOC ZEN-BOUDDHISME" in data["wikilinks_out"]
    assert "Carlo Rovelli" in data["wikilinks_out"]
    # Inline tags
    assert "#enneagramme" in data["tags_inline"]
    assert "#base-5" in data["tags_inline"]
    # Content no longer contains raw `[[wikilinks]]` syntax (obsidian-export
    # has rewritten them as standard markdown links).
    assert "[[Base 5]]" not in data["content"]
    assert "Base 5" in data["content"]


def test_empty_file_is_400() -> None:
    resp = requests.post(
        f"{BASE_URL}/convert",
        files={"file": ("empty.md", b"", "text/markdown")},
        timeout=5,
    )
    assert resp.status_code == 400


def test_bad_frontmatter_falls_back_instead_of_500() -> None:
    """obsidian-export refuses malformed YAML frontmatter (plain text between
    --- markers). The sidecar must fall back to the raw source instead of
    propagating a 500 to the n8n pipeline."""
    data = _post("note-bad-frontmatter.md")
    assert data["stats"]["fallback_used"] is True
    assert "obsidian-export failed" in (data.get("warning") or "")
    assert "frontmatter" in data["content"]  # raw source still indexable
    assert "obsidian-export" in data["wikilinks_out"]
    assert "#regression" in data["tags_inline"]
    assert "#yaml-strict" in data["tags_inline"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-xvs"]))
