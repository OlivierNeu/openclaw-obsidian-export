# openclaw-obsidian-export

Minimal HTTP service wrapping [zoni/obsidian-export](https://github.com/zoni/obsidian-export) (the Rust CLI that converts Obsidian-flavoured markdown to CommonMark).

Built to drop into the OpenClaw / `openclaw-notes` n8n ingestion pipelines, alongside `openclaw-gotenberg` and `openclaw-docling`.

## Why

LightRAG ingests our entire Drive corpus, including Obsidian vaults with thousands of `.md` notes. Sending those raw means LightRAG's LLM has to re-discover the relations already encoded as `[[wikilinks]]`, and the YAML frontmatter ends up as noisy literal text inside chunks.

This service:

- Normalises Obsidian `[[Note]]` / `[[Note|alias]]` / `![[embed]]` to standard CommonMark via the official `obsidian-export` binary.
- Extracts the YAML frontmatter as structured JSON **and** as a human-readable bullet list (ready to prepend to the LightRAG payload).
- Returns the outgoing wikilinks and inline `#tags` as side-car metadata.

## API

### `GET /health`

```json
{"status": "ok", "obsidian_export_version": "obsidian-export 25.3.0"}
```

### `POST /convert`

Multipart-form-data body:

| field      | type     | required | description                          |
| ---------- | -------- | -------- | ------------------------------------ |
| `file`     | file     | yes      | Raw `.md` content                    |
| `filename` | string   | no       | Override the note name (else uses the multipart filename) |

Response:

```json
{
  "content": "# Sample\n\nSee [Sekito Shitou](Sekito%20Shitou.md) ...",
  "frontmatter": {"tags": ["zen", "kusen"], "type": "kusen"},
  "frontmatter_text": "- tags: zen, kusen\n- type: kusen",
  "wikilinks_out": ["Sekito Shitou", "MOC ZEN-BOUDDHISME"],
  "tags_inline": ["#zazen", "#observation"],
  "stats": {
    "input_bytes": 1234,
    "output_bytes": 1180,
    "wikilinks_count": 2,
    "tags_count": 2
  }
}
```

Errors:

| HTTP | when                                                              |
| ---- | ----------------------------------------------------------------- |
| 400  | empty file                                                        |
| 413  | input > 10 MB                                                     |
| 500  | `obsidian-export` returned an error (stderr in `detail`)          |
| 503  | `obsidian-export` binary unreachable at `/health`                 |
| 504  | `obsidian-export` exceeded the 30 s subprocess timeout            |

## Usage from `docker-compose.yml`

```yaml
  obsidian-export:
    image: neuolivier/openclaw-obsidian-export:latest
    container_name: openclaw-obsidian-export
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
    mem_limit: 256m
```

Then from n8n: `POST http://openclaw-obsidian-export:8080/convert`.

## Build & release

```bash
docker build -t neuolivier/openclaw-obsidian-export:dev .
docker run --rm -p 8080:8080 neuolivier/openclaw-obsidian-export:dev
curl -fsS http://localhost:8080/health
```

CI builds `linux/amd64` only (upstream `obsidian-export` does not ship ARM64 Linux binaries). The image is published to:

- `ghcr.io/olivierneu/openclaw-obsidian-export`
- `docker.io/neuolivier/openclaw-obsidian-export`

with tags: `latest`, `YYYY.MM.DD`, and `obsidian-export-vX.Y.Z` (pinning the upstream binary version baked in).

## Pinning `obsidian-export`

The binary version is pinned in the Dockerfile via the `OBSIDIAN_EXPORT_VERSION` build-arg. Bumping it is a one-line change. The CI verifies the sha256 of the downloaded tarball against the upstream `.sha256` file.

## License

MIT.
