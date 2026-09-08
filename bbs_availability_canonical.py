#!/usr/bin/env python3
"""Canonical Big Five league slugs for Big Balls Sports Data availability feed."""
from __future__ import annotations
from typing import Any, Dict, Optional
import bbs_availability_importer as base

# Slugs shown by the current Big Balls soccer/injury documentation.
base.LEAGUES = [
    ("epl", "Premier League"),
    ("laliga", "La Liga"),
    ("seriea", "Serie A"),
    ("bundesliga", "Bundesliga"),
    ("ligue1", "Ligue 1"),
]


def run_import(database_url: Optional[str] = None) -> Dict[str, Any]:
    return base.run_import(database_url)


if __name__ == "__main__":
    import json
    print(json.dumps(run_import(), ensure_ascii=False, indent=2, default=str))
