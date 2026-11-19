"""ガイドラインPDFから、本文の行をページ・位置・書体つきで取り出す。

出力: data/build/lines.jsonl（1行 = PDFの1行）
図の中の小さな文字は捨て、本文・見出し・図表の題・表の中身を残す。
"""

import json
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "ガイドライン3-1.pdf"
OUT = ROOT / "data" / "build" / "lines.jsonl"

HEADER_Y = 55  # これより上はページ上の欄外（項番号）
FOOTER_Y = 785  # これより下はページ番号
BODY_SIZE = 10.4  # 本文・見出しの文字の大きさ（10.6pt）
TABLE_SIZE = 8.9  # 表の中身（9pt）。これより小さい文字は図の中の文字とみなす


def page_marks(page):
    """欄外の項番号と、印刷されたページ番号を返す。"""
    head = ""
    foot = ""
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"]).strip()
            if not text:
                continue
            if line["bbox"][1] < HEADER_Y:
                head = text
            elif line["bbox"][1] > FOOTER_Y:
                foot = text
    return head, foot


def keep(text, size):
    if not text:
        return False
    if size >= BODY_SIZE:
        # 図の中の「A」「1 2 3」のような大きめの記号だけの行を捨てる
        return len(text) > 2 or text.endswith("。")
    if size >= TABLE_SIZE:
        return len(text) >= 6
    return False


def main():
    if not PDF.exists():
        sys.exit(f"PDF がありません: {PDF}")
    doc = pymupdf.open(PDF)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    dropped = 0
    with OUT.open("w", encoding="utf-8") as out:
        for index in range(doc.page_count):
            page = doc[index]
            head, foot = page_marks(page)
            printed = int(foot) if re.fullmatch(r"\d+", foot) else None
            # 付録のページ番号は「- Appendix1_7 -」の形
            label = str(printed) if printed else re.sub(r"^[-\s]+|[-\s]+$", "", foot)
            blocks = page.get_text("dict", sort=True)["blocks"]
            # 見出しの「2.4.2」などの番号は小さな画像で入っている
            marks = [
                b["bbox"]
                for b in blocks
                if b["type"] == 1 and b["bbox"][2] - b["bbox"][0] < 60 and b["bbox"][3] - b["bbox"][1] < 20
            ]
            for block in blocks:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    spans = [span for span in line["spans"] if span["text"].strip()]
                    if not spans:
                        continue
                    x0, y0, x1, y1 = line["bbox"]
                    if y0 < HEADER_Y or y0 > FOOTER_Y:
                        continue
                    text = "".join(span["text"] for span in line["spans"]).strip()
                    size = max(span["size"] for span in spans)
                    middle = (y0 + y1) / 2
                    numimg = next(
                        (
                            round(m[0], 1)
                            for m in marks
                            if x0 - 45 <= m[2] <= x0 + 3 and abs((m[1] + m[3]) / 2 - middle) < 6
                        ),
                        None,
                    )
                    # 番号画像つきの行は「趣旨」「台帳」のように短くても見出しなので残す
                    if not (numimg is not None and size >= BODY_SIZE) and not keep(text, size):
                        dropped += 1
                        continue
                    font = spans[0]["font"]
                    row = {
                        "pdf": index + 1,
                        "page": printed,
                        "label": label,
                        "head": head,
                        "x": round(x0, 1),
                        "y": round(y0, 1),
                        "x1": round(x1, 1),
                        "size": round(size, 1),
                        "gothic": "Gothic" in font or "Bold" in font,
                        "numimg": numimg,
                        "text": text,
                    }
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    kept += 1
    print(f"pages {doc.page_count} lines {kept} dropped {dropped} -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
