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
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import datetime as _dt

import frontmatter
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("obsidian-export-svc")

OBSIDIAN_EXPORT_BIN = "/usr/local/bin/obsidian-export"
PANDOC_BIN = "pandoc"
MAX_INPUT_BYTES = 10 * 1024 * 1024  # 10 MB — Obsidian notes are tiny in practice
# epub reference books (Watzlawick, Mouravieff, ...) are much larger than a
# note and may embed images. Allow up to 80 MB (Flatten caps Drive at 100 MB).
MAX_EPUB_BYTES = 80 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
# Sample at most this many pages for the routing heuristic — extract_text /
# page.images on a 500-page book is slow and unnecessary just to decide a
# route. Pages are sampled EVENLY across the whole document (not the first
# N): a long PDF with a textual cover/TOC and figures only after page 30
# must still be seen as figure-rich.
PDF_STATS_SAMPLE_PAGES = 40
# Where /persist-md writes verbatim markdown caches. Host bind-mount via
# docker-compose; see openclaw-notes/docker-compose.yml.
KNOWLEDGE_MD_ROOT = Path("/data/knowledge-md")
MAX_PERSIST_NAME_LEN = 180   # leaves room for "--{md5=32}.md" within FS limits
ARCHIVE_DIRNAME = "_archive"  # orphan verbatim files are MOVED here, not deleted
# Trailing md5 in a cache filename: "<name>--<md5>.md".
CACHE_MD5_RE = re.compile(r"--([a-f0-9]{8,64})\.md$")
# Route to Mistral only when images are DENSE per page (figures/diagrams the
# RAG must interpret). A scanned document is ≈1 full-page image per page —
# that is NOT this case: docling+OCR handles scans locally for free. Using a
# per-page density (not a raw count) is what distinguishes a figure-rich deck
# from a long scanned book. Tunable; the n8n IF can override using the raw
# numbers returned alongside `recommend`.
MISTRAL_IMAGES_PER_PAGE_THRESHOLD = 2.5

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

        fallback_reason: str | None = None
        try:
            subprocess.run(
                [OBSIDIAN_EXPORT_BIN, str(vault_dir), str(out_dir)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            # obsidian-export refuses some real-world inputs from human-edited
            # vaults — most commonly malformed YAML frontmatter (e.g. plain
            # text between --- markers instead of a key:value mapping). Rather
            # than failing the whole pipeline item, fall back to the source
            # markdown so the document is still ingested into LightRAG, just
            # without CommonMark normalisation. Wikilink / frontmatter /
            # tag extraction below still runs against the raw source.
            # Extract the "Error:" block from stderr (which may be preceded
            # by unrelated wikilink "Warning:" lines for orphan references).
            stderr = exc.stderr or ""
            error_lines = []
            seen_error = False
            for line in stderr.splitlines():
                if line.startswith("Error:"):
                    seen_error = True
                if seen_error:
                    error_lines.append(line)
                    if len(error_lines) >= 4:
                        break
            error_block = " | ".join(error_lines) if error_lines else stderr.splitlines()[0] if stderr else "unknown error"
            fallback_reason = ("obsidian-export failed: " + error_block)[:400]
            log.warning(
                "obsidian-export failed on %s (status=%s) — falling back to raw input. stderr=%s",
                safe_name, exc.returncode, stderr[:300],
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="obsidian-export timed out") from exc

        out_file = out_dir / safe_name
        if not out_file.exists():
            # obsidian-export sometimes drops the file when its content is
            # entirely filtered (e.g. only embeds with no resolution) or when
            # the converter aborted before writing. Fall back to the input.
            if fallback_reason is None:
                fallback_reason = "obsidian-export produced no output"
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
            "warning": fallback_reason,
            "stats": {
                "input_bytes": len(raw),
                "output_bytes": len(converted.encode("utf-8")),
                "wikilinks_count": len(wikilinks),
                "tags_count": len(tags),
                "fallback_used": fallback_reason is not None,
            },
        }
    )


@app.post("/convert-epub")
async def convert_epub(
    file: UploadFile = File(...),
    filename: str | None = Form(default=None),
) -> JSONResponse:
    """Convert an .epub book to GitHub-flavoured markdown via pandoc.

    Returns the SAME JSON contract as /convert so the n8n markdown branch
    (Prepare LightRAG (md)) can consume epub and Obsidian notes uniformly.
    Replaces the broken epub -> Gotenberg/LibreOffice -> PDF -> OCR path
    (LibreOffice mangles epub into a 1-page stub with no text layer).
    """
    raw = await file.read()
    if len(raw) > MAX_EPUB_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"epub too large: {len(raw)} bytes > {MAX_EPUB_BYTES}",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    with tempfile.TemporaryDirectory(prefix="epub_") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "book.epub"  # ASCII path: Unicode title stays out of argv
        out = tmp_path / "out.md"
        src.write_bytes(raw)

        # Force a UTF-8 locale for the child too (belt-and-braces with the
        # image-level ENV) so accented book titles round-trip.
        env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        try:
            proc = subprocess.run(
                [
                    PANDOC_BIN,
                    str(src),
                    "-f", "epub",
                    "-t", "gfm",
                    "--wrap=none",
                    "--markdown-headings=atx",
                    "-o", str(out),
                ],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().splitlines()
            detail = " | ".join(stderr[:4]) if stderr else "pandoc failed"
            log.warning("pandoc epub conversion failed: %s", detail)
            raise HTTPException(
                status_code=422, detail=f"pandoc epub conversion failed: {detail}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(
                status_code=504, detail="pandoc epub conversion timed out"
            ) from exc

        if not out.exists():
            raise HTTPException(
                status_code=422, detail="pandoc produced no output for this epub"
            )
        content = out.read_text(encoding="utf-8", errors="replace").strip()

    if not content:
        # A valid-looking epub that yields no text (DRM, images-only, broken
        # container) is useless for the RAG — fail loud so the n8n pipeline
        # counts it and the post-Save-Token alert surfaces it.
        raise HTTPException(
            status_code=422,
            detail="epub converted to empty markdown (no extractable text)",
        )

    fm_data, fm_human = _epub_metadata(raw)

    return JSONResponse(
        {
            "content": content,
            "frontmatter": fm_data,
            "frontmatter_text": fm_human,
            "wikilinks_out": [],   # epub has no Obsidian wikilinks
            "tags_inline": [],     # epub has no Obsidian inline tags
            "warning": None,
            "stats": {
                "input_bytes": len(raw),
                "output_bytes": len(content.encode("utf-8")),
                "wikilinks_count": 0,
                "tags_count": 0,
                "fallback_used": False,
            },
        }
    )


@app.post("/pdf-stats")
async def pdf_stats(file: UploadFile = File(...)) -> JSONResponse:
    """Cheap routing heuristic: is this PDF text-dominant or image-rich?

    Returns raw facts (pages, image_count, per-page densities, text length)
    plus a `recommend` field. The n8n IF makes the final call with a
    tunable threshold so the route can be retuned without rebuilding.

    Routing intent:
      - text-dominant / scanned text  -> docling-serve (local, 0 cost)
      - figure/diagram-rich (images we want INTERPRETED) -> Mistral OCR (paid)
      - unparseable here -> docling (safe cheap default, never default-to-paid)
    """
    import io

    raw = await file.read()
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413, detail=f"pdf too large: {len(raw)} bytes"
        )
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    try:
        reader = PdfReader(io.BytesIO(raw))
        total_pages = len(reader.pages)
        # Evenly-spaced indices across the WHOLE document so figures that
        # only appear late (after a textual cover/TOC) are still sampled.
        if total_pages <= PDF_STATS_SAMPLE_PAGES:
            indices = list(range(total_pages))
        else:
            step = total_pages / PDF_STATS_SAMPLE_PAGES
            indices = sorted(
                {int(i * step) for i in range(PDF_STATS_SAMPLE_PAGES)}
            )
        sampled = len(indices) or 1
        text_chars = 0
        image_count = 0
        for idx in indices:
            page = reader.pages[idx]
            try:
                text_chars += len((page.extract_text() or "").strip())
            except Exception:  # noqa: BLE001 — a bad page must not 500 the route
                pass
            try:
                image_count += len(page.images)
            except Exception:  # noqa: BLE001
                pass

        images_per_page = round(image_count / sampled, 3)
        text_chars_per_page = round(text_chars / sampled, 1)

        # Decide on per-page image DENSITY, not raw count: a scanned book of
        # N pages has image_count==N (≈1 full-page image/page) and must stay
        # on free docling+OCR. Only a genuinely figure-dense document
        # (several distinct images per page) is worth paid Mistral image
        # interpretation. text_chars is intentionally NOT a gate here —
        # gating on "little text" is exactly what mis-routed scans before.
        recommend = (
            "mistral"
            if images_per_page >= MISTRAL_IMAGES_PER_PAGE_THRESHOLD
            else "docling"
        )
        return JSONResponse(
            {
                "pages": total_pages,
                "sampled_pages": sampled,
                "image_count": image_count,
                "images_per_page": images_per_page,
                "text_chars": text_chars,
                "text_chars_per_page": text_chars_per_page,
                "recommend": recommend,
                "parse_error": None,
            }
        )
    except Exception as exc:  # noqa: BLE001 — encrypted/corrupt PDF
        log.warning("pdf-stats parse failed, defaulting to docling: %s", exc)
        return JSONResponse(
            {
                "pages": 0,
                "sampled_pages": 0,
                "image_count": 0,
                "images_per_page": 0,
                "text_chars": 0,
                "text_chars_per_page": 0,
                "recommend": "docling",
                "parse_error": str(exc)[:200],
            }
        )


@app.post("/persist-md")
async def persist_md(payload: dict[str, Any]) -> JSONResponse:
    """Persist a converted markdown to the NAS verbatim cache.

    Lets the agent quote / grep / open in Obsidian the actual source
    extract of a LightRAG-indexed document. The file is a flat .md with a
    YAML frontmatter carrying every Drive metadata field so traceability
    survives outside LightRAG. Identity = `<safe-name>--<md5>.md` so a
    human can browse by name AND lookup by md5 via shell glob.

    The pipeline calls this in a fire-and-forget Code node — persistence
    failure must NOT block ingestion (LightRAG remains the source of
    truth; this is a derived cache).
    """
    owner = str(payload.get("owner", "")).strip()
    if not owner or not re.match(r"^[a-z0-9][a-z0-9_\-]{0,30}$", owner):
        raise HTTPException(status_code=400, detail="invalid 'owner'")
    md5 = str(payload.get("md5", "")).strip().lower()
    if not re.match(r"^[a-f0-9]{8,64}$", md5):
        raise HTTPException(status_code=400, detail="invalid 'md5'")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="empty 'content'")

    file_name = payload.get("fileName") or "doc"
    safe = _safe_persist_name(file_name)
    filename = f"{safe}--{md5}.md"

    owner_dir = KNOWLEDGE_MD_ROOT / owner
    owner_dir.mkdir(parents=True, exist_ok=True)
    target = owner_dir / filename

    frontmatter_fields = [
        ("fileId", payload.get("fileId")),
        ("fileName", file_name),
        ("fileUrl", payload.get("fileUrl")),
        ("fileMimeType", payload.get("fileMimeType")),
        ("fileExtension", payload.get("fileExtension")),
        ("fileSize", payload.get("fileSize")),
        ("fileCreatedTime", payload.get("fileCreatedTime")),
        ("fileModifiedTime", payload.get("fileModifiedTime")),
        ("fileOwner", payload.get("fileOwner")),
        ("fileOwnerEmail", payload.get("fileOwnerEmail")),
        ("md5", md5),
        ("owner", owner),
        ("converter", payload.get("converter")),
        ("ingestedAt", payload.get("ingestedAt")),
    ]
    lines = ["---"]
    for k, v in frontmatter_fields:
        if v is None or v == "":
            continue
        # Always-quoted scalar: safe under any value (colons, hashes, etc).
        s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
        lines.append(f'{k}: "{s}"')
    lines.append("---")
    lines.append("")
    body = "\n".join(lines) + content.rstrip() + "\n"

    # Atomic write so a concurrent reader never sees a half-written file.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        pass
    tmp.replace(target)

    return JSONResponse(
        {
            "path": str(target),
            "filename": filename,
            "bytes": len(body.encode("utf-8")),
        }
    )


def _safe_persist_name(name: str) -> str:
    """Sanitise a Drive filename for filesystem use, preserving readability."""
    base = Path(name).stem if "." in name else name
    base = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip()
    base = re.sub(r"\s+", " ", base)
    base = re.sub(r"_+", "_", base)
    return (base or "doc")[:MAX_PERSIST_NAME_LEN]


@app.get("/cache-list")
def cache_list(owner: str) -> JSONResponse:
    """List the verbatim cache for an owner: every live (non-archived) .md
    with the md5 parsed from its filename. Drives the reconciliation
    workflow's level-2 diff (cache vs Drive-live). Filename-only — no file
    reads — so it stays cheap even at several thousand files.
    """
    if not re.match(r"^[a-z0-9][a-z0-9_\-]{0,30}$", owner):
        raise HTTPException(status_code=400, detail="invalid 'owner'")
    owner_dir = KNOWLEDGE_MD_ROOT / owner
    files = []
    if owner_dir.is_dir():
        for entry in owner_dir.iterdir():
            if entry.is_dir():
                continue  # skips _archive/
            m = CACHE_MD5_RE.search(entry.name)
            if not m:
                continue
            files.append({"filename": entry.name, "md5": m.group(1)})
    return JSONResponse({"owner": owner, "count": len(files), "files": files})


@app.post("/cache-archive")
async def cache_archive(payload: dict[str, Any]) -> JSONResponse:
    """Move orphan cache files into <owner>/_archive/ (tombstone, not delete).

    Called by the reconciliation workflow for files whose md5 is no longer
    on the Drive. Idempotent: a filename already gone / already archived is
    silently counted as done. Path-traversal is blocked (basename only).
    """
    owner = str(payload.get("owner", "")).strip()
    if not re.match(r"^[a-z0-9][a-z0-9_\-]{0,30}$", owner):
        raise HTTPException(status_code=400, detail="invalid 'owner'")
    filenames = payload.get("filenames")
    if not isinstance(filenames, list):
        raise HTTPException(status_code=400, detail="'filenames' must be a list")

    owner_dir = KNOWLEDGE_MD_ROOT / owner
    archive_dir = owner_dir / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived, skipped = [], []
    for raw_name in filenames:
        name = Path(str(raw_name)).name  # basename only — no traversal
        if not name or not CACHE_MD5_RE.search(name):
            skipped.append(str(raw_name))
            continue
        src = owner_dir / name
        if not src.is_file():
            skipped.append(name)  # already gone / already archived
            continue
        src.replace(archive_dir / name)
        archived.append(name)

    return JSONResponse(
        {"owner": owner, "archived": len(archived), "skipped": len(skipped)}
    )


def _epub_metadata(raw: bytes) -> tuple[dict[str, Any] | None, str]:
    """Best-effort title/creator/language from the epub OPF. Never raises."""
    try:
        import io

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            container = zf.read("META-INF/container.xml")
            cns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            opf_path = ET.fromstring(container).find(
                ".//c:rootfile", cns
            ).attrib["full-path"]
            opf = ET.fromstring(zf.read(opf_path))
            dc = "{http://purl.org/dc/elements/1.1/}"
            meta: dict[str, Any] = {}
            for tag in ("title", "creator", "language", "publisher", "date"):
                el = opf.find(f".//{dc}{tag}")
                if el is not None and (el.text or "").strip():
                    meta[tag] = el.text.strip()
        if not meta:
            return None, ""
        human = "\n".join(f"- {k}: {v}" for k, v in meta.items())
        return meta, human
    except Exception as exc:  # noqa: BLE001 — metadata is optional, never fatal
        log.warning("epub metadata extraction failed: %s", exc)
        return None, ""


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
