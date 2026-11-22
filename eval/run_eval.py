"""図表検索の評価。3つの方式を同じ質問で比べ、eval/results.md に表を書く。

  keyword: 昨年の Notion キーワード検索に相当（図表一覧に言葉が入っているかだけ。app.js の通常検索。tools/keyword_search.py）。
           質問文をそのまま入れる
  keyword_words : keyword に、質問の keywords（実務者が通常検索に打ち込みそうな単語を空白区切り）を入れる
  normal_v2 : 新しい通常検索（AI なし。tools/normal_search.py）。単語は完全一致、文章か完全一致0件なら本文 BM25
  〇〇_words : 〇〇 に keywords を入れる（keyword_words・bm25_words・normal_v2_words）
  legacy : 以前の方式（図表の目次全体を Gemini に渡す）
  bm25   : 本文 BM25 ＋ 言い換え辞書 ＋ 本文→図表のつながり（AI なし）
  rag    : bm25 の上位だけを Gemini に渡して選ばせる
  agent  : 目次から項を選び、その項の本文を読んで選ぶ（Gemini を最大3回。tools/agent.py）
  mixed  : agent の目次に、本文 BM25 で当たった項をヒントとして添える（混合型）
  agent_lite : 軽量エージェント。rag の1回目で次の一手（ok/clarify/retry/standards）も答えさせ、
               retry のときだけ探し直して2回目を呼ぶ（最大2回。目次は渡さない。tools/agent_lite.py）

使い方:
  python eval/run_eval.py                 # bm25 だけ（キー不要）
  python eval/run_eval.py --all           # 3方式（GEMINI_API_KEY が必要。.env でも可）
  python eval/run_eval.py --methods rag   # 方式を選ぶ
  python eval/run_eval.py --source claude # 質問の出どころで絞る（claude = 調整用、user = 実務者の確認用、wg = WG で集めた質問）
  python eval/run_eval.py --methods keyword_words --source claude --out lite --keep  # 前回の表に方式を足す

質問（eval/questions.jsonl）の keywords は質問文から自然に取れる言葉だけにする（正解の図表名に合わせない）。

本文の命中 = 画面に出す「本文の該当箇所」上位3件のどれかが、正解の項（items）に入った割合。
以前の方式は本文を見ないので数えない。
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.rag import GeminiError, Guideline, base_number, embed_query, norm_num, page_id  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.jsonl"
RESULTS = ROOT / "eval" / "results.md"
DETAIL = ROOT / "eval" / "results.json"
# --source を付けたときは別のファイルに書く（全体の結果を上書きしない）
K = 5
PASSAGES = 3


def api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "GEMINI_API_KEY":
                key = value.strip().strip('"').strip("'")
    return key


def numbers(guide, ids):
    return [guide.by_id[i]["number"] for i in ids if i in guide.by_id]


def score(expected, got):
    """expected のどれか1つが上位 K 件に入れば命中。MRR は最初に当たった順位で数える。"""
    want = {base_number(n) for n in expected} | {norm_num(n) for n in expected}
    for rank, number in enumerate(got, start=1):
        if norm_num(number) in want or base_number(number) in want:
            return (1 if rank <= K else 0), 1 / rank, rank
    return 0, 0.0, None


def item_hit(items, passages):
    """正解の項（"2.3.1" や付録 "A12"）に、上位の抜粋が入ったか。"""
    for passage in passages[:PASSAGES]:
        no = passage["no"]
        if any(no == item or no.startswith(item + ".") for item in items):
            return 1
    return 0


_agents = {}
QUERY_CACHE = ROOT / "data" / "build" / "query_vectors.json"
_query_cache = None


def query_vector(question, key):
    """質問の意味ベクトル。何度も評価するので保存して使い回す（時間は初回の分を数える）。"""
    global _query_cache
    if _query_cache is None:
        _query_cache = json.loads(QUERY_CACHE.read_text(encoding="utf-8")) if QUERY_CACHE.exists() else {}
    if question not in _query_cache:
        _query_cache[question] = embed_query(question, key)
        QUERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        QUERY_CACHE.write_text(json.dumps(_query_cache), encoding="utf-8")
    return _query_cache[question]


def run_method(guide, method, question, key):
    started = time.perf_counter()
    usage = None
    passages = None
    note = None  # normal_v2 で使った検索（記録の「切り替えの理由」と同じ言葉）
    if method in ("hybrid", "rag_hybrid"):
        qvec = query_vector(question, key)
        result = guide.search(question, api_key=key, qvec=qvec, use_llm=method == "rag_hybrid")
        usage = result["usage"]
        passages = result["passages"]
    elif method in ("keyword", "keyword_words"):
        from tools.keyword_search import keyword_search

        rows = keyword_search(guide.figures, question)
        result = {"matches": [{"id": page_id(r["url"])} for r in rows]}
    elif method in ("normal_v2", "normal_v2_words"):
        from tools.normal_search import normal_search

        rows, engine, note, _ = normal_search(guide, question)
        result = {"matches": [{"id": page_id(r["url"])} for r in rows]}
    elif method == "agent_lite":
        if method not in _agents:
            from tools.agent_lite import AgentLite

            _agents[method] = AgentLite(guide)
        result = _agents[method].search(question, key)
        usage = result["usage"]
        passages = result["passages"] or None
    elif method in ("bm25", "bm25_words"):
        result = guide.search(question)
        passages = result["passages"]
    elif method == "rag":
        result = guide.search(question, api_key=key)
        usage = result["usage"]
        passages = result["passages"]
    elif method in ("agent", "mixed"):
        if method not in _agents:
            from tools.agent import Agent

            _agents[method] = Agent(guide, hints=method == "mixed")
        result = _agents[method].search(question, key)
        usage = result["usage"]
        passages = result["passages"]
    elif method == "legacy":
        import server  # 以前の方式のプロンプトは server.py にある

        result = server.legacy_search(question, "", "", key)
        usage = result["usage"]
    else:
        raise ValueError(method)
    elapsed = time.perf_counter() - started
    return numbers(guide, [m["id"] for m in result["matches"]]), usage, elapsed, passages, note


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--methods", default="")
    parser.add_argument("--source", default="", help="claude / user / wg で質問を絞る")
    parser.add_argument("--out", default="", help="結果ファイル名の後ろに付ける言葉（前の結果を上書きしない）")
    parser.add_argument("--keep", action="store_true", help="同じ結果ファイルにある、今回走らせない方式の結果を残す")
    parser.add_argument("--wait", type=float, default=4.5, help="Gemini を呼ぶ間隔（秒）。無料枠の回数制限よけ")
    args = parser.parse_args()
    methods = args.methods.split(",") if args.methods else (["legacy", "bm25", "rag"] if args.all else ["bm25"])
    key = api_key()
    no_ai = ("bm25", "bm25_words", "keyword", "keyword_words", "normal_v2", "normal_v2_words")
    if any(m not in no_ai for m in methods) and not key:
        sys.exit("GEMINI_API_KEY がありません。環境変数か .env に入れてください。")

    guide = Guideline(ROOT)
    questions = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.source:
        questions = [q for q in questions if q.get("source") == args.source]
    if not questions:
        sys.exit(f"source={args.source} の質問がまだありません（eval/questions.jsonl に source が {args.source} の行を足してください）。")
    detail = []
    summary = {}
    for method in methods:
        hits, rr, tokens, times, errors, places, calls, statuses = [], [], [], [], 0, [], [], {}
        for item in questions:
            question = item["q"]
            if method.endswith("_words"):
                if not item.get("keywords"):
                    print(f"[{method}] keywords がないので質問文のまま探す: {question}")
                question = item.get("keywords") or question
            try:
                got, usage, elapsed, passages, note = run_method(guide, method, question, key)
            except GeminiError as error:
                print(f"[{method}] error: {error}")
                errors += 1
                got, usage, elapsed, passages, note = [], None, 0.0, None, None
            if note:
                statuses[note] = statuses.get(note, 0) + 1
            place = item_hit(item["items"], passages) if passages is not None and item.get("items") else None
            if place is not None:
                places.append(place)
            hit, recip, rank = score(item["expected"], got)
            hits.append(hit)
            rr.append(recip)
            times.append(elapsed)
            if usage and usage.get("prompt"):
                tokens.append(usage["prompt"])  # 2回呼んだ質問は合計
            calls.append((usage or {}).get("calls", 1 if usage else 0))
            first = ((usage or {}).get("steps") or [None])[0]
            if method == "agent_lite" and isinstance(first, str):
                statuses[first] = statuses.get(first, 0) + 1
            detail.append(
                {
                    "method": method,
                    "q": item["q"],
                    "expected": item["expected"],
                    "got": got[:8],
                    "rank": rank,
                    "items": item.get("items"),
                    "passages": [p["no"] for p in (passages or [])[:PASSAGES]],
                    "place": place,
                    "calls": (usage or {}).get("calls"),
                    "steps": (usage or {}).get("steps"),
                    "tokens": (usage or {}).get("prompt"),
                    "output": (usage or {}).get("output"),
                    "thoughts": (usage or {}).get("thoughts"),
                    "model": (usage or {}).get("model"),
                }
            )
            mark = "o" if hit else "x"
            print(f"[{method}] {mark} rank={rank} {item['q']}  -> {', '.join(got[:5])}")
            if method in ("legacy", "rag", "rag_hybrid", "agent", "mixed", "agent_lite"):
                time.sleep(args.wait)
        summary[method] = {
            "recall": sum(hits) / len(hits),
            "mrr": sum(rr) / len(rr),
            "tokens": statistics.median(tokens) if tokens else None,
            "tokens_max": max(tokens) if tokens else None,
            "calls": sum(calls) / len(calls) if calls else 0,
            "calls_max": max(calls) if calls else 0,
            "statuses": statuses,
            "time": statistics.median(times),
            "errors": errors,
            "place": sum(places) / len(places) if places else None,
        }

    tag = "_".join(x for x in (args.source, args.out) if x)
    results = RESULTS.with_name(f"results_{tag}.md") if tag else RESULTS
    detail_path = DETAIL.with_name(f"results_{tag}.json") if tag else DETAIL
    if args.keep and detail_path.exists():
        # 前回の結果のうち、今回走らせなかった方式はそのまま残す（Gemini を呼び直さない）
        old = json.loads(detail_path.read_text(encoding="utf-8"))
        kept = [m for m in old["summary"] if m not in summary]
        summary = {**{m: old["summary"][m] for m in kept}, **summary}
        detail = [row for row in old["detail"] if row["method"] in kept] + detail
        print(f"前回の結果から残した方式: {', '.join(kept) or 'なし'}")
    order = [
        "keyword", "keyword_words", "legacy", "bm25", "bm25_words", "normal_v2", "normal_v2_words",
        "hybrid", "rag", "rag_hybrid", "agent", "mixed", "agent_lite",
    ]
    summary = dict(sorted(summary.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))
    detail.sort(key=lambda row: order.index(row["method"]) if row["method"] in order else len(order))

    names = {
        "legacy": "以前の方式（目次全体）",
        "bm25": "本文BM25（AIなし）",
        "hybrid": "ハイブリッド（BM25＋意味、AIなし）",
        "rag": "RAG（BM25＋Gemini）",
        "rag_hybrid": "RAG（ハイブリッド＋Gemini）",
        "agent": "エージェント（目次→本文、Gemini 最大3回）",
        "mixed": "混合型（本文検索のヒント＋目次→本文）",
        "keyword": "keyword（文章のまま。昨年の方式、AIなし）",
        "keyword_words": "keyword（単語で入力。昨年の方式、AIなし）",
        "bm25_words": "本文BM25（単語で入力、AIなし）",
        "normal_v2": "通常検索・新（文章のまま、AIなし）",
        "normal_v2_words": "通常検索・新（単語で入力、AIなし）",
        "agent_lite": "軽量エージェント（RAG＋次の一手、Gemini 最大2回）",
    }
    lines = [
        "# 図表検索の評価結果",
        "",
        f"質問 {len(questions)} 問（`eval/questions.jsonl`{'、source=' + args.source if args.source else ''}）。"
        f"命中 = 正解の図表のどれかが上位{K}件に入った割合。本文の命中 = 上位{PASSAGES}件の抜粋が正解の項に入った割合。",
        "",
        f"| 方式 | 図表の命中率 (Recall@{K}) | MRR | 本文の命中率 | AI の呼び出し（平均／最大） | 入力token（中央値） | 入力token（最大） | 応答時間（中央値） |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for method, row in summary.items():
        tokens = f"{row['tokens']:,.0f}" if row["tokens"] else "0（AIなし）"
        tokens_max = f"{row['tokens_max']:,.0f}" if row["tokens_max"] else "0"
        call = f"{row['calls']:.2f}／{row['calls_max']}"
        err = f"（エラー {row['errors']}）" if row["errors"] else ""
        place = f"{row['place']:.0%}" if row["place"] is not None else "—（本文を見ない）"
        lines.append(
            f"| {names[method]}{err} | {row['recall']:.0%} | {row['mrr']:.2f} | {place} | {call} | {tokens} | {tokens_max} | {row['time']:.1f} 秒 |"
        )
    for method, row in summary.items():
        if row["statuses"]:
            order = ["ok", "clarify", "retry", "standards", "number", "図表番号", "単語", "文章のため自動", "0件のため自動"]
            parts = [f"{x} {row['statuses'][x]}" for x in order if x in row["statuses"]]
            label = "使った検索の内訳" if method.startswith("normal_v2") else "1回目の status の内訳"
            lines += ["", f"{names[method]} の{label}: " + "／".join(parts)]
    lines += ["", "## 外れた質問", ""]
    for row in detail:
        if row["rank"] is None or row["rank"] > K:
            lines.append(f"- [{row['method']}] {row['q']} … 正解 {', '.join(row['expected'])} ／ 結果 {', '.join(row['got'][:5]) or 'なし'}")
    lines += ["", "## 本文の場所が外れた質問", ""]
    for row in detail:
        if row["place"] == 0:
            lines.append(f"- [{row['method']}] {row['q']} … 正解の項 {', '.join(row['items'])} ／ 結果 {', '.join(row['passages'])}")
    results.write_text("\n".join(lines) + "\n", encoding="utf-8")
    detail_path.write_text(json.dumps({"summary": summary, "detail": detail}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines[: lines.index("## 外れた質問")]))
    print(f"-> {results.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
