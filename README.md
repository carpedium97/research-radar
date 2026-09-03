# Research Radar

每天早上六點自動抓 PubMed、arXiv、bioRxiv、medRxiv，用 Claude 依你的研究輪廓
打分排序，產出一頁中文摘要。手機加到主畫面就是一個 App。

---

## 一、建置（大約 20 分鐘，一次做完就不用再碰）

### 1. 開一個 GitHub repo

建議設成 **public**。理由：GitHub Pages 在免費方案下只有 public repo 能開，
而這個 repo 裡面只有公開論文的中繼資料和你的關鍵字，沒有任何敏感內容。
如果你不想讓別人看到你的研究輪廓，那就設 private，改成用 email 寄結果
（見下方「不想用 Pages」）。

把這個資料夾的內容整包推上去。

### 2. 拿一把 Anthropic API key

到 <https://console.anthropic.com> 建立 API key，儲值 5 美金就夠跑很久。
這跟你 Claude 訂閱是分開計費的兩件事。

### 3. 設定 repo secrets

repo → Settings → Secrets and variables → Actions → New repository secret：

| 名稱 | 必要 | 內容 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 是 | 上一步拿到的 key |
| `CONTACT_EMAIL` | 建議 | 你的 email，NCBI 要求識別呼叫者身分 |
| `NCBI_API_KEY` | 選用 | 在 NCBI 帳號設定頁免費申請，速率上限從 3/秒 提到 10/秒 |

### 4. 開啟 GitHub Pages

repo → Settings → Pages → Source 選 **Deploy from a branch**，
branch 選 `main`、資料夾選 `/docs`。存檔後網址會是
`https://<你的帳號>.github.io/<repo 名>/`。

### 5. 填 config.yaml 裡的 EZproxy 前綴

用你的陽明帳號登入圖書館電子資源，隨便點進一個資料庫，看網址列。
把 `login?url=` 為止那一整段（含 `login?url=`）貼進 `config.yaml` 的
`ezproxy_prefix`。之後輸出頁每篇論文旁邊都會多一個「全文（校內）」連結，
手機上點一下就直接進 PDF。

前綴填錯或留空都不會讓程式壞掉，只是少一個連結。

### 6. 手動跑第一次

repo → Actions → 「daily radar」 → Run workflow。
兩三分鐘後 `docs/index.html` 會被更新並自動 commit 回來。

### 7. 手機加到主畫面

用 Safari 或 Chrome 開 Pages 網址 → 分享 → 加入主畫面。
之後它的行為就跟一個 App 一樣。

---

## 二、日常使用

早上打開，只看「值得讀」那一區，通常 3–8 篇。每篇有：

- 論文標題
- Claude 寫的一句中文，說明這篇對**你**的意義（不是重述摘要）
- 分類標籤、期刊、日期
- PubMed / DOI / 全文（校內）三個連結

要收進 library 的，用 **Zotero Connector** 存（Zotero 設定裡可以填學校 proxy，
存的時候會連 PDF 一起抓下來），接你原本的 Zotero + Better BibTeX 流程。

要細讀的，見下面的深讀腳本。

---

## 三、調校

前兩週一定會不準，這是正常的。三個旋鈕：

**分數不對** → 改 `config.yaml` 的 `profile`。這是最有效的一個。
把打了高分但你其實不想看的類型，明確寫進「他『不』需要」那段。

**太多雜訊** → 把 `thresholds.read` 從 7 調到 8。

**漏掉東西** → 期刊清單和 `topic_terms` 加詞。
期刊名要用 PubMed 的正式刊名或 ISO 縮寫，寫錯只會抓不到、不會報錯。

**成本** → 每天大約 60–150 篇進打分，用 Haiku 一個月落在 1–3 美金。
真的要壓，把 `max_score_items` 調小。

---

## 四、深讀腳本（跑在你自己電腦上）

排程那支跑在 GitHub 的機器上，連不到陽明的 VPN，所以拿不到全文。
全文的部分你自己下載、自己在本機處理：

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
mkdir -p inbox
# 把想細看的 PDF 丟進 inbox/
python deepread.py
```

每份 PDF 會在 `notes/` 產出一份結構化中文筆記：一句話結論、研究設計、
主要發現、方法學問題、對你計畫的可用之處、值得追的引用。
處理完的 PDF 會移到 `done/`。

**不要把這個自動化成批次下載。** 出版商（Elsevier、Wiley、Springer）的
反爬蟲一旦判定異常，封的是整個學校的 IP 段，圖書館會回頭查到帳號。
手動一篇一篇下載完全沒問題，腳本代抓是另一回事。

如果 `notes/` 裡的 PDF 或筆記涉及未公開資料，記得把 `inbox/`、`notes/`、
`done/` 加進 `.gitignore`，不要推上 public repo。

---

## 五、不想用 Pages（改成寄 email）

把 `daily.yml` 最後那個 commit 步驟換成寄信的 action，
或最省事的做法：在 `radar.py` 的 `render()` 最後加一段用
[Resend](https://resend.com) 或 SendGrid 的 API 把 `page` 這個變數寄給自己。
HTML 已經是可以直接當信件內容的格式。

---

## 六、涵蓋不到的東西

老實講清楚這個系統抓不到什麼：

- **MICCAI、ACL、AMIA、CHIL 的會議論文**：只能靠作者自己丟 arXiv 時側面攔截。
  正式接受名單要自己去看議程。
- **OHBM 年會摘要**：完全抓不到，只能人工。
- **Nature 系列的 news & views、editorial**：PubMed 有收但常常沒摘要，
  會因為缺摘要被打低分。

這三塊建議一年手動看兩次（會議 accepted paper 公布時），不要試圖自動化。
