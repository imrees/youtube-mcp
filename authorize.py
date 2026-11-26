#!/usr/bin/env python3
"""Authorize the YouTubeMCP server: `.venv/bin/python authorize.py` (add --status to check)."""
import sys

from youtube_mcp.authorize import main

if __name__ == "__main__":
    sys.exit(main())
