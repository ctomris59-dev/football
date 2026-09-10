# Big Five Football Intelligence — Perşembe Karar Sistemi

Bu repo artık tek bir kullanıcı akışına göre düzenlenmiştir:

> **Perşembe hazırlık → Türkiye oranı açılınca uluslararası no-vig doğrulama → iki listeyi oluşturup dondurma → bahis yap → bitti.**

Amaç 5 büyük ligde üç hedef market için erken ve oynanabilir seçim üretmektir:

- 2.5 Gol Üst
- KG Var
- 8.5 Toplam Korner Üst

Sistem hiçbir zaman seçim sayısını doldurmak için zayıf maç eklemez ve hiçbir bahis için garanti iddiasında bulunmaz.

---

## 1. Canlı karar mimarisi

```text
Perşembe 18:00 Türkiye
        │
        ▼
Güncel fikstür + form + gol/şut/korner geçmişi
        │
        ├─ Güncel sakatlık / oyuncu bağlamı
        ├─ Starter continuity / player coverage
        └─ Rest / fikstür sıkışıklığı
        │
        ▼
Kalibre V1 model olasılıkları
        │
        ▼
Türkiye resmi oynanabilir oranını bekle
        │
        ▼
Uluslararası paired same-book fiyatlardan no-vig fair market
        │
        ▼
Model ↔ uluslararası piyasa sanity kontrolü
        │
        ▼
Türkiye fiyatına karşı edge / EV
        │
        ▼
🛡️ YÜKSEK GÜVEN
💰 YÜKSEK GÜVEN + VALUE
        │
        ▼
Haftalık karar dondurulur
```

**T-3 / T-1 / confirmed-lineup yeniden seçim akışı yoktur.** Kullanıcının bahis kararı Perşembe listesidir. Sonraki oran veya kadro hareketleri yeni kupon üretmez.

---

## 2. Fiyatların rolleri

### Türkiye oranı

Türkiye resmi İddaa fiyatı **tek executable price**'dır.

- bahis bu fiyat üzerinden yapılır;
- model EV bu fiyat üzerinden hesaplanır;
- ilk görülen geçerli fiyat `opening_price` olarak değişmez biçimde saklanır.

### Uluslararası piyasa

Uluslararası fiyat **bahis fiyatı değildir**. Sadece bağımsız fair-market doğrulamasıdır.

- aynı bookmaker'ın iki karşıt yönü birlikte bulunmadan no-vig hesaplanmaz;
- aşırı veya bozuk fiyat çiftleri reddedilir;
- birden fazla bookmaker varsa konsensüs kullanılır;
- model ile uluslararası fair olasılık arasında aşırı ayrışma varsa seçim bloklanır.

Bu ayrım, geçmişte görülen `29 / 12 / 26` gibi bozuk corner fiyatlarının value listesine sızmasını önler.

---

## 3. İki liste

### 🛡️ YÜKSEK GÜVEN

Bir seçim için:

- model güveni varsayılan olarak `>= 0.70`;
- erken veri kalitesi filtresi geçilmiş;
- ciddi bilinen sakatlık / kaleci / kadro sürekliliği blocker'ı bulunmuyor;
- Türkiye'de oynanabilir güncel fiyat mevcut;
- uluslararası piyasa doğrulaması mevcut ve modelle anormal çatışmıyor;
- bir maçtan en fazla bir seçim alınır.

### 💰 YÜKSEK GÜVEN + VALUE

Yukarıdakilere ek olarak:

- model güveni `>= 0.65`;
- modelin Türkiye fiyatına karşı edge'i `>= 0.015`;
- model EV `>= 0.02`;
- uluslararası fair piyasanın da Türkiye fiyatına karşı pozitif avantajı vardır;
- uluslararası referans kalite filtresini geçmiştir.

Value listesi boşsa sistem açıkça **uygun value bahis yok** sonucunu verir.

---

## 4. Perşembe otomasyonu

Production akışı iki Render cron ile çalışır.

### 4.1 `football-weekly-preview-refresh`

- zaman: Perşembe 15:00 UTC = 18:00 Türkiye;
- komut: `python live_refresh.py`;
- görev: güncel fikstür, FotMob availability ve player context hazırlığı;
- aynı koşuda `opening_watch` ilk kez denenir.

### 4.2 `football-thursday-opening-watch`

- Render schedule: `*/15 * * * 4,5`;
- komut: `python thursday_schedule_tick.py`;
- script Europe/Istanbul saatine göre sadece:
  - Perşembe 18:00 sonrası,
  - Cuma 12:00'ye kadar
  çalışır;
- önce `/thursday-list` kontrol edilir;
- hafta zaten finalized ise hiçbir provider çağrısı yapılmaz;
- değilse `/opening-watch` çağrılır.

Bu cron sayesinde listeyi dondurmak için elle endpoint çağırmak gerekmez.

Render cron zamanları UTC'dir; zaman dilimi kontrolü script içinde `Europe/Istanbul` ile yapılır.

---

## 5. Web sayfası ve API

Production servis:

`football-dataset-export`

Kullanıcı ekranı:

- `GET /persembe`
- `GET /thursday` — alias

Bu sayfa yalnızca:

- durum,
- 🛡️ Yüksek Güven,
- 💰 Yüksek Güven + Value

gösterir.

API:

- `GET /health` — public health
- `GET /opening-watch` — dar public freeze/watch endpoint'i
- `GET /thursday-list` — immutable haftalık sonuç
- `POST /refresh` — korumalı manuel hazırlık
- `GET /status` — korumalı teknik durum
- `GET /download` — korumalı aktif veri export'u

`AUTO_LIVE_REFRESH=false` production varsayılanıdır. Deploy işlemi veri toplama zamanlayıcısı değildir.

---

## 6. Canlı karar verileri

Canlı seçim yolunda yalnız karar kalitesine doğrudan katkı veren veriler kullanılır:

- 5 büyük lig fikstürü;
- tarihsel skor / gol;
- şut ve isabetli şut;
- korner;
- home/away form;
- güncel roster;
- gerçek geçmiş start verileri;
- starter continuity;
- player coverage;
- bilinen güncel sakatlık etkisi;
- kaleci sakatlık sinyali;
- rest days / fikstür sıkışıklığı;
- V1 calibrated model probability;
- Türkiye resmi fiyatı;
- uluslararası paired same-book no-vig fair probability.

Tarihsel ve araştırma tabloları model doğrulaması için tutulabilir; fakat haftalık kullanıcı kararına otomatik olarak dahil edilmez.

---

## 7. Aktif kaynaklar

### Football-Data / ESPN

Gol, şut, korner, fikstür ve form tabanı.

### ESPN roster / historical starts

Beklenen kadro gücü ve starter continuity için kullanılır. Günler öncesinden resmi ilk 11 olduğu iddia edilmez.

### FotMob

Güncel sakatlık/availability kaynağıdır. Public endpoint değişebileceği için fail-soft çalışır; başarısızlık `0 sakat` anlamına gelmez.

### Türkiye İddaa

Gerçek oynanabilir fiyat ve opening price kaynağıdır.

### OddsPapi

Yalnız uluslararası paired same-book no-vig fair-market referansı için kullanılır. Türkiye'de bahis yapılan fiyat değildir.

### BBS / SofaScore / eski T-1-T-3 katmanları

Final Perşembe karar yolunun parçası değildir. Eski araştırma/legacy kod veya veri varsa production seçim politikasını yönetmez.

---

## 8. Turkey price store

`turkey_value_workflow.py` artık liste üretmez.

Sadece:

- fiyat doğrulama,
- snapshot saklama,
- immutable opening price,
- freshest executable Turkey price

işlerini yapar.

**Tek production liste builder:** `thursday_decision_engine.build_decision()`.

---

## 9. Ana environment değişkenleri

### Genel / secret

| Değişken | Değer / tür | Açıklama |
|---|---|---|
| `DATABASE_URL` | Render Postgres | ana veritabanı |
| `DOWNLOAD_TOKEN` | generated secret | korumalı API/export |
| `VALIDATION_TRIGGER_TOKEN` | generated secret | doğrulama endpoint'i |
| `ODDSPAPI_API_KEY` | secret / `sync:false` | uluslararası fair-market verisi |
| `AUTO_LIVE_REFRESH` | `false` | deploy sırasında refresh yok |

`BBS_API_KEY` final Perşembe yolunda zorunlu değildir ve Blueprint'in aktif karar secret'ı olarak tanımlanmaz.

### Türkiye fiyatı

| Değişken | Varsayılan |
|---|---:|
| `TR_PRICE_MIN` | `1.01` |
| `TR_PRICE_MAX` | `5.00` |
| `TR_PRICE_MAX_AGE_HOURS` | `6` |

### Yüksek güven / value

| Değişken | Varsayılan |
|---|---:|
| `HIGH_CONFIDENCE_MIN` | `0.70` |
| `VALUE_MIN_CONFIDENCE` | `0.65` |
| `VALUE_MIN_EDGE` | `0.015` |
| `VALUE_MIN_EV` | `0.02` |
| `THURSDAY_LIST_LIMIT` | `10` |

### Erken veri kalitesi

| Değişken | Varsayılan |
|---|---:|
| `THURSDAY_MIN_MODEL_DATA_QUALITY` | `0.80` |
| `THURSDAY_MIN_PLAYER_COVERAGE` | `0.70` |
| `THURSDAY_MIN_STARTER_CONTINUITY` | `0.50` |
| `THURSDAY_MAX_KNOWN_INJURY_IMPACT` | `0.20` |
| `THURSDAY_MIN_REST_DAYS` | `2.5` |
| `THURSDAY_CANDIDATE_COVERAGE_MIN` | `0.75` |
| `THURSDAY_NO_CANDIDATE_BULLETIN_COVERAGE_MIN` | `0.70` |

### Uluslararası doğrulama

| Değişken | Varsayılan |
|---|---:|
| `INTERNATIONAL_REFERENCE_MAX_AGE_HOURS` | `6` |
| `INTERNATIONAL_REFRESH_MAX_AGE_HOURS` | `2` |
| `INTERNATIONAL_FIXTURE_TOLERANCE_HOURS` | `12` |
| `INTERNATIONAL_MATCH_SCORE_MIN` | `1.55` |
| `INTERNATIONAL_MATCH_SIDE_MIN` | `0.72` |
| `INTERNATIONAL_MATCH_MARGIN_MIN` | `0.08` |
| `INTERNATIONAL_MAX_DISPERSION` | `0.06` |
| `INTERNATIONAL_MAX_SHARP_DELTA` | `0.08` |
| `INTERNATIONAL_MIN_TR_EDGE` | `0.01` |
| `INTERNATIONAL_MIN_TR_EV` | `0.01` |
| `INTERNATIONAL_MAX_MODEL_DIVERGENCE` | `0.12` |

Bu production eşikleri `render.yaml` içinde açıkça pinlenmiştir; kod varsayılanına gizlice bırakılmaz.

---

## 10. Secret / Blueprint davranışı

Secret değerler GitHub'a yazılmaz.

`ODDSPAPI_API_KEY` Blueprint'te `sync:false` olarak belgelenmiştir. Render'ın davranışı gereği:

- yeni Blueprint kurulurken değer dashboard'da verilmelidir;
- mevcut service update'lerinde `sync:false` değeri Blueprint tarafından üzerine yazılmaz;
- `render.yaml` içinde olmayan mevcut env değişkenleri de Render tarafından otomatik silinmez.

Bu yüzden secret'ın varlığı ayrıca canlı service konfigürasyonunda korunmalıdır.

---

## 11. Tarihsel collector

`football-bulk-collector` haftalık karar cron'u değildir.

- yıllık/manuel tarihsel veri bakım işidir;
- `API_FOOTBALL_KEY` kullanabilir;
- Perşembe listesi bunun çalışmasına bağlı değildir.

---

## 12. CI / production-ready tanımı

Her main push'ta:

- Python compile/import kontrolleri;
- genel `football-ci`;
- Thursday market/no-vig validation

geçmelidir.

Production-ready kabulü için:

1. ilgili final SHA CI'da başarılı olmalı;
2. aynı SHA Render web service'te live olmalı;
3. cron servisleri aynı repo/main'i kullanmalı;
4. `/persembe`, `/thursday-list` ve `/opening-watch` yolu çalışır durumda olmalı.

---

## 13. Fail-closed prensipleri

- Türkiye fiyatı yok → liste freeze edilmez.
- Uluslararası fair-market referansı yok → value/decision zorlanmaz.
- Same-book iki taraf yok → no-vig yok.
- Bozuk fiyat → market referansına girmez.
- Model/piyasa aşırı ayrışıyor → seçim bloklanır.
- Sakaltık/oyuncu verisi eksik → sağlıklı/0 varsayılmaz.
- Kaliteyi geçen yeterli bahis yok → liste sayısı zorla doldurulmaz.

Temel prensip:

> **Eksik veya şüpheli veriyle bahis üretmek yerine seçimi blokla.**
