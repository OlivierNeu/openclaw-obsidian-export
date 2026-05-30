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


def _make_epub() -> bytes:
    """Build a minimal valid EPUB2 in memory (title/creator + one chapter)."""
    import io
    import zipfile

    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles><rootfile full-path=\"content.opf\" "
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
        'unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Le Test Sandokai</dc:title>"
        "<dc:creator>Denis Crozet</dc:creator>"
        "<dc:language>fr</dc:language>"
        '<dc:identifier id="id">urn:uuid:test</dc:identifier>'
        "</metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" '
        'media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    # Includes Calibre-style noise (empty page-anchor span, img, hlink) that
    # the cleanup must strip while keeping the prose.
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch1</title>'
        '</head><body><div class="calibre1"><h1>Chapitre Un</h1>'
        '<span id="page_1"></span>'
        '<img src="images/cover.jpeg" class="calibre5" />'
        "<p>Le bleu n'est pas le vert. "
        '<a href="#frag" class="hlink">Voir Figure 1</a>. '
        "Vernassier &amp; Carrette &gt; analyse. Commentaires épars.</p>"
        "</div></body></html>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype must be the first entry and stored uncompressed.
        zf.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", opf)
        zf.writestr("ch1.xhtml", chapter)
    return buf.getvalue()


def test_epub_converts_to_markdown_via_pandoc() -> None:
    """epub -> GFM markdown + OPF metadata, same contract as /convert."""
    resp = requests.post(
        f"{BASE_URL}/convert-epub",
        files={"file": ("book.epub", _make_epub(), "application/epub+zip")},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["content"]
    # Prose preserved
    assert "Chapitre Un" in content
    assert "bleu" in content
    assert "Commentaires épars" in content
    # Noise stripped: no raw HTML tag, no empty anchor span, no image ref
    assert "<span" not in content
    assert "<div" not in content
    assert "<img" not in content
    assert "calibre" not in content
    assert "![" not in content
    # HTML entities decoded (Calibre epubs are riddled with &amp;/&gt;)
    assert "Vernassier & Carrette > analyse" in content
    assert "&amp;" not in content
    assert "&gt;" not in content
    # Cross-ref link text survives (the URL fragment may be dropped/kept)
    assert "Figure 1" in content
    assert data["frontmatter"]["title"] == "Le Test Sandokai"
    assert data["frontmatter"]["creator"] == "Denis Crozet"
    assert "title: Le Test Sandokai" in data["frontmatter_text"]
    assert data["wikilinks_out"] == []
    assert data["tags_inline"] == []
    assert data["stats"]["output_bytes"] > 0
    assert data["stats"]["fallback_used"] is False


def test_empty_epub_is_400() -> None:
    resp = requests.post(
        f"{BASE_URL}/convert-epub",
        files={"file": ("empty.epub", b"", "application/epub+zip")},
        timeout=10,
    )
    assert resp.status_code == 400


def test_garbage_epub_is_422() -> None:
    """A non-epub blob must fail loud (422) so the n8n pipeline counts it."""
    resp = requests.post(
        f"{BASE_URL}/convert-epub",
        files={"file": ("bad.epub", b"not a real epub at all", "application/epub+zip")},
        timeout=15,
    )
    assert resp.status_code == 422


def test_persist_md_writes_file_with_frontmatter() -> None:
    """Smoke: POST /persist-md returns a path + writes YAML frontmatter."""
    md5 = "abcdef0123456789abcdef0123456789"
    body = {
        "owner": "jerome",
        "md5": md5,
        "fileName": "Smoke: persist test.epub",
        "fileUrl": "https://drive.google.com/file/d/FAKE/view",
        "fileId": "FAKE",
        "fileMimeType": "application/epub+zip",
        "converter": "epub",
        "content": "# Title\n\nVerbatim content.\n",
    }
    resp = requests.post(f"{BASE_URL}/persist-md", json=body, timeout=10)
    resp.raise_for_status()
    out = resp.json()
    assert out["filename"].endswith(f"--{md5}.md")
    assert "Smoke_ persist test" in out["filename"] or "Smoke persist test" in out["filename"]
    assert "/jerome/" in out["path"]


def test_persist_md_rejects_invalid_owner() -> None:
    resp = requests.post(
        f"{BASE_URL}/persist-md",
        json={"owner": "../etc", "md5": "a" * 32, "content": "x", "fileName": "x"},
        timeout=5,
    )
    assert resp.status_code == 400


def test_persist_md_rejects_empty_content() -> None:
    resp = requests.post(
        f"{BASE_URL}/persist-md",
        json={"owner": "jerome", "md5": "a" * 32, "content": "   "},
        timeout=5,
    )
    assert resp.status_code == 400


def test_cache_list_then_archive_roundtrip() -> None:
    """persist a file, see it in /cache-list, archive it, see it gone."""
    md5 = "1234567890abcdef1234567890abcdef"
    persist = requests.post(
        f"{BASE_URL}/persist-md",
        json={
            "owner": "jerome",
            "md5": md5,
            "fileName": "Roundtrip doc.pdf",
            "content": "# Roundtrip\n\nbody\n",
        },
        timeout=10,
    )
    persist.raise_for_status()
    fname = persist.json()["filename"]

    listed = requests.get(f"{BASE_URL}/cache-list", params={"owner": "jerome"}, timeout=10)
    listed.raise_for_status()
    md5s = {f["md5"] for f in listed.json()["files"]}
    assert md5 in md5s

    arch = requests.post(
        f"{BASE_URL}/cache-archive",
        json={"owner": "jerome", "filenames": [fname]},
        timeout=10,
    )
    arch.raise_for_status()
    assert arch.json()["archived"] == 1

    listed2 = requests.get(f"{BASE_URL}/cache-list", params={"owner": "jerome"}, timeout=10)
    listed2.raise_for_status()
    md5s2 = {f["md5"] for f in listed2.json()["files"]}
    assert md5 not in md5s2  # moved to _archive/, no longer live


def test_cache_archive_blocks_path_traversal() -> None:
    resp = requests.post(
        f"{BASE_URL}/cache-archive",
        json={"owner": "jerome", "filenames": ["../../../etc/passwd"]},
        timeout=5,
    )
    resp.raise_for_status()
    assert resp.json()["archived"] == 0


def test_cache_list_rejects_invalid_owner() -> None:
    resp = requests.get(f"{BASE_URL}/cache-list", params={"owner": "../x"}, timeout=5)
    assert resp.status_code == 400


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-xvs"]))
