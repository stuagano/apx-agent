#!/usr/bin/env python
"""Run the Databricks MCP Server."""

import argparse
import logging
import os
import sys

if os.environ.get("DATABRICKS_MCP_DEBUG"):
    logging.basicConfig(
        level=logging.DEBUG,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

from databricks_mcp_server.server import mcp

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Databricks MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "http", "streamable-http"],
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host for HTTP/SSE transports (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port for HTTP/SSE transports (default: 8080)")
    args = parser.parse_args()

    kwargs = {}
    if args.transport in ("sse", "http", "streamable-http"):
        kwargs["host"] = args.host
        kwargs["port"] = args.port

    mcp.run(transport=args.transport, **kwargs)
