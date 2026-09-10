# Incremental Feature Audit Results — 2026-09-10

## Decision

**Production remains `v1_only`.** None of the five predeclared challengers passed the two-fold, ISO-week-block promotion gate. The 2026/27 live holdout was excluded from tuning.

Audit policy: `weekly-feature-challenger-v1` / `incremental-feature-ranking-v1`.

## Baseline

Frozen V1 weekly Top-10:

- pooled: 505/740 = **68.243%**, selected-pick Brier **0.219427**
- Fold 2425: 251/370 = **67.838%**
- Fold 2526: 254/370 = **68.649%**

## One-at-a-time challengers

| Feature | Pooled hit rate | Gain vs V1 | Selected Brier Δ | Fold 2425 | Fold 2526 | Coverage 2425 / 2526 | Changed picks | Bootstrap P(gain>0) | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| xG regression | 68.108% | -0.135 pp | +0.000543 | 67.838% | 68.378% | 78.82% / 75.50% | 10 | 25.40% | FAIL |
| Elo balance | 68.243% | +0.000 pp | -0.000070 | 67.838% | 68.649% | 100% / 100% | 14 | 38.70% | FAIL |
| Pace proxy | 67.297% | -0.946 pp | +0.002880 | 66.486% | 68.108% | 99.26% / 99.49% | 31 | 2.20% | FAIL |
| Venue balance | 67.973% | -0.270 pp | +0.000828 | 67.568% | 68.378% | 99.31% / 99.62% | 7 | 0.00% | FAIL |
| Lineup stability | **68.514%** | **+0.271 pp** | **-0.001158** | **68.378%** | **68.649%** | 54.05% / 55.97% | 27 | **68.55%** | FAIL |

`pp` = percentage points.

## Why lineup stability was not activated

Lineup stability was the only challenger that improved both pooled hit rate and selected-pick Brier without a fold regression. However, the predeclared gate required:

- pooled hit-rate gain >= **+0.50 pp**;
- bootstrap **P(gain > 0) >= 80%**;
- no fold regression;
- feature coverage >= 50% in each fold;
- at least 20 changed picks;
- selected-pick Brier not worse.

Lineup stability achieved only **+0.271 pp** and **68.55% bootstrap support**, so lowering thresholds after seeing the result would be post-hoc overfitting. It therefore remains shadow/research-only.

## Feature conclusions

- **xG regression:** reject current formulation for production ranking. It lost one hit overall and worsened selected Brier.
- **Elo:** neutral. Calibration/Brier changed microscopically, but weekly Top-10 hit performance did not improve and too few selections changed.
- **Pace:** reject current formulation. It materially worsened both folds and selected Brier.
- **Venue:** reject current formulation. It worsened both folds and barely changed the selected set.
- **Lineup stability:** **promising shadow candidate**, but evidence is not strong enough for activation yet.

## Governance consequence

The audit wrote `policy_activation_registry.policy_key = weekly-feature-challenger-v1` with `active_mode = v1_only`. No raw V1 probabilities were changed, and no new feature is allowed to affect future weekly ranking from this audit.

The correct next experiment, if pursued, is a separately predeclared lineup-stability challenger with better historical lineup coverage and the same untouched 2627 holdout—not weaker thresholds on this completed test.
