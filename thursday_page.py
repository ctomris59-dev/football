#!/usr/bin/env python3
"""Minimal user-facing Thursday betting dashboard HTML.

The page intentionally exposes only the operational decision surface:
status + frozen High Confidence + High Confidence Value lists.
"""
from __future__ import annotations


def render_page() -> str:
    return r'''<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <meta name="theme-color" content="#0b1220" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="apple-mobile-web-app-title" content="Perşembe Bahis" />
  <title>Perşembe Bahis Listesi</title>
  <style>
    :root{--bg:#07101d;--panel:#0d1828;--panel2:#111f32;--line:#21334a;--text:#f4f7fb;--muted:#96a8be;--ok:#38d39f;--warn:#f4bd50;--blue:#63a7ff;--green:#4bdb9f;--shadow:0 18px 50px rgba(0,0,0,.25)}
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(180deg,#07101d 0%,#0a1422 100%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
    .wrap{max-width:1080px;margin:0 auto;padding:28px 18px 56px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--blue);font-weight:800}.hero h1{font-size:clamp(28px,5vw,44px);line-height:1.05;margin:7px 0 10px}.hero p{margin:0;color:var(--muted);max-width:720px;line-height:1.55}.stamp{white-space:nowrap;color:var(--muted);font-size:13px;padding-top:8px}
    .status{background:linear-gradient(135deg,#0f1d30,#0c1727);border:1px solid var(--line);border-radius:20px;padding:18px 20px;box-shadow:var(--shadow);margin-bottom:22px}.statusTop{display:flex;align-items:center;justify-content:space-between;gap:16px}.statusTitle{font-size:18px;font-weight:800}.badge{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:800}.badge.pending{background:rgba(244,189,80,.12);color:var(--warn);border:1px solid rgba(244,189,80,.25)}.badge.final{background:rgba(56,211,159,.12);color:var(--ok);border:1px solid rgba(56,211,159,.25)}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}.step{padding:12px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.02)}.step b{display:block;font-size:13px}.step span{display:block;font-size:11px;color:var(--muted);margin-top:4px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.section{background:var(--panel);border:1px solid var(--line);border-radius:20px;overflow:hidden;box-shadow:var(--shadow)}.sectionHead{padding:18px 18px 15px;border-bottom:1px solid var(--line)}.sectionHead h2{margin:0;font-size:19px}.sectionHead p{margin:6px 0 0;color:var(--muted);font-size:12px}.list{padding:10px}.pick{background:var(--panel2);border:1px solid #21344c;border-radius:15px;padding:14px;margin:8px 0}.teams{font-size:15px;font-weight:800;line-height:1.35}.market{margin-top:6px;color:#dbe7f5;font-size:14px}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}.metric{background:#0a1524;border:1px solid #1b2e44;border-radius:11px;padding:9px 10px}.metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em}.metric b{display:block;margin-top:3px;font-size:14px}.metric.good b{color:var(--green)}.empty{padding:34px 20px;text-align:center;color:var(--muted);font-size:14px}.footer{text-align:center;color:#71869e;font-size:11px;margin-top:22px;line-height:1.5}.refresh{border:1px solid var(--line);background:#0d1a2b;color:#dbe7f5;border-radius:10px;padding:9px 12px;font-weight:700;cursor:pointer}.refresh:active{transform:translateY(1px)}
    @media(max-width:760px){.wrap{padding:22px 13px 42px}.hero{display:block}.stamp{margin-top:10px}.grid{grid-template-columns:1fr}.flow{grid-template-columns:1fr 1fr}.statusTop{align-items:flex-start}.metrics{grid-template-columns:1fr 1fr}}
  </style>
</head>
<body>
<main class="wrap">
  <header class="hero">
    <div><div class="eyebrow">Football Decision Engine</div><h1>Perşembe Bahis Listesi</h1><p>Perşembe hazırlığı → Türkiye açılış oranı → uluslararası no-vig doğrulama → iki liste → bahis yap → bitti.</p></div>
    <div class="stamp" id="updated">Kontrol ediliyor…</div>
  </header>

  <section class="status">
    <div class="statusTop"><div><div class="statusTitle" id="statusTitle">Haftalık karar kontrol ediliyor</div><div style="color:var(--muted);font-size:13px;margin-top:5px" id="statusText">Veri okunuyor…</div></div><div style="display:flex;gap:8px;align-items:center"><button class="refresh" onclick="loadData()">Yenile</button><div class="badge pending" id="badge"><span class="dot"></span><span id="badgeText">BEKLENİYOR</span></div></div></div>
    <div class="flow">
      <div class="step"><b>1. Model</b><span>Form + oyuncu + kadro bağlamı</span></div>
      <div class="step"><b>2. Türkiye oranı</b><span>Gerçek oynanabilir açılış fiyatı</span></div>
      <div class="step"><b>3. Dünya piyasası</b><span>Paired same-book no-vig kontrolü</span></div>
      <div class="step"><b>4. Dondur</b><span>Haftanın iki listesi kilitlenir</span></div>
    </div>
  </section>

  <div class="grid">
    <section class="section"><div class="sectionHead"><h2>🛡️ Yüksek Güven</h2><p>Modelin en güçlü ve Perşembe günü oynanabilir seçimleri.</p></div><div class="list" id="highList"><div class="empty">Liste henüz kesinleşmedi.</div></div></section>
    <section class="section"><div class="sectionHead"><h2>💰 Yüksek Güven + Value</h2><p>Yüksek güven + dünya piyasası doğrulaması + Türkiye fiyat avantajı.</p></div><div class="list" id="valueList"><div class="empty">Liste henüz kesinleşmedi.</div></div></section>
  </div>
  <div class="footer">Bu sayfa yalnızca haftalık dondurulmuş kararı gösterir. T−1/T−3 yeni bahis listesi üretilmez. Sayfayı tarayıcı favorilerine veya ana ekrana sabitleyebilirsin.</div>
</main>
<script>
const $=id=>document.getElementById(id);
const pct=x=>x===null||x===undefined?'—':(Number(x)*100).toFixed(1).replace('.',',')+'%';
const odd=x=>x===null||x===undefined?'—':Number(x).toFixed(2).replace('.',',');
const marketName=x=>({over_2_5:'2.5 ÜST',btts:'KG VAR',corners_over_8_5:'8.5 KORNER ÜST'}[x]||x||'—');
function clear(el){while(el.firstChild)el.removeChild(el.firstChild)}
function metric(label,value,good=false){const d=document.createElement('div');d.className='metric'+(good?' good':'');const s=document.createElement('span');s.textContent=label;const b=document.createElement('b');b.textContent=value;d.append(s,b);return d}
function pickCard(p,isValue){const card=document.createElement('article');card.className='pick';const t=document.createElement('div');t.className='teams';t.textContent=`${p.home||''} – ${p.away||''}`;const m=document.createElement('div');m.className='market';m.textContent=marketName(p.market||p.selection);const ms=document.createElement('div');ms.className='metrics';ms.append(metric('Model güveni',pct(p.confidence),true));ms.append(metric('Türkiye oranı',odd(p.tr_price||p.tr_opening_price)));if(isValue){ms.append(metric('Dünya fair',pct(p.international_fair_probability||p.international_probability||p.market_reference_probability)));ms.append(metric('Model EV',pct(p.model_ev_vs_tr||p.ev),true));}card.append(t,m,ms);return card}
function renderList(id,items,isValue){const el=$(id);clear(el);if(!items||!items.length){const d=document.createElement('div');d.className='empty';d.textContent=isValue?'Uygun value bahis yok.':'Bu hafta kriterleri geçen seçim yok.';el.appendChild(d);return}items.forEach(p=>el.appendChild(pickCard(p,isValue)))}
async function loadData(){try{const r=await fetch('/thursday-list',{cache:'no-store'});const d=await r.json();$('updated').textContent='Son kontrol: '+new Date().toLocaleString('tr-TR');const finalized=d.status==='finalized';$('badge').className='badge '+(finalized?'final':'pending');$('badgeText').textContent=finalized?'LİSTE DONDURULDU':'BÜLTEN BEKLENİYOR';$('statusTitle').textContent=finalized?'Bu haftanın bahis listesi hazır':'Haftalık karar henüz hazır değil';$('statusText').textContent=finalized?`Liste ${d.finalized_at?new Date(d.finalized_at).toLocaleString('tr-TR'):''} tarihinde kilitlendi. Sonradan değiştirilmez.`:'Türkiye hedef oranları ve uluslararası doğrulama tamamlanınca liste otomatik burada görünecek.';if(finalized){renderList('highList',d.high_confidence,false);renderList('valueList',d.high_confidence_value,true)}}catch(e){$('statusTitle').textContent='Bağlantı hatası';$('statusText').textContent='Sayfayı yenileyerek tekrar dene.'}}
loadData();setInterval(loadData,60000);
</script>
</body></html>'''
