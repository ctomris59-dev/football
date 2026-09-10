# Araştırma ve Model Yönetişimi

Bu dosya production bahis motoruna hangi sinyalin girebileceğini belirleyen kanıt kapısını tanımlar. Ana ilke: **daha fazla feature otomatik olarak daha iyi model değildir**.

## Production gerçeği

- Aktif olasılık çekirdeği: `model_engine_v1.py`.
- V1; geçmiş gol, isabetli şut ve korner oranları ile lig ortalamasına shrinkage kullanır.
- `model_engine.py` xG-aware yardımcı/teşhis modelidir; production V1 değildir.
- `value_backtest.py` xG-aware teşhis modelini ölçer. Bu dosyadaki model-vs-market Brier sonucu production V1'e atfedilemez.
- Production V1'in lig/market/walk-forward ve O/U 2.5 market karşılaştırması `edge_structure_audit.py` ile ölçülür.

## Kabul edilmiş / reddedilmiş / bekleyen sinyaller

| Sinyal | Durum | Production etkisi | Gerekçe |
|---|---|---|---|
| V1 gol/SOT/korner + shrinkage | ACTIVE | Olasılık çekirdeği | Mevcut leakage-safe politika lideri |
| xG ağırlıklı model | DIAGNOSTIC / REJECTED AS PRIMARY | Yok | Önceki testte V1 hit-rate üstünlüğünü geçemedi |
| Pressure | SHADOW / REJECTED | Yok | Forward-validasyon kapısını geçmedi |
| Starter continuity | SHADOW / REJECTED | Yok | Forward-validasyon kapısını geçmedi |
| Pressure + continuity | SHADOW / REJECTED | Yok | Korner seçimine aşırı yüklendi; aktivasyon reddedildi |
| Score-state | VALIDATION-GATED | Varsayılan yok | Yalnız kendi leakage-safe registry testi onaylarsa kullanılabilir |
| Promotion prior | VALIDATION-GATED | Varsayılan yok | Yalnız kendi leakage-safe registry testi onaylarsa kullanılabilir |
| Elo | CANDIDATE | Yok | Veri mevcut; önce incremental OOS test gerekir |
| Odds movement | RESEARCH ONLY | Yok | Ham snapshot'lardan leakage-safe backtest gerekir; mutable JSON doğrudan kullanılamaz |
| Hakem | BACKLOG CANDIDATE | Yok | Yeni veri maliyeti var; mevcut sinyaller test edilmeden eklenmez |
| Set-piece / stil | BACKLOG CANDIDATE | Yok | Özellikle korner için aday; incremental OOS kanıt gerekir |
| Hava / saha boyutu | LOW PRIORITY | Yok | Beklenen sinyal/zaman oranı düşük |

## Zorunlu validasyon sırası

Bir feature production'a girmeden önce:

1. **Causal timestamp / leakage kontrolü:** yalnız karar anında bilinen veri.
2. **Walk-forward:** en az iki ayrı ileri-zaman fold'u. Varsayılan yapımız 2023/24 -> 2024/25 ve 2023/24+2024/25 -> 2025/26.
3. **Lig kırılımı:** Big Five'ın her biri ayrı raporlanır.
4. **Market kırılımı:** O2.5, BTTS, O8.5 korner ayrı raporlanır.
5. **Kalibrasyon:** Brier + log loss + confidence-band reliability.
6. **Selection performansı:** hit rate tek başına değil; örneklem ve Wilson %95 aralığı ile.
7. **Market benchmark:** mümkün olan yerde aynı marketin no-vig piyasa olasılığına karşı paired karşılaştırma.
8. **Stability gate:** iyileşme tek lig, tek market veya tek fold tarafından sürükleniyorsa production'a alınmaz.
9. **Önceden tanımlı eşikler:** sonuç görüldükten sonra threshold optimize edilmez.
10. **Production değişikliği ayrı commit:** araştırma script'i activation registry'yi veya production probability'yi değiştiremez.

## Odds movement için özel leakage kuralı

`prematch_feature_snapshots.odds_movement` alanı geçmişte yeniden enrich edildiğinde daha sonraki fiyatları içerebilir. Bu nedenle geriye dönük production testi için doğrudan güvenli değildir.

`odds_movement_backtest.py` bunun yerine `oddspapi_market_prices` ham snapshot'larını kullanır ve her pick için yalnız **`snapshot_hour <= model pick timestamp`** olan fiyatları kabul eder. Hareket, aynı bookmaker'ın iki taraflı fiyatından hesaplanan no-vig seçili olasılığın ilk -> son değişimidir.

Bu test olumlu çıksa bile movement otomatik aktive edilmez. Önceden tanımlanmış yeni/future OOS dönemde tekrar doğrulanması gerekir.

## Karar prensibi

Amaç "model piyasayı her maçta yensin" değildir. Production seçim mimarisi şu şekilde kalır:

**V1 model güveni + uluslararası paired same-book no-vig piyasa doğrulaması + Türkiye'de gerçekten oynanabilen fiyat.**

Türkiye fiyatı executable price'dır. Uluslararası piyasa model sapmasını ve value'nin piyasa açısından da makul olup olmadığını kontrol eden referanstır.
