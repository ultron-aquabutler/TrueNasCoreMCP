#!/usr/bin/env python3
"""Wrapper that runs the TrueNAS MCP Server with streamable-http transport.

Fixes applied (2026-07-01):
- DNS rebinding protection disabled via TransportSecuritySettings
  (auto-enables when FastMCP host defaults to 127.0.0.1 and Traefik
   sends wamcp-truenas.loc.wallacearizona.us as Host header, causing 421)
"""
import sys
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("truenas-mcp-wrapper")

try:
    from truenas_mcp_server.server import create_server, get_settings
    from mcp.server.transport_security import TransportSecuritySettings
    import uvicorn
    import anyio

    # Read API key from file
    api_key_file = os.environ.get("TRUENAS_API_KEY_FILE")
    if api_key_file and os.path.isfile(api_key_file):
        with open(api_key_file) as f:
            os.environ["TRUENAS_API_KEY"] = f.read().strip()
        logger.info(f"Read API key from {api_key_file}")

    # Create the server and force streamable-http transport
    server = create_server()

    # Disable DNS rebinding protection — we're behind Traefik with TLS
    # This is set AFTER create_server() because FastMCP auto-enables it
    # when host defaults to 127.0.0.1 during __init__
    server.mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    # Override settings for proper HTTP binding
    server.mcp.settings.host = "0.0.0.0"
    server.mcp.settings.port = 8000
    server.mcp.settings.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    logger.info(f"Starting TrueNAS MCP Server on {server.mcp.settings.host}:{server.mcp.settings.port}")
    logger.info(f"Transport: streamable-http at {server.mcp.settings.streamable_http_path}")

    # Use the streamable-http async runner directly
    anyio.run(server.mcp.run_streamable_http_async)

except Exception as e:
    logger.error(f"Fatal error: {e}")
    sys.exit(1)
