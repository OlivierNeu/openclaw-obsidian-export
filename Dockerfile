# syntax=docker/dockerfile:1.7

# openclaw-obsidian-export — minimal HTTP wrapper around zoni/obsidian-export.
# Goal: receive a single Obsidian markdown file over HTTP multipart, run the
# official obsidian-export Rust binary on it, and return CommonMark + parsed
# frontmatter + extracted wikilinks/tags as JSON.

FROM python:3.12-slim AS runtime

ARG OBSIDIAN_EXPORT_VERSION=v25.3.0
ARG TARGETARCH

LABEL org.opencontainers.image.title="openclaw-obsidian-export"
LABEL org.opencontainers.image.description="HTTP service wrapping zoni/obsidian-export for n8n / OpenClaw pipelines"
LABEL org.opencontainers.image.source="https://github.com/olivierneu/openclaw-obsidian-export"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.base.name="python:3.12-slim"

# Install runtime deps (curl for healthcheck only, kept lean).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Download the official obsidian-export Rust binary from GitHub Releases.
# Only x86_64-unknown-linux-gnu is published upstream; ARM64 builds would
# require a multi-stage cargo build (not implemented — Synology NAS targets
# in this project are x86_64).
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) OE_ARCH="x86_64-unknown-linux-gnu" ;; \
      *) echo "FATAL: TARGETARCH=${TARGETARCH} not supported (upstream ships only x86_64)" >&2; exit 1 ;; \
    esac; \
    OE_TARBALL="obsidian-export-${OE_ARCH}.tar.xz"; \
    OE_URL="https://github.com/zoni/obsidian-export/releases/download/${OBSIDIAN_EXPORT_VERSION}/${OE_TARBALL}"; \
    curl -fsSL -o "/tmp/${OE_TARBALL}" "${OE_URL}"; \
    curl -fsSL -o "/tmp/${OE_TARBALL}.sha256" "${OE_URL}.sha256"; \
    cd /tmp; \
    sha256sum -c "${OE_TARBALL}.sha256"; \
    tar -xJf "${OE_TARBALL}" --strip-components=1 -C /usr/local/bin "obsidian-export-${OE_ARCH}/obsidian-export"; \
    chmod +x /usr/local/bin/obsidian-export; \
    rm -f "/tmp/${OE_TARBALL}" "/tmp/${OE_TARBALL}.sha256"; \
    /usr/local/bin/obsidian-export --version

# Drop xz-utils after extraction — only curl is needed at runtime (healthcheck).
RUN apt-get purge -y --auto-remove xz-utils

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

# Non-root user. n8n calls us over the docker bridge, no need for root.
RUN useradd --create-home --shell /bin/bash --uid 10001 app \
 && chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
