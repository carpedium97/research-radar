#!/usr/bin/env python3
"""
深讀 —— 跑在你自己的電腦上，不在 GitHub Actions 上。

把想細看的論文 PDF 丟進 inbox/ 資料夾，跑：
    python deepread.py

每個 PDF 會產出一份同名的 .md 結構化中文筆記到 notes/，PDF 移到 done/。
已經處理過的不會重複做。

    export ANTHROPIC_API_KEY=sk-ant-...
"""

import os
import sys
import shutil

import requests
from pypdf import PdfReader

ROOT = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(ROOT, "inbox")
NOTES = os.path.join(ROOT, "notes")
DONE = os.path.join(ROOT, "done")
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

TEMPLATE = """你是一位資深的精神醫學研究方法學審閱者。讀完下面這篇論文全文，
用正體中文輸出一份結構化筆記，格式如下，不要加任何前後客套：

## 一句話
（這篇做了什麼、結論是什麼，一句講完）

## 研究設計
- 設計類型：
- 樣本：來源、N、族群特徵、納入排除
- 主要變項與測量：
- 統計／模型：

## 主要發現
（條列 3-5 點，附上關鍵數字與信賴區間；沒報就寫「未報告」）

## 方法學問題
（誠實列出。特別注意：樣本代表性、outcome ascertainment、
資料洩漏、過度配適、多重比較、外部驗證有沒有做、效果量是否臨床有意義。
沒有明顯問題就直說沒有，不要硬湊。）

## 對這位讀者的用處
讀者是臺北市立聯合醫院松德院區的精神科主治醫師、陽明交大神經科學博士生。
主軸是（1）中文精神科住院病歷的 LLM 結構化擷取與再入院預測，
（2）神經影像結合成癮與思覺失調症族群。
請具體說明：
- 可以直接借用的東西（方法、量表、統計做法、pipeline 設計、評估指標）
- 可以引用的位置（計畫書背景／方法辯護／討論）
- 不適用的地方與原因

## 值得追的引用
（文中提到、他應該回去找來讀的 2-4 篇，寫出作者年份與為什麼）

---
論文全文：

"""


def extract(path, max_chars=180000):
    reader = PdfReader(path)
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts)
    if len(text) < 500:
        return None          # 大概是掃描檔，沒有文字層
    return text[:max_chars]


def ask(text):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 4000,
              "messages": [{"role": "user", "content": TEMPLATE + text}]},
        timeout=300)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def main():
    if not KEY:
        sys.exit("缺少 ANTHROPIC_API_KEY")
    for d in (INBOX, NOTES, DONE):
        os.makedirs(d, exist_ok=True)

    pdfs = [f for f in sorted(os.listdir(INBOX)) if f.lower().endswith(".pdf")]
    if not pdfs:
        print(f"inbox 是空的。把 PDF 丟到 {INBOX} 再跑一次。")
        return

    for name in pdfs:
        src = os.path.join(INBOX, name)
        out = os.path.join(NOTES, os.path.splitext(name)[0] + ".md")
        if os.path.exists(out):
            print(f"跳過（已有筆記）：{name}")
            continue
        print(f"讀取：{name}")
        text = extract(src)
        if text is None:
            print(f"  這份沒有文字層，可能是掃描檔，先跳過：{name}")
            continue
        try:
            note = ask(text)
        except Exception as e:
            print(f"  失敗：{e}")
            continue
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# {os.path.splitext(name)[0]}\n\n{note}\n")
        shutil.move(src, os.path.join(DONE, name))
        print(f"  已寫入 notes/{os.path.basename(out)}")


if __name__ == "__main__":
    main()
