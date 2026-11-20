"""Python 版（tools/rag.py・tools/agent_lite.py）とブラウザ版（rag.js）・中継（proxy/worker.js）が同じ結果を出すか確かめる。

  python tools/check_parity.py

評価の質問すべてで、次を比べる。Node が必要。Gemini は呼ばない。
  - Gemini に送る抜粋の id と、図表の候補の id の並び（rag.py ⇔ rag.js）
  - 軽量エージェントのプロンプト（1回目・確認なし・2回目）（agent_lite.py ⇔ worker.js）
  - 探し直しの検索語で探した候補（agent_lite.py ⇔ rag.js）
  - 図表番号の見分け、次の一手の確かめ方（clean_next ⇔ cleanNext）
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.agent_lite import AgentLite, agent_prompt, clean_next, has_figure_number, retry_query  # noqa: E402
from tools.rag import Guideline, clean_request  # noqa: E402

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
const src = fs.readFileSync(path.join(root, "proxy", "worker.js"), "utf8").replace("export default {", "const __worker = {");
const box = { console, URL, Response: function () {}, Headers: function () {} };
vm.createContext(box);
vm.runInContext(src + "\n;this.W = { cleanRequest, buildAgentPrompt, cleanNext, cleanPrevious };", box);
const W = box.W;
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const out = input.questions.map((q) => {
  const req = guide.request(q);
  const { passages, rows } = W.cleanRequest(req.passages, req.candidates, figures);
  const previous = W.cleanPrevious(rows.slice(0, 2).map((r) => r.number).concat(["図9.9.9-9", ""]), figures).map((p) => p.number);
  const retry = guide.request(rag.retryQuery("土壌汚染状況調査", q));
  return {
    passages: req.passages.map((p) => [p.id, p.quote]),
    candidates: req.candidates.map((c) => [c.id, c.chunk]),
    prompts: [
      W.buildAgentPrompt(q, passages, rows, 1, true, []),
      W.buildAgentPrompt(q, passages, rows, 1, false, []),
      W.buildAgentPrompt(q, passages, rows, 2, false, previous),
    ],
    retry: retry.candidates.map((c) => c.id),
    number: rag.hasFigureNumber(q),
  };
});
const next = input.next.map(([parsed, attempt, allow]) => W.cleanNext(parsed, attempt, allow));
const numbers = input.numbers.map((q) => rag.hasFigureNumber(q));
process.stdout.write(JSON.stringify({ out, next, numbers }));
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
NUMBERS = ["表2.3.1-2 を見たい", "図5.10.7-2", "図 2.3 - 1", "図2.5.1-1(1)", "表２．３．１－２", "2.3.1項", "表の見方", "12条"]


def main():
    questions = [json.loads(line)["q"] for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    questions += ["12条", "ボーリング", "VOC 土壌ガス 代表地点", "ｐｃｅ　分解生成物"]
    guide = Guideline(ROOT)
    agent = AgentLite(guide)
    run = subprocess.run(
        ["node", "-e", NODE, str(ROOT)],
        input=json.dumps({"questions": questions, "next": NEXT_CASES, "numbers": NUMBERS}, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
    )
    if run.returncode:
        sys.exit(run.stderr.decode("utf-8", errors="replace"))
    js = json.loads(run.stdout.decode("utf-8"))
    bad = 0
    for question, other in zip(questions, js["out"]):
        req = guide.request(question)
        passages, rows = clean_request(req["passages"], req["candidates"], guide.by_id)
        previous = [n for _, n in agent.clean_previous([r["number"] for r in rows[:2]] + ["図9.9.9-9", ""])]
        py = {
            "passages": [[p["id"], p["quote"]] for p in req["passages"]],
            "candidates": [[c["id"], c["chunk"]] for c in req["candidates"]],
            "prompts": [
                agent_prompt(question, passages, rows, 1, True, []),
                agent_prompt(question, passages, rows, 1, False, []),
                agent_prompt(question, passages, rows, 2, False, previous),
            ],
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
    total = len(questions) + len(NEXT_CASES) + len(NUMBERS)
    print(f"{total} 件中 {total - bad} 件が一致（質問 {len(questions)}・次の一手 {len(NEXT_CASES)}・図表番号 {len(NUMBERS)}）")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
