# DataGaffer Gap Analysis — football system

Date: 2026-09-10

This document compares useful publicly described DataGaffer concepts with the current `football` repository. It is an engineering gap analysis, not a claim that DataGaffer's proprietary formulas have been reproduced.

## Governing rule

The frozen production probability core remains `model_engine_v1.py`. New predictive signals are **CHALLENGER / SHADOW** until they pass the repository's existing leakage-safe two-fold protocol (2425 and 2526 test folds, week-block bootstrap, calibration, market benchmark and stability gate). The 2627 season remains a protected live holdout.

The all-competition schedule/rest correction is different: it fixes incomplete operational context (league-only rest) and is allowed to block/de-rank a candidate when an actual intervening official match exists. It does not modify raw V1 probabilities.

## Gap matrix

| Capability | DataGaffer-style idea | Existing football state | Change in this branch | Production status |
|---|---|---|---|---|
| All-competition fatigue/rest | Account for fixture congestion and short recovery | Domestic rest previously incomplete; schedule collector now covers domestic + UEFA | Unified into match-environment snapshot as home/away rest and fatigue asymmetry | **ACTIVE operational correction**; raw V1 unchanged |
| Pending intervening match | Do not finalize before important midweek match is played | Added in `schedule_context.py` / weekly ranking | Preserved and surfaced in unified environment | **ACTIVE safety gate** |
| xG regression | Compare chance quality with realised goals | Understat xG data existed; xG-aware model was diagnostic | Explicit recent xG-minus-goals attack/defence deltas | **SHADOW** |
| Pace / match openness | Summarise likely game tempo/openness | Goal/corner pressure signals existed separately | Transparent 0–100 pace proxy from those signals | **SHADOW** |
| Venue strength | Home/away performance matters | V1 already uses home/away splits implicitly | Explicit home/away venue indices and venue gap | **SHADOW** |
| Opponent-adjusted team strength | Strength context beyond raw recent form | Internal Elo tables already existed | Elo values/gap included in one environment snapshot | **SHADOW / candidate** |
| Expected XI / injuries | Player availability and lineup strength | Player context, injuries, continuity already existed | Unified lineup-stability summary + original player context | **SHADOW**, existing hard injury gates remain operational |
| Goal environment | One interpretable projected scoring environment | V1 already produces home/away goal lambdas | Added transparent `projected_total_goals` + balance metric | **SHADOW display/research** |
| "XoG" | Proprietary/unclear DataGaffer statistic | No verified public formula | **Not copied.** Our metric is explicitly named Projected Goal Environment (PGE) and derived from V1 lambdas | **SHADOW** |
| Scoreline simulation | Convert projected scoring rates into score/outcome probabilities | V1 already uses Poisson analytically for totals/BTTS | Added exact independent-Poisson score grid, 1X2, BTTS, O2.5 and top scorelines | **SHADOW** |
| 10,000 simulations | Monte Carlo representation of match distribution | Not required for independent Poisson | Exact grid used instead: deterministic and free of Monte Carlo noise under the same assumptions | **SHADOW** |
| Dixon–Coles | Low-score correlation correction | Not in production | Deliberately **not activated**; rho must be time-causally estimated and two-fold validated | **BACKLOG CHALLENGER** |
| Pressure / control indices | PPDA/possession/attacking pressure style metrics | Existing pressure/style proxies use xG, shots, SOT, corners and possession | Reused as transparent inputs; no proprietary index names/formulas copied | **SHADOW** |
| AGIX / NEC / TCIX-like concepts | Early aggression / identity / control | Partial analogues exist in pressure/style data | No fake one-to-one replication; candidate proxies only after causal historical coverage is proven | **BACKLOG** |
| H2H | Matchup history | Historical results exist | Not promoted: sparse/manager-era dependent and needs OOS proof | **BACKLOG / diagnostic** |
| Manager H2H | Manager matchup context | No robust manager-history table | Not added without causal timestamped data | **BACKLOG** |
| First-half / timeline model | First-half and game-state projections | Historical first-half coverage is not sufficiently established in current validated path | Not added yet | **BACKLOG** |
| Referee | Referee style/cards/fouls | No robust validated historical source in current production DB | Not added | **BACKLOG** |
| Weather / pitch | Environmental match context | No validated historical source | Not added; lower priority | **BACKLOG** |
| Market reality check | Use bookmaker information as sanity/calibration | International paired same-book no-vig reference already exists | **Kept separate from model features** rather than blended into PGE/pace/Elo | **ACTIVE as safety/value layer** |
| Multi-line corners | Evaluate actual offered corner line | 8.5-only was a gap | Already extended to 7.5/8.5/9.5/10.5 using same frozen V1 corner lambda | **ACTIVE operational market coverage** |

## New unified shadow context

`match_environment_builder.py` writes `match_environment_snapshots` for upcoming fixtures. Each snapshot contains:

- Projected Goal Environment from frozen V1 goal lambdas;
- exact independent-Poisson scoreline distribution;
- pace proxy and band;
- recent xG regression deltas;
- venue-role strength indices and gap;
- Elo values and gap;
- all-competition rest plus fatigue asymmetry;
- expected-XI/injury/continuity context and lineup-stability score;
- feature coverage and source/activation metadata.

The weekly list exposes this object inside `early_context.match_environment`, explicitly tagged `shadow_only_no_ranking_effect`. This makes the data inspectable without silently changing picks.

## Why exact Poisson instead of 10,000 Monte Carlo simulations?

With the current independent-Poisson assumptions, every scoreline probability can be calculated directly. An exact finite grid gives essentially the same distribution with no random simulation variance. Monte Carlo becomes useful only if a future challenger introduces dependencies or more complex event processes that cannot be integrated exactly.

## What must happen before any new feature affects picks

1. Build time-causal historical versions of the candidate feature. No future snapshots may leak backward.
2. Compare V1 against V1 + one incremental feature, not a bundle of many changes.
3. Test Fold 1: train/discover on 2324, evaluate 2425.
4. Freeze the rule, then confirm on Fold 2: train 2324+2425, evaluate 2526.
5. Use ISO-week block bootstrap and report Brier/log loss, calibration, hit rate, ROI, sample size and CI.
6. Verify the effect is not driven by one league/market and apply partial pooling to small cells.
7. Keep 2627 out of tuning.
8. Only a challenger passing the existing registry gate may alter production ranking/probabilities.

## Recommended validation order

1. **xG regression** — strongest existing data coverage and clear causal interpretation.
2. **Elo gap / opponent strength** — existing infrastructure, easy to timestamp historically.
3. **Pace proxy** — potentially useful for O2.5/BTTS/corners, but must prove incremental value.
4. **Venue explicit index** — V1 already contains venue splits, so incremental gain may be small.
5. **Lineup stability / player context** — potentially useful but historical causal coverage must be verified.
6. **Dixon–Coles** — fit rho only after the simpler feature tests; activate only on repeatable OOS gain.

The objective is not feature count. It is repeatable out-of-sample improvement over the frozen V1 baseline.