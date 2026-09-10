#!/usr/bin/env python3
"""Policy freeze and model-change control for the 2026/27 live holdout.

The freeze is an audit snapshot, not a permanent ban on future improvement. A new
challenger may enter production only when its registry evidence proves that it was
validated on the predeclared historical OOS folds and did not use 2026/27 outcomes
for tuning. Bug fixes and infrastructure-only changes are logged separately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
FREEZE_PATH = Path(__file__).with_name("policy_freeze_2026_27.json")
ALLOWED_CHANGE_TYPES = {"BUG_FIX", "CHALLENGER", "INFRA_ONLY"}
REQUIRED_TEST_SEASONS = ("2425", "2526")
LIVE_HOLDOUT_SEASON = "2627"
REQUIRED_GATE_VERSION = "two-fold-week-block-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_freeze_registry(
 freeze_key TEXT PRIMARY KEY,
 installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 source_sha TEXT NOT NULL,
 config_hash TEXT NOT NULL,
 config JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS model_change_log(
 id BIGSERIAL PRIMARY KEY,
 changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 change_type TEXT NOT NULL,
 source_sha TEXT,
 description TEXT NOT NULL,
 behavior_change BOOLEAN NOT NULL,
 evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
 CHECK (change_type IN ('BUG_FIX','CHALLENGER','INFRA_ONLY'))
);
"""


def load_freeze(path: Path = FREEZE_PATH) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def freeze_hash(config: Dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def activation_evidence_is_safe(metrics: Any) -> bool:
    """Return True only for a predeclared, holdout-safe challenger gate result."""
    if not isinstance(metrics, dict):
        return False
    protocol = metrics.get("validation_protocol")
    if not isinstance(protocol, dict):
        return False
    test_seasons = tuple(str(x) for x in protocol.get("test_seasons") or [])
    return bool(
        metrics.get("gate_passed") is True
        and str(protocol.get("gate_version") or "") == REQUIRED_GATE_VERSION
        and test_seasons == REQUIRED_TEST_SEASONS
        and protocol.get("holdout_excluded") is True
        and str(protocol.get("live_holdout_season") or "") == LIVE_HOLDOUT_SEASON
    )


def guarded_activation_mode(active_mode: Any, metrics: Any) -> str:
    mode = str(active_mode or "v1_only")
    if mode == "v1_only":
        return mode
    return mode if activation_evidence_is_safe(metrics) else "v1_only"


def registry_activation_mode(conn, policy_key: str = "four-layer-v5") -> str:
    """Read a challenger registry row but fail closed unless evidence meets the new gate."""
    try:
        row = conn.execute(
            "SELECT active_mode,metrics FROM policy_activation_registry WHERE policy_key=%s ORDER BY validated_at DESC LIMIT 1",
            (policy_key,),
        ).fetchone()
    except Exception:
        return "v1_only"
    if not row:
        return "v1_only"
    return guarded_activation_mode(row[0], row[1])


def install_freeze(database_url: Optional[str] = None) -> Dict[str, Any]:
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    config = load_freeze()
    digest = freeze_hash(config)
    import psycopg
    from psycopg.types.json import Jsonb
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        conn.execute(
            """INSERT INTO policy_freeze_registry(freeze_key,source_sha,config_hash,config)
               VALUES(%s,%s,%s,%s)
               ON CONFLICT(freeze_key) DO UPDATE SET source_sha=EXCLUDED.source_sha,
                   config_hash=EXCLUDED.config_hash,config=EXCLUDED.config,installed_at=NOW()""",
            (str(config["freeze_key"]), str(config["source_sha"]), digest, Jsonb(config)),
        )
    return {"status": "installed", "freeze_key": config["freeze_key"], "config_hash": digest}


def record_change(
    change_type: str,
    description: str,
    *,
    behavior_change: bool,
    source_sha: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
    database_url: Optional[str] = None,
) -> Dict[str, Any]:
    kind = str(change_type or "").upper()
    if kind not in ALLOWED_CHANGE_TYPES:
        raise ValueError(f"change_type must be one of {sorted(ALLOWED_CHANGE_TYPES)}")
    if kind == "INFRA_ONLY" and behavior_change:
        raise ValueError("INFRA_ONLY cannot declare behavior_change=True")
    if kind == "CHALLENGER" and behavior_change and not activation_evidence_is_safe(evidence or {}):
        raise ValueError("CHALLENGER behavior change requires a passed two-fold holdout-safe gate")
    db = (database_url or DATABASE_URL).strip()
    if not db:
        raise RuntimeError("Missing DATABASE_URL")
    import psycopg
    from psycopg.types.json import Jsonb
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(SCHEMA)
        row_id = conn.execute(
            """INSERT INTO model_change_log(change_type,source_sha,description,behavior_change,evidence)
               VALUES(%s,%s,%s,%s,%s) RETURNING id""",
            (kind, source_sha, description, bool(behavior_change), Jsonb(evidence or {})),
        ).fetchone()[0]
    return {"status": "logged", "id": int(row_id), "change_type": kind}


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install-freeze")
    log = sub.add_parser("log")
    log.add_argument("change_type", choices=sorted(ALLOWED_CHANGE_TYPES))
    log.add_argument("description")
    log.add_argument("--behavior-change", action="store_true")
    log.add_argument("--source-sha")
    args = parser.parse_args(argv)
    if args.command == "install-freeze":
        print(json.dumps(install_freeze(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(record_change(
        args.change_type,
        args.description,
        behavior_change=args.behavior_change,
        source_sha=args.source_sha,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
