#!/usr/bin/env python3
"""Compatibility entrypoint for Thursday confidence/value V5 finalization."""
import thursday_opening_watch_v4 as _watch
from confidence_core_v5 import POLICY_VERSION, build as _build_v5

# Reuse the proven V4 collection/finalization plumbing while swapping only the
# publication engine/source identity to V5.
CURRENT_SOURCE = f"{POLICY_VERSION}+iddaa_official+multibook+opponent_xg+dixon_coles"
_watch.build_core = _build_v5
_watch.POLICY_VERSION = POLICY_VERSION
_watch.CURRENT_SOURCE = CURRENT_SOURCE

main = _watch.main
latest_final = _watch.latest_final

if __name__ == "__main__":
    import json
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
