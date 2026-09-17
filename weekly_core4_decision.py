#!/usr/bin/env python3
"""Compatibility entrypoint for the production evidence-complete V4 engine."""
from confidence_core_v4 import *  # noqa: F401,F403

if __name__ == "__main__":
    import json
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))
