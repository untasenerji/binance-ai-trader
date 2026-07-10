# Binance AI Trader — Tam Teşekküllü Teknik ve Ürün Şartnamesi

**Belge sürümü:** 1.0
**Doğrulama tarihi:** 10 Temmuz 2026
**Hedef ortam:** Windows + VS Code + OpenAI Codex
**Hedef borsa:** Binance Global, USDⓈ-M Futures
**Hedef çalışma şekli:** Yerel bilgisayarda çalışan, gerçek hesap bağlantısı son aşamada açılan, düşük sermayeli canlı pilot

> Bu sistem kâr garantisi vermez. Kaldıraçlı vadeli işlemler anaparanın tamamının kaybedilmesine yol açabilir. Programın ana görevi “tahmin etmek” değil; ölçülebilir stratejileri, kademeli emirleri, maliyetleri ve riski disiplinli biçimde yürütmektir.

---

## 1. Yönetici özeti

Kurulacak ürün, yalnızca “AI long/short desin” şeklinde çalışan basit bir bot olmayacaktır. Sistem dört bağımsız katmandan oluşacaktır:

1. **Piyasa ve hesap veri katmanı:** Binance REST ve WebSocket verilerini toplar, doğrular ve saklar.
2. **Strateji ve planlama katmanı:** Sinyal üretir; kademeli giriş, stop, hedef ve pozisyon miktarlarını hesaplar.
3. **Risk ve yürütme katmanı:** Emirleri gönderir, kısmi dolumları takip eder, sunucu tarafı stopları kurar ve bağlantı hatalarında hesabı korur.
4. **OpenAI destek katmanı:** Piyasa rejimini sınıflandırır, sinyal eleştirisi ve işlem sonrası analiz üretir; ancak hiçbir zaman tek başına emir açamaz veya Binance anahtarına erişemez.

İlk canlı pilotun temel sınırı 20 USDT olacaktır. Fakat “20 USDT” kod içine gömülü bir sayı değil, bağlantı sihirbazında kullanıcı tarafından onaylanan bir **sermaye üst limiti** olacaktır. Sistem, hesapta daha fazla bakiye bulunsa bile pilot sınırının üzerinde pozisyon oluşturmayacaktır.

Kullanıcı arayüzü tek başına güvenlik otoritesi değildir. V1 pilot değerleri için UI ayarlarının üzerinde backend tarafından zorunlu olarak uygulanan mutlak güvenlik tavanları bulunacaktır. UI yalnız bu tavanlara eşit veya daha düşük değer gönderebilir; tavanı aşan her istek backend tarafından reddedilir ve hiçbir emir planına dönüşmez.

Canlı API anahtarları, bütün kod, test, arayüz, güvenlik ve hata senaryoları tamamlanmadan istenmeyecektir. Son fazda kullanıcı anahtarları **VS Code sohbetine değil**, yerel bağlantı sihirbazına girecektir.

---

## 2. Kilitli ürün kararları

| ID | Karar | Durum |
|---|---|---|
| D-001 | Binance USDⓈ-M Futures kullanılacak. | Kilitli |
| D-002 | V1, tek hesap ve tek yerel kurulum için modüler monolit olacak. | Kilitli |
| D-003 | Gerçek emir adaptörü geliştirilecek fakat son faza kadar kapalı kalacak. | Kilitli |
| D-004 | İlk gerçek işlem testnet yerine canlı pilot olabilir; önce canlı `order/test` ve salt-okunur kontroller çalışacaktır. | Kilitli |
| D-005 | One-way position mode kullanılacak; aynı sembolde eşzamanlı long ve short V1’de yasak. | Kilitli |
| D-006 | Yalnızca isolated margin; cross ve auto-add-margin yasak. | Kilitli |
| D-007 | Kaldıraç ve sermaye sınırı kullanıcı onayı olmadan yükseltilemez. | Kilitli |
| D-008 | Her pozisyon sunucu tarafında STOP_MARKET koruması olmadan açık bırakılamaz. | Kilitli |
| D-009 | Stop kabul edilmezse pozisyon azaltma/kapatma acil akışı çalışır. | Kilitli |
| D-010 | Martingale, zarara kontrolsüz ekleme ve likidasyona kadar bekleme yasak. | Kilitli |
| D-011 | Kademeler toplam risk bütçesine göre hesaplanır; “her düşüşte daha fazla al” mantığı varsayılan değildir. | Kilitli |
| D-012 | Minimum emir şartı riski aşıyorsa işlem atlanır; miktar zorla büyütülmez. | Kilitli |
| D-013 | OpenAI emir yetkisine ve Binance kimlik bilgilerine sahip olmayacak. | Kilitli |
| D-014 | AI, `advisory` veya `veto_only` çalışabilir; `order_authority` modu bulunmayacak. | Kilitli |
| D-015 | Para çekme ve transfer API yetkileri kapalı olacaktır. | Kilitli |
| D-016 | Tüm parasal ve fiyat hesapları `Decimal` ile yapılacak; binary float yasak. | Kilitli |
| D-017 | Her emir benzersiz client ID ve yerel idempotency kaydıyla izlenecek. | Kilitli |
| D-018 | Binance gerçeği her zaman yerel veritabanından üstündür; yeniden başlatmada reconciliation yapılır. | Kilitli |
| D-019 | Kısmi dolum, çift olay, olay sırası bozukluğu ve 503 “durum belirsiz” senaryoları test edilmeden canlı mod açılmaz. | Kilitli |
| D-020 | OpenAI web search, canlı sinyal döngüsünün zorunlu parçası olmayacak; maliyet ve gecikme sınırına tabidir. | Kilitli |
| D-021 | Canlı işlem tek bir UI düğmesiyle açılmaz; çok aşamalı aktivasyon ve kullanıcı onayı gerekir. | Kilitli |
| D-022 | Bot, başlangıçta var olan manuel pozisyon veya emirle aynı sembolde çalışmayı reddeder. | Kilitli |
| D-023 | Her faz kendi test raporunu üretmeden sonraki faza geçilemez. | Kilitli |
| D-024 | Codex hiçbir zaman `.env`, keyring veya gerçek secret değerlerini ekrana/loga yazmayacak. | Kilitli |
| D-025 | Kâr hedefi değil, risk uyumu ve maliyet sonrası beklenen değer ölçülecek. | Kilitli |
| D-026 | V1 pilot profilindeki risk, sermaye, kaldıraç ve kademe tavanları backend'de mutlak hard limit olarak uygulanır. UI yalnız daha düşük değer seçebilir; daha yüksek isteği backend reddeder. | Kilitli |
| D-027 | Sistem işlem sayısını doldurmak veya belirli sıklıkta işlem açmak için emir üretmez. Yalnız strateji, risk, veri kalitesi, maliyet ve geçerlilik koşulları birlikte sağlandığında işlem adayı oluşturur. Uygun fırsat yoksa sıfır işlem doğru sistem davranışıdır. Zaman dilimi ve tarama sıklığı fırsat arama hızını değiştirir; işlem açma zorunluluğu yaratmaz. | Kilitli |

---

## 3. Resmî API araştırmasından çıkan zorunlu teknik gerçekler

Binance güncel dokümantasyonuna göre:

- Normal emirler `POST /fapi/v1/order` üzerinden ve imzalı istekle gönderilir. [B1]
- TP/SL ve trailing stop gibi koşullu emirler Algo Order servisine taşınmıştır; güncel uç `POST /fapi/v1/algoOrder` olmalıdır. [B2][B3]
- `ISOLATED` ve `CROSSED` marjin tipi sembol bazında değiştirilebilir; V1 yalnızca `ISOLATED` kullanacaktır. [B4]
- Kaldıraç sembol bazında ayarlanabilir; sistem sunucudan dönen gerçek değeri doğrulamadan emir açmayacaktır. [B5]
- Position mode değişikliği tüm sembolleri etkiler ve açık emir/pozisyon varken reddedilebilir. V1 One-way Mode’u başlangıç kontrolünde doğrulayacaktır. [B6]
- `exchangeInfo`, sembol filtrelerini ve rate limit yapılarını sağlar; quantity/price kesinlikle sabit ondalıkla varsayılmayacaktır. [B7]
- Kullanıcının gerçek maker/taker komisyonu API’den okunabilir. [B8]
- Kullanıcıya özel leverage bracket ve notional limit API’den okunabilir. [B9]
- User Data Stream 60 dakika sonra kapanır; keepalive gerekir. [B10]
- WebSocket bağlantıları 24 saatle sınırlıdır; ping/pong ve kontrollü yeniden bağlantı gerekir. [B11]
- Yeni routed WebSocket yollarında public/market/private ayrımı vardır; eski URL varsayımları kullanılmamalıdır. [B12]
- HTTP 503’ün bazı mesajlarında emrin durumu **UNKNOWN** olabilir; aynı emri körlemesine tekrar göndermek çift pozisyon yaratabilir. Önce WebSocket veya order query ile doğrulama gerekir. [B13]
- 429 sonrasında geri çekilmemek 418 IP banına yol açabilir. [B13]
- `order/test`, emri matching engine’e göndermeden canlı parametre doğrulaması yapar. [B14]
- Auto-Cancel All Open Orders uç noktası bir heartbeat/“dead-man switch” olarak kullanılabilir. [B15]

OpenAI güncel dokümantasyonuna göre:

- Yeni uygulamalar için Responses API, tool calling ve Structured Outputs tercih edilmelidir. [O1][O2][O3]
- GPT-5.6 ailesinde maliyet/kalite dengesi için `gpt-5.6-terra`, yüksek hacimli daha ekonomik kullanım için `gpt-5.6-luna` önerilmektedir. Model seçimi konfigürasyonla yapılacaktır. [O4]
- Codex VS Code uzantısı kod okuyabilir, düzenleyebilir ve komut/test çalıştırabilir. [O5]
- Uzun işlerde `/plan`, `/goal`, `AGENTS.md` ve doğrulama döngüsü kullanılabilir. [O6][O7][O8]
- MCP ayarları proje kapsamındaki `.codex/config.toml` içinde tutulabilir; Codex IDE ve CLI aynı yapılandırmayı paylaşır. [O9]
- OpenAI Docs MCP’nin resmî sunucu adresi `https://developers.openai.com/mcp`’dir. [O10]

---

## 4. Sistem mimarisi

```text
┌───────────────────────────────────────────────────────────────┐
│                    LOCAL WEB UI (React/TS)                    │
│ Dashboard · Kademe Planı · Risk · Emirler · AI · Sağlık      │
└──────────────────────────────┬────────────────────────────────┘
                               │ HTTPS/localhost + WebSocket
┌──────────────────────────────▼────────────────────────────────┐
│                  APPLICATION API (FastAPI)                    │
│ Auth-lite · Config · Query API · Commands · Event streaming   │
└───────────┬──────────────────┬──────────────────┬─────────────┘
            │                  │                  │
┌───────────▼──────────┐ ┌─────▼────────────┐ ┌──▼──────────────┐
│ Strategy & Planner   │ │ Risk Engine      │ │ AI Orchestrator │
│ signals, ladders     │ │ hard limits      │ │ advisory/veto   │
└───────────┬──────────┘ └─────┬────────────┘ └──┬──────────────┘
            │                  │                  │
            └──────────┬───────┘                  │ Responses API
                       │                          │
┌──────────────────────▼──────────────────────────▼──────────────┐
│                  EXECUTION ORCHESTRATOR                        │
│ State machine · idempotency · partial fills · reconciliation   │
└───────────────────┬───────────────────────┬────────────────────┘
                    │                       │
          ┌─────────▼──────────┐  ┌────────▼──────────┐
          │ Binance REST       │  │ Binance WebSocket │
          │ orders/account     │  │ market/user data  │
          └─────────┬──────────┘  └────────┬──────────┘
                    │                       │
          ┌─────────▼───────────────────────▼──────────┐
          │ PostgreSQL + append-only event/audit log   │
          └────────────────────────────────────────────┘
```

### 4.1 Mimari yaklaşım

- **Modüler monolit:** Finansal doğruluk için tek işlem alanı ve net transaction sınırları; V1’de gereksiz mikroservis yok.
- **Event-driven iç yapı:** Binance olayları, strateji olayları, emir olayları ve risk olayları tipli event’lerdir.
- **Adapter pattern:** `ExchangeAdapter`, `AIProvider`, `Strategy`, `RiskPolicy`, `NotificationProvider` arayüzleri.
- **State machine:** Her trade planı açıkça tanımlanmış durumlardan geçer.
- **Append-only audit:** Kritik emir ve risk olayları değiştirilemez kayıt olarak saklanır.
- **Reconciliation-first:** Başlangıçta ve bağlantı geri geldiğinde Binance hesap durumu yeniden okunur.

### 4.2 Önerilen teknoloji

| Katman | Seçim | Gerekçe |
|---|---|---|
| Backend | Python 3.12+ | Binance/OpenAI entegrasyonu, Decimal, test ekosistemi |
| API | FastAPI + Pydantic v2 | Tipli sözleşme, async I/O, otomatik OpenAPI |
| DB | PostgreSQL + SQLAlchemy 2 + Alembic | Canlı para için güvenilir transaction ve migration |
| Cache/lock | İlk sürümde süreç içi; ihtiyaç doğrulanırsa Redis | Gereksiz bağımlılığı önleme |
| Frontend | React + TypeScript + Vite | Hızlı yerel dashboard, güçlü tip güvenliği |
| UI data | TanStack Query + native WebSocket | Gerçek zamanlı veri ve cache |
| Grafik | Lightweight Charts veya doğrulanmış eşdeğer | Finansal grafik katmanları |
| Test | pytest, Hypothesis, respx/httpx mocks, Playwright | Birim, property, kontrat ve E2E |
| Paket | Docker Compose tercihli; Windows yerel fallback | Tek komutlu kurulum ve tekrarlanabilir ortam |
| Secret | OS keyring + `.env.example` | Anahtarların kaynak koddan ayrılması |

Codex, proje başlangıcında güncel kararlı sürümleri resmî kaynaklardan doğrulayacak ve lock dosyalarına sabitleyecektir. “Latest” bağımlılık çalışma zamanında kullanılmayacaktır.

---

## 5. Trade yaşam döngüsü ve durum makinesi

```text
DRAFT
  ↓
CANDIDATE
  ↓ risk + symbol + cost checks
PLANNED
  ↓ user/system arming rules
ARMED
  ↓
ENTRY_PENDING ──→ PARTIALLY_FILLED ──→ POSITION_PROTECTED
  │                       │                    │
  │ timeout/cancel        │ stop install fail │
  ▼                       ▼                    ▼
CANCELLED            EMERGENCY_REDUCE     MANAGING_POSITION
                                                  │
                              ┌───────────────────┼──────────────────┐
                              ▼                   ▼                  ▼
                         ADD_PENDING          TP_PARTIAL         STOP_TRIGGERED
                              │                   │                  │
                              └──────────────┬────┴──────────────────┘
                                             ▼
                                           CLOSED
```

Her state transition:

- benzersiz event ID,
- trade plan ID,
- Binance order/algo order ID,
- client order ID,
- önceki ve yeni durum,
- neden kodu,
- UTC zaman damgası,
- ilgili fiyat/miktar,
- kaynak (`strategy`, `risk`, `user`, `binance`, `recovery`)

ile kaydedilir.

### 5.1 Yasak geçişler

- Stop yokken `MANAGING_POSITION` durumuna geçilemez.
- Risk limiti aşılıyorsa `ARMED → ENTRY_PENDING` geçişi yapılamaz.
- UNKNOWN emir sonucu çözülmeden aynı `clientOrderId` veya ekonomik olarak eşdeğer yeni emir gönderilemez.
- Botun açmadığı pozisyona otomatik yönetim uygulanamaz.
- `HALTED` durumunda yalnızca risk azaltıcı/cancel/close komutları çalışabilir.

---

## 6. Kademeli giriş motoru

Kademeli giriş “zarara ekleme” ile aynı şey değildir. Kademeler işlem açılmadan önce tek bir plan olarak hesaplanır. Tüm kademeler dolsa dahi stopta oluşacak toplam kayıp, önceden belirlenen risk bütçesini geçemez.

### 6.1 Desteklenecek gerçek kademe varyantları

#### Varyant A — Sabit yüzde aralığı

- Girişler referans fiyatın belirli yüzdelerinde konumlanır.
- Basit ve şeffaftır.
- Volatilite değiştiğinde aralıklar anlamsızlaşabilir.
- Yalnızca sembol ve zaman dilimi bazlı validasyon sonrası açılır.

#### Varyant B — ATR/volatilite aralığı

- Kademeler `entry_i = reference ± k_i × ATR` ile oluşur.
- Piyasanın değişen oynaklığına uyarlanır.
- ATR pencere ve katsayıları walk-forward testle seçilir; sabit “sihirli sayı” yoktur.

#### Varyant C — Teknik seviye tabanlı

- Pivot, önceki swing, VWAP bandı, Donchian sınırı veya doğrulanmış destek/direnç seviyeleri.
- Seviyeler çakışırsa birleştirme/tick rounding uygulanır.
- Seviyeler stopun yanlış tarafına düşerse plan reddedilir.

#### Varyant D — Order-book likidite tabanlı

- Yerel order book üzerinden gerçek likidite kümeleri ve spread izlenir.
- Geçmiş order book verisi yoksa candle backtesti yapılmaz; önce canlı shadow veri toplanır.
- Sahte kesinlikten kaçınmak için V1 canlı pilotta varsayılan olarak kapalıdır.

#### Varyant E — Zaman dilimli giriş (TWAP-benzeri)

- Büyük emir etkisini azaltmak için planlanan miktar zamana bölünür.
- 20 USDT pilotta gerekli olmayabilir; altyapı destekler fakat etkinleştirme veriyle gerekçelendirilir.

### 6.2 Miktar dağıtım varyantları

| Varyant | Açıklama | Canlı pilot durumu |
|---|---|---|
| Equal notional | Her kademede eşit USDT notional | Test edilebilir |
| Front-loaded | İlk kademede daha yüksek pay | Test edilebilir |
| Back-loaded | Fiyat ters gittikçe daha yüksek pay | Varsayılan kapalı; martingale riski |
| Equal risk contribution | Her kademe stopta eşit risk katkısı yapar | Önerilen güvenli temel |
| Liquidity weighted | Miktar görülen likiditeye göre dağıtılır | Shadow veri sonrası |

### 6.3 Toplam risk formülü

Hesap özsermayesi `E`, işlem risk oranı `r` ise:

```text
R_budget = min(E × r, absolute_risk_cap, remaining_daily_risk)
```

Long işlemde kademe `i` için:

```text
risk_per_unit_i = (entry_i - stop)
                + estimated_entry_fee_per_unit_i
                + estimated_exit_fee_per_unit_i
                + slippage_buffer_per_unit_i
                + funding_buffer_per_unit_i
```

Short işlemde fiyat farkının yönü ters çevrilir.

Kademe risk ağırlıkları `w_i` ve `Σw_i = 1` ise:

```text
raw_qty_i = (R_budget × w_i) / risk_per_unit_i
qty_i = floor_to_step_size(raw_qty_i)
```

Sonra şu şartlar birlikte doğrulanır:

```text
Σ(qty_i × risk_per_unit_i) ≤ R_budget
qty_i ≥ minQty
qty_i × entry_i ≥ minNotional
qty_i ≤ maxQty
Σnotional ≤ leverage_bracket_limit
required_margin ≤ pilot_equity_cap
stop is on correct side of every entry
```

Yuvarlama sonrasında herhangi bir kademe minimum notional altında kalırsa üç seçenek vardır:

1. Kademe sayısını azaltmak,
2. İşlemi tamamen atlamak,
3. Kullanıcı daha yüksek risk/bakiye onayı verirse yeniden hesaplamak.

**Sistem otomatik olarak riski artırarak minimum notional’a ulaşmayacaktır.**

### 6.4 Ağırlıklı ortalama ve gerçekleşen risk

Dolmuş kademeler kümesi `F` için:

```text
avg_entry = Σ(qty_i × fill_price_i) / Σqty_i
filled_notional = Σ(qty_i × fill_price_i)
actual_stop_risk = Σ(qty_i × |fill_price_i - stop|) + actual/estimated costs
```

UI, hem planlanan hem gerçekleşen ortalamayı gösterir. Kısmi dolumlarda stop, `closePosition=true` kullanılan sunucu tarafı koruma ile mevcut pozisyonun tamamını kapsar; pending girişler stop tetiklenince iptal edilir.

### 6.5 Kademe iptal koşulları

- Sinyal geçerlilik süresi doldu.
- Stop tetiklendi veya fiyat stopun ötesine geçti.
- Günlük zarar limiti aşıldı.
- Spread/volatilite “circuit breaker” sınırını aştı.
- Binance symbol status işlem açmaya uygun değil.
- Açık pozisyon/manuel emir çatışması tespit edildi.
- Daha önce dolmadan fiyat TP bölgesine gitti.
- AI `veto_only` modunda kritik olay/uygunsuz rejim işaretledi.
- Bağlantı güvenilirliği gereken eşiğin altına indi.

---

## 7. Çıkış motoru

### 7.1 Stop-loss

- Koşullu emir güncel Algo Order uçlarıyla gönderilir. [B2]
- Varsayılan çalışma fiyatı `MARK_PRICE` olarak yapılandırılabilir; sembol `triggerProtect` bilgisi `exchangeInfo` üzerinden alınır.
- `closePosition=true` ile mevcut pozisyonun tamamı korunur; quantity gönderilmez.
- Stop emri başarılı şekilde doğrulanmadan trade “protected” sayılmaz.
- Stop kurulamıyorsa:
  1. yeni girişler iptal edilir,
  2. açık miktar reduce-only market/korumalı limit ile azaltılır,
  3. olay kritik alarm olarak kaydedilir,
  4. bot `HALTED` durumuna geçer.

### 7.2 Kâr alma varyantları

- Tek hedef, tamamı reduce-only limit.
- TP1/TP2/TP3 yüzde dağılımı.
- R-multiple hedefleri (`1R`, `1.5R`, `2R` gibi), ancak katsayılar backtest sonucu seçilir.
- TP1 sonrası break-even stop; opsiyonel ve test edilmeden açılmaz.
- Son parça trailing stop; callback rate borsa sınırlarına göre doğrulanır.
- Time stop: sinyal süresi dolduğunda pozisyon azaltma/kapatma.

Çıkış miktarları daima:

```text
Σ(open_reduce_only_exit_qty) ≤ current_position_qty
```

olmalıdır. Rounding remainder son çıkış emrine eklenir.

### 7.3 Funding ve maliyet

Trade planı şu maliyetleri ayrı saklar:

- maker/taker komisyonu,
- gerçekleşen slippage,
- spread maliyeti,
- funding tahmini ve gerçekleşeni,
- OpenAI kullanım maliyeti,
- toplam net PnL.

OpenAI maliyeti trade PnL’dan ayrı operasyon maliyeti olarak da raporlanır; küçük sermayede AI maliyetinin getiriyi geçmesi açıkça görünür.

---

## 8. Strateji araştırma ve seçme sistemi

Program birden fazla strateji modülünü destekler; fakat ilk canlı pilotta aynı anda yalnızca **bir onaylı strateji** aktif olur.

### 8.1 Aday strateji aileleri

1. **Trend pullback:** Üst zaman trendi + alt zaman geri çekilme + volatilite filtresi.
2. **Volatility breakout:** Donchian/price range + ATR + volume doğrulaması.
3. **Mean reversion:** Yalnızca düşük trend rejiminde bant dışı sapma ve geri dönüş.
4. **Order-book imbalance:** Canlı depth, spread ve trade-flow; yalnızca yeterli shadow veri sonrası.
5. **No-trade baseline:** İşlem açmamanın sonuçlarını karşılaştırmak için zorunlu kontrol modeli.

İndikatörler emir açma garantisi değildir. Her strateji aynı `SignalCandidate` sözleşmesini üretir:

```json
{
  "strategy_id": "trend_pullback_v1",
  "symbol": "BTCUSDT",
  "direction": "LONG",
  "reference_price": "...",
  "invalidation_price": "...",
  "timeframe": "15m",
  "valid_until": "UTC timestamp",
  "features": {},
  "reason_codes": [],
  "confidence_score": null
}
```

`confidence_score` modelin kâr olasılığı gibi sunulmaz; yalnızca kendi kalibrasyon testi varsa kullanılır.

### 8.2 Test metodolojisi

- Look-ahead bias yasak.
- Mum kapanmadan kapanış verisi kullanılamaz.
- Komisyon, spread, slippage ve funding dahil edilir.
- Train/validation/test zaman olarak ayrılır.
- Walk-forward analiz zorunlu.
- Parametre taraması için sonuç seçme yanlılığı raporlanır.
- Out-of-sample performans in-sample’dan ayrı gösterilir.
- En kötü drawdown, losing streak ve tail loss ölçülür.
- Order-book stratejisi candle verisiyle sahte backtest edilmez.
- Canlıya geçiş ölçütü yalnız “net kâr” değildir; işlem sayısı, drawdown, maliyet hassasiyeti ve parametre stabilitesi birlikte değerlendirilir.

### 8.3 Canlı pilot seçim kapısı

Bir strateji, aşağıdakiler olmadan canlıya alınmaz:

- veri bütünlüğü testi,
- birim ve property testleri,
- out-of-sample raporu,
- maliyet iki katına çıktığında stres testi,
- en az bir replay testi,
- shadow/rehearsal çalışma raporu,
- kullanıcıya gösterilen açık risk özeti.

Kullanıcı “doğrudan gerçek” istediği için testnet zorunlu değildir; ancak kod ve gerçek hesap bağlantısı öncesi yukarıdaki yazılım testleri zorunludur. İlk matching-engine emri canlı pilot olacaktır ve ayrıca açık onay isteyecektir.

---

## 9. Risk motoru

Risk motoru stratejiden ve AI’dan bağımsızdır. Hiçbir üst katman risk motorunu aşamaz.

### 9.1 Risk limitleri

| Limit | Açıklama |
|---|---|
| pilot_equity_cap | Botun kullanabileceği toplam bakiye üst sınırı |
| risk_per_trade | Stopta izin verilen maksimum kayıp |
| daily_loss_limit | Gün içinde gerçekleşen + kilitlenmiş zarar sınırı |
| weekly_drawdown_limit | Haftalık sermaye düşüşü kapısı |
| max_leverage | Sembol ayarı doğrulanmış üst sınır |
| max_open_positions | V1 varsayılan 1 |
| max_pending_entries | Trade başına kademe sayısı sınırı |
| max_symbol_exposure | Tek sembol notional sınırı |
| max_total_exposure | Tüm bot notional toplamı |
| max_spread_bps | Yeni emir açma filtresi |
| max_slippage_bps | Market fallback iptal sınırı |
| stale_data_ms | Eski veriyle emir açmayı engeller |
| max_clock_skew_ms | Binance server time farkı sınırı |
| consecutive_loss_limit | Belirli sayı kayıptan sonra halt |
| ai_daily_budget_usd | AI maliyet tavanı |

### 9.1.1 Backend hard safety envelope

V1 canlı pilotunda hesaplanan etkin limit her alan için `min(UI/config değeri, backend hard cap)` olacaktır. Backend hard cap değerleri Bölüm 18'de tanımlanan canlı pilot profilidir: `pilot_equity_cap_usdt <= 20`, `max_leverage <= 2`, `max_concurrent_positions <= 1`, `max_active_strategy_count <= 1`, `max_stages <= 2`, `risk_per_trade_usdt <= 0.10`, `daily_loss_limit_usdt <= 0.30`, `weekly_drawdown_limit_usdt <= 0.80`, `consecutive_loss_limit <= 3`, `openai_daily_budget_usd <= 0.02`; market entry, back-loaded ladder ve server-side stop kuralları gevşetilemez.

Frontend doğrulaması yalnız ergonomi içindir. Komut, trade planı, config değişikliği veya yerel API isteği bu tavanlardan herhangi birini aşarsa backend `HARD_RISK_LIMIT_EXCEEDED` ile reddeder; değer kırpılmaz, risk otomatik büyütülmez ve emir gönderilmez. Hard cap değişikliği D-026 için yeni karar kaydı, risk/migration etkisi, kaynak referansı ve açık kullanıcı onayı gerektirir.

### 9.2 Circuit breaker’lar

- WebSocket market data stale.
- User data stream kopuk.
- REST saat senkronizasyonu bozuk.
- 429/418 veya tekrarlayan 5xx.
- 503 UNKNOWN çözülmemiş emir.
- DB yazma başarısız.
- Binance ve yerel pozisyon uyuşmazlığı.
- Stop emri yok/yanlış.
- Hesap position mode veya margin mode beklenenden farklı.
- Sembol filtreleri değişti.
- Günlük zarar veya kayıp serisi sınırı.
- Kullanıcı acil durdurma.

### 9.3 Acil durdurma seviyeleri

1. **Pause new entries:** Yeni giriş yok, mevcut pozisyon yönetilir.
2. **Cancel pending entries:** Dolmamış girişler iptal edilir, stop/TP korunur.
3. **Risk reduce:** Pozisyon kontrollü küçültülür.
4. **Flatten:** Kullanıcı onaylı acil tamamen kapatma.
5. **Hard halt:** Servis yalnız izleme/reconciliation modunda kalır.

---

## 10. Binance entegrasyon tasarımı

### 10.1 Başlangıç ön kontrolü

Canlı adapter etkinleştiğinde sırasıyla:

1. Server time ve clock skew kontrolü.
2. API key izin testi.
3. Account/balance okuma.
4. One-way mode doğrulama.
5. Multi-assets mode kapalı doğrulama.
6. Whitelist semboller için symbol config okuma.
7. Isolated margin, auto-add-margin false, leverage sınırı doğrulama.
8. Exchange filters, triggerProtect ve status cache.
9. User commission rate okuma.
10. Leverage bracket/notional limit okuma.
11. Açık manuel pozisyon ve emir çatışması kontrolü.
12. User Data Stream açma ve event doğrulama.
13. Public/market stream bağlantısı.
14. `order/test` ile quantity/price/signature doğrulaması.
15. Canlı aktivasyon onayı.

### 10.2 Emir kimliği

Örnek biçim:

```text
UTA1-{trade_short_id}-{role}-{stage}-{attempt}
```

- 36 karakter sınırını aşmaz.
- `role`: `EN`, `TP`, `CL` gibi kısa kod.
- Aynı ekonomik emrin retry’sinde yeni rastgele emir üretmek yerine idempotency kayıtları kontrol edilir.

### 10.3 503 UNKNOWN algoritması

```text
POST order → HTTP 503 + UNKNOWN message
       ↓
trade state = ORDER_STATUS_UNKNOWN
       ↓
aynı clientOrderId ile yeni emir GÖNDERME
       ↓
User Data Stream event ara
       ↓ bulunmazsa
Query Order by clientOrderId
       ↓ bulunmazsa kontrollü kısa bekleme + tekrar query
       ↓ kesin sonuç
NEW/FILLED/PARTIAL/CANCELED/NOT_FOUND_AFTER_WINDOW
```

Yalnız “kesin bulunamadı” politikası tamamlandıktan sonra yeni attempt ID ile emir tekrar denenebilir. [B13]

### 10.4 Rate limit politikası

- Response header’ları takip edilir.
- Merkezi token bucket/throttler kullanılır.
- 429 → exponential backoff + new entries pause.
- 418 → hard halt; otomatik deneme yok.
- Batch order yalnız avantajı doğrulanırsa; UNKNOWN riskinde tek emir tercih edilir.

### 10.5 WebSocket dayanıklılığı

- 24 saat dolmadan planlı reconnect.
- Ping/pong handling.
- Exponential backoff + jitter.
- Sequence/snapshot ile local order book doğrulama. [B16]
- Duplicate event deduplication.
- Out-of-order event tolerance.
- User Data Stream keepalive.
- Reconnect sonrası full reconciliation.

### 10.6 Dead-man switch

`countdownCancelAll` belirli aralıkla yenilenebilir; heartbeat kesilirse normal açık emirleri iptal eder. [B15]

Uygulama politikası:

- Pending giriş ve standart TP emirleri için değerlendirilir.
- Sunucu tarafı Algo stopun yanlışlıkla kaldırılmaması ayrıca test edilir.
- Mekanizma ilk canlı sürümde feature flag altında olacak; Binance davranışı kontrat testinde doğrulanmadan açılmayacaktır.

---

## 11. OpenAI runtime entegrasyonu

### 11.1 AI’nın görev sınırı

AI şunları yapabilir:

- Piyasa rejimini sınıflandırmak.
- Strateji sinyalindeki çelişkileri belirtmek.
- Sağlanan haber/olay özetinden risk etiketi çıkarmak.
- İşlem gerekçesini insan diline çevirmek.
- Gün sonu ve işlem sonrası analiz üretmek.
- Log/anomali açıklaması sunmak.

AI şunları yapamaz:

- Binance API anahtarını görmek.
- Doğrudan emir fonksiyonu çağırmak.
- Risk limitini değiştirmek.
- Stopu kaldırmak veya kaldıraç artırmak.
- Verilmeyen canlı veriyi uydurmak.
- “Kesin kazanır” veya olasılığı kalibre edilmemiş güven skoru vermek.

### 11.2 Çalışma modları

| Mod | Davranış |
|---|---|
| off | AI çağrısı yok |
| advisory | AI sonucu UI/log için; trade kararını etkilemez |
| veto_only | AI yalnız önceden tanımlı kritik risklerde trade’i engelleyebilir |
| post_trade_only | Sadece kapanan işlemleri analiz eder |

V1 başlangıç modu `advisory` olacaktır. `veto_only`, geriye dönük olarak yarar/zarar etkisi ölçülmeden etkinleştirilmeyecektir.

### 11.3 Model ve maliyet politikası

- Operasyonel kısa sınıflandırmalar: konfigüre edilebilir ekonomik model (`gpt-5.6-luna` aday).
- Karmaşık günlük inceleme: daha güçlü model (`gpt-5.6-terra` aday).
- Model isimleri konfigürasyondadır; uygulama açılırken mevcutluk kontrol edilir.
- Her request token, gecikme ve tahmini maliyet olarak kaydedilir.
- Günlük AI bütçesi dolunca AI `off/advisory unavailable` olur; risk motoru bozulmaz.
- Web search canlı emir yolunda varsayılan kapalıdır.

### 11.4 Runtime sistem promptu

```text
Sen Binance USDⓈ-M Futures için çalışan bir RİSK ANALİZ YARDIMCISISIN.
Emir verme yetkin yoktur. Hiçbir koşulda long/short emri, miktar, kaldıraç veya
stop değişikliği talimatı üretme. Yalnızca sana verilen yapılandırılmış veriyi
kullan; eksik veriyi tahmin etme. Piyasa verisi eskiyse, çelişkiliyse veya
hesaplama doğrulanamıyorsa sonucu INSUFFICIENT_DATA yap.

Öncelik sırası:
1) veri bütünlüğü,
2) risk ihlali,
3) rejim/volatilite sınıflandırması,
4) açık ve kısa gerekçe.

Kâr garantisi, kesinlik veya uydurma olasılık üretme. Yanıt yalnız belirtilen
JSON şemasına uygun olsun. Açıklama alanında en fazla 3 somut neden kullan.
```

### 11.5 Structured Output şeması

```json
{
  "status": "OK | INSUFFICIENT_DATA | ERROR",
  "market_regime": "TREND | RANGE | HIGH_VOLATILITY | DISLOCATED | UNKNOWN",
  "risk_level": "LOW | MEDIUM | HIGH | CRITICAL",
  "veto": false,
  "veto_reason_codes": [],
  "data_quality_flags": [],
  "observations": ["..."],
  "valid_until_utc": "...",
  "model_version": "..."
}
```

Veto yalnız allowlist neden kodları için kabul edilir:

- `STALE_DATA`
- `SPREAD_SPIKE`
- `VOLATILITY_SHOCK`
- `EVENT_RISK`
- `SIGNAL_CONTRADICTION`
- `INSUFFICIENT_DATA`

AI’nın serbest metinle yeni bir risk kuralı yaratmasına izin verilmez.

---

## 12. UI/UX tasarımı

Arayüz, koyu temalı yerel kontrol merkezi olacaktır. Kırmızı/yeşil yalnız PnL için değil, güvenlik durumu için de tutarlı kullanılır. Kritik aksiyonlar iki aşamalı onay ister.

### 12.1 Ana dashboard wireframe

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ BINANCE AI TRADER   LIVE: KAPALI   WS: OK   USER STREAM: OK   HALT [■]   │
├──────────────┬──────────────┬──────────────┬──────────────┬───────────────┤
│ Pilot Bakiye │ Günlük Net   │ Açık Risk   │ AI Maliyeti  │ Sistem Sağlığı│
│ 20.00 USDT   │ -0.03 USDT   │ 0.08 USDT   │ 0.004 USD    │ 98 / 100      │
├────────────────────────────────────────────┬──────────────────────────────┤
│ FİYAT GRAFİĞİ                              │ AKTİF TRADE PLANI            │
│ Entry 1 ─────────────────────              │ BTCUSDT LONG                 │
│ Avg Entry ───────────────────              │ Kademe 1: FILLED             │
│ Entry 2 ─────────────────────              │ Kademe 2: PENDING            │
│ TP1/TP2 ─────────────────────              │ Stop: SERVER CONFIRMED       │
│ Stop ────────────────────────              │ Plan Risk: 0.10 USDT         │
├────────────────────────────────────────────┼──────────────────────────────┤
│ SON EMİRLER / DOLUMLAR                     │ RİSK & AI                     │
│ time · role · qty · price · status         │ Daily loss: 22% of limit     │
│                                             │ AI: advisory / medium vol    │
└────────────────────────────────────────────┴──────────────────────────────┘
```

### 12.2 Ekranlar

1. **Genel Bakış:** Bakiye, PnL, risk, sistem sağlığı, aktif trade.
2. **Piyasa Radar:** Whitelist semboller, spread, ATR, hacim, funding, veri tazeliği.
3. **Trade Planlayıcı:** Kademe varyantı, girişler, ağırlıklar, stop, TP, maliyet ve max loss.
4. **Pozisyonlar ve Emirler:** Standard order ve algo order ayrı görünür.
5. **Risk Merkezi:** Günlük/haftalık limitler, circuit breaker ve geçmiş halt nedenleri.
6. **Strateji Laboratuvarı:** Backtest, walk-forward, maliyet stres testi, parametre stabilitesi.
7. **AI Merkezi:** Mod, son JSON değerlendirmeleri, model maliyeti, veto geçmişi.
8. **Sistem Sağlığı:** REST/WS gecikmesi, reconnect, clock skew, DB, queue, rate limit.
9. **Denetim Günlüğü:** Filtrelenebilir immutable event listesi.
10. **Bağlantı Sihirbazı:** Binance/OpenAI anahtarı, izin testi ve canlı aktivasyon.

### 12.3 Trade plan kartı

Her kademede:

- plan fiyatı,
- gerçek dolum ortalaması,
- miktar,
- notional,
- stopta risk katkısı,
- tahmini komisyon,
- durum,
- kalan geçerlilik süresi,
- Binance order ID,
- iptal nedeni

gösterilir.

### 12.4 Kritik UI kuralları

- “Kaldıraç” yanında tahmini likidasyon ve stop mesafesi birlikte gösterilir.
- Brüt PnL tek başına ana metrik olamaz; net PnL varsayılandır.
- Bağlantı kopuksa eski fiyat yeşil görünemez; `STALE` etiketi alır.
- Live toggle, testler geçmeden disabled.
- Acil flatten, normal butonlardan farklı yer ve iki onaylıdır.
- Kullanıcı API secret’ını kaydettikten sonra UI bir daha tam değer göstermez.

---

## 13. Veri modeli

Ana tablolar:

- `system_config_versions`
- `exchange_symbols`
- `market_candles`
- `market_snapshots`
- `strategy_signals`
- `trade_plans`
- `trade_stages`
- `orders`
- `algo_orders`
- `fills`
- `positions_snapshots`
- `risk_snapshots`
- `account_snapshots`
- `ai_assessments`
- `system_events`
- `reconciliation_runs`
- `daily_performance`
- `test_runs`
- `activation_approvals`

### 13.1 Para alanları

- DB’de `NUMERIC`/decimal.
- API modellerinde string → Decimal.
- UI’ya string veya sabit hassasiyetli formatla gönderilir.
- Float dönüşümü yalnız grafik kütüphanesinin görsel ekseninde, trade hesabı dışında yapılabilir.

### 13.2 Audit log

Kritik olaylarda hash chain opsiyonu uygulanabilir:

```text
record_hash = SHA256(previous_hash + canonical_json(record))
```

Bu yapı düzenleme tespitini kolaylaştırır; hukuki değişmezlik iddiası değildir.

---

## 14. Güvenlik

### 14.1 Binance anahtarı

- Ayrı API key.
- Sadece okuma + Futures trade.
- Withdrawal ve universal transfer kapalı.
- Mümkünse sabit IP allowlist.
- UI/frontend’e asla gönderilmez.
- Log, exception, telemetry ve AI promptlarından redakte edilir.
- OS keyring’de saklanır; `.env` yalnız geliştirme fallback’i.

### 14.2 OpenAI anahtarı

- Yalnız backend.
- AI request’lerine Binance key/secret, tam hesap kimliği veya gereksiz kişisel veri girmez.
- Kullanım bütçesi ve timeout.
- `store`/retention ayarları güncel politika ve kullanıcı tercihiyle konfigüre edilir.

### 14.3 Codex güvenliği

- Repo `AGENTS.md` gerçek secret okumayı yasaklar.
- `.env`, keyring exportları, DB dump ve log secrets `.gitignore` altında.
- Codex sandbox/approval politikası; destructive komutlar onay ister.
- MCP allowlist: yalnız ihtiyaç duyulan sunucular.
- **Binance trade yapan bir MCP server kurulmaz.** Kod geliştirme ajanına canlı emir aracı verilmez.

---

## 15. MCP ve Codex araç planı

### Zorunlu

1. **OpenAI Developer Docs MCP**
   Güncel Responses API, Structured Outputs, Codex ve MCP bilgisi. [O10]

2. **Playwright skill/MCP**
   UI’nin gerçek tarayıcıda davranış ve breakpoint testleri. [O11]

### Opsiyonel

3. **GitHub MCP/entegrasyonu**
   Repo issue, PR ve code review akışı varsa.

4. **Context7**
   Yalnız üçüncü taraf kütüphane dokümanını güncel okumak için; kararların birincil kaynağı değildir.

### Yasak

- Binance anahtarına erişen üçüncü taraf MCP.
- Emir gönderen “trading agent” MCP.
- Kaynağı belirsiz, secret isteyen MCP.
- Gerekçesiz bütün araçları aynı anda kurmak.

Örnek proje `.codex/config.toml`:

```toml
[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"

# Playwright kurulumu, Codex'in güncel resmî skill/plugin yoluyla yapılmalıdır.
# Bilinmeyen npx paketleri otomatik güvenilir sayılmaz.
```

---

## 16. Fazlı geliştirme planı

### Faz 0 — Araştırma kilidi ve repo anayasası

**Üretilecekler**

- `AGENTS.md`
- `PLAN.md`
- `docs/DECISIONS.md`
- `docs/SOURCES.md`
- `docs/PHASE_STATUS.md`
- Resmî Binance/OpenAI endpoint matrisi

**Test/kapı**

- Bütün endpointler güncel resmî dokümana bağlı.
- Eski conditional order akışı kullanılmıyor.
- Varsayımlar “config”, “araştırılacak” veya “kilitli karar” olarak etiketlenmiş.

### Faz 1 — Proje iskeleti ve geliştirme ortamı

**Üretilecekler**

- Backend/frontend klasörleri
- Docker Compose ve Windows fallback
- Lint, type-check, test komutları
- Pre-commit ve CI
- Secret scanning

**Kapı**

- Tek komutla kurulum.
- Boş uygulama + health endpoint + UI açılır.
- Tüm statik kontroller geçer.

### Faz 2 — Domain modelleri ve Decimal hesap çekirdeği

**Üretilecekler**

- Trade plan, stage, order, fill, risk value objects
- Rounding ve exchange filter yardımcıları
- State machine

**Test**

- Birim + Hypothesis property test.
- Float kullanılmadığını lint/test doğrular.
- Geçersiz state transition reddedilir.

### Faz 3 — Public Binance veri katmanı

**Üretilecekler**

- Server time, exchangeInfo, klines, mark price, funding, bookTicker
- Routed WebSocket istemcisi
- Data freshness ve reconnect

**Test**

- Gerçek public endpoint smoke testi.
- 24h reconnect simülasyonu.
- Ping/pong, stale data ve snapshot/sequence testi.

### Faz 4 — Veritabanı, replay ve audit

**Üretilecekler**

- Migrations
- Event store/audit
- Market data recorder
- Replay runner

**Test**

- Restart sonrası state geri yükleme.
- Duplicate event idempotency.
- DB write failure circuit breaker.

### Faz 5 — Strategy SDK ve araştırma laboratuvarı

**Üretilecekler**

- Strategy interface
- No-trade baseline
- Trend pullback, breakout, mean reversion aday modülleri
- Backtest/walk-forward engine

**Test**

- Look-ahead kontrolleri.
- Komisyon/slippage/funding dahil.
- Fixture sonuçları deterministik.

### Faz 6 — Kademe ve pozisyon boyutlandırma motoru

**Üretilecekler**

- Kademe fiyat varyantları
- Miktar dağıtım varyantları
- Total risk solver
- Stop/TP planner

**Test**

- Bütün kademeler dolduğunda max loss ≤ budget.
- Min notional riski aşıyorsa `SKIP_TRADE`.
- Long/short simetrisi.
- Rounding sonrası exit miktarı pozisyonu aşmaz.

### Faz 7 — Exchange simulator ve failure injection

**Üretilecekler**

- Partial fill, duplicate, delayed, rejected order simulator
- 503 UNKNOWN, 429, 418, 1021, disconnect senaryoları
- Slippage/spread modeli

**Test**

- Her failure senaryosu için otomatik test.
- Duplicate economic order oluşmaması.
- Stop failure emergency flow.

### Faz 8 — Gerçek Binance adapteri, fakat kilitli

**Üretilecekler**

- Signed REST
- User Data Stream
- Normal order + Algo order
- Reconciliation
- order/test
- Feature flag: `LIVE_TRADING_ENABLED=false`

**Test**

- Mock/contract tests.
- API key olmadan uygulama tam çalışır.
- Secret hiçbir logda görünmez.

### Faz 9 — OpenAI entegrasyonu

**Üretilecekler**

- `AIProvider`
- Responses API + Structured Outputs
- Advisory/veto/post-trade modları
- Cost/latency tracking
- Mock provider

**Test**

- Invalid JSON reddi.
- Timeout halinde güvenli davranış.
- Prompt injection benzeri metinler emir yetkisi oluşturamaz.
- AI sonucu risk motorunu aşamaz.

### Faz 10 — Tam UI

**Üretilecekler**

- 10 ana ekran
- Canlı WebSocket UI
- Trade plan görselleştirmesi
- Risk ve health panelleri
- Connection wizard kapalı/locked durum

**Test**

- Playwright E2E.
- Responsive desktop/tablet.
- Stale/error/empty/loading durumları.
- Kritik buton onay akışları.

### Faz 11 — Operasyon, gözlem ve bildirim

**Üretilecekler**

- Structured logging
- Metrics
- Daily report
- Alert adapters
- Backup/restore

**Test**

- Log redaction.
- Disk dolu, DB down, restart, clock skew.
- Alert deduplication.

### Faz 12 — Güvenlik ve chaos testleri

**Üretilecekler**

- Threat model
- Dependency/security scan
- Process kill recovery
- Network partition tests

**Kapı**

- Açık pozisyon varken program öldürülüp yeniden başlatıldığında reconciliation başarılı.
- Stop Binance tarafında korunuyor.
- Kritik açık bulgu yok.

### Faz 13 — Canlı öncesi kabul

**Üretilecekler**

- `FINAL_ACCEPTANCE_REPORT.md`
- Bütün test sonuçları
- Risk config preview
- Kullanıcı kullanım kılavuzu

**Kapı**

- Faz 0–12 yeşil.
- Canlı bağlantı ekranı ilk kez açılabilir.
- Henüz gerçek emir yok.

### Faz 14 — Bağlantı sihirbazı ve 20 USDT canlı pilot

**Akış**

1. Kullanıcı Binance API key/secret’ı yerel UI’ya girer.
2. Salt-okunur kontroller yapılır.
3. Kullanıcı Futures trade yetkisini yerel Binance panelinde açar.
4. IP allowlist kontrol edilir.
5. Account mode, margin, leverage ve manuel pozisyon çatışması doğrulanır.
6. Live `order/test` çalıştırılır.
7. Risk özeti gösterilir.
8. Kullanıcı tam aktivasyon ifadesini yazar.
9. İlk trade adayı oluştuğunda emir planı bir kez daha kullanıcıya gösterilir.
10. İlk matching-engine emri canlı gönderilir.

**İlk 50 işlem kuralı**

- Sermaye ve kaldıraç otomatik artmaz.
- Tek strateji, tek pozisyon.
- Her işlem sonrası reconciliation.
- 10 işlemde bir ara rapor.
- Kritik hata → hard halt.

---

## 17. Test matrisi

| Kategori | Senaryo | Beklenen sonuç |
|---|---|---|
| Hesap | Yanlış API izni | Canlı kilit açılmaz |
| Hesap | Manuel pozisyon var | Aynı sembol reddedilir |
| Mode | Cross açık | Isolated doğrulama/değiştirme veya abort |
| Mode | Hedge mode | One-way kontrolü başarısızsa abort |
| Emir | Kısmi dolum | Stop mevcut pozisyonu korur, risk güncellenir |
| Emir | Duplicate WS event | Tek fill kaydı |
| Emir | 503 UNKNOWN | Kör retry yok; query/reconcile |
| Emir | Stop reddedildi | Emergency reduce + halt |
| Emir | TP miktarı fazla | Planner reddeder |
| Filtre | Min notional | Risk artmaz; skip/az kademe |
| Filtre | Tick/step değişti | Cache yenilenir, eski plan iptal |
| Ağ | Market WS koptu | New entries pause |
| Ağ | User stream koptu | New entries pause + reconcile |
| Ağ | 24 saat | Planlı reconnect |
| Rate | 429 | Backoff, yeni giriş pause |
| Rate | 418 | Hard halt |
| Saat | -1021 | Saat senkronu, retry policy |
| DB | Yazma başarısız | Emir açma yok; risk azaltma çalışabilir |
| AI | Timeout | Mod politikasına göre advisory yok/veto fail-safe |
| AI | Geçersiz JSON | Reddedilir, emir yetkisi doğmaz |
| UI | Stale data | Fiyat stale etiketi |
| Recovery | Process kill | Startup reconciliation |
| Recovery | Yerel order var, Binance yok | Kesin state çözümü |
| Recovery | Binance position var, local yok | Orphan position alert + halt |

---

## 18. Canlı pilot için örnek güvenlik profili

Aşağıdaki değerler kâr varsayımı değildir. V1'de backend hard cap'tir; bağlantı sihirbazı bunlardan yalnız daha düşük değerleri seçebilir ve kullanıcı onayı ister:

```yaml
profile: live_pilot_20_usdt
pilot_equity_cap_usdt: 20
margin_type: ISOLATED
position_mode: ONE_WAY
max_leverage: 2
max_concurrent_positions: 1
max_active_strategy_count: 1
max_stages: 2
risk_per_trade_usdt: 0.10
daily_loss_limit_usdt: 0.30
weekly_drawdown_limit_usdt: 0.80
consecutive_loss_limit: 3
allow_market_entry: false
allow_back_loaded_ladder: false
require_server_side_stop: true
openai_mode: advisory
openai_daily_budget_usd: 0.02
```

Minimum notional veya sembol filtresi nedeniyle 0.10 USDT riskle geçerli emir üretilemiyorsa bot işlem açmaz. “Günlük 1–2 USDT kazanma” zorunlu hedef olarak kodlanmaz; bu, 20 USDT sermayede günlük %5–10 anlamına gelir ve sistemi aşırı risk almaya iter.

---

## 19. Codex çalışma protokolü

Codex her fazda:

1. `AGENTS.md`, `PLAN.md`, `MASTER_SPEC.md` ve mevcut `PHASE_STATUS.md` dosyasını okur.
2. Resmî dokümanda değişen uçları doğrular.
3. Önce faz planını ve kabul kriterlerini yazar.
4. Küçük, denetlenebilir commit’lerle uygular.
5. Testleri çalıştırır.
6. Hataları düzeltir ve tekrar test eder.
7. `docs/test-reports/phase-XX.md` oluşturur.
8. `PHASE_STATUS.md` günceller.
9. Faz kapısı geçmediyse sonraki faza geçmez.
10. Kullanıcıdan yalnız gerçek karar veya yetki gerektiğinde soru sorar.

Codex, API anahtarını sohbet içinde istemeyecek. Faz 14’e kadar `mock`, public endpoint veya secret’sız adapter kullanacaktır.

---

## 20. Codex’e verilecek ana prompt

Ayrı `CODEX_MASTER_PROMPT.md` dosyasında hazırdır. Özet komut:

```text
/plan Bu repodaki AGENTS.md, PLAN.md ve MASTER_SPEC.md dosyalarını okuyup
fazları, risk sınırlarını ve kabul kriterlerini doğrula. Resmî Binance ve OpenAI
dokümanlarını gerekli MCP/araçlarla kontrol et. Henüz kod yazma; çelişki ve
eksik kararları PHASE_STATUS.md içinde belirt.

/goal PLAN.md'yi faz faz uygula. Her fazın otomatik testlerini, raporunu ve
kabul kapısını tamamlamadan sonraki faza geçme. Gerçek Binance anahtarı veya
canlı emir izni isteme; bunu yalnız Faz 14'te yerel bağlantı sihirbazı hazır,
bütün testler yeşil ve FINAL_ACCEPTANCE_REPORT.md tamamlandıktan sonra yap.
```

---

## 21. Son kabul ölçütleri

Program “tamamlandı” sayılmaz; şu maddelerin tümü gerekir:

- Güncel normal ve Algo Order uçları kullanılıyor.
- State machine ve idempotency testleri yeşil.
- Kademeler stop riskini aşmıyor.
- Stop başarısızlığı güvenli kapanıyor.
- 503 UNKNOWN çift emir üretmiyor.
- Partial fill ve restart recovery çalışıyor.
- UI bütün kritik durumları gösteriyor.
- Secret scanning temiz.
- AI hiçbir şekilde trade yetkisi elde edemiyor.
- OpenAI maliyeti ölçülüyor ve sınırlı.
- Canlı aktivasyon çok aşamalı.
- Kullanıcıya ilk emirden önce gerçek max loss gösteriliyor.

---

## 22. Resmî kaynaklar

### Binance

- **[B1]** New Order — https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#new-order
- **[B2]** New Algo Order — https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#new-algo-order
- **[B3]** Change Log / conditional order migration — https://developers.binance.com/legacy-docs/derivatives/change-log
- **[B4]** Change Margin Type — aynı Trade REST API belgesi, `change-margin-type`
- **[B5]** Change Initial Leverage — aynı belge, `change-initial-leverage`
- **[B6]** Change Position Mode — aynı belge, `change-position-mode`
- **[B7]** Exchange Information — https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#exchange-information
- **[B8]** User Commission Rate — https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#user-commission-rate
- **[B9]** Notional and Leverage Brackets — aynı Account belgesi
- **[B10]** User Data Streams — https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/user-data-streams
- **[B11]** WebSocket API General Info — https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-api-general-info
- **[B12]** WebSocket Market Streams Connect — https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect
- **[B13]** General Info / 503 and rate limits — https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- **[B14]** Test Order — Trade REST API, `test-order`
- **[B15]** Auto-Cancel All Open Orders — Trade REST API, `auto-cancel-all-open-orders`
- **[B16]** Local Order Book — https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly
- **[B17]** Error Codes — https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code
- **[B18]** Official Python connectors — https://developers.binance.com/en/docs/sdks-tools/connectors/python

### OpenAI / Codex

- **[O1]** Responses API overview — https://developers.openai.com/api/reference/overview/
- **[O2]** Function calling — https://developers.openai.com/api/docs/guides/function-calling
- **[O3]** Structured Outputs — https://developers.openai.com/api/docs/guides/structured-outputs
- **[O4]** Model guidance — https://developers.openai.com/api/docs/guides/latest-model
- **[O5]** Codex IDE — https://developers.openai.com/codex/ide
- **[O6]** Codex prompting — https://developers.openai.com/codex/prompting
- **[O7]** Follow a goal — https://developers.openai.com/codex/use-cases/follow-goals
- **[O8]** AGENTS.md / cloud environments — https://developers.openai.com/codex/cloud/environments
- **[O9]** Codex MCP — https://developers.openai.com/codex/mcp
- **[O10]** OpenAI Docs MCP — https://developers.openai.com/learn/docs-mcp
- **[O11]** Frontend + Playwright use case — https://developers.openai.com/codex/use-cases/frontend-designs
- **[O12]** Codex sandbox and approvals — https://developers.openai.com/codex/concepts/sandboxing
- **[O13]** OpenAI API pricing — https://developers.openai.com/api/docs/pricing
