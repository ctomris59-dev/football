#!/usr/bin/env python3
"""Hardened free Football-Data current Asian/total-goals bridge.

Uses the official current fixture CSV path and retries transient 5xx/429 responses.
All parsing/mapping/quality semantics remain owned by football_data_asian_bridge.
"""
from __future__ import annotations
import json, time
from typing import Any, Dict, List, Optional
import requests
import football_data_asian_bridge as base

OFFICIAL_URL="https://www.football-data.co.uk/matches/resources/fixtures.csv"

class Bridge(base.Bridge):
    def fetch_rows(self)->List[Dict[str,str]]:
        last=None
        urls=[OFFICIAL_URL]
        # Preserve an explicitly configured non-default URL as a secondary mirror.
        if base.SOURCE_URL and base.SOURCE_URL not in {OFFICIAL_URL,"https://www.football-data.co.uk/fixtures.csv"}:urls.append(base.SOURCE_URL)
        for url in urls:
            for attempt in range(4):
                try:
                    if base.REQUEST_DELAY:time.sleep(base.REQUEST_DELAY)
                    r=self.s.get(url,timeout=(5.0,base.TIMEOUT))
                    if r.status_code in {429,500,502,503,504}:
                        last=RuntimeError(f"HTTP {r.status_code} {url}");time.sleep(min(8,1.5*(attempt+1)));continue
                    r.raise_for_status();text=None
                    for enc in ("utf-8-sig","cp1252","latin-1"):
                        try:text=r.content.decode(enc);break
                        except UnicodeDecodeError:pass
                    import csv,io
                    return [{str(k).strip().lstrip("\ufeff"):(v.strip() if isinstance(v,str) else v) for k,v in row.items() if k}
                            for row in csv.DictReader(io.StringIO(text if text is not None else r.text)) if row]
                except Exception as exc:last=exc;time.sleep(min(8,2**attempt))
        raise RuntimeError(f"Football-Data official fixture feed unavailable after retries: {last}")

def run_import(database_url:Optional[str]=None)->Dict[str,Any]:
    old=base.SOURCE_URL;base.SOURCE_URL=OFFICIAL_URL
    i=Bridge(database_url)
    try:return i.run()
    finally:
        i.close();base.SOURCE_URL=old

if __name__=="__main__":print(json.dumps(run_import(),ensure_ascii=False,indent=2))
