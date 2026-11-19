"""Python 版（tools/rag.py）とブラウザ版（rag.js）が同じ結果を出すか確かめる。

  python tools/check_parity.py

評価の質問すべてで、Gemini に送る抜粋の id と、図表の候補の id の並びを比べる。Node が必要。
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.rag import Guideline  # noqa: E402

NODE = r"""
const fs = require("fs");
const path = require("path");
const root = process.argv[1];
const { Guideline } = require(path.join(root, "rag.js"));
const read = (name) => JSON.parse(fs.readFileSync(path.join(root, "data", name), "utf8"));
const guide = new Guideline(read("chunks.json"), read("figures.json"), read("synonyms.json"));
const questions = JSON.parse(fs.readFileSync(0, "utf8"));
const out = questions.map((q) => {
  const req = guide.request(q);
  return { passages: req.passages.map((p) => [p.id, p.quote]), candidates: req.candidates.map((c) => [c.id, c.chunk]) };
});
process.stdout.write(JSON.stringify(out));
"""


def main():
    questions = [json.loads(line)["q"] for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    questions += ["12条", "ボーリング", "VOC 土壌ガス 代表地点", "ｐｃｅ　分解生成物"]
    guide = Guideline(ROOT)
    run = subprocess.run(
        ["node", "-e", NODE, str(ROOT)],
        input=json.dumps(questions, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    js = json.loads(run.stdout.decode("utf-8"))
    bad = 0
    for question, other in zip(questions, js):
        req = guide.request(question)
        py = {
            "passages": [[p["id"], p["quote"]] for p in req["passages"]],
            "candidates": [[c["id"], c["chunk"]] for c in req["candidates"]],
        }
        if py != other:
            bad += 1
            print(f"差あり: {question}")
            for key in ("passages", "candidates"):
                if py[key] != other[key]:
                    print(f"  {key}\n    py {[x[0] for x in py[key]]}\n    js {[x[0] for x in other[key]]}")
    print(f"{len(questions)} 問中 {len(questions) - bad} 問が一致")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
