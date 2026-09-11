FROM python:3.12-slim

WORKDIR /app

# Install system deps for sentence-transformers
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gcc && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY apsimo/ ./apsimo/
COPY colony_sidecar/ ./colony_sidecar/

RUN pip install --no-cache-dir .

# Create state directory
RUN mkdir -p /var/lib/colony
COPY docker-entrypoint.sh /usr/local/bin/apsimo-container
RUN chmod 755 /usr/local/bin/apsimo-container

EXPOSE 7777

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:7777/v1/host/health || exit 1

ENTRYPOINT ["apsimo-container"]
CMD ["apsimo", "start"]
