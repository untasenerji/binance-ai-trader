# OpenAI Codex — Ana Uygulama Promptu

Aşağıdaki promptu, bu klasörü VS Code’da açtıktan sonra Codex paneline yapıştır.

```text
/plan

Bu repoda gerçek para ile çalışacak Binance USDⓈ-M Futures uygulaması geliştiriyoruz.
Önce AGENTS.md, PLAN.md, MASTER_SPEC.md, docs/DECISIONS.md ve
 docs/PHASE_STATUS.md dosyalarını eksiksiz oku.

ZORUNLU ÇALIŞMA KURALLARI

1. MASTER_SPEC.md ürün ve risk anayasasıdır. Çelişen bir istek görürsen gizlice
   yorumlama; docs/PHASE_STATUS.md içinde BLOCKER olarak yaz ve bana sor.
2. Binance ile ilgili her endpoint, parametre ve davranışı yalnız güncel resmî
   Binance Developer Docs üzerinden doğrula. Eski conditional-order örneklerini
   kopyalama; USDⓈ-M TP/SL/trailing emirlerinde güncel Algo Order servisini kullan.
3. OpenAI API/Codex/MCP konularında OpenAI Developer Docs MCP'yi kullan.
4. Üçüncü taraf paket sürümlerini güncel resmî doküman/repository ile doğrula ve
   lock dosyasına sabitle. Deprecated Binance futures connector kullanma.
5. Gerçek Binance API key/secret isteme, `.env` içeriği okuma veya canlı emir
   gönderme. Faz 14'e kadar mock, public API ve canlı `/order/test` için adapter
   hazırlığı dışında secret gerektiren işlem yapma.
6. Parasal/fiyat/miktar hesaplarında yalnız Decimal kullan. Float ile trade hesabı
   yapılmasını lint ve test ile engelle.
7. OpenAI modeline Binance credential veya emir fonksiyonu verme. AI yalnız
   advisory/veto/post-trade rolündedir; risk motorunu aşamaz.
8. One-way mode, isolated margin, auto-add-margin false, max leverage config,
   server-side stop ve pilot equity cap kuralları hard constraint'tir.
9. Minimum notional'a ulaşmak için riski otomatik artırma. Geçerli miktar
   üretilemiyorsa SKIP_TRADE dön.
10. Her emirde idempotency ve benzersiz client order ID kullan. HTTP 503 UNKNOWN
    sonucunda kör retry yapma; WebSocket/order query ile çöz.
11. Her fazdan sonra testleri çalıştır, docs/test-reports/phase-XX.md üret,
    PHASE_STATUS.md güncelle ve kabul kapısı geçmediyse sonraki faza geçme.
12. UI'yi Playwright ile gerçek tarayıcıda kontrol et. Loading, empty, stale,
    disconnected, partial-fill, stop-missing ve halt durumlarını ayrı test et.
13. Secret, API key, signature, auth header ve kişisel hesap verisini loglama.
14. Kodun çalışması için gereken komutları README’ye yaz; Windows + VS Code
    kullanıcısının tek tek uygulayabileceği biçimde hazırla.
15. Her fazda küçük ve geri alınabilir commitler üret. Kod yazmadan önce o fazın
    kısa uygulama planını ve kabul kriterlerini göster.

ŞİMDİKİ GÖREV

A) Henüz kod yazma.
B) Belgeler arasındaki çelişkileri, eksik kararları ve güncel API değişikliklerini
   araştır.
C) PLAN.md'yi somut dosya/komut/test çıktılarıyla iyileştir.
D) Faz 0 kabul raporunu oluştur.
E) Bana Faz 1'e başlamaya hazır olup olmadığını, varsa yalnız gerçek blocker'ları
   söyle.
```

Faz 0 onaylandıktan sonra ikinci komut:

```text
/goal PLAN.md'yi faz faz uygula. Her fazın kodunu, otomatik testlerini, güvenlik
kontrollerini, Playwright doğrulamasını ve test raporunu tamamlamadan sonraki faza
geçme. FINAL_ACCEPTANCE_REPORT.md tamamlanıp bütün kapılar yeşil olmadan benden
Binance bağlantısı isteme. Faz 14'e geldiğinde API anahtarlarını sohbet içinde
istemek yerine yerel Bağlantı Sihirbazını aç ve bana Binance'te hangi izinleri
etkinleştireceğimi adım adım göster. İlk gerçek emirden önce planlanan maksimum
zararı, kademeleri, stopu, komisyon/slippage tahminini ve canlı sermaye sınırını
UI'da göstererek açık onay iste.
```
