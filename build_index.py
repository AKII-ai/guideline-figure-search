"""ガイドライン本文の図表言及を figures.json の context に結ぶ。"""

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MD = ROOT / "ガイドライン3-1.md"
FIGURES = ROOT / "data" / "figures.json"
CITE = re.compile(r"[図表]\d+(?:\.\d+)*-\d+")


def norm_num(value):
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("‐", "-").replace("－", "-").replace("ー", "-")
    return text


def paragraphs(text):
    pieces = []
    for line in text.splitlines():
        line = line.lstrip("#").strip()
        if line:
            pieces.append(line)
    buf = ""
    for piece in pieces:
        buf = f"{buf} {piece}".strip()
        if len(buf) >= 420:
            yield "", buf
            buf = piece
    if buf:
        yield "", buf


def main():
    rows = json.loads(FIGURES.read_text(encoding="utf-8"))
    by_number = {}
    for row in rows:
        by_number.setdefault(norm_num(row.get("number")), []).append(row)

    contexts = {key: [] for key in by_number}
    cited = 0
    linked = 0
    for heading, paragraph in paragraphs(MD.read_text(encoding="utf-8")):
        numbers = []
        for raw in CITE.findall(unicodedata.normalize("NFKC", paragraph)):
            number = norm_num(raw)
            if number not in numbers:
                numbers.append(number)
        if not numbers:
            continue
        cited += len(numbers)
        snippet = paragraph.strip()
        if len(snippet) > 420:
            snippet = snippet[:420] + "…"
        block = snippet
        for number in numbers:
            if number not in contexts:
                continue
            linked += 1
            bucket = contexts[number]
            if block not in bucket and len(bucket) < 2:
                bucket.append(block)

    with_context = 0
    for number, group in by_number.items():
        text = " ".join(contexts.get(number) or [])
        if len(text) > 900:
            text = text[:900] + "…"
        for row in group:
            row["context"] = text
            if text:
                with_context += 1

    FIGURES.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"figures {len(rows)} with_context {with_context} mentions {cited} linked {linked}")


if __name__ == "__main__":
    main()
