#!/usr/bin/env python3
"""Robust season resolver for FotMob strength/style importer."""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Set

import fotmob_strength_style_importer as base


def season_variants(label: str) -> Set[str]:
    nums = re.findall(r"\d+", str(label or ""))
    out = {re.sub(r"[^0-9a-z]", "", str(label).lower())}
    if len(nums) >= 2:
        y1, y2 = nums[0], nums[1]
        out.update({f"{y1}{y2}", f"{y1[-2:]}{y2[-2:]}", y1})
    return {x for x in out if x}


def norm(v: Any) -> str:
    return re.sub(r"[^0-9a-z]", "", str(v or "").lower())


def recursive_season_id(obj: Any, label: str) -> Optional[str]:
    variants = season_variants(label)
    if isinstance(obj, str):
        return obj if norm(obj) in variants else None
    if isinstance(obj, dict):
        id_values = [obj.get(k) for k in ("seasonId", "season_id", "id") if obj.get(k) is not None]
        label_values = [obj.get(k) for k in ("name", "seasonName", "label", "year") if obj.get(k) is not None]
        # A season object often has name=26/27 and id=2026/2027 (or numeric id).
        if any(norm(v) in variants for v in label_values):
            if id_values:
                return str(id_values[0])
        # Some FotMob payloads use the season string directly as id.
        for v in id_values:
            if norm(v) in variants:
                return str(v)
        # Root metadata such as selectedSeason/latestSeason is only a hint; recurse
        # into the season collection first so a league id is never mistaken for a season id.
        for v in obj.values():
            got = recursive_season_id(v, label)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = recursive_season_id(v, label)
            if got:
                return got
    return None


base.recursive_season_id = recursive_season_id


def run_import(database_url=None):
    result = base.run_import(database_url)
    print("FOTMOB_STRENGTH_V2_RESULT", json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result


if __name__ == "__main__":
    print(json.dumps(run_import(), ensure_ascii=False, indent=2))
