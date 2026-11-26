#!/usr/bin/env python3
"""Launcher for the YouTubeMCP server (stdio). Point your MCP client at this file.

Example: /absolute/path/.venv/bin/python /absolute/path/youtube_mcp_server.py
"""
from youtube_mcp.server import main

if __name__ == "__main__":
    main()
