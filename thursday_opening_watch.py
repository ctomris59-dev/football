#!/usr/bin/env python3
"""Compatibility entrypoint for Thursday confidence/value V3 finalization."""
from thursday_opening_watch_v3 import *  # noqa: F401,F403

if __name__ == "__main__":
    import json
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
