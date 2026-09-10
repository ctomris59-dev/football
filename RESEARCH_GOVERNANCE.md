# Araştırma ve Model Yönetişimi

Bu dosya production bahis motoruna hangi sinyalin girebileceğini belirleyen kanıt kapısını tanımlar. Ana ilke: **daha fazla feature otomatik olarak daha iyi model değildir**.

## Production gerçeği

- Aktif olasılık çekirdeği: `model_engine_v1.py`.
- V1; geçmiş gol, isabetli şut ve korner oranları ile lig ortalamasına shrinkage kullanır.
- `model_engine.py` xG-aware yardımcı/teşhis modelidir; production V1 değildir.
- `value_backtest.py` xG-aware teşhis modelini ölçer. Bu dosyadaki model-vs-market Brier sonucu production V1'e atfedilemez.
- Production V1'in lig/market/walk-forward ve O/U 2.5 market karşılaştırması `edge_structure_audit.py` ile ölçülür.
- 2026/27 baseline policy snapshot'ı `policy_freeze_2026_27.json` içinde tarih/SHA damgalı olarak dondurulmuştur.

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

## Dondurulmuş 2026/27 holdout politikası

2026/27 sezonu canlı holdout'tur. Sonuçları izlenebilir ve raporlanabilir; **feature, ağırlık, threshold veya policy ayarı için kullanılamaz**. `edge_structure_audit.py`, `2627` test fold'u olarak verilirse varsayılan olarak fail-closed davranır.

Dondurulan baseline şu üç şeyi kanıtlar:

1. Hangi commit/SHA'dan başlanmıştır.
2. V1 ağırlıkları, minimum confidence/price ve V5 legacy `MIN_IMPROVEMENT` dahil hangi eşikler geçerlidir.
3. Hangi feature'lar aktif, hangileri shadow/validation-gated durumdadır.

2026/27 tamamlandığında üçüncü fold ancak açık bir protokol değişikliğiyle eklenebilir; canlı sezon sürerken bu koruma kaldırılmaz.

## Değişiklik sınıfları

Her model/research değişikliği aşağıdaki sınıflardan biri olmalıdır:

- `BUG_FIX`: Yanlış hesap, leakage, veri eşlemesi veya açık implementasyon hatası. Neden düzeltildiği ve davranış etkisi loglanır.
- `CHALLENGER`: Yeni feature, ağırlık, threshold, ranking veya seçim davranışı. İki-fold tarihsel gate geçmeden production davranışını değiştiremez.
- `INFRA_ONLY`: Logging, performans, taşıma, hata mesajı vb. Tahmin/seçim sonucunu değiştiremez.

`research_change_control.py` bu ayrımı DB'de `model_change_log` tablosuna kaydeder. Production V5 registry'si non-`v1_only` bir mod döndürse bile, registry metrics içinde aşağıdaki kanıt yoksa `production_predictor_v5.py` fail-closed biçimde `v1_only` kullanır:

- `gate_passed=true`
- `gate_version=two-fold-week-block-v1`
- test sezonları tam olarak `2425,2526`
- `holdout_excluded=true`
- live holdout `2627`

Böylece eski tek-split backtest sonucu yanlışlıkla production aktivasyonuna dönüşemez.

## Zorunlu validasyon sırası

Bir feature production'a girmeden önce:

1. **Causal timestamp / leakage kontrolü:** yalnız karar anında bilinen veri.
2. **Walk-forward:** iki ayrı ileri-zaman fold'u: 2023/24 -> 2024/25 ve 2023/24+2024/25 -> 2025/26.
3. **Week-block bootstrap:** pick-level naive bootstrap kullanılmaz. ISO haftaları blok olarak resample edilir.
4. **Pooled OOS stratification:** Fold-1 ve Fold-2 haftaları kendi fold'u içinde resample edilir, sonra birleştirilir.
5. **Lig × market × confidence kırılımı:** Big Five, market ve confidence band üçlü hücreler halinde raporlanır.
6. **Small-n partial pooling:** küçük hücreler önce lig×market ebeveynine, o da market ebeveynine doğru shrink edilir. Ham oran tek başına production kanıtı değildir.
7. **Kalibrasyon:** Brier + log loss + confidence-band reliability.
8. **Market benchmark:** mümkün olan yerde aynı marketin no-vig piyasa olasılığına karşı paired Brier karşılaştırması.
9. **Selection performansı:** ROI ve hit rate ikincil metriklerdir; sample ve CI olmadan yorumlanmaz.
10. **Stability gate:** iyileşme yalnız tek fold/lig/market tarafından sürükleniyorsa production'a alınmaz.
11. **Önceden tanımlı eşikler:** sonuç görüldükten sonra threshold optimize edilmez.
12. **Production değişikliği ayrı ve loglu:** challenger kanıtı olmadan activation registry production davranışını değiştiremez.

## Bootstrap ve bucket yorumu

Ana CI birimi **haftadır**. Aynı haftadaki maçlar/pick'ler birlikte taşınır. Pooled OOS hesapta her fold kendi hafta havuzundan replacement ile örneklenir. Bu yöntem fold kompozisyonunu korur ve büyük fold'un küçük fold'un belirsizliğini yapay biçimde yutmasını önler.

Bucket raporu `raw` ve `shrunk` değerleri birlikte gösterir. `n<15` hücre `insufficient`, `15-29` `weak`, `30-59` `moderate`; daha büyük örnekler ancak CI da makulse `stronger` olarak işaretlenir. `insufficient/weak` hücre production kararında kullanılamaz.

## CLV: girdi ile değerlendirmeyi ayır

CLV `clv_backtest.py` içinde bağımsızdır. Closing odds model girdisi değildir; karar sonrasındaki değerlendirme hedefidir.

- **Decision-time price:** aynı bookmaker için paired iki taraflı fiyatın, kayıtlı prediction timestamp'ine kadar olan son snapshot'ı.
- **Closing price:** aynı bookmaker için kickoff'tan önceki son paired snapshot.
- Her iki fiyat no-vig selected probability'ye çevrilir; ayrıca price-shortening yönünü veren log-price CLV raporlanır.
- Closing snapshot hiçbir model/filter/threshold/ranking fonksiyonuna geri beslenmez.

2026/27 CLV gözlemi yapılabilir ancak frozen policy'yi tune etmek için kullanılamaz.

## Odds movement için özel leakage kuralı

`prematch_feature_snapshots.odds_movement` alanı geçmişte yeniden enrich edildiğinde daha sonraki fiyatları içerebilir. Bu nedenle geriye dönük production testi için doğrudan güvenli değildir.

`odds_movement_backtest.py` bunun yerine `oddspapi_market_prices` ham snapshot'larını kullanır ve her pick için yalnız **`snapshot_hour <= model pick timestamp`** olan fiyatları kabul eder. Hareket, aynı bookmaker'ın iki taraflı fiyatından hesaplanan no-vig seçili olasılığın ilk -> son değişimidir.

Bu test olumlu çıksa bile movement otomatik aktive edilmez. Önceden tanımlanmış yeni/OOS dönemde tekrar doğrulanması gerekir.

## Karar prensibi

Amaç "model piyasayı her maçta yensin" değildir. Production seçim mimarisi şu şekilde kalır:

**V1 model güveni + uluslararası paired same-book no-vig piyasa doğrulaması + Türkiye'de gerçekten oynanabilen fiyat.**

Türkiye fiyatı executable price'dır. Uluslararası piyasa model sapmasını ve value'nin piyasa açısından da makul olup olmadığını kontrol eden referanstır.
