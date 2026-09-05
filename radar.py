#!/usr/bin/env python3
"""
Research Radar
抓 PubMed / arXiv / bioRxiv / medRxiv，用 Claude 依設定檔的評分標準打分，
產出一頁中文摘要頁面。支援收藏與讚／倒讚回饋。

跑法：
    python radar.py                 # 用 config.yaml（個人研究雷達，每天）
    python radar.py teaching.yaml   # 用 teaching.yaml（教學新知，每週）

環境變數：
    ANTHROPIC_API_KEY  （必要）
    NCBI_API_KEY       （選用，PubMed 速率上限 3/秒 → 10/秒）
    CONTACT_EMAIL      （選用，NCBI 要求識別身分用）
"""

import os
import re
import sys
import json
import time
import html
from datetime import datetime, timedelta, timezone

import requests
import feedparser
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
TPE = timezone(timedelta(hours=8))

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_KEY = os.environ.get("NCBI_API_KEY", "")
CONTACT = os.environ.get("CONTACT_EMAIL", "radar@example.com")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "research-radar/2.0 (personal literature alert)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def as_text(v, default=""):
    """模型偶爾會把字串欄位回成 list 或 dict，全部壓成字串。"""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return as_text(v[0], default) if v else default
    if isinstance(v, dict):
        return as_text(next(iter(v.values()), ""), default) if v else default
    if v is None:
        return default
    return str(v).strip()


def as_int(v, default=0):
    try:
        return int(float(as_text(v, str(default))))
    except Exception:
        return default


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:120]


# ---------------------------------------------------------------- state

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return set(d.get("ids", [])), set(d.get("titles", []))
    except Exception:
        return set(), set()


def save_state(path, ids, titles):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(ids)[-8000:],
                   "titles": sorted(titles)[-8000:]}, f, ensure_ascii=False)


def load_feedback(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return ([d for d in data if d.get("v") == 1][-20:],
                [d for d in data if d.get("v") == -1][-20:])
    except Exception:
        return [], []


# ---------------------------------------------------------------- PubMed

_LAST_CALL = [0.0]


def _pace():
    """NCBI 速率限制：有 key 每秒 10 次，沒有 key 每秒 3 次。保守一點抓。"""
    gap = 0.15 if NCBI_KEY else 0.45
    wait = gap - (time.monotonic() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[0] = time.monotonic()


def _ncbi(method, url, **kw):
    """被 429／5xx 擋下時指數退避重試。GitHub runner 是共用 IP，會被誤傷。"""
    delay = 3.0
    last = None
    for attempt in range(5):
        _pace()
        try:
            r = SESSION.request(method, url, timeout=90, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from NCBI")
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if attempt < 4:
                log(f"    NCBI 被擋，{delay:.0f} 秒後重試（{e}）")
                time.sleep(delay)
                delay *= 2
    raise last


def _esearch(term, days, retmax):
    p = {"db": "pubmed", "term": term, "retmax": retmax, "retmode": "json",
         "datetype": "edat", "reldate": days, "sort": "date",
         "tool": "research-radar", "email": CONTACT}
    if NCBI_KEY:
        p["api_key"] = NCBI_KEY
    r = _ncbi("GET", f"{EUTILS}/esearch.fcgi", params=p)
    return r.json().get("esearchresult", {}).get("idlist", [])


def _efetch(pmids):
    import xml.etree.ElementTree as ET
    out = []
    for i in range(0, len(pmids), 150):
        chunk = pmids[i:i + 150]
        data = {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml",
                "tool": "research-radar", "email": CONTACT}
        if NCBI_KEY:
            data["api_key"] = NCBI_KEY
        try:
            r = _ncbi("POST", f"{EUTILS}/efetch.fcgi", data=data)
            root = ET.fromstring(r.content)
        except Exception as e:
            log(f"  efetch 第 {i // 150 + 1} 批放棄：{e}")
            continue
        for art in root.iter("PubmedArticle"):
            pmid = art.findtext(".//MedlineCitation/PMID") or ""
            te = art.find(".//ArticleTitle")
            title = "".join(te.itertext()).strip() if te is not None else ""
            parts = []
            for ab in art.findall(".//Abstract/AbstractText"):
                lbl = ab.get("Label")
                txt = "".join(ab.itertext()).strip()
                parts.append(f"{lbl}: {txt}" if lbl else txt)
            journal = (art.findtext(".//Journal/ISOAbbreviation")
                       or art.findtext(".//Journal/Title") or "")
            ptypes = [p.text for p in art.findall(".//PublicationType")
                      if p.text]
            doi = ""
            for aid in art.findall(".//ArticleId"):
                if aid.get("IdType") == "doi":
                    doi = (aid.text or "").strip()
            if not title:
                continue
            out.append({
                "id": f"pmid:{pmid}", "source": "PubMed", "venue": journal,
                "title": title, "abstract": " ".join(parts), "doi": doi,
                "ptypes": ptypes,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "date": "",
            })
    return out


def fetch_pubmed(cfg):
    """支援兩種寫法：
       新：pubmed.queries = [{name, term, max}, ...]（可直接寫 PubMed 語法）
       舊：pubmed.journals / pubmed.topic_terms
    """
    days = cfg["lookback_days"]
    pm = cfg.get("pubmed") or {}
    blocks = []
    if pm.get("queries"):
        blocks = [(q.get("name", "query"), q["term"], q.get("max", 200))
                  for q in pm["queries"]]
    else:
        if pm.get("journals"):
            blocks.append(("期刊掃描",
                           "(" + " OR ".join(f'"{j}"[Journal]'
                                             for j in pm["journals"]) + ")",
                           pm.get("journal_max", 200)))
        if pm.get("topic_terms"):
            blocks.append(("主題掃描",
                           "(" + " OR ".join(f'"{t}"[Title/Abstract]'
                                             for t in pm["topic_terms"]) + ")",
                           pm.get("topic_max", 200)))
    ids = []
    for name, term, mx in blocks:
        try:
            got = _esearch(term, days, mx)
            log(f"  pubmed／{name}：{len(got)} 筆")
            ids += got
        except Exception as e:
            log(f"  pubmed／{name} 失敗：{e}")
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    log(f"  pubmed 合計 {len(ids)} 筆待取詳細內容")
    try:
        got = _efetch(ids)
    except Exception as e:
        log(f"  pubmed efetch 失敗：{e}")
        got = []
    if ids and not got:
        log("  警告：一篇都沒取到。多半是 NCBI 擋下（429）。"
            "請設定 NCBI_API_KEY secret，或稍後重跑。")
    return got


# ---------------------------------------------------------------- arXiv

def fetch_arxiv(cfg, cutoff):
    ax = cfg.get("arxiv") or {}
    if not ax.get("categories"):
        return []
    cats = " OR ".join(f"cat:{c}" for c in ax["categories"])
    terms = " OR ".join(f'abs:"{t}"' for t in ax.get("terms", []))
    q = f"({cats}) AND ({terms})" if terms else f"({cats})"
    params = {"search_query": q, "start": 0,
              "max_results": ax.get("max_results", 100),
              "sortBy": "submittedDate", "sortOrder": "descending"}
    hdrs = {"Accept": "application/atom+xml,text/xml;q=0.9,*/*;q=0.8",
            "User-Agent": f"research-radar/2.0 (mailto:{CONTACT})"}
    r = None
    for attempt in range(3):
        try:
            r = SESSION.get("https://export.arxiv.org/api/query",
                            params=params, headers=hdrs, timeout=90)
            r.raise_for_status()
            break
        except Exception as e:
            log(f"  arxiv 第 {attempt + 1} 次失敗：{e}")
            r = None
            time.sleep(5 * (attempt + 1))
    if r is None:
        log("  arxiv 這輪放棄，其他來源照常")
        return []
    out = []
    for e in feedparser.parse(r.text).entries:
        try:
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            continue
        if pub < cutoff:
            continue
        out.append({
            "id": f"arxiv:{e.id.rsplit('/', 1)[-1]}", "source": "arXiv",
            "venue": ", ".join(t.term for t in getattr(e, "tags", [])[:3]),
            "title": re.sub(r"\s+", " ", e.title).strip(),
            "abstract": re.sub(r"\s+", " ", e.summary).strip(),
            "doi": "", "ptypes": [], "url": e.link,
            "date": pub.strftime("%Y-%m-%d"),
        })
    log(f"  arxiv：{len(out)} 筆")
    return out


# ---------------------------------------------------------------- bio/medRxiv

def fetch_rxiv(cfg, start, end):
    rx = cfg.get("rxiv") or {}
    if not rx.get("servers"):
        return []
    cats = {c.lower() for c in rx.get("categories", [])}
    kws = [k.lower() for k in rx.get("keywords", [])]
    out = []
    for server in rx["servers"]:
        cursor, total, seen_n = 0, None, 0
        while True:
            url = f"https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}"
            try:
                j = SESSION.get(url, timeout=60).json()
            except Exception as e:
                log(f"  {server} 失敗：{e}")
                break
            coll = j.get("collection") or []
            if not coll:
                break
            if total is None:
                try:
                    total = int((j.get("messages") or [{}])[0].get("total") or 0)
                except Exception:
                    total = 0
            for p in coll:
                seen_n += 1
                if cats and (p.get("category") or "").lower() not in cats:
                    continue
                blob = f"{p.get('title','')} {p.get('abstract','')}".lower()
                if kws and not any(k in blob for k in kws):
                    continue
                doi = p.get("doi", "")
                out.append({
                    "id": f"{server}:{doi}", "source": server,
                    "venue": p.get("category", ""),
                    "title": (p.get("title") or "").strip(),
                    "abstract": (p.get("abstract") or "").strip(),
                    "doi": doi, "ptypes": [],
                    "url": f"https://doi.org/{doi}" if doi else "",
                    "date": p.get("date", ""),
                })
            cursor += 100
            if (total and cursor >= total) or cursor > 3000:
                break
            time.sleep(0.3)
        log(f"  {server}：掃過 {seen_n} 筆，留下 "
            f"{len([x for x in out if x['source'] == server])} 筆")
    return out


# ---------------------------------------------------------------- scoring

def calibration_block(up, dn):
    if not up and not dn:
        return ""
    def fmt(rows):
        return "\n".join(f"  - {r.get('title','')}（{r.get('venue','')}）"
                         for r in rows)
    b = ["\n以下是他過去對推薦結果的實際回饋。這是最重要的校準依據，"
         "請讓你的評分向這個方向靠攏：\n"]
    if up:
        b.append("【他明確認可、覺得推得好的】\n" + fmt(up))
    if dn:
        b.append("\n【他明確覺得推錯、不該出現在高分區的】\n" + fmt(dn) +
                 "\n（這些不是壞論文，只是對他沒用。同類型的分數要壓低。）")
    return "\n".join(b) + "\n"


DEFAULT_RUBRIC = ("10 = 一定要讀；7-9 = 直接相關，值得讀；"
                  "5-6 = 邊緣相關，掃標題就好；0-4 = 不相關。"
                  "請嚴格，大多數論文應該落在 0-4。")
DEFAULT_WHY = ("一句正體中文（40 字以內），說明這篇對他的意義，"
               "不要重述摘要。")


def score_items(items, cfg, calib):
    if not items:
        return []
    profile = cfg["profile"]
    rubric = cfg.get("rubric") or DEFAULT_RUBRIC
    why_hint = cfg.get("why_instruction") or DEFAULT_WHY
    tags = cfg.get("tags") or ["其他"]
    scored, BATCH = [], 10
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        listing = "\n\n".join(
            f"[{n}]\n期刊：{it['venue']}\n"
            + (f"類型：{', '.join(it.get('ptypes', [])[:4])}\n"
               if it.get("ptypes") else "")
            + f"標題：{it['title']}\n"
              f"摘要：{(it['abstract'] or '(無摘要)')[:1800]}"
            for n, it in enumerate(batch))
        prompt = (
            f"{profile}\n{calib}\n"
            f"請針對下面每一篇論文評分。\n\n{listing}\n\n"
            "請只輸出一個 JSON 陣列，不要有任何前後說明或 markdown 標記。"
            "每個元素包含：\n"
            '  "n": 編號（整數）\n'
            f'  "score": 0-10 的整數。{rubric}\n'
            f'  "why": {why_hint}\n'
            f'  "tag": 從 {json.dumps(tags, ensure_ascii=False)} 擇一。\n')
        try:
            r = SESSION.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 3000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=180)
            r.raise_for_status()
            txt = "".join(b.get("text", "") for b in r.json().get("content", []))
            txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(),
                         flags=re.MULTILINE).strip()
            arr = json.loads(txt)
        except Exception as e:
            log(f"  打分失敗（第 {i // BATCH + 1} 批）：{e}")
            arr = []
        by_n = {}
        if isinstance(arr, list):
            for a in arr:
                if isinstance(a, dict) and "n" in a:
                    by_n[as_int(a["n"], -1)] = a
        for n, it in enumerate(batch):
            a = by_n.get(n, {})
            it["score"] = max(0, min(10, as_int(a.get("score"), 0)))
            it["why"] = as_text(a.get("why"))
            tag = as_text(a.get("tag"), tags[-1]) or tags[-1]
            it["tag"] = tag if tag in tags else tags[-1]
            scored.append(it)
        log(f"  已打分 {min(i + BATCH, len(items))}/{len(items)}")
    return scored


# ---------------------------------------------------------------- render

CSS = """
:root{
  --paper:#fbfaf7; --ink:#16181c; --muted:#6b7280; --rule:#e3e0d8;
  --accent:#1b3a5c; --hot:#8c2f24; --save:#9a6b12;
}
@media (prefers-color-scheme:dark){
  :root{ --paper:#14161a; --ink:#e8e6e1; --muted:#8b8f98; --rule:#282c33;
         --accent:#8fb4d9; --hot:#e0796a; --save:#d3a44a; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;
  line-height:1.6;-webkit-text-size-adjust:100%}
.wrap{max-width:44rem;margin:0 auto;padding:2rem 1.15rem 5rem}
header{padding-bottom:1.1rem;border-bottom:2px solid var(--ink);margin-bottom:1.6rem}
h1{font-family:Georgia,"Noto Serif TC",serif;font-weight:400;
  font-size:1.5rem;margin:0 0 .2rem;letter-spacing:-.01em}
.stamp{color:var(--muted);font-size:.82rem}
.stamp a{margin-left:.6rem}
h2{font-family:Georgia,"Noto Serif TC",serif;font-weight:400;font-size:1.05rem;
  margin:2.4rem 0 .2rem;color:var(--accent)}
h2.saved{color:var(--save)}
.item{display:grid;grid-template-columns:2.4rem 1fr;gap:.85rem;
  padding:1.05rem 0;border-top:1px solid var(--rule)}
.sc{font-family:Georgia,serif;font-size:1.5rem;line-height:1.15;
  color:var(--muted);font-variant-numeric:tabular-nums;padding-top:.05rem}
.sc.hi{color:var(--hot)}
.ttl{font-family:Georgia,"Noto Serif TC",serif;font-size:1.02rem;
  line-height:1.35;margin:0 0 .35rem}
.why{margin:0 0 .45rem}
.meta{color:var(--muted);font-size:.78rem;margin:0 0 .35rem}
.links a{color:var(--accent);font-size:.8rem;margin-right:.9rem;
  text-underline-offset:2px}
a{color:var(--accent)}
.acts{margin-top:.5rem;display:flex;gap:1.1rem;align-items:center}
.acts button{background:none;border:0;padding:0;font:inherit;font-size:.8rem;
  color:var(--muted);cursor:pointer;-webkit-tap-highlight-color:transparent}
.acts button.on{font-weight:600}
.acts .save.on{color:var(--save)}
.acts .up.on{color:var(--accent)}
.acts .dn.on{color:var(--hot)}
details{margin-top:2rem;border-top:1px solid var(--rule);padding-top:.8rem}
summary{cursor:pointer;color:var(--muted);font-size:.86rem}
.empty{color:var(--muted);padding:2rem 0}
footer{margin-top:3.5rem;padding-top:1rem;border-top:1px solid var(--rule);
  color:var(--muted);font-size:.75rem}
footer button{background:none;border:1px solid var(--rule);border-radius:3px;
  padding:.4rem .7rem;font:inherit;font-size:.78rem;color:var(--ink);
  cursor:pointer;margin:.6rem .5rem .6rem 0}
#dump{width:100%;height:9rem;margin-top:.6rem;font-size:.7rem;
  font-family:ui-monospace,monospace;display:none;
  background:var(--paper);color:var(--ink);border:1px solid var(--rule)}
"""

JS = """
const KS=window.NS+'.saved', KF=window.NS+'.feedback';
const rd=k=>{try{return JSON.parse(localStorage.getItem(k))||{}}catch(e){return{}}};
const wr=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v))}catch(e){
  alert('儲存空間寫入失敗，收藏可能不會保留。')}};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function linksHtml(it){
  let h='';
  if(it.url) h+='<a href="'+esc(it.url)+'">'+
    (it.source==='PubMed'?'PubMed':it.source==='arXiv'?'arXiv':'預印本')+'</a>';
  if(it.doi) h+='<a href="https://doi.org/'+esc(it.doi)+'">DOI</a>';
  if(it.doi && window.PROXY) h+='<a href="'+
    esc(window.PROXY+'https://doi.org/'+it.doi)+'">全文（校內）</a>';
  return '<div class="links">'+h+'</div>';
}

function itemHtml(it){
  const meta=[it.tag,it.venue,it.date].filter(Boolean).join(' · ');
  return '<div class="item"><div class="sc">'+esc(it.score)+'</div><div>'
    +'<p class="ttl">'+esc(it.title)+'</p>'
    +(it.why?'<p class="why">'+esc(it.why)+'</p>':'')
    +'<p class="meta">'+esc(meta)+'</p>'+linksHtml(it)
    +'<div class="acts" data-id="'+esc(it.id)+'">'
    +'<button class="save">收藏</button><button class="up">讚</button>'
    +'<button class="dn">倒讚</button></div></div></div>';
}

function renderSaved(){
  const box=document.getElementById('savedbox');
  const arr=Object.values(rd(KS)).sort((a,b)=>(b._at||0)-(a._at||0));
  box.innerHTML = arr.length
    ? '<h2 class="saved">待讀清單（'+arr.length+'）</h2>'+arr.map(itemHtml).join('')
    : '';
  paint();
}

function paint(){
  const saved=rd(KS), fb=rd(KF);
  document.querySelectorAll('.acts').forEach(el=>{
    const id=el.dataset.id, v=(fb[id]||{}).v;
    const sb=el.querySelector('.save');
    sb.classList.toggle('on', !!saved[id]);
    sb.textContent = saved[id]?'已收藏':'收藏';
    el.querySelector('.up').classList.toggle('on', v===1);
    el.querySelector('.dn').classList.toggle('on', v===-1);
  });
}

document.addEventListener('click', e=>{
  const btn=e.target.closest('.acts button'); if(!btn) return;
  const id=btn.closest('.acts').dataset.id;
  const it=(window.ITEMS||{})[id] || rd(KS)[id];
  if(!it) return;
  if(btn.classList.contains('save')){
    const s=rd(KS);
    if(s[id]) delete s[id]; else s[id]=Object.assign({},it,{_at:Date.now()});
    wr(KS,s); renderSaved();
  } else {
    const v=btn.classList.contains('up')?1:-1, f=rd(KF);
    if((f[id]||{}).v===v) delete f[id];
    else f[id]={v:v,title:it.title,venue:it.venue,tag:it.tag,at:Date.now()};
    wr(KF,f); paint();
  }
});

window.addEventListener('DOMContentLoaded',()=>{
  renderSaved();
  document.getElementById('expbtn').addEventListener('click',()=>{
    const arr=Object.entries(rd(KF)).map(([id,o])=>({id:id,v:o.v,
      title:o.title,venue:o.venue,tag:o.tag}));
    const txt=JSON.stringify(arr,null,1), ta=document.getElementById('dump');
    ta.style.display='block'; ta.value=txt; ta.select();
    if(navigator.clipboard) navigator.clipboard.writeText(txt).then(()=>{
      document.getElementById('expbtn').textContent='已複製 '+arr.length+' 筆回饋';},()=>{});
  });
});
"""


def render(items, cfg):
    proxy = (cfg.get("ezproxy_prefix") or "").strip()
    th = cfg.get("thresholds", {"read": 7, "scan": 5})
    title = cfg.get("title", "研究雷達")
    ns = cfg.get("namespace", "radar")
    outfile = cfg.get("output", "index.html")
    items.sort(key=lambda x: -x["score"])
    read = [i for i in items if i["score"] >= th["read"]]
    scan = [i for i in items if th["scan"] <= i["score"] < th["read"]]
    rest = [i for i in items if i["score"] < th["scan"]]
    now = datetime.now(TPE)

    def links_html(it):
        out = []
        if it.get("url"):
            lbl = {"PubMed": "PubMed", "arXiv": "arXiv"}.get(it["source"], "預印本")
            out.append(f'<a href="{html.escape(it["url"])}">{lbl}</a>')
        if it.get("doi"):
            d = f"https://doi.org/{it['doi']}"
            out.append(f'<a href="{html.escape(d)}">DOI</a>')
            if proxy:
                out.append(f'<a href="{html.escape(proxy + d)}">全文（校內）</a>')
        return f'<div class="links">{"".join(out)}</div>'

    def item_html(it, hot):
        meta = " · ".join(x for x in [it.get("tag"), it.get("venue"),
                                      it.get("date")] if x)
        return (
            f'<div class="item"><div class="{"sc hi" if hot else "sc"}">'
            f'{it["score"]}</div><div>'
            f'<p class="ttl">{html.escape(it["title"])}</p>'
            + (f'<p class="why">{html.escape(it["why"])}</p>'
               if it.get("why") else "")
            + f'<p class="meta">{html.escape(meta)}</p>'
            + links_html(it)
            + f'<div class="acts" data-id="{html.escape(it["id"])}">'
              '<button class="save">收藏</button>'
              '<button class="up">讚</button>'
              '<button class="dn">倒讚</button></div></div></div>')

    body = []
    if cfg.get("group_by") == "tag":
        pool = read + scan
        order = cfg.get("tags", [])
        for t in order:
            grp = [i for i in pool if i.get("tag") == t]
            if not grp:
                continue
            body.append(f"<h2>{html.escape(t)}</h2>")
            body += [item_html(i, i["score"] >= th["read"]) for i in grp]
        misc = [i for i in pool if i.get("tag") not in order]
        if misc:
            body.append("<h2>其他</h2>")
            body += [item_html(i, i["score"] >= th["read"]) for i in misc]
        if not pool:
            body.append('<p class="empty">這一輪沒有達到門檻的東西。</p>')
    else:
        if read:
            body.append("<h2>值得讀</h2>")
            body += [item_html(i, True) for i in read]
        if scan:
            body.append("<h2>掃一眼</h2>")
            body += [item_html(i, False) for i in scan]
        if not read and not scan:
            body.append('<p class="empty">今天沒有命中的東西。</p>')
    if rest:
        body.append(f'<details><summary>其餘 {len(rest)} 篇（低相關）</summary>')
        body += [item_html(i, False) for i in rest[:60]]
        body.append("</details>")

    keep = ("id", "title", "why", "tag", "venue", "date", "url", "doi",
            "score", "source")
    lookup = {it["id"]: {k: it.get(k, "") for k in keep} for it in items}
    items_json = json.dumps(lookup, ensure_ascii=False).replace("<", "\\u003c")

    nav = ""
    for label, href in (cfg.get("siblings") or {}).items():
        nav += f'<a href="{html.escape(href)}">{html.escape(label)}</a>'
    stamp = (f"{now:%Y 年 %m 月 %d 日} · 掃過 {len(items)} 篇 · "
             f"值得讀 {len(read)} 篇{nav}")

    page = f"""<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{html.escape(title)}">
<meta name="theme-color" content="#fbfaf7">
<title>{html.escape(title)} {now:%m/%d}</title>
<style>{CSS}</style></head><body><div class="wrap">
<header><h1>{html.escape(title)}</h1>
<div class="stamp">{stamp}</div></header>
<section id="savedbox"></section>
{''.join(body)}
<footer>
<button id="expbtn">匯出回饋（複製到剪貼簿）</button>
<textarea id="dump" readonly></textarea>
<p>收藏與回饋存在這台裝置的瀏覽器裡，換裝置不會同步。
分數由 Claude 依設定檔的評分標準評定，僅供分流參考，不代表論文品質。</p>
</footer>
</div>
<script>
window.NS = {json.dumps(ns)};
window.ITEMS = {items_json};
window.PROXY = {json.dumps(proxy)};
{JS}
</script>
</body></html>"""

    arch = os.path.join(DOCS, "archive", os.path.splitext(outfile)[0])
    os.makedirs(arch, exist_ok=True)
    with open(os.path.join(DOCS, outfile), "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(arch, f"{now:%Y-%m-%d}.html"), "w",
              encoding="utf-8") as f:
        f.write(page)
    log(f"已輸出 docs/{outfile}（值得讀 {len(read)}、掃一眼 {len(scan)}）")


# ---------------------------------------------------------------- main

def main():
    if not ANTHROPIC_KEY:
        sys.exit("缺少 ANTHROPIC_API_KEY")
    cfg_name = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    with open(os.path.join(ROOT, cfg_name), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    log(f"設定檔：{cfg_name}")

    state_path = os.path.join(ROOT, "state",
                              cfg.get("state", "seen.json"))
    fb_path = os.path.join(ROOT, cfg.get("feedback", "feedback.json"))

    days = cfg["lookback_days"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    up, dn = load_feedback(fb_path)
    if up or dn:
        log(f"讀到回饋：讚 {len(up)}、倒讚 {len(dn)}")
    calib = calibration_block(up, dn)

    log("抓取中…")
    items = fetch_pubmed(cfg)
    items += fetch_arxiv(cfg, cutoff)
    items += fetch_rxiv(cfg, (now - timedelta(days=days)).strftime("%Y-%m-%d"),
                        now.strftime("%Y-%m-%d"))

    seen_ids, seen_titles = load_state(state_path)
    fresh, ids_now, titles_now = [], set(), set()
    for it in items:
        nt = norm_title(it["title"])
        if (it["id"] in seen_ids or nt in seen_titles
                or it["id"] in ids_now or nt in titles_now):
            continue
        ids_now.add(it["id"])
        titles_now.add(nt)
        fresh.append(it)
    log(f"去重後新增 {len(fresh)} 篇（原始 {len(items)} 篇）")

    cap = cfg.get("max_score_items", 150)
    if len(fresh) > cap:
        log(f"超過上限，只送前 {cap} 篇打分")
        fresh = fresh[:cap]

    render(score_items(fresh, cfg, calib), cfg)
    save_state(state_path, seen_ids | ids_now, seen_titles | titles_now)


if __name__ == "__main__":
    main()
