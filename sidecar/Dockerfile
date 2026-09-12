FROM python:3.12-slim

WORKDIR /app

# Install system deps for sentence-transformers
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gcc && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY pacomind/ ./pacomind/

RUN pip install --no-cache-dir .

# Create state directory
RUN mkdir -p /var/lib/pacomind
COPY docker-entrypoint.sh /usr/local/bin/pacomind-container
RUN chmod 755 /usr/local/bin/pacomind-container

EXPOSE 7777

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:7777/v1/host/health || exit 1

ENTRYPOINT ["pacomind-container"]
CMD ["pacomind", "start"]
