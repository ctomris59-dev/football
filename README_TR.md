# Big Five Football Intelligence — Veri, Model ve Top‑10 Tahmin Sistemi

Bu repo artık yalnızca bir API-Football collector değildir. Sistem; Avrupa'nın 5 büyük ligindeki maçları farklı veri kaynaklarından birleştirir, veri kalitesini kontrol eder, üç bahis marketi için açıklanabilir olasılık üretir ve yalnızca kalite eşiklerini geçen maçlardan haftalık **Top‑10** listesi oluşturur.

> Hedef marketler: **2.5 Gol Üst/Alt**, **BTTS Var/Yok**, **8.5 Toplam Korner Üst/Alt**.

## 1. Kapsam

Ligler:

- Premier League
- La Liga
- Serie A
- Bundesliga
- Ligue 1

Ana dönemler:

- 2023/24 — tarihsel taban
- 2024/25 — tarihsel taban
- 2025/26 — model/backtest için ana tam sezon
- 2026/27 — güncel sezon / canlı tahmin dönemi

Sistem tek bir veri sağlayıcısına bağlı değildir. Bir kaynak geçici olarak başarısız olduğunda mümkün olan alanlarda diğer kaynaklarla devam eder ve eksik veri **"0" veya "sağlıklı"** olarak yorumlanmaz.

---

# 2. Mimari

```text
Football-Data mirrors ─┐
ESPN current season ───┤
Understat xG ──────────┤
OddsPapi markets ──────┤
FotMob injuries ───────┤
BBS lineups ───────────┤──> Render PostgreSQL
Sofascore optional ────┘          │
                                  v
                     Prematch Context Builder
                                  │
                         Availability Enricher
                                  │
                         Odds Movement Enricher
                                  │
                          Readiness Audit v4
                                  │
                          Production Predictor
                                  │
                     2.5 / BTTS / Corner 8.5
                                  │
                    one pick per match -> Top‑10
                                  │
                    FastAPI status / predictions
```

Render bileşenleri:

- PostgreSQL: `football-dataset-db`
- Web/API: `football-dataset-export`
- Tarihsel API-Football collector cron: `football-bulk-collector`

GitHub repo: `ctomris59-dev/football`

---

# 3. Veri kaynakları

## 3.1 Football-Data tarihsel veri

Ana kullanım:

- skor
- gol
- ev/deplasman
- şut
- isabetli şut
- korner (`HC + AC`)
- kartlar
- mevcut tarihsel bahis kolonları

Türetilen hedefler:

- `over_2_5`
- `btts`
- `corners_over_8_5`

Tarihsel tam sezonlar modelin form, lig tabanı ve backtest katmanını oluşturur.

## 3.2 ESPN — güncel sezon ve fikstür

Kullanım:

- 2026/27 tamamlanmış maçlar
- yaklaşan maçlar
- skorlar
- boxscore istatistikleri
- roster / lineup varlığı
- bazı maçlarda bookmaker / total line bilgisi
- takım programı

`espn_team_schedule_importer.py`, yalnızca lig fikstürüne bakmak yerine takımın ESPN'de görünen daha geniş programını kaydeder. Bu katman **dinlenme günü ve fikstür yoğunluğu** hesabını güçlendirir.

## 3.3 Understat — xG/xGA

Maç bazında:

- home xG
- away xG
- xG geçmişi

Model xG mevcut ve yeterli olduğunda bunu ek sinyal olarak kullanır; xG eksikse model çalışmayı tamamen bırakmaz.

## 3.4 OddsPapi — gerçek piyasa fiyatları

Aktif marketler:

- 2.5 Goals Over/Under
- BTTS Yes/No
- Total Corners 8.5 Over/Under

Sistem:

- market ID'lerini dinamik keşfeder,
- fixture snapshot saklar,
- decimal fiyatları saklar,
- ilk ve son snapshot arasında gerçek fiyat hareketi hesaplar,
- iki yönün fiyatı varsa no-vig piyasa olasılığı çıkarır.

**Piyasa olasılığı model olasılığının yerine geçirilmez.** Piyasa yalnızca bağımsız bir kalibrasyon / agreement / kalite sinyali olarak tutulur.

## 3.5 FotMob — güncel sakatlık durumu

Bir takımın squad payload'ından:

- `injured`
- `injury`
- oyuncu
- pozisyon
- varsa `expectedReturn`

alanları alınır.

Bu kaynak sözleşmeli/resmî geliştirici API'si olmadığı için **fail-soft** çalışır. Erişim bozulursa sistem sakat oyuncu sayısını sıfır kabul etmez; ilgili readiness alanını eksik sayar.

## 3.6 BBS / Big Balls Sports

BBS key kullanılır. Futbolda ileriye dönük `/injuries` cevabı faydalı oyuncu satırı üretmediği için bu yol production'da varsayılan olarak kapalıdır.

BBS'nin kullanım alanı:

- scheduled match eşleştirmesi
- yayınlandığında starting XI
- bench
- lineup confirmation sinyali

## 3.7 Sofascore — opsiyonel redundancy

Amaç:

- event eşleştirme
- `missingPlayers`
- `confirmed` lineup bayrağı

Render/WAF 403 durumunda bu kaynak **optional failure** olarak kaydedilir ve ana pipeline durmaz.

## 3.8 API-Football

Free plan nedeniyle 2025/26+ erişimi sınırlı olduğundan production sistemin güncel omurgası değildir.

Mevcut collector 2024 erişilebilir tarihsel detaylarını resume-safe şekilde toplayabilir:

- fixture
- event
- lineup
- statistics
- player stats
- season players
- API'nin izin verdiği injuries

Bu veri ek enrichment olarak tutulur.

---

# 4. PostgreSQL veri katmanı

## Tarihsel / ana maç tabloları

- `football_data_matches`
- `football_data_source_state`
- `football_data_import_runs`
- `espn_current_matches`
- `espn_upcoming`
- `espn_import_runs`

## ESPN context

- `espn_odds_snapshots`
- `espn_prematch_snapshots`
- `espn_advanced_match_stats`
- `espn_context_runs`
- `espn_team_schedule_events`
- `espn_team_schedule_runs`

## xG

- `understat_matches`
- `understat_team_seasons`
- `understat_source_state`
- `understat_import_runs`

## Bookmaker / market

- `oddspapi_tournaments`
- `oddspapi_market_catalog`
- `oddspapi_fixture_snapshots`
- `oddspapi_market_prices`
- `oddspapi_import_runs`

## Oyuncu uygunluğu / lineup

- `fotmob_team_availability_snapshots`
- `fotmob_fixture_availability_snapshots`
- `fotmob_availability_runs`
- `bbs_lineup_snapshots`
- `bbs_lineup_runs`
- `sofascore_availability_snapshots`
- `sofascore_availability_runs`

Legacy/audit için eski BBS absence ve ESPN injury tabloları tutulabilir; production availability kararı bunlara körü körüne dayanmaz.

## Maç önü birleşik feature katmanı

- `prematch_feature_snapshots`
- `prematch_context_runs`

Bu tablo maç başına aşağıdaki bağlamı birleştirir:

- ev/deplasman takımı
- maç tarihi
- dinlenme günleri
- 7/14 günlük maç yoğunluğu
- roster/lineup varlığı
- güncel injury snapshot
- match-specific lineup sinyali
- OddsPapi fixture eşleşmesi
- 2.5 / BTTS / corner 8.5 market varlığı
- odds snapshot yaşı
- gerçek odds movement JSON'u

## Readiness

- `prediction_readiness_snapshots`
- `data_readiness_runs`

## Production tahmin

- `production_prediction_runs`
- `production_predictions`

## Backtest

- `model_backtest_runs`
- `model_policy_backtest_runs`
- `model_value_backtest_runs`

---

# 5. Model

`model_engine.py` açıklanabilir, recency-weighted Poisson tabanlıdır.

Temel sinyaller:

- takımın son formu
- gol üretme / yeme oranı
- ev/deplasman ayrımı
- isabetli şut
- lig ortalamasına shrinkage
- xG/xGA mevcutsa xG katkısı
- takım korner üretme / verme oranı
- zaman ağırlığı

Çıktılar:

- `p_over_2_5`
- `p_btts`
- `p_corners_over_8_5`
- beklenen ev golü / deplasman golü
- beklenen toplam korner
- sample count
- `data_quality`
- `xg_used`

Model sakatlık sayısını doğrudan keyfi bir gol katsayısına çevirmemektedir. Oyuncu önemini güvenilir şekilde ölçmeden "3 sakat = -0.25 gol" gibi doğrulanmamış bir kural kullanmak yerine injury/lineup bilgisi **readiness ve seçim güveni** katmanında tutulur.

---

# 6. Readiness Audit v4

Tahmin motoru yalnızca olasılık hesaplamaz; önce o market için veri yeterli mi kontrol eder.

## Gol / BTTS provisional readiness

Temel koşullar:

- iki takım için yeterli maç geçmişi
- yeterli xG geçmişi
- ilgili bookmaker marketinin var olması
- odds snapshot'ın bayat olmaması

Varsayılan odds freshness: **12 saat**.

## Korner provisional readiness

- iki takım için yeterli korner geçmişi
- 8.5 corner marketi mevcut
- odds snapshot fresh

## Final context readiness

Daha sıkıdır:

- schedule context mevcut
- güncel current injury report mevcut
- maç için confirmed-current lineup sinyali mevcut

Confirmed lineup günler önceden doğal olarak mevcut olmayabilir. Bu nedenle sistem **provisional prediction** ile **final pre-kickoff prediction** kavramlarını ayırır.

## Blocker örnekleri

- `insufficient_match_history`
- `insufficient_xg_history`
- `insufficient_corner_history`
- `schedule_context_missing`
- `current_injury_report_missing`
- `match_lineup_not_confirmed_current`
- `ou25_odds_missing`
- `btts_odds_missing`
- `corner85_odds_missing`
- `odds_snapshot_stale`

Eksik kaynak asla otomatik olarak olumlu veri kabul edilmez.

---

# 7. Odds movement

`odds_movement_enricher.py` her OddsPapi fixture için ilk ve son snapshot'ı karşılaştırır.

Üç markette outcome bazında saklanan örnek yapı:

```json
{
  "first_price": 1.95,
  "latest_price": 1.82,
  "absolute_delta": -0.13,
  "percent_delta": -6.667
}
```

Şu aşamada hareket bilgisi **saklanır ve raporlanır**, ancak yeterli tarihsel movement backtest'i olmadan production modeline yönsel ağırlık olarak zorla eklenmez.

---

# 8. Production Predictor ve Top‑10

`production_predictor.py` yaklaşan maçları tarar.

Her maç için üç market ayrı ayrı hesaplanır:

1. 2.5 Üst/Alt
2. BTTS Var/Yok
3. 8.5 Korner Üst/Alt

Her market kaydında:

- seçilen yön
- seçilen yönün model olasılığı
- bookmaker fiyatı
- no-vig piyasa olasılığı
- model data quality
- readiness score
- final-context durumu
- xG kullanıldı mı
- odds snapshot yaşı
- dinlenme günleri
- sakat sayıları
- availability source
- blocker listesi
- model detay JSON'u

bulunur.

## Top‑10 seçim politikası

- yalnız provisional-ready marketler aday olabilir,
- varsayılan minimum model güveni: `%60`,
- varsayılan minimum fiyat: `1.20`,
- model confidence + data quality + readiness + model/piyasa agreement birlikte sıralanır,
- **bir maçtan en fazla bir seçim** Top‑10'a girebilir,
- kaliteyi geçen 10 maç yoksa sistem 10'u zorla doldurmaz.

Bu Top‑10 sıralama politikası bir "garantili bahis" veya kanıtlanmış pozitif ROI iddiası değildir. Amaç veri kalitesi düşük seçimleri otomatik elemek ve en sağlam adayları sıralamaktır.

---

# 9. Backtest sonuçları

Bugüne kadarki testlerde:

## Eski form modeli / rolling weekly Top‑10

- 370 seçim
- 253 doğru
- **%68.38 hit rate**

## xG ağırlığı artırılmış v2

- 370 seçim
- 250 doğru
- **%67.57 hit rate**

Sonuç: xG 2.5 gol kalibrasyonunda faydalı olsa da genel Top‑10 başarı oranını otomatik artırmadı. Bu nedenle xG yardımcı sinyal olarak kullanılmaktadır; tek başına her markete baskın ağırlık verilmemektedir.

## Hybrid policy denemesi

- yaklaşık **%66.76**
- korner marketini aşırı seçtiği için production policy olarak reddedildi.

## 2.5 gol market/value backtest

- piyasa Brier: **0.24165**
- model Brier: **0.24514**
- naive model-vs-market edge bahisleri negatif ROI üretti.

Bu nedenle sistem:

> "model %65 dedi, otomatik value bet"

mantığını kullanmaz.

Bookmaker fiyatı modelin yerine geçmez; piyasa bağımsız benchmark ve filtre olarak kullanılır.

---

# 10. Live refresh sırası

`live_refresh.py`:

1. Football-Data 2023/24
2. Football-Data diğer tarihsel sezonlar
3. ESPN current season / fixtures
4. ESPN prematch context
5. ESPN team schedule
6. Understat xG
7. OddsPapi market snapshot
8. FotMob current injuries
9. opsiyonel BBS absence (varsayılan kapalı)
10. BBS lineups
11. opsiyonel Sofascore
12. prematch context builder
13. actual odds movement enrichment
14. availability enrichment
15. readiness v4
16. production predictions / Top‑10

Provider freshness gate varsayılanları:

| Katman | Minimum yeniden çağrı aralığı |
|---|---:|
| ESPN current | kendi importer freshness kontrolü |
| ESPN prematch context | 2 saat |
| ESPN team schedule | 12 saat |
| Understat | yaklaşık 6 saat / internal season cache |
| OddsPapi | 8 saat |
| FotMob injuries | 6 saat |

Bu yapı özellikle düşük API kotalarını deploy veya manuel refresh yüzünden tüketmemek için kullanılır.

`AUTO_LIVE_REFRESH=false` production varsayılanıdır. **Deploy veri toplama tetikleyicisi değildir.**

---

# 11. Web API

Base URL:

`https://football-dataset-export.onrender.com`

## Public health

`GET /health`

## Protected status

`GET /status`

## Protected predictions

`GET /predictions`

Varsayılan `top_only=true`; son başarılı production run'ın yalnız Top‑10 kayıtlarını döndürür.

## Protected refresh

`POST /refresh`

Aynı anda ikinci refresh başlamasını lock engeller.

## Protected dataset export

`GET /download`

Tüm ana tabloları JSON Lines olarak ZIP'e koyar.

### Authentication

Tercih edilen:

```http
Authorization: Bearer <DOWNLOAD_TOKEN>
```

Eski uyumluluk için `?token=` da desteklenir; fakat token'ın URL/browser geçmişine düşmemesi için Bearer daha güvenlidir.

API key veya token hiçbir zaman GitHub source içine yazılmamalıdır.

---

# 12. CI / kalite kontrolü

`.github/workflows/ci.yml` her main push'ta:

- Python 3.12 kurar,
- requirements yükler,
- bütün Python modüllerini compile eder,
- production modüllerini import smoke testinden geçirir.

Render deploy başarılı olsa bile CI başarısızsa sürüm production-ready kabul edilmemelidir.

---

# 13. Güvenlik

Environment secrets:

- `DATABASE_URL`
- `DOWNLOAD_TOKEN`
- `ODDSPAPI_API_KEY`
- `BBS_API_KEY`
- opsiyonel `API_FOOTBALL_KEY`

Kurallar:

- secret GitHub'a commit edilmez,
- dataset/status/predictions/refresh korumalıdır,
- Bearer auth tercih edilir,
- missing injury/lineup verisi `0` diye yorumlanmaz,
- deploy sırasında otomatik provider çağrıları varsayılan kapalıdır.

---

# 14. Önemli environment ayarları

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `AUTO_LIVE_REFRESH` | `false` | deploy sırasında refresh yapma |
| `ODDSPAPI_REFRESH_HOURS` | `8` | bookmaker kota koruması |
| `FOTMOB_REFRESH_HOURS` | `6` | current injury tazeliği |
| `ESPN_CONTEXT_REFRESH_HOURS` | `2` | maç önü context |
| `TEAM_SCHEDULE_REFRESH_HOURS` | `12` | takım programı |
| `CURRENT_INJURY_MAX_AGE_HOURS` | `18` | injury snapshot freshness |
| `MATCH_LINEUP_MAX_AGE_HOURS` | `6` | lineup freshness |
| `READINESS_ODDS_MAX_AGE_HOURS` | `12` | market snapshot freshness |
| `PREDICTION_LOOKAHEAD_DAYS` | `7` | production horizon |
| `PREDICTION_MIN_CONFIDENCE` | `0.60` | aday minimum güven |
| `PREDICTION_MIN_PRICE` | `1.20` | aday minimum fiyat |
| `LIVE_REFRESH_BBS` | `false` | forward BBS absence probe kapalı |
| `LIVE_REFRESH_BBS_LINEUPS` | `true` | BBS lineup açık |
| `LIVE_REFRESH_SOFASCORE` | `true` | optional redundancy |

---

# 15. Bilinen sınırlar

1. **FotMob ve Sofascore public web endpointleri sözleşmeli API değildir.** Şema/WAF değişebilir. Bu yüzden fail-soft kullanılır.
2. BBS futbol injury endpointi production forward injury kaynağı olarak güvenilir sonuç vermedi; açıkça devre dışıdır.
3. Confirmed starting XI çoğu ligde kickoff'a yakın yayınlanır. Günler önce `final_context_ready=false` olması hata değildir.
4. Corner 8.5 marketi bütün maçlarda günler önceden açılmayabilir. Market yoksa sistem korner seçimini zorlamaz.
5. xG modeli iyileştirebilir ama geçmiş testte global Top‑10 hit rate'i otomatik yükseltmemiştir.
6. Model/backtest geçmiş performansı gelecekte kâr veya isabet garantisi değildir.
7. Gerçek odds movement artık saklanır; movement sinyalinin yönsel bahis etkisi yeterli tarihsel snapshot oluşmadan production sıralamasına eklenmemiştir.
8. Sakatlık verisi şu anda current injury count/readiness olarak kullanılır. Oyuncu önem katsayısı yeterince doğrulanmadan gol/kornere keyfi matematiksel ceza uygulanmaz.

---

# 16. Production çalışma prensibi

Maç haftası ideal akış:

```text
Güncel fixture
   ↓
Form + home/away + shots + xG + corners
   ↓
Rest/congestion
   ↓
Current injuries
   ↓
Bookmaker prices + movement
   ↓
Provisional readiness
   ↓
Model probabilities
   ↓
Top candidate pool
   ↓
Kickoff'a yakın confirmed lineup refresh
   ↓
Final readiness
   ↓
Final Top‑10
```

Sistemin temel güvenlik prensibi:

> **Eksik veriyle yüksek güven üretmek yerine seçimi blokla veya kalite puanını düşür.**
