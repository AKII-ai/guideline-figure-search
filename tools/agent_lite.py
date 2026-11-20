"""軽量エージェント（agent_lite）。評価用の Python 版。

RAG の1回目の呼び出しで、図表選びと一緒に「次の一手」（status）も答えさせる。判断だけのために AI を呼ばない。

  0. 質問に図表番号（表2.3.1-2 など）があれば、通常検索（キーワード）だけで返す。AI は呼ばない
  1. 1回目：今の RAG と同じ内容（質問・上位6チャンクの抜粋・候補20件）＋ status の判断基準
     status = ok / clarify / retry / standards
  2. ok        … そのまま終わる（AI 1回）
     clarify   … 利用者に確認する（1つの質問につき1回まで）。評価では利用者がいないので1回目の図表を結果にする
     retry     … retry_query と元の質問で本文を探し直し（AI なし）、2回目を呼ぶ。2回目は status を ok に固定
     standards … 法令の基準値アプリへ案内する。図表の候補があればあわせて出す
  AI の呼び出しは1つの質問につき最大2回。2回目には1回目の結果を図表番号だけで渡す（抜粋や理由は渡さない）。

目次は渡さない（tools/agent.py は目次を毎回渡すので入力が約11,500 token になった）。
"""

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.keyword_search import keyword_search  # noqa: E402
from tools.rag import MATCH_SCHEMA, base_number, build_prompt, call_gemini, clean_request, page_id  # noqa: E402

STATUSES = ("ok", "clarify", "retry", "standards")
STANDARDS_URL = "https://akii-ai.github.io/standards-web/"
# 図表番号（表2.3.1-2、図5.10.7-2、図2.3-1、図2.5.1-1(1) など）
FIGURE_NUMBER = re.compile(r"[図表]\s*\d+(?:\s*[.．]\s*\d+)+\s*[-－‐―ー−]\s*\d+", re.ASCII)
RETRY_QUERY_MAX = 60
CLARIFY_MAX = 80
OPTION_MAX = 30
OPTIONS_MAX = 3

STATUS_RULES = (
    "status は次の一手。迷ったら ok。\n"
    "- ok: 候補の中に質問に合う図表がある\n"
    "{clarify}"
    "- retry: 候補が質問に合わず、言い方を変えれば当たりそうなとき。retry_query にガイドラインでの言い方の検索語（60字まで）\n"
    "- standards: 数値の基準値そのもの（〇〇の土壌溶出量基準は何 mg/L か等）を聞いているとき。substance に物質名、medium に土壌／地下水などの別\n"
    "status が ok 以外でも、候補に役立つ図表があれば matches に入れる。"
)
CLARIFY_RULE = (
    "- clarify: 質問の意味が2通り以上に取れ、それぞれ別の項に行き着くときだけ。"
    "clarify_question に確認の一文（80字まで・改行なし）、clarify_options に選択肢2〜3個（各30字まで）\n"
)

AGENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "status": {"type": "STRING", "enum": list(STATUSES)},
        "matches": {**MATCH_SCHEMA["properties"]["matches"], "maxItems": 5},
        # 入れ子にすると確認の一文が改行の繰り返しで止まらなくなることがあったので、平らにして短く縛る
        "clarify_question": {"type": "STRING", "description": "確認の一文。80字まで。改行なし"},
        "clarify_options": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 3},
        "retry_query": {"type": "STRING", "description": "検索語。60字まで"},
        "substance": {"type": "STRING", "description": "物質名"},
        "medium": {"type": "STRING", "description": "土壌／地下水など"},
    },
    "required": ["status", "matches"],
    "propertyOrdering": ["status", "clarify_question", "clarify_options", "retry_query", "substance", "medium", "matches"],
}


def has_figure_number(query):
    # 全角の数字も半角にしてから見る（rag.js と同じ）
    return bool(FIGURE_NUMBER.search(unicodedata.normalize("NFKC", query or "")))


def retry_query(retry, query):
    """探し直すときの検索語：言い換えた言葉＋元の質問。rag.js の retryQuery と同じ。"""
    return f"{retry} {query}"


def clip(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean_next(parsed, attempt, allow_clarify=True):
    """AI が返した次の一手を確かめる。worker.js の cleanNext と同じ決まり。"""
    status = str(parsed.get("status") or "ok")
    if status not in STATUSES or attempt >= 2 or (status == "clarify" and not allow_clarify):
        status = "ok"
    out = {"status": status}
    if status == "clarify":
        options = [clip(o, OPTION_MAX) for o in (parsed.get("clarify_options") or []) if clip(o, OPTION_MAX)][:OPTIONS_MAX]
        question = clip(parsed.get("clarify_question"), CLARIFY_MAX)
        if not question or len(options) < 2:
            out["status"] = "ok"  # 確認の形になっていなければ、そのまま結果を出す
        else:
            out["clarify"] = {"question": question, "options": options}
    elif status == "retry":
        retry = clip(parsed.get("retry_query"), RETRY_QUERY_MAX)
        if retry:
            out["retry_query"] = retry
        else:
            out["status"] = "ok"
    elif status == "standards":
        out["standards"] = {"substance": clip(parsed.get("substance"), 30), "medium": clip(parsed.get("medium"), 20)}
    return out


def agent_prompt(query, passages, rows, attempt, allow_clarify, previous):
    base = build_prompt(query, passages, rows)
    if attempt >= 2:
        prev = "、".join(previous) if previous else "なし"
        return (
            base
            + "\n\n言い換えて探し直した2回目です。1回目に選んだ図表番号: "
            + prev
            + "\n候補の中から選び直してください。1回目の図表のほうが合うなら、その番号を id に返してもよい。"
        )
    return base + "\n\n" + STATUS_RULES.format(clarify=CLARIFY_RULE if allow_clarify else "")


class AgentLite:
    def __init__(self, guide):
        self.guide = guide
        self.by_number = {}
        for row in guide.figures:
            key = base_number(row.get("number"))
            if key:
                self.by_number.setdefault(key, row)

    def clean_previous(self, raw):
        """1回目の図表番号は figures.json にある番号だけ通す。worker.js の cleanPrevious と同じ。[(id, 番号)]"""
        out = []
        for value in (raw if isinstance(raw, list) else [])[:10]:
            row = self.by_number.get(base_number(clip(value, 30)))
            if not row or any(i == page_id(row["url"]) for i, _ in out):
                continue
            out.append((page_id(row["url"]), row["number"]))
            if len(out) >= 5:
                break
        return out

    def ask(self, query, req, attempt, api_key, allow_clarify=True, previous=()):
        """Gemini を1回呼ぶ。worker.js が受け取って確かめるのと同じ内容。"""
        passages, rows = clean_request(req["passages"], req["candidates"], self.guide.by_id)
        # 2回目に渡す「1回目の図表番号」は figures.json にある番号だけ
        previous = self.clean_previous(list(previous or []))
        if not rows:
            return {"matches": [], "status": "ok"}, None
        attempt = 2 if attempt == 2 else 1
        allow_clarify = attempt == 1 and allow_clarify
        schema = AGENT_SCHEMA if attempt == 1 else MATCH_SCHEMA
        prompt = agent_prompt(query, passages, rows, attempt, allow_clarify, [n for _, n in previous])
        parsed, usage = call_gemini(prompt, schema, api_key)
        allowed = {r["id"] for r in rows} | {i for i, _ in previous}
        chunk_of = {r["id"]: r["chunk"] for r in rows}
        known = {p["id"] for p in passages} | set(chunk_of.values())
        matches = []
        for item in parsed.get("matches") or []:
            item_id = self.guide.resolve(item.get("id"), allowed)
            if not item_id or any(m["id"] == item_id for m in matches):
                continue
            chunk = str(item.get("chunk") or "")
            matches.append(
                {
                    "id": item_id,
                    "reason": str(item.get("reason") or "").strip(),
                    "chunk": chunk if chunk in known else chunk_of.get(item_id, ""),
                }
            )
            if len(matches) >= 5:
                break
        result = clean_next(parsed, attempt, allow_clarify)
        result["matches"] = matches
        return result, usage

    def search(self, query, api_key, allow_clarify=True):
        """戻り値は Guideline.search と同じ形。usage は全呼び出しの合計、steps に status の流れを残す。"""
        total = {"prompt": 0, "output": 0, "calls": 0, "steps": []}

        def add(usage):
            total["calls"] += 1
            total["prompt"] += (usage or {}).get("prompt") or 0
            total["output"] += (usage or {}).get("output") or 0

        if has_figure_number(query):
            rows = keyword_search(self.guide.figures, query)
            total["steps"].append("number")
            matches = [{"id": page_id(r["url"]), "reason": "", "chunk": ""} for r in rows[:8]]
            return {"matches": matches, "passages": [], "usage": total, "next": {"status": "number"}}

        req = self.guide.request(query)
        first, usage = self.ask(query, req, 1, api_key, allow_clarify)
        add(usage)
        total["steps"].append(first["status"])
        passages = req["passages"]
        final = first
        if first["status"] == "retry":
            # ブラウザで探し直す（AI なし）。言い換えた言葉と元の質問をあわせて探す
            retry_req = self.guide.request(retry_query(first["retry_query"], query))
            previous = [self.guide.by_id[m["id"]]["number"] for m in first["matches"]]
            second, usage = self.ask(query, retry_req, 2, api_key, allow_clarify, previous)
            add(usage)
            total["steps"].append(second["status"])
            if second["matches"]:
                final = {**second, "retry_query": first["retry_query"]}
                passages = retry_req["passages"]
        if not total["prompt"]:
            total["prompt"] = None
        nxt = {k: v for k, v in first.items() if k != "matches"}
        return {"matches": final["matches"], "passages": passages, "usage": total, "next": nxt}


if __name__ == "__main__":
    import os

    from tools.rag import Guideline

    guide = Guideline(ROOT)
    question = " ".join(sys.argv[1:]) or "地歴調査で集める資料"
    result = AgentLite(guide).search(question, os.environ.get("GEMINI_API_KEY", ""))
    for m in result["matches"]:
        print(guide.by_id[m["id"]]["number"], m["reason"])
    print(result["next"], result["usage"])
