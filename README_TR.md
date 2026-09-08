# 5 Büyük Lig – API-Football Toplu Veri Çekici

Bu paket, API-Football'dan **5 büyük lig × 2 sezon** verisini elle kopyala-yapıştır yapmadan toplamak için hazırlandı.

Varsayılan kapsam:

- Premier League — league id **39**
- La Liga — **140**
- Serie A — **135**
- Bundesliga — **78**
- Ligue 1 — **61**
- season **2024** = 2024/25
- season **2025** = 2025/26

## Ne topluyor?

1. `/leagues?id=...&season=...`  
   Coverage kontrolü.

2. `/fixtures?league=...&season=...`  
   Bütün sezon fikstürü, skorlar, takımlar, tarih, statü vb.

3. `/fixtures?ids=ID1-ID2-...` — **20 maça kadar tek çağrı**  
   API-Football'un toplu fixture detay yolunu kullanır. Dönen yanıttan:
   - events
   - lineups
   - fixture statistics
   - fixture player statistics
   alanlarını saklar.

4. Gömülü veri eksikse opsiyonel fallback:
   - `/fixtures/events?fixture=...`
   - `/fixtures/lineups?fixture=...`
   - `/fixtures/statistics?fixture=...`
   - `/fixtures/players?fixture=...`

5. `/players?league=...&season=...&page=...`  
   Sezon oyuncu istatistiklerini sayfa sayfa toplar.

6. `/injuries?league=...&season=...`  
   Sakatlık + ceza (suspension) listesini **1 çağrıda tüm sezon** için çeker (fixture bazlı değil, çok daha ucuz). Önce `league_coverage.coverage.injuries` kontrol edilir, `false` ise atlanır.

   **Önemli:** API'nin `type` alanı pratikte hep `"Missing Fixture"` değerini taşıyor — Injury/Suspension ayrımı gerçekte `reason` metninde (örn. "Thigh Injury" vs "Suspended"). Bu ayrımı model tarafında `reason` üzerinden kendimiz türeteceğiz.

## Neden doğrudan `.zip` dosyasına yazmıyor?

Render Cron Job / One-Off Job yerel diski kalıcı değildir. İş bitince oraya yazılan dosyaya güvenemeyiz.

Bu nedenle:

**API-Football → Collector → Render Postgres → Export Web Service → ZIP**

akışı kullanılıyor.

Toplama işi yarıda kesilirse veriler Postgres'te kalır ve tekrar çalıştırınca script kaldığı yerden devam eder.

**Tarihsel sezonlar** tamamlandıktan sonra state kilidi korunur ve gereksiz yere tekrar indirilmez. **Aktif sezon** için ise istenirse `REFRESH_ACTIVE_SEASON=true` ile fixtures, injuries ve sezon oyuncu verileri yeniden tazelenebilir.

---

# Kurulum – terminal kullanmadan

## 1. Bu paketi GitHub reposuna yükle

ZIP'i aç ve içindeki dosyaları yeni bir GitHub reposunun köküne yükle.

Dosyalar:

- `collector.py`
- `export_app.py`
- `requirements.txt`
- `render.yaml`

## 2. Render'da Blueprint oluştur

Render Dashboard → **New → Blueprint** → GitHub reposunu seç.

`render.yaml` otomatik olarak:

- kalıcı Postgres,
- veri indirme Web Service'i,
- manuel tetiklenebilir collector Cron Job

oluşturur.

Blueprint oluşturma sırasında Render senden:

`API_FOOTBALL_KEY`

değerini ister. Anahtarı sadece Render Dashboard'a gir; GitHub'a koyma.

## 3. Toplu çekimi başlat

Render Dashboard:

**football-bulk-collector → Runs → Trigger Run**

Bundan sonra terminal gerekmez.

Script:

- hangi sezon/lig kaldığını veritabanından kontrol eder,
- tamamlanan adımları yeniden çekmez,
- API rate-limit header'larını izler,
- günlük limit kritik seviyeye gelirse veriyi kaydedip temiz şekilde durur.

Tekrar `Trigger Run` dersen kaldığı yerden devam eder.

### One-Off Job kullanmak istersen

Aynı kodun komutu:

`python collector.py`

Render One-Off Jobs aynı build ve environment variable'ları bir base service'ten alabilir. Ancak Render'ın güncel dokümantasyonunda **One-Off Job oluşturma Render API üzerinden** tarif ediliyor; Dashboard'dan tamamen elle tek-tık akış isteyen kullanıcı için yukarıdaki **Cron Job → Trigger Run** yolu daha basittir.

---

# API planı hakkında

Script API çağrılarını mümkün olduğunca azaltır.

En büyük optimizasyon:

`/fixtures?ids=...`

ile **20 fixture'ı tek istekte** çekmesidir.

Bu yüzden eski "her maç için lineups + statistics + players = 3 ayrı çağrı" yaklaşımından çok daha ucuzdur.

Yine de Free plan:

- günlük yalnızca 100 istek,
- tarihsel sezon erişimi sınırlı

olduğu için 2 sezon × 5 lig toplu tarihsel çekimde ücretli API-Football planı daha güvenlidir.

Script `x-ratelimit-requests-remaining` header'ını takip eder ve son `20` isteği güvenlik payı olarak bırakır.

---

# Veriyi indir

Toplama tamamlandıktan sonra:

Render Dashboard → `football-dataset-export` → Environment

içinden `DOWNLOAD_TOKEN` değerini gör.

Sonra tarayıcıda:

`https://SENIN-SERVIS-ADRESIN.onrender.com/status?token=DOWNLOAD_TOKEN`

ile satır sayılarını kontrol et.

Veri tamam ise:

`https://SENIN-SERVIS-ADRESIN.onrender.com/download?token=DOWNLOAD_TOKEN`

adresini aç.

Tarayıcı tek bir ZIP indirir.

ZIP içinde:

- `league_coverage.jsonl`
- `fixtures.jsonl`
- `fixture_details.jsonl`
- `injuries.jsonl`
- `season_players.jsonl`
- `collection_runs.jsonl`
- `api_call_log.jsonl`
- `manifest.json`

bulunur.

Her `.jsonl` dosyasında her satır tek bir JSON kaydıdır.

---

# Model için özellikle işimize yarayacak alanlar

`fixture_details.statistics` içinden tipik olarak:

- shots on target
- shots off target
- total shots
- **blocked shots**
- shots inside box
- shots outside box
- **corner kicks**
- ball possession
- goalkeeper saves
- passes / pass accuracy
- cards

gelecektir.

Bu, özellikle:

- **Over 2.5**
- **BTTS**
- **korner tahmini**

modelleri için değerlidir.

`fixture_details.lineups`:

- ilk 11
- formasyon
- yedekler
- teknik direktör

bilgisini sağlar.

`fixture_details.players`:

- minutes
- rating
- shots
- passes / key passes
- tackles
- interceptions
- duels
- dribbles
- cards

gibi maç bazlı oyuncu bilgilerini tutar.

---

# Güvenlik

- API key kaynak koda yazılmaz.
- `DOWNLOAD_TOKEN` olmadan dataset indirilemez.
- Postgres `ipAllowList: []` ile dış internete kapalıdır.
- Collector tekrar çalıştırılabilir; `UPSERT` + `collection_state` ile duplicate üretmez.

---

# Ayarlanabilir environment variable'lar

| Değişken | Varsayılan |
|---|---|
| `SEASONS` | `2024,2025` |
| `LEAGUES` | 5 büyük lig |
| `BATCH_SIZE` | `20` |
| `DAILY_REQUEST_RESERVE` | `20` |
| `INCLUDE_SEASON_PLAYERS` | `true` |
| `INCLUDE_INJURIES` | `true` |
| `REFRESH_ACTIVE_SEASON` | `false` |
| `ACTIVE_SEASON` | `2026` |
| `ENABLE_FALLBACK` | `true` |
| `MAX_FALLBACK_REQUESTS` | `500` |
| `ONLY_FINISHED_FOR_DETAILS` | `true` |

## Daha sonra 2026/27 eklemek

İlk kez 2026/27 sezonunu toplamak için Render Environment'ta:

`SEASONS=2024,2025,2026`

`ACTIVE_SEASON=2026`

olarak ayarla ve `REFRESH_ACTIVE_SEASON=false` bırak. Böylece ilk backfill resume-safe çalışır; kota nedeniyle tekrar tetiklersen kaldığı yerden devam eder.

İlk aktif sezon backfill'i tamamlandıktan sonra güncel veriyi yenilemek istediğin çalıştırmada:

`REFRESH_ACTIVE_SEASON=true`

yap. Bu mod coverage + fixture listesini, injuries snapshot'ını ve sezon oyuncu sayfalarını yeniden çeker. Yeni biten maçların detayları da otomatik eklenir. Refresh tamamlandıktan sonra sürekli gereksiz çağrı yapmamak için değişkeni tekrar `false` yapabilirsin.

Tarihsel 2024/25 ve 2025/26 sezonlarının tamamlanmış state'leri bu işlemden etkilenmez.
