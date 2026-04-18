FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python deps
COPY sidecar/pyproject.toml sidecar/
RUN pip install --no-cache-dir -e "./sidecar[neo4j,lancedb]"

# Copy the sidecar code
COPY sidecar/ sidecar/

# Default env
ENV COLONY_SIDECAR_HOST=0.0.0.0
ENV COLONY_SIDECAR_PORT=7777
ENV LOG_LEVEL=info

EXPOSE 7777

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:7777/v1/host/health', timeout=3)" || exit 1

CMD ["colony", "start"]
