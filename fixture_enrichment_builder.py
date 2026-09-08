#!/usr/bin/env python3
"""Build current pre-match enrichment features: player impact, style, Elo and promotion priors."""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL=os.getenv('DATABASE_URL','').strip()

SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS fixture_enrichment_snapshots(
  event_id TEXT NOT NULL,
  snapshot_hour TIMESTAMPTZ NOT NULL,
  match_date TIMESTAMPTZ NOT NULL,
  league_name TEXT NOT NULL,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL,
  home_fotmob_team_id TEXT,
  away_fotmob_team_id TEXT,
  home_expected_xi_strength DOUBLE PRECISION,
  away_expected_xi_strength DOUBLE PRECISION,
  home_top11_strength DOUBLE PRECISION,
  away_top11_strength DOUBLE PRECISION,
  home_injury_impact DOUBLE PRECISION,
  away_injury_impact DOUBLE PRECISION,
  home_key_injuries JSONB NOT NULL DEFAULT '[]'::jsonb,
  away_key_injuries JSONB NOT NULL DEFAULT '[]'::jsonb,
  home_goalkeeper_injured BOOLEAN,
  away_goalkeeper_injured BOOLEAN,
  home_style JSONB,
  away_style JSONB,
  corner_style_index DOUBLE PRECISION,
  home_elo DOUBLE PRECISION,
  away_elo DOUBLE PRECISION,
  elo_diff DOUBLE PRECISION,
  elo_abs_diff DOUBLE PRECISION,
  home_promotion_prior JSONB,
  away_promotion_prior JSONB,
  home_promoted BOOLEAN NOT NULL DEFAULT FALSE,
  away_promoted BOOLEAN NOT NULL DEFAULT FALSE,
  player_coverage DOUBLE PRECISION,
  enrichment_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,
  built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(event_id,snapshot_hour)
);
CREATE INDEX IF NOT EXISTS idx_fixture_enrichment_latest ON fixture_enrichment_snapshots(match_date,snapshot_hour DESC);
CREATE TABLE IF NOT EXISTS fixture_enrichment_runs(
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  fixtures INTEGER NOT NULL DEFAULT 0,
  player_mapped INTEGER NOT NULL DEFAULT 0,
  style_mapped INTEGER NOT NULL DEFAULT 0,
  elo_mapped INTEGER NOT NULL DEFAULT 0,
  promotion_mapped INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
'''

ALIASES={
 'man utd':'manchester united','man united':'manchester united','man city':'manchester city',
 'nottm forest':'nottingham forest','wolves':'wolverhampton wanderers','spurs':'tottenham hotspur',
 'tottenham':'tottenham hotspur','milan':'ac milan','inter':'inter milan','psg':'paris saint germain',
 'paris sg':'paris saint germain','ath bilbao':'athletic club','athletic bilbao':'athletic club',
 'mgladbach':'borussia monchengladbach','borussia m gladbach':'borussia monchengladbach',
}

def canon(v:Any)->str:
    s=unicodedata.normalize('NFKD',str(v or '')).encode('ascii','ignore').decode().lower().replace("'",'')
    s=re.sub(r'\b(fc|cf|ssc|ac|calcio|club|football club|afc)\b',' ',s)
    s=re.sub(r'[^a-z0-9]+',' ',s).strip(); s=re.sub(r'\s+',' ',s)
    return ALIASES.get(s,s)

def jf(v:Any)->Dict[str,Any]: return v if isinstance(v,dict) else {}
def jl(v:Any)->List[Any]: return v if isinstance(v,list) else []
def f(v:Any)->Optional[float]:
    try: return None if v is None else float(v)
    except Exception: return None

def latest_fotmob_fixture(conn,event_id:str):
    return conn.execute('''SELECT snapshot_hour,home_fotmob_team_id,away_fotmob_team_id,
                                  home_injured_players,away_injured_players
                           FROM fotmob_fixture_availability_snapshots
                           WHERE espn_event_id=%s ORDER BY snapshot_hour DESC LIMIT 1''',(event_id,)).fetchone()

def player_rows(conn,team_id:Optional[str])->List[Tuple]:
    if not team_id: return []
    d=conn.execute('SELECT MAX(snapshot_date) FROM fotmob_player_strength_snapshots WHERE team_id=%s',(team_id,)).fetchone()[0]
    if not d: return []
    return conn.execute('''SELECT player_id,player_name,position,minutes_played,strength_score,metrics
                           FROM fotmob_player_strength_snapshots
                           WHERE team_id=%s AND snapshot_date=%s AND strength_score IS NOT NULL''',(team_id,d)).fetchall()

def team_style(conn,team_id:Optional[str])->Optional[Dict[str,Any]]:
    if not team_id: return None
    row=conn.execute('''SELECT metrics,relative_metrics,top11_strength,player_coverage
                        FROM fotmob_team_style_snapshots WHERE team_id=%s
                        ORDER BY snapshot_date DESC LIMIT 1''',(team_id,)).fetchone()
    if not row: return None
    return {'metrics':jf(row[0]),'relative':jf(row[1]),'top11_strength':f(row[2]),'player_coverage':int(row[3] or 0)}

def player_feature(rows:List[Tuple],injured:List[Any])->Dict[str,Any]:
    if not rows:
        return {'expected':None,'top11':None,'impact':None,'key':[],'gk_injured':None,'coverage':0.0}
    inj_ids={str(x.get('id')) for x in injured if isinstance(x,dict) and x.get('id') is not None}
    inj_names={canon(x.get('name')) for x in injured if isinstance(x,dict) and x.get('name')}
    max_min=max([f(r[3]) or 0 for r in rows] or [1]) or 1
    parsed=[]
    for pid,name,pos,minutes,strength,metrics in rows:
        mins=f(minutes) or 0; st=f(strength) or 0
        importance=st*(0.30+0.70*math.sqrt(max(0.0,mins/max_min)))
        is_inj=str(pid) in inj_ids or canon(name) in inj_names
        parsed.append({'id':str(pid),'name':name,'position':pos,'minutes':mins,'strength':st,
                       'importance':importance,'injured':is_inj,'metrics':jf(metrics)})
    top=sorted(parsed,key=lambda x:(x['minutes'],x['importance']),reverse=True)[:11]
    denom=sum(x['importance'] for x in top) or 1.0
    injured_imp=sum(x['importance'] for x in parsed if x['injured'])
    impact=max(0.0,min(0.55,injured_imp/denom))
    available=sorted([x for x in parsed if not x['injured']],key=lambda x:(x['minutes'],x['importance']),reverse=True)[:11]
    top11=sum(x['strength'] for x in top)/len(top) if top else None
    expected=sum(x['strength'] for x in available)/len(available) if available else None
    key=sorted([x for x in parsed if x['injured']],key=lambda x:x['importance'],reverse=True)[:6]
    key_out=[{'id':x['id'],'name':x['name'],'position':x['position'],'importance':round(x['importance'],4),
              'strength':round(x['strength'],4),'minutes':x['minutes']} for x in key]
    gk=any(x['injured'] and any(k in str(x['position'] or '').lower() for k in ('goal','keeper','gk')) for x in parsed)
    return {'expected':expected,'top11':top11,'impact':impact,'key':key_out,'gk_injured':gk,'coverage':min(1.0,len(parsed)/18.0)}

def corner_style(style:Optional[Dict[str,Any]])->Optional[float]:
    if not style: return None
    r=jf(style.get('relative'))
    vals=[]
    weights={'corner_taken_team':0.34,'accurate_cross_team':0.20,'poss_won_att_3rd_team':0.18,
             'ontarget_scoring_att_team':0.15,'possession_percentage_team':0.08,'expected_goals_team':0.05}
    for k,w in weights.items():
        v=f(r.get(k))
        if v is not None and v>0: vals.append((w,v))
    if not vals: return None
    sw=sum(w for w,_ in vals)
    return sum(w*v for w,v in vals)/sw

def elo_for(conn,team:str,league:str,match_date:datetime)->Optional[float]:
    row=conn.execute('SELECT clubelo_club FROM clubelo_team_map WHERE system_team=%s AND league_name=%s',(team,league)).fetchone()
    if not row:
        maps=conn.execute('SELECT system_team,clubelo_club FROM clubelo_team_map WHERE league_name=%s',(league,)).fetchall()
        ct=canon(team); cand=[m for m in maps if canon(m[0])==ct]
        if not cand: return None
        club=cand[0][1]
    else: club=row[0]
    d=match_date.date()
    h=conn.execute('''SELECT elo FROM clubelo_history WHERE clubelo_club=%s AND from_date<=%s
                      AND (to_date IS NULL OR to_date>=%s) ORDER BY from_date DESC LIMIT 1''',(club,d,d)).fetchone()
    if h: return f(h[0])
    s=conn.execute('''SELECT elo FROM clubelo_daily_snapshots WHERE club=%s AND snapshot_date<=%s
                      ORDER BY snapshot_date DESC LIMIT 1''',(club,d)).fetchone()
    return f(s[0]) if s else None

def promotion(conn,team:str,league:str)->Optional[Dict[str,Any]]:
    rows=conn.execute('''SELECT team_name,source_division,source_matches,source_metrics,transferred_relative,transfer_factors
                         FROM promotion_priors WHERE target_season='2627' AND parent_league_name=%s''',(league,)).fetchall()
    ct=canon(team)
    for r in rows:
        if canon(r[0])==ct:
            return {'team_name':r[0],'source_division':r[1],'source_matches':int(r[2] or 0),
                    'source_metrics':jf(r[3]),'transferred_relative':jf(r[4]),'transfer_factors':jf(r[5])}
    return None

def build(database_url:Optional[str]=None)->Dict[str,Any]:
    db=(database_url or DATABASE_URL).strip()
    if not db: raise RuntimeError('Missing DATABASE_URL')
    with psycopg.connect(db,autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
        rid=conn.execute("INSERT INTO fixture_enrichment_runs(status) VALUES('running') RETURNING id").fetchone()[0]
        fixtures=player_mapped=style_mapped=elo_mapped=promo_mapped=0
        try:
            upcoming=conn.execute('''SELECT event_id,match_date,league_name,home_team,away_team FROM espn_upcoming
                                     WHERE is_current=TRUE AND match_date>=NOW()-INTERVAL '2 hours'
                                     AND match_date<=NOW()+INTERVAL '8 days' ORDER BY match_date''').fetchall()
            hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
            for eid,dt,league,home,away in upcoming:
                fixtures+=1
                fm=latest_fotmob_fixture(conn,eid)
                hid=aid=None; hi=[]; ai=[]
                if fm:
                    _,hid,aid,hi,ai=fm; hi=jl(hi); ai=jl(ai)
                hp=player_feature(player_rows(conn,hid),hi); ap=player_feature(player_rows(conn,aid),ai)
                if hp['coverage']>0 and ap['coverage']>0: player_mapped+=1
                hs=team_style(conn,hid); as_=team_style(conn,aid)
                hcs,acs=corner_style(hs),corner_style(as_)
                csi=(hcs+acs)/2 if hcs is not None and acs is not None else (hcs if hcs is not None else acs)
                if hs and as_: style_mapped+=1
                he,ae=elo_for(conn,home,league,dt),elo_for(conn,away,league,dt)
                if he is not None and ae is not None: elo_mapped+=1
                hpr,apr=promotion(conn,home,league),promotion(conn,away,league)
                if hpr or apr: promo_mapped+=1
                coverage_parts=[hp['coverage']>0,ap['coverage']>0,hs is not None,as_ is not None,he is not None,ae is not None]
                coverage=sum(int(x) for x in coverage_parts)/len(coverage_parts)
                conn.execute('''INSERT INTO fixture_enrichment_snapshots(
                    event_id,snapshot_hour,match_date,league_name,home_team,away_team,home_fotmob_team_id,away_fotmob_team_id,
                    home_expected_xi_strength,away_expected_xi_strength,home_top11_strength,away_top11_strength,
                    home_injury_impact,away_injury_impact,home_key_injuries,away_key_injuries,
                    home_goalkeeper_injured,away_goalkeeper_injured,home_style,away_style,corner_style_index,
                    home_elo,away_elo,elo_diff,elo_abs_diff,home_promotion_prior,away_promotion_prior,
                    home_promoted,away_promoted,player_coverage,enrichment_coverage,built_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                  ON CONFLICT(event_id,snapshot_hour) DO UPDATE SET
                    home_expected_xi_strength=EXCLUDED.home_expected_xi_strength,away_expected_xi_strength=EXCLUDED.away_expected_xi_strength,
                    home_top11_strength=EXCLUDED.home_top11_strength,away_top11_strength=EXCLUDED.away_top11_strength,
                    home_injury_impact=EXCLUDED.home_injury_impact,away_injury_impact=EXCLUDED.away_injury_impact,
                    home_key_injuries=EXCLUDED.home_key_injuries,away_key_injuries=EXCLUDED.away_key_injuries,
                    home_goalkeeper_injured=EXCLUDED.home_goalkeeper_injured,away_goalkeeper_injured=EXCLUDED.away_goalkeeper_injured,
                    home_style=EXCLUDED.home_style,away_style=EXCLUDED.away_style,corner_style_index=EXCLUDED.corner_style_index,
                    home_elo=EXCLUDED.home_elo,away_elo=EXCLUDED.away_elo,elo_diff=EXCLUDED.elo_diff,elo_abs_diff=EXCLUDED.elo_abs_diff,
                    home_promotion_prior=EXCLUDED.home_promotion_prior,away_promotion_prior=EXCLUDED.away_promotion_prior,
                    home_promoted=EXCLUDED.home_promoted,away_promoted=EXCLUDED.away_promoted,player_coverage=EXCLUDED.player_coverage,
                    enrichment_coverage=EXCLUDED.enrichment_coverage,built_at=NOW()''',
                  (eid,hour,dt,league,home,away,hid,aid,hp['expected'],ap['expected'],hp['top11'],ap['top11'],hp['impact'],ap['impact'],
                   Jsonb(hp['key']),Jsonb(ap['key']),hp['gk_injured'],ap['gk_injured'],Jsonb(hs) if hs else None,Jsonb(as_) if as_ else None,csi,
                   he,ae,(he-ae if he is not None and ae is not None else None),(abs(he-ae) if he is not None and ae is not None else None),
                   Jsonb(hpr) if hpr else None,Jsonb(apr) if apr else None,bool(hpr),bool(apr),(hp['coverage']+ap['coverage'])/2,coverage))
            conn.execute('''UPDATE fixture_enrichment_runs SET finished_at=NOW(),status='success',fixtures=%s,player_mapped=%s,
                            style_mapped=%s,elo_mapped=%s,promotion_mapped=%s,message='ok' WHERE id=%s''',
                         (fixtures,player_mapped,style_mapped,elo_mapped,promo_mapped,rid))
            result={'status':'success','fixtures':fixtures,'player_mapped':player_mapped,'style_mapped':style_mapped,
                    'elo_mapped':elo_mapped,'promotion_mapped':promo_mapped}
            print('FIXTURE_ENRICHMENT_RESULT',json.dumps(result,separators=(',',':'))); return result
        except Exception as exc:
            conn.execute("UPDATE fixture_enrichment_runs SET finished_at=NOW(),status='failed',message=%s WHERE id=%s",(str(exc)[:1000],rid)); raise

if __name__=='__main__':
    print(json.dumps(build(),ensure_ascii=False,indent=2))
