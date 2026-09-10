# V1 Yapısal Walk-Forward Bulguları — 10 Eylül 2026

Bu belge `edge_structure_audit.py` tarafından production veritabanında üretilen ilk iki-fold ileri-zaman denetiminin özetidir. Hiçbir sonuç tek başına production feature aktivasyonu anlamına gelmez.

## Tasarım

- Production çekirdeği: `model_engine_v1`.
- Fold 1: 2023/24 geçmişi -> 2024/25 testi; test sezonunda maçlar yalnız tahmin edildikten sonra geçmişe eklenir.
- Fold 2: 2023/24 + 2024/25 geçmişi -> 2025/26 testi; aynı causal güncelleme uygulanır.
- Her fold: Big Five toplam 1.75k civarı maç.
- Haftalık seçim teşhisi: her ISO haftasının ranking'e göre ilk 10 seçimi.

## Ana sonuçlar

### O/U 2.5: production V1 vs tarihsel no-vig piyasa

| Dönem | N | V1 Brier | Market Brier | V1 - Market |
|---|---:|---:|---:|---:|
| 2024/25 ileri test | 1,752 | 0.24310 | 0.23808 | +0.00502 |
| 2025/26 ileri test | 1,751 | 0.24592 | 0.24165 | +0.00427 |
| Birleşik | 3,503 | 0.24451 | 0.23987 | +0.00464 |

Pozitif fark marketin daha düşük Brier verdiği anlamına gelir. Birleşik yaklaşık %95 fark aralığı +0.00252 ile +0.00676'dır. Sonuç iki ayrı ileri-zaman fold'unda aynı yöndedir.

**Karar:** O/U 2.5 için V1'i piyasanın yerine koymak yanlış olur. Uluslararası no-vig piyasa referansı production kararında korunmalıdır.

### Haftalık Top-10 yapısı

| Kırılım | N | İsabet |
|---|---:|---:|
| 2024/25 fold | 370 | %67.84 |
| 2025/26 fold | 370 | %68.65 |
| Birleşik | 740 | %68.24 |

Fold'lar arası toplam performans istikrarlı görünmektedir. Ancak kompozisyon dengeli değildir:

| Market | N | Pay | İsabet |
|---|---:|---:|---:|
| 8.5 Korner Üst | 699 | %94.5 | %68.38 |
| 2.5 Üst | 30 | %4.1 | %73.33 |
| BTTS | 11 | %1.5 | %45.45 |

2.5 Üst ve özellikle BTTS için örneklem küçüktür. `Top-10 %68` ifadesi pratikte ağırlıklı olarak **korner modelinin performansını** anlatmaktadır; üç marketin eşit kanıtı değildir.

### Haftalık Top-10 lig kırılımı

| Lig | N | İsabet | Not |
|---|---:|---:|---|
| La Liga | 89 | %77.53 | Büyük ölçüde korner; umut verici fakat fold-bazlı alt grup stabilitesi ayrıca doğrulanmalı |
| Premier League | 439 | %68.79 | En büyük örneklem; büyük ölçüde korner |
| Serie A | 40 | %65.00 | Küçük örneklem |
| Bundesliga | 145 | %63.45 | Ana ortalamanın altında |
| Ligue 1 | 27 | %59.26 | Çok küçük örneklem |

Bu tablo ligleri production'dan otomatik çıkarmak için kullanılmaz. Önce lig×market sonucunun iki fold'da da aynı yönde olup olmadığı ayrı doğrulanmalıdır.

### Güven/kalibrasyon teşhisi

Haftalık Top-10 içerisinde ham V1 skor bantları:

| Ham V1 bandı | N | Ortalama söylenen olasılık | Gerçek isabet |
|---|---:|---:|---:|
| 0.65–0.70 | 289 | %68.10 | %68.17 |
| 0.70–0.75 | 269 | %72.16 | %64.68 |
| 0.75–0.80 | 127 | %76.88 | %72.44 |
| 0.80+ | 31 | %82.22 | %74.19 |

Dolayısıyla raw V1 yüzdesini kullanıcıya kusursuz kalibre edilmiş gerçek olasılık diye sunmak doğru değildir. Production'da uluslararası piyasa doğrulaması korunmalı; kullanıcı arayüzünde sayı `V1 model tahmini` olarak adlandırılmalıdır. İleride post-hoc kalibrasyon ancak ayrı forward-validasyonla denenebilir.

## Odds movement

İlk leakage-safe movement auditinde değerlendirilebilir historical production-V1 pick sayısı **0** çıktı. Bunun anlamı movement'ın işe yaramadığı değil; karar anından önce en az iki geçerli ham OddsPapi snapshot'ı + sonradan gerçekleşen maç sonucu ile eşleşen tarihsel production örneklemi henüz oluşmamış.

**Karar:** odds movement production'a alınmaz. Ham snapshotlar ileriye dönük biriktirilir; yeterli örneklem oluşunca aynı önceden tanımlı test tekrarlanır.

## Sonraki araştırma sırası

1. Mevcut lig×market performansının iki fold'daki stabilitesini ayrı raporla.
2. Production'ın gerçek Perşembe dondurulmuş kararlarını prospectively settle et; High Confidence ve Value listelerini ayrı Brier/hit-rate/ROI/CLV ile izle.
3. Odds movement için karar-anı snapshot örneklemini biriktir; yeterli N olmadan kullanma.
4. Mevcut Elo'yu incremental forward testte dene.
5. Ancak bunlardan sonra referee ve set-piece/style gibi yeni veri kaynaklarına yatırım yap.

Hava/saha boyutu düşük öncelikte kalır.
