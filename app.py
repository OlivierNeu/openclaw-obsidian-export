"""HTTP wrapper around the zoni/obsidian-export CLI.

POST /convert  multipart-form-data with:
    file        (required)  the .md content as a file part
    filename    (optional)  override the original filename (used by obsidian-export
                            as the source note name; defaults to the multipart
                            "filename" field)

Returns JSON:
    {
      "content":          "<converted CommonMark>",
      "frontmatter":      { ...parsed YAML keys... } | null,
      "frontmatter_text": "<human-readable bullet rendering of frontmatter>",
      "wikilinks_out":    ["Target Note", ...],
      "tags_inline":      ["#tag1", "#tag2", ...],
      "stats": {
        "input_bytes":  1234,
        "output_bytes": 1180,
        "wikilinks_count": 5,
        "tags_count":      3
      }
    }

GET /health     liveness probe (always 200 if the process is up and the
                obsidian-export binary is reachable).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import datetime as _dt

import frontmatter
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("obsidian-export-svc")

OBSIDIAN_EXPORT_BIN = "/usr/local/bin/obsidian-export"
MAX_INPUT_BYTES = 10 * 1024 * 1024  # 10 MB — Obsidian notes are tiny in practice

# Inline wikilinks like [[Target]], [[Target|alias]], or embeds ![[Target]].
# We strip the optional "#heading" or "^block" fragment from the target so we
# index the destination note rather than a positional fragment.
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]|#^]+)(?:[#^][^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")

# Inline #tags. Excludes leading-# in code fences and headings — that's
# handled by skipping fenced blocks before scanning. We accept unicode
# tag characters (Obsidian allows them).
TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z0-9_\-/]+)")

CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

app = FastAPI(
    title="openclaw-obsidian-export",
    description="HTTP wrapper around zoni/obsidian-export — converts a single "
    "Obsidian markdown note to CommonMark and extracts wikilinks/tags/frontmatter.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness probe. Returns 200 if the obsidian-export binary is reachable."""
    try:
        result = subprocess.run(
            [OBSIDIAN_EXPORT_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return {"status": "ok", "obsidian_export_version": result.stdout.strip()}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.exception("obsidian-export binary unreachable")
        raise HTTPException(status_code=503, detail=f"obsidian-export unavailable: {exc!s}") from exc


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    filename: str | None = Form(default=None),
) -> JSONResponse:
    """Convert a single Obsidian .md note to CommonMark + extract metadata."""
    raw = await file.read()
    if len(raw) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large: {len(raw)} bytes > {MAX_INPUT_BYTES}",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    safe_name = _safe_filename(filename or file.filename or "note.md")
    if not safe_name.endswith(".md"):
        safe_name = f"{safe_name}.md"

    # obsidian-export operates on a vault directory, not a single file. We
    # create a temporary vault with just this one note, run the converter,
    # then read the (potentially rewritten) note back from the output dir.
    with tempfile.TemporaryDirectory(prefix="oe_") as tmp:
        tmp_path = Path(tmp)
        vault_dir = tmp_path / "vault"
        out_dir = tmp_path / "out"
        vault_dir.mkdir()

        out_dir.mkdir()
        src_file = vault_dir / safe_name
        src_file.write_bytes(raw)

        try:
            subprocess.run(
                [OBSIDIAN_EXPORT_BIN, str(vault_dir), str(out_dir)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            log.error("obsidian-export failed: stderr=%s", exc.stderr)
            raise HTTPException(
                status_code=500,
                detail=f"obsidian-export failed: {exc.stderr or exc.stdout or 'unknown error'}",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="obsidian-export timed out") from exc

        out_file = out_dir / safe_name
        if not out_file.exists():
            # obsidian-export sometimes drops the file when its content is
            # entirely filtered (e.g. only embeds with no resolution). Fall
            # back to the original to keep ingestion deterministic.
            log.warning("obsidian-export produced no output for %s — falling back to input", safe_name)
            shutil.copy(src_file, out_file)

        converted_raw = out_file.read_text(encoding="utf-8")

    source_text = raw.decode("utf-8", errors="replace")
    fm_data, fm_human = _parse_frontmatter(source_text)
    wikilinks = _extract_wikilinks(source_text)
    tags = _extract_tags(source_text)

    # obsidian-export preserves the YAML frontmatter block at the top of the
    # converted output. We return frontmatter as a separate structured field
    # (frontmatter + frontmatter_text), so we strip the inline YAML block from
    # `content` to avoid duplication when the caller concatenates the two.
    converted = _strip_frontmatter(converted_raw)

    return JSONResponse(
        {
            "content": converted,
            "frontmatter": fm_data,
            "frontmatter_text": fm_human,
            "wikilinks_out": wikilinks,
            "tags_inline": tags,
            "stats": {
                "input_bytes": len(raw),
                "output_bytes": len(converted.encode("utf-8")),
                "wikilinks_count": len(wikilinks),
                "tags_count": len(tags),
            },
        }
    )


def _safe_filename(name: str) -> str:
    """Strip path components and any character that could escape the tempdir."""
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._\- ]", "_", base) or "note.md"


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- ... ---) from a markdown string."""
    try:
        return frontmatter.loads(text).content.lstrip("\n")
    except Exception:  # noqa: BLE001 — leave content untouched if parsing fails
        return text


def _parse_frontmatter(source: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML frontmatter, return (dict, human-readable bullet rendering)."""
    try:
        post = frontmatter.loads(source)
    except Exception as exc:  # noqa: BLE001 — frontmatter library can raise broadly
        log.warning("frontmatter parse failed: %s", exc)
        return None, ""
    if not post.metadata:
        return None, ""
    lines = []
    for key, value in post.metadata.items():
        rendered = _render_frontmatter_value(value)
        lines.append(f"- {key}: {rendered}")
    return _jsonify(dict(post.metadata)), "\n".join(lines)


def _jsonify(value: Any) -> Any:
    """Recursively coerce YAML-parsed values into JSON-safe primitives.

    PyYAML parses `date: 2025-09-20` as a `datetime.date`, which `json.dumps`
    cannot serialize. The same goes for `datetime`, `tuple`, `set`, etc.
    """
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _render_frontmatter_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _extract_wikilinks(source: str) -> list[str]:
    """Extract unique [[Target]] wikilink targets from the body (skipping code fences)."""
    body = CODE_FENCE_RE.sub("", source)
    seen: set[str] = set()
    out: list[str] = []
    for match in WIKILINK_RE.finditer(body):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


def _extract_tags(source: str) -> list[str]:
    body = CODE_FENCE_RE.sub("", source)
    seen: set[str] = set()
    out: list[str] = []
    for match in TAG_RE.finditer(body):
        tag = f"#{match.group(1)}"
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out
