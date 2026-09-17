#!/usr/bin/env python3
"""Compatibility entrypoint for Thursday confidence/value V4 finalization."""
from thursday_opening_watch_v4 import *  # noqa: F401,F403

if __name__ == "__main__":
    import json
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
