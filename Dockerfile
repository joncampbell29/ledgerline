# One image for the web app, the sync job and the MCP server. See compose.yaml.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Run as an unprivileged user. UID 1000 matches the first user on most Linux/WSL hosts,
# so files written to bind-mounted folders stay owned by you.
RUN useradd --uid 1000 --create-home app \
 && mkdir -p /data /secrets /audit \
 && chown app:app /data /secrets /audit

ENV PATH="/app/.venv/bin:$PATH" \
    LEDGERLINE_DATA_DIR=/data \
    LEDGERLINE_SECRETS_DIR=/secrets \
    LEDGERLINE_AUDIT_DIR=/audit

USER app
EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]