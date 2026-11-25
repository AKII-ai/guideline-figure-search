"""Python 版（tools/rag.py・tools/agent_lite.py）とブラウザ版（rag.js）・中継（proxy/worker.js）が同じ結果を出すか確かめる。

  python tools/check_parity.py

評価の質問すべてで、次を比べる。Node が必要。Gemini は呼ばない。
  - Gemini に送る抜粋の id と、図表の候補の id の並び（rag.py ⇔ rag.js）
  - 軽量エージェントのプロンプト（1回目・確認なし・2回目）（agent_lite.py ⇔ worker.js）
  - 記載箇所の候補（rag.py ⇔ rag.js）と、AI の答えの確かめ方（pick_sections ⇔ pickSections）
  - 探し直しの検索語で探した候補（agent_lite.py ⇔ rag.js）
  - Gemini のモデル名と送る中身（rag.py ⇔ worker.js）、料金表のモデル名
  - 図表番号の見分け、次の一手の確かめ方（clean_next ⇔ cleanNext）
  - 通常検索：文章かどうかの見分け（normal_search.py ⇔ rag.js）と、完全一致の並び（normal_search.py ⇔ app.js の localHits）
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.agent_lite import (  # noqa: E402
    AGENT_SCHEMA,
    MAX_TOKENS,
    RETRY_SCHEMA,
    AgentLite,
    agent_prompt,
    clean_next,
    has_figure_number,
    retry_query,
)
from tools.normal_search import exact_hits, is_sentence  # noqa: E402
from tools.rag import MATCH_SCHEMA, MODEL, Guideline, clean_request, clean_sections, gemini_payload, pick_sections  # noqa: E402

NODE = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const root = process.argv[1];
const rag = require(path.join(root, "rag.js"));
const read = (name) => JSON.parse(fs.readFileSync(path.join(root, "data", name), "utf8"));
const figures = read("figures.json");
const guide = new rag.Guideline(read("chunks.json"), figures, read("synonyms.json"));
// worker.js は ES module。export を外して関数だけ取り出す
const src = fs.readFileSync(path.join(root, "proxy", "worker.js"), "utf8").replace("export default {", "const __worker = {").replace("export class ", "class ");
const box = { console, URL, Response: function () {}, Headers: function () {} };
vm.createContext(box);
vm.runInContext(src + "\n;this.W = { cleanRequest, cleanSections, pickSections, buildAgentPrompt, cleanNext, cleanPrevious, MODEL, geminiPayload, AGENT_SCHEMA, MATCH_SCHEMA, RETRY_SCHEMA, AGENT_MAX_TOKENS };", box);
const W = box.W;
const input = JSON.parse(fs.readFileSync(0, "utf8"));
// "@0"・"@1"・"@last" をその質問の候補の id に置き換える
const fillPicks = (picks, sections) => ({
  sections: picks.sections.map((x) => {
    if (!x || typeof x !== "object" || !String(x.id).startsWith("@")) return x;
    const at = x.id === "@last" ? sections.length - 1 : Number(x.id.slice(1));
    return { ...x, id: sections[at] ? sections[at].id : "" };
  }),
});
const out = input.questions.map((q) => {
  const req = guide.request(q);
  const { passages, rows } = W.cleanRequest(req.passages, req.candidates, figures);
  const sections = W.cleanSections(req.sections);
  const previous = W.cleanPrevious(rows.slice(0, 2).map((r) => r.number).concat(["図9.9.9-9", ""]), figures).map((p) => p.number);
  const retry = guide.request(rag.retryQuery("土壌汚染状況調査", q));
  return {
    passages: req.passages.map((p) => [p.id, p.quote]),
    candidates: req.candidates.map((c) => [c.id, c.chunk]),
    sections: req.sections.map((s) => [s.id, s.ch, s.no, s.title, s.page, s.quote]),
    prompts: [
      W.buildAgentPrompt(q, passages, rows, 1, true, [], sections),
      W.buildAgentPrompt(q, passages, rows, 1, false, [], sections),
      W.buildAgentPrompt(q, passages, rows, 2, false, previous, sections),
      W.buildAgentPrompt(q, passages, rows, 1, true, []),
    ],
    picked: W.pickSections(fillPicks(input.picks, sections), sections),
    retry: retry.candidates.map((c) => c.id),
    number: rag.hasFigureNumber(q),
  };
});
const next = input.next.map(([parsed, attempt, allow]) => W.cleanNext(parsed, attempt, allow));
const numbers = input.numbers.map((q) => rag.hasFigureNumber(q));
// app.js の通常検索（完全一致）を取り出して動かす
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const pick = (name) => {
  const start = app.indexOf(`function ${name}(`);
  let depth = 0;
  let end = app.indexOf("{", start);
  for (; end < app.length; end++) {
    if (app[end] === "{") depth++;
    else if (app[end] === "}" && --depth === 0) break;
  }
  return app.slice(start, end + 1);
};
const normalBox = { state: { rows: figures, kind: "", chapter: "" } };
vm.createContext(normalBox);
vm.runInContext(
  ["passesFilter", "localHits", "scoreRow", "fieldHits", "haystack", "tokenize", "compact", "normalize"].map(pick).join(";") +
    ";this.localHits = localHits;",
  normalBox
);
const normal = input.normal.map((q) => ({ sentence: rag.isSentence(q), exact: normalBox.localHits(q).map((r) => r.url) }));
const sent = {
  model: W.MODEL,
  pricing: read("pricing.json").model,
  agent: W.geminiPayload("質問", W.AGENT_SCHEMA, 1024),
  match: W.geminiPayload("質問", W.MATCH_SCHEMA, 1024),
  retry: W.geminiPayload("質問", W.RETRY_SCHEMA, W.AGENT_MAX_TOKENS),
};
process.stdout.write(JSON.stringify({ out, next, numbers, normal, sent }));
"""

NEXT_CASES = [
    ({"status": "ok"}, 1, True),
    ({"status": "clarify", "clarify_question": "どちらですか？\n\n", "clarify_options": ["土壌", "地下水", "ガス", "その他"]}, 1, True),
    ({"status": "clarify", "clarify_question": "どちらですか", "clarify_options": ["土壌"]}, 1, True),
    ({"status": "clarify", "clarify_question": "どちらですか", "clarify_options": ["土壌", "地下水"]}, 1, False),
    ({"status": "retry", "retry_query": "あ" * 80}, 1, True),
    ({"status": "retry", "retry_query": "  "}, 1, True),
    ({"status": "retry", "retry_query": "言い換え"}, 2, False),
    ({"status": "standards", "substance": "トリクロロエチレン", "medium": "土壌"}, 1, True),
    ({"status": "hack"}, 1, True),
    ({}, 1, True),
]
# AI の答え（記載箇所）の確かめ方：候補にない id・同じ章の2件目・長い理由・形の違う項目
PICKS = {
    "sections": [
        {"id": "c9999", "reason": "候補にない"},
        {"id": "@0", "reason": "  一つ目\n" + "あ" * 60},
        {"id": "@1", "reason": "同じ章の2件目かもしれない"},
        "文字列",
        {"id": "@last", "reason": ""},
        {"id": "@0", "reason": "重ね"},
    ]
}


def fill_picks(picks, sections):
    """"@0"・"@1"・"@last" をその質問の候補の id に置き換える（check_parity の NODE と同じ）。"""
    out = []
    for x in picks["sections"]:
        if not isinstance(x, dict) or not str(x.get("id")).startswith("@"):
            out.append(x)
            continue
        at = len(sections) - 1 if x["id"] == "@last" else int(x["id"][1:])
        out.append({**x, "id": sections[at]["id"] if 0 <= at < len(sections) else ""})
    return {"sections": out}


NORMAL_EXTRA = ["おそれ", "地歴の流れ", "表2.3.1-2", "表２．３．１－２", "自然由来 盛土", "遮水工封じ込めを適用できる汚染って何？", "ｐｃｅ　分解生成物"]
NUMBERS = ["表2.3.1-2 を見たい", "図5.10.7-2", "図 2.3 - 1", "図2.5.1-1(1)", "表２．３．１－２", "2.3.1項", "表の見方", "12条"]


def main():
    rows = [json.loads(line) for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    questions = [row["q"] for row in rows]
    normal = questions + [row["keywords"] for row in rows if row.get("keywords")] + NORMAL_EXTRA
    questions += ["12条", "ボーリング", "VOC 土壌ガス 代表地点", "ｐｃｅ　分解生成物"]
    guide = Guideline(ROOT)
    agent = AgentLite(guide)
    run = subprocess.run(
        ["node", "-e", NODE, str(ROOT)],
        input=json.dumps(
            {"questions": questions, "next": NEXT_CASES, "numbers": NUMBERS, "normal": normal, "picks": PICKS}, ensure_ascii=False
        ).encode("utf-8"),
        capture_output=True,
    )
    if run.returncode:
        sys.exit(run.stderr.decode("utf-8", errors="replace"))
    js = json.loads(run.stdout.decode("utf-8"))
    bad = 0
    for question, other in zip(questions, js["out"]):
        req = guide.request(question)
        passages, rows = clean_request(req["passages"], req["candidates"], guide.by_id)
        sections = clean_sections(req["sections"])
        previous = [n for _, n in agent.clean_previous([r["number"] for r in rows[:2]] + ["図9.9.9-9", ""])]
        py = {
            "passages": [[p["id"], p["quote"]] for p in req["passages"]],
            "candidates": [[c["id"], c["chunk"]] for c in req["candidates"]],
            "sections": [[s["id"], s["ch"], s["no"], s["title"], s["page"], s["quote"]] for s in req["sections"]],
            "prompts": [
                agent_prompt(question, passages, rows, 1, True, [], sections),
                agent_prompt(question, passages, rows, 1, False, [], sections),
                agent_prompt(question, passages, rows, 2, False, previous, sections),
                agent_prompt(question, passages, rows, 1, True, []),
            ],
            "picked": pick_sections(fill_picks(PICKS, sections), sections),
            "retry": [c["id"] for c in guide.request(retry_query("土壌汚染状況調査", question))["candidates"]],
            "number": has_figure_number(question),
        }
        if py != other:
            bad += 1
            print(f"差あり: {question}")
            for key in py:
                if py[key] != other[key]:
                    print(f"  {key} が違う")
    for case, other in zip(NEXT_CASES, js["next"]):
        if clean_next(*case) != other:
            bad += 1
            print(f"次の一手の確かめ方に差あり: {case} → py {clean_next(*case)} / js {other}")
    for text, other in zip(NUMBERS, js["numbers"]):
        if has_figure_number(text) != other:
            bad += 1
            print(f"図表番号の見分けに差あり: {text}")
    for text, other in zip(normal, js["normal"]):
        py = {"sentence": is_sentence(text), "exact": [row["url"] for row in exact_hits(guide.figures, text)]}
        if py != other:
            bad += 1
            print(f"通常検索に差あり: {text}")
    sent = {
        "model": MODEL,
        "pricing": MODEL,
        "agent": gemini_payload("質問", AGENT_SCHEMA, 1024),
        "match": gemini_payload("質問", MATCH_SCHEMA, 1024),
        "retry": gemini_payload("質問", RETRY_SCHEMA, MAX_TOKENS),
    }
    for name, value in sent.items():
        if value != js["sent"][name]:
            bad += 1
            print(f"Gemini に送る中身に差あり: {name}")
    total = len(sent) + len(questions) + len(NEXT_CASES) + len(NUMBERS) + len(normal)
    print(
        f"{total} 件中 {total - bad} 件が一致（送る中身 {len(sent)}・質問 {len(questions)}・次の一手 {len(NEXT_CASES)}・"
        f"図表番号 {len(NUMBERS)}・通常検索 {len(normal)}）"
    )
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
