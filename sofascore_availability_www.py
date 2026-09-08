#!/usr/bin/env python3
"""Use Sofascore's public www host for the optional availability importer."""
from __future__ import annotations
from typing import Any, Dict, Optional
import sofascore_availability_importer as base

base.BASE = "https://www.sofascore.com/api/v1"


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    return base.run_import(database_url)


if __name__ == "__main__":
    import json
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
