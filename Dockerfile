FROM python:3.12-slim

LABEL org.opencontainers.image.title="bcb-mcp" \
      org.opencontainers.image.description="MCP server for Brazilian Central Bank (BCB-SGS) time series" \
      org.opencontainers.image.source="https://github.com/msperlin/bcb-mcp" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BCB_MCP_CACHE_DIR=/data \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

# Default: Streamable HTTP so the container is reachable from the host.
CMD ["bcb-mcp", "--http", "--host", "0.0.0.0", "--port", "8000", "--path", "/mcp"]
