#!/usr/bin/env python3
"""Explainable Big Five probability engine (xG-aware v2).

Markets:
- Over/Under 2.5 goals
- BTTS Yes/No
- Over/Under 8.5 total corners

Uses recency-weighted team form, home/away splits, league shrinkage, Poisson
probabilities and, when sufficiently available, Understat xG/xGA. Callers must
pass only information known before the fixture being scored.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_RECENT_MATCHES = 18
DEFAULT_HALF_LIFE_MATCHES = 7.0
DEFAULT_PRIOR_MATCHES = 7.0
MIN_XG_SAMPLES = 3

@dataclass(frozen=True)
class Prediction:
    p_over_2_5: float
    p_btts: float
    p_corners_over_8_5: float
    lambda_home_goals: float
    lambda_away_goals: float
    lambda_total_corners: float
    home_sample: int
    away_sample: int
    data_quality: float
    xg_used: bool = False

    def as_dict(self) -> Dict[str, float | int | bool]:
        return {
            "p_over_2_5": self.p_over_2_5,
            "p_under_2_5": 1.0 - self.p_over_2_5,
            "p_btts": self.p_btts,
            "p_btts_no": 1.0 - self.p_btts,
            "p_corners_over_8_5": self.p_corners_over_8_5,
            "p_corners_under_8_5": 1.0 - self.p_corners_over_8_5,
            "lambda_home_goals": self.lambda_home_goals,
            "lambda_away_goals": self.lambda_away_goals,
            "lambda_total_corners": self.lambda_total_corners,
            "home_sample": self.home_sample,
            "away_sample": self.away_sample,
            "data_quality": self.data_quality,
            "xg_used": self.xg_used,
        }

def _f(value: Any) -> Optional[float]:
    if value is None: return None
    try: return float(value)
    except (TypeError, ValueError): return None

def _clip(x: float, lo: float, hi: float) -> float: return min(hi, max(lo, x))

def _weighted_mean(values: Sequence[Optional[float]], half_life: float = DEFAULT_HALF_LIFE_MATCHES) -> Tuple[Optional[float], int]:
    total=weights=0.0; count=0; decay=math.exp(math.log(0.5)/max(0.1,half_life))
    for i,value in enumerate(values):
        if value is None: continue
        w=decay**i; total += value*w; weights += w; count += 1
    return ((total/weights) if count and weights>0 else None, count)

def _shrink(observed: Optional[float], n: int, prior: float, prior_matches: float = DEFAULT_PRIOR_MATCHES) -> float:
    if observed is None or n<=0: return prior
    return (observed*n + prior*prior_matches)/(n+prior_matches)

def _poisson_cdf(k:int,lam:float)->float:
    lam=max(1e-9,lam); term=math.exp(-lam); acc=term
    for i in range(1,k+1): term*=lam/i; acc+=term
    return _clip(acc,0.0,1.0)

def poisson_prob_over(line_floor:int,lam:float)->float: return _clip(1.0-_poisson_cdf(line_floor,lam),0.0,1.0)

def _league_baselines(history: Sequence[Dict[str, Any]]) -> Dict[str,float]:
    def avg(key:str,default:float)->float:
        vals=[_f(m.get(key)) for m in history]; vals=[v for v in vals if v is not None]
        return sum(vals)/len(vals) if vals else default
    return {
        "home_goals":avg("home_goals",1.50), "away_goals":avg("away_goals",1.20),
        "home_sot":avg("home_shots_on_target",4.8), "away_sot":avg("away_shots_on_target",4.0),
        "home_corners":avg("home_corners",5.3), "away_corners":avg("away_corners",4.4),
        "home_xg":avg("home_xg",1.50), "away_xg":avg("away_xg",1.20),
    }

def _ratio(value:float,baseline:float)->float:
    return 1.0 if baseline<=1e-9 else _clip(value/baseline,0.35,2.50)

def _team_recent(history: Sequence[Dict[str,Any]],team:str,*,venue:Optional[str],limit:int)->List[Dict[str,Any]]:
    out=[]
    for m in reversed(history):
        if venue=="home" and m.get("home_team")!=team: continue
        if venue=="away" and m.get("away_team")!=team: continue
        if venue is None and team not in (m.get("home_team"),m.get("away_team")): continue
        out.append(m)
        if len(out)>=limit: break
    return out

def _weighted_rate(values: Sequence[Optional[float]],prior:float)->Tuple[float,int]:
    mean,n=_weighted_mean(values); return _shrink(mean,n,prior),n

def _venue_rate(matches:Sequence[Dict[str,Any]],key:str,prior:float)->Tuple[float,int]:
    return _weighted_rate([_f(m.get(key)) for m in matches],prior)

def _overall_team_rate(matches:Sequence[Dict[str,Any]],team:str,metric:str,prior:float)->Tuple[float,int]:
    vals=[]
    for m in matches:
        if m.get("home_team")==team: vals.append(_f(m.get("home_"+metric)))
        elif m.get("away_team")==team: vals.append(_f(m.get("away_"+metric)))
    return _weighted_rate(vals,prior)

def predict_match(history:Sequence[Dict[str,Any]],home_team:str,away_team:str,*,recent_matches:int=DEFAULT_RECENT_MATCHES)->Prediction:
    base=_league_baselines(history)
    h_home=_team_recent(history,home_team,venue="home",limit=recent_matches)
    a_away=_team_recent(history,away_team,venue="away",limit=recent_matches)
    h_all=_team_recent(history,home_team,venue=None,limit=recent_matches)
    a_all=_team_recent(history,away_team,venue=None,limit=recent_matches)

    h_gf,h_n=_venue_rate(h_home,"home_goals",base["home_goals"]); h_ga,_=_venue_rate(h_home,"away_goals",base["away_goals"])
    a_gf,a_n=_venue_rate(a_away,"away_goals",base["away_goals"]); a_ga,_=_venue_rate(a_away,"home_goals",base["home_goals"])
    h_gf_all,_=_overall_team_rate(h_all,home_team,"goals",(base["home_goals"]+base["away_goals"])/2)
    a_gf_all,_=_overall_team_rate(a_all,away_team,"goals",(base["home_goals"]+base["away_goals"])/2)
    h_gf=.78*h_gf+.22*h_gf_all; a_gf=.78*a_gf+.22*a_gf_all
    lam_h_goals=base["home_goals"]*math.sqrt(_ratio(h_gf,base["home_goals"])*_ratio(a_ga,base["home_goals"]))
    lam_a_goals=base["away_goals"]*math.sqrt(_ratio(a_gf,base["away_goals"])*_ratio(h_ga,base["away_goals"]))

    h_sf,_=_venue_rate(h_home,"home_shots_on_target",base["home_sot"]); h_sa,_=_venue_rate(h_home,"away_shots_on_target",base["away_sot"])
    a_sf,_=_venue_rate(a_away,"away_shots_on_target",base["away_sot"]); a_sa,_=_venue_rate(a_away,"home_shots_on_target",base["home_sot"])
    lam_h_shot=base["home_goals"]*math.sqrt(_ratio(h_sf,base["home_sot"])*_ratio(a_sa,base["home_sot"]))
    lam_a_shot=base["away_goals"]*math.sqrt(_ratio(a_sf,base["away_sot"])*_ratio(h_sa,base["away_sot"]))

    h_xgf,hxgf_n=_venue_rate(h_home,"home_xg",base["home_xg"]); h_xga,hxga_n=_venue_rate(h_home,"away_xg",base["away_xg"])
    a_xgf,axgf_n=_venue_rate(a_away,"away_xg",base["away_xg"]); a_xga,axga_n=_venue_rate(a_away,"home_xg",base["home_xg"])
    xg_used=min(hxgf_n,hxga_n,axgf_n,axga_n)>=MIN_XG_SAMPLES
    if xg_used:
        lam_h_xg=base["home_xg"]*math.sqrt(_ratio(h_xgf,base["home_xg"])*_ratio(a_xga,base["home_xg"]))
        lam_a_xg=base["away_xg"]*math.sqrt(_ratio(a_xgf,base["away_xg"])*_ratio(h_xga,base["away_xg"]))
        lam_h=_clip(.40*lam_h_goals+.20*lam_h_shot+.40*lam_h_xg,.20,4.50)
        lam_a=_clip(.40*lam_a_goals+.20*lam_a_shot+.40*lam_a_xg,.15,4.00)
    else:
        lam_h=_clip(.72*lam_h_goals+.28*lam_h_shot,.20,4.50)
        lam_a=_clip(.72*lam_a_goals+.28*lam_a_shot,.15,4.00)
    p_over25=poisson_prob_over(2,lam_h+lam_a)
    p_btts=_clip((1.0-math.exp(-lam_h))*(1.0-math.exp(-lam_a)),0.0,1.0)

    h_cf,hc_n=_venue_rate(h_home,"home_corners",base["home_corners"]); h_ca,_=_venue_rate(h_home,"away_corners",base["away_corners"])
    a_cf,ac_n=_venue_rate(a_away,"away_corners",base["away_corners"]); a_ca,_=_venue_rate(a_away,"home_corners",base["home_corners"])
    lam_h_c=base["home_corners"]*math.sqrt(_ratio(h_cf,base["home_corners"])*_ratio(a_ca,base["home_corners"]))
    lam_a_c=base["away_corners"]*math.sqrt(_ratio(a_cf,base["away_corners"])*_ratio(h_ca,base["away_corners"]))
    lam_corners=_clip(lam_h_c+lam_a_c,3.0,16.0); p_corners=poisson_prob_over(8,lam_corners)

    home_sample=max(h_n,hc_n,len(h_all)); away_sample=max(a_n,ac_n,len(a_all))
    quality=_clip(min(home_sample,away_sample)/15.0,0.20,1.0)
    if xg_used: quality=_clip(quality+0.05,0.20,1.0)
    return Prediction(p_over25,p_btts,p_corners,lam_h,lam_a,lam_corners,home_sample,away_sample,quality,xg_used)

def best_market(prediction:Prediction)->Dict[str,Any]:
    options=[("over_2_5",prediction.p_over_2_5,"2.5 ÜST","2.5 ALT"),("btts",prediction.p_btts,"BTTS VAR","BTTS YOK"),("corners_over_8_5",prediction.p_corners_over_8_5,"8.5 KORNER ÜST","8.5 KORNER ALT")]
    market,p_yes,yes_label,no_label=max(options,key=lambda x:max(x[1],1.0-x[1])); yes=p_yes>=.5; confidence=p_yes if yes else 1.0-p_yes
    return {"market":market,"selection":yes_label if yes else no_label,"selection_yes":yes,"probability":confidence,"raw_yes_probability":p_yes,"data_quality":prediction.data_quality,"xg_used":prediction.xg_used}
