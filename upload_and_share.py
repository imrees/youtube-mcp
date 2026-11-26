#!/usr/bin/env python3
"""Finder Quick Action "Upload & Share" (see docs/QUICK_ACTION.md). `.venv/bin/python upload_and_share.py --help`"""
import sys

from youtube_mcp.quick_action import main

if __name__ == "__main__":
    sys.exit(main())
