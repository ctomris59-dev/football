#!/usr/bin/env python3
"""User-facing Thursday betting dashboard.

The primary product is one reliability-ranked Top-10 list. Positions 1-4 are Core 4,
5-8 are Strong, and 9-10 are Other Reliable. Positive Turkey-executable model EV is
shown only as a VALUE badge on the existing ranked pick; value never becomes a
separate list and never changes the reliability order.
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
    :root{--bg:#07101d;--panel:#0d1828;--panel2:#111f32;--line:#21334a;--text:#f4f7fb;--muted:#96a8be;--ok:#38d39f;--warn:#f4bd50;--blue:#63a7ff;--green:#4bdb9f;--gold:#f4bd50;--shadow:0 18px 50px rgba(0,0,0,.25)}
    *{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#07101d 0%,#0a1422 100%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
    .wrap{max-width:1080px;margin:0 auto;padding:28px 18px 56px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--blue);font-weight:800}.hero h1{font-size:clamp(28px,5vw,44px);line-height:1.05;margin:7px 0 10px}.hero p{margin:0;color:var(--muted);max-width:760px;line-height:1.55}.stamp{white-space:nowrap;color:var(--muted);font-size:13px;padding-top:8px}
    .status{background:linear-gradient(135deg,#0f1d30,#0c1727);border:1px solid var(--line);border-radius:20px;padding:18px 20px;box-shadow:var(--shadow);margin-bottom:22px}.statusTop{display:flex;align-items:center;justify-content:space-between;gap:16px}.statusTitle{font-size:18px;font-weight:800}.badge{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:800}.badge.pending{background:rgba(244,189,80,.12);color:var(--warn);border:1px solid rgba(244,189,80,.25)}.badge.final{background:rgba(56,211,159,.12);color:var(--ok);border:1px solid rgba(56,211,159,.25)}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}.step{padding:12px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.02)}.step b{display:block;font-size:13px}.step span{display:block;font-size:11px;color:var(--muted);margin-top:4px}
    .stack{display:grid;grid-template-columns:1fr;gap:18px}.section{background:var(--panel);border:1px solid var(--line);border-radius:20px;overflow:hidden;box-shadow:var(--shadow)}.sectionHead{padding:18px 18px 15px;border-bottom:1px solid var(--line)}.sectionHead h2{margin:0;font-size:19px}.sectionHead p{margin:6px 0 0;color:var(--muted);font-size:12px}.list{padding:10px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.pick{background:var(--panel2);border:1px solid #21344c;border-radius:15px;padding:14px}.pick.core{border-color:rgba(56,211,159,.42)}.pick.strong{border-color:rgba(99,167,255,.38)}.teams{font-size:15px;font-weight:800;line-height:1.35}.market{margin-top:6px;color:#dbe7f5;font-size:14px}.row{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:8px}.tier{display:inline-block;padding:5px 8px;border-radius:999px;background:rgba(99,167,255,.10);border:1px solid rgba(99,167,255,.25);font-size:10px;font-weight:800;color:#b9d7ff}.tier.core{background:rgba(56,211,159,.10);border-color:rgba(56,211,159,.28);color:var(--green)}.tier.other{background:rgba(150,168,190,.10);border-color:rgba(150,168,190,.25);color:#c6d3e1}.value{display:inline-block;padding:5px 8px;border-radius:999px;background:rgba(244,189,80,.12);border:1px solid rgba(244,189,80,.30);font-size:10px;font-weight:900;color:var(--gold)}.strict{display:inline-block;padding:5px 8px;border-radius:999px;background:rgba(56,211,159,.08);border:1px solid rgba(56,211,159,.20);font-size:10px;font-weight:800;color:#9df1d1}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:12px}.metric{background:#0a1524;border:1px solid #1b2e44;border-radius:11px;padding:9px 10px}.metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em}.metric b{display:block;margin-top:3px;font-size:14px}.metric.good b{color:var(--green)}.metric.valueMetric b{color:var(--gold)}.empty{grid-column:1/-1;padding:34px 20px;text-align:center;color:var(--muted);font-size:14px}.footer{text-align:center;color:#71869e;font-size:11px;margin-top:22px;line-height:1.5}.refresh{border:1px solid var(--line);background:#0d1a2b;color:#dbe7f5;border-radius:10px;padding:9px 12px;font-weight:700;cursor:pointer}.refresh:active{transform:translateY(1px)}
    @media(max-width:760px){.wrap{padding:22px 13px 42px}.hero{display:block}.stamp{margin-top:10px}.flow{grid-template-columns:1fr 1fr}.statusTop{align-items:flex-start}.list{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
  </style>
</head>
<body>
<main class="wrap">
  <header class="hero">
    <div><div class="eyebrow">Football Decision Engine</div><h1>Perşembe Bahis Listesi</h1><p>BTTS + Alt/Üst + Korner + 1X2 aynı havuzda yarışır. Ana sıralama yalnız tahmin güvenilirliğine göre yapılır; Türkiye'de gerçek pozitif EV varsa mevcut seçime sadece 💰 VALUE rozeti eklenir.</p></div>
    <div class="stamp" id="updated">Kontrol ediliyor…</div>
  </header>

  <section class="status">
    <div class="statusTop"><div><div class="statusTitle" id="statusTitle">Haftalık karar kontrol ediliyor</div><div style="color:var(--muted);font-size:13px;margin-top:5px" id="statusText">Veri okunuyor…</div></div><div style="display:flex;gap:8px;align-items:center"><button class="refresh" onclick="loadData()">Yenile</button><div class="badge pending" id="badge"><span class="dot"></span><span id="badgeText">BEKLENİYOR</span></div></div></div>
    <div class="flow">
      <div class="step"><b>1. Model</b><span>V1 + veri kalitesi + kadro bağlamı</span></div>
      <div class="step"><b>2. Piyasa kontrolü</b><span>Yabancı no-vig yalnız sanity/çelişki kontrolü</span></div>
      <div class="step"><b>3. Güven sırası</b><span>Her maçtan en güçlü tek seçim</span></div>
      <div class="step"><b>4. Türkiye fiyatı</b><span>Sadece oynanabilir fiyat ve varsa VALUE rozeti</span></div>
    </div>
  </section>

  <div class="stack">
    <section class="section"><div class="sectionHead"><h2>🛡️ Core 4</h2><p>Haftanın güven sıralamasında ilk 4. Oran düşük diye cezalandırılmaz; öncelik isabet olasılığıdır.</p></div><div class="list" id="coreList"><div class="empty">Liste henüz kesinleşmedi.</div></div></section>
    <section class="section"><div class="sectionHead"><h2>⭐ Güçlü Seçimler</h2><p>Sıralamada 5–8. Core 4'ün hemen arkasındaki güçlü tahminler.</p></div><div class="list" id="strongList"><div class="empty">Liste henüz kesinleşmedi.</div></div></section>
    <section class="section"><div class="sectionHead"><h2>🎯 Diğer Güvenilir Seçimler</h2><p>Sıralamada 9–10. Top-10'u tamamlayan güvenilir tahminler.</p></div><div class="list" id="otherList"><div class="empty">Liste henüz kesinleşmedi.</div></div></section>
  </div>
  <div class="footer">Sıralama tahmin güvenilirliği içindir; ekonomik value ile aynı şey değildir. 💰 VALUE yalnız model olasılığı × Türkiye'deki gerçek oynanabilir oran pozitif EV üretiyorsa gösterilir. Yabancı oran ile Türkiye oranı ham olarak karşılaştırılmaz.</div>
</main>
<script>
const $=id=>document.getElementById(id);
const pct=x=>x===null||x===undefined?'—':(Number(x)*100).toFixed(1).replace('.',',')+'%';
const odd=x=>x===null||x===undefined?'—':Number(x).toFixed(2).replace('.',',');
function clear(el){while(el.firstChild)el.removeChild(el.firstChild)}
function metric(label,value,cls=''){const d=document.createElement('div');d.className='metric '+cls;const s=document.createElement('span');s.textContent=label;const b=document.createElement('b');b.textContent=value;d.append(s,b);return d}
function pickKey(p){return `${p.event_id||''}|${p.market||''}|${p.selection||''}`}
function valueMap(items){const m=new Map();(items||[]).forEach(v=>m.set(pickKey(v),v));return m}
function tierForIndex(i){if(i<4)return {label:'CORE 4',cls:'core',card:'core'};if(i<8)return {label:'GÜÇLÜ',cls:'',card:'strong'};return {label:'DİĞER GÜVENİLİR',cls:'other',card:''}}
function pickCard(p,index,vmap){const tierInfo=tierForIndex(index);const matchedValue=vmap.get(pickKey(p));const hasValue=Boolean(p.is_value||matchedValue);const ev=p.model_ev_vs_tr??p.value_model_ev_vs_tr??matchedValue?.model_ev_vs_tr??matchedValue?.ev;const card=document.createElement('article');card.className='pick '+tierInfo.card;const t=document.createElement('div');t.className='teams';t.textContent=`#${index+1} ${p.home||''} – ${p.away||''}`;const m=document.createElement('div');m.className='market';m.textContent=p.selection||p.market||'—';const badges=document.createElement('div');badges.className='row';const tier=document.createElement('span');tier.className='tier '+tierInfo.cls;tier.textContent=tierInfo.label;badges.appendChild(tier);if(p.strict_high_confidence){const s=document.createElement('span');s.className='strict';s.textContent='≥%70 MODEL';badges.appendChild(s)}if(hasValue){const v=document.createElement('span');v.className='value';v.textContent='💰 VALUE';badges.appendChild(v)}const ms=document.createElement('div');ms.className='metrics';ms.append(metric('Model tahmini',pct(p.model_probability_estimate??p.confidence),'good'));ms.append(metric('Türkiye oranı',odd(p.tr_price||p.tr_opening_price)));ms.append(metric('Dünya fair',pct(p.international_fair_probability||p.international_probability||p.market_reference_probability)));if(hasValue&&ev!==null&&ev!==undefined)ms.append(metric('TR Model EV',pct(ev),'valueMetric'));card.append(t,m,badges,ms);return card}
function renderGroup(id,items,startIndex,vmap){const el=$(id);clear(el);if(!items.length){const d=document.createElement('div');d.className='empty';d.textContent='Bu bölüm için henüz seçim yok.';el.appendChild(d);return}items.forEach((p,i)=>el.appendChild(pickCard(p,startIndex+i,vmap)))}
function renderRanked(items,values){const picks=items||[];const vmap=valueMap(values);renderGroup('coreList',picks.slice(0,4),0,vmap);renderGroup('strongList',picks.slice(4,8),4,vmap);renderGroup('otherList',picks.slice(8,10),8,vmap)}
async function loadData(){try{const r=await fetch('/thursday-list',{cache:'no-store'});const d=await r.json();$('updated').textContent='Son kontrol: '+new Date().toLocaleString('tr-TR');const finalized=d.status==='finalized';$('badge').className='badge '+(finalized?'final':'pending');$('badgeText').textContent=finalized?'LİSTE DONDURULDU':'BÜLTEN BEKLENİYOR';$('statusTitle').textContent=finalized?'Bu haftanın Top-10 listesi hazır':'Haftalık karar henüz hazır değil';$('statusText').textContent=finalized?`Liste ${d.finalized_at?new Date(d.finalized_at).toLocaleString('tr-TR'):''} tarihinde kilitlendi. Sıralama güvene göre; value yalnız rozet olarak gösterilir.`:'Türkiye bülteni yeterli kapsama ulaşınca Top-10 güven sırasına göre oluşacak. Value çıkması beklenmez ve listeyi belirlemez.';if(finalized){renderRanked(d.weekly_reliable||d.high_confidence||[],d.high_confidence_value||[])}}catch(e){$('statusTitle').textContent='Bağlantı hatası';$('statusText').textContent='Sayfayı yenileyerek tekrar dene.'}}
loadData();setInterval(loadData,60000);
</script>
</body></html>'''
