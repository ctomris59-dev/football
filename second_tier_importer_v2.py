#!/usr/bin/env python3
"""Second-tier importer v2.

Extends the existing free Football-Data second-tier history with 2023/24 so
promotion carryover can be tested out-of-sample before it is used in production.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import second_tier_importer as base

SEASONS = ["2324", "2425", "2526"]


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    previous = list(base.SEASONS)
    try:
        base.SEASONS[:] = SEASONS
        return base.run_import(database_url)
    finally:
        base.SEASONS[:] = previous


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
