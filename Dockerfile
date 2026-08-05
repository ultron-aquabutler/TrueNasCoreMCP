FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN useradd -m -u 1000 truenas

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir -e .

# Wrapper script reads _FILE env vars (docker secrets) and exports them
# as plain env vars before exec'ing the Python wrapper. The upstream
# pydantic-settings layer reads TRUENAS_API_KEY directly with no _FILE
# awareness, so this shim is needed for compose stacks that use secrets.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY run_server.py /app/run_server.py
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chmod 755 /app /app/run_server.py \
    && chown -R truenas:truenas /app

# run_server.py is the wrapper that:
#   - Reads the API key from the secret file (defence-in-depth alongside the
#     entrypoint script, both end up exporting TRUENAS_API_KEY)
#   - Forces streamable-http transport and binds 0.0.0.0:8000
#   - Disables FastMCP DNS rebinding protection (required behind Traefik with
#     TLS termination — FastMCP auto-enables it when host defaults to 127.0.0.1
#     and refuses requests with Host: wamcp-truenas.loc.wallacearizona.us)
# Stays out of the upstream PR scope; lives in the fork until upstream
# supports secrets natively.

EXPOSE 8000
USER truenas
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/mcp || exit 1
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "/app/run_server.py"]
