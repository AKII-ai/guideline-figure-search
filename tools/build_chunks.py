"""PDFから取り出した行（data/build/lines.jsonl）を、見出しつきのチャンクにまとめる。

- 章・節・項は、PDFの目次ページから組み立てる（番号は目次の並び順で数える）。
- それより下の見出し（(1)、①など）は、番号画像のついたゴシック体の行で見分ける。
- チャンクは項 → 小見出し → 段落の順に区切る。オーバーラップはしない。
  前後の文脈は path（章 › 節 › 項 › 小見出し）で補う。

出力: data/chunks.json
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINES = ROOT / "data" / "build" / "lines.jsonl"
FIGURES = ROOT / "data" / "figures.json"
OUT = ROOT / "data" / "chunks.json"

TOC_PAGES = range(3, 11)  # 目次（PDFの3〜10ページ目）
BODY_FIRST = 12  # 本文の始まり（PDFのページ番号、1始まり）
TARGET = 600  # チャンクの目安の長さ（文字）
LIMIT = 900  # これを超える段落は「。」で切る

CITE = re.compile(r"[図表]\s*\d+(?:\.\d+)*\s*[-－‐―ー−]\s*\d+")
REF = re.compile(r"(?:法|令|規則|施行令|施行規則)第\d+条(?:の\d+)?(?:第\d+項)?")
DOTS = re.compile(r"\s*[.．·・…‥]{3,}.*$")
LEADER = re.compile(r"\s*[.．·・…‥]{4,}[\s.．·・…‥]*")  # 目次の点線


def nfkc(text):
    return unicodedata.normalize("NFKC", text or "")


def norm_num(value):
    text = nfkc(value)
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[-－‐―ー−]", "-", text)


def key(text):
    """見出しの照合用。記号・空白を除いて比べる。"""
    text = nfkc(text)
    return re.sub(r"[\s・･（）()「」『』、，,。．.]", "", text)


def join_text(left, right):
    if left and right and left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


def load_lines():
    if not LINES.exists():
        sys.exit("先に python tools/extract_pdf.py を実行してください。")
    return [json.loads(line) for line in LINES.open(encoding="utf-8")]


def parse_toc(lines):
    """目次から (level, title, page) の並びを作る。level: 1=章, 2=節, 3=項。"""
    entries = []
    for row in lines:
        if row["pdf"] not in TOC_PAGES or row["text"].startswith("Appendix"):
            continue
        text = row["text"]
        if text in ("目次", "（参考資料）"):
            continue
        page_match = re.search(r"(\d+)\s*$", text)
        title = DOTS.sub("", text).strip()
        title = re.sub(r"\s*\d+\s*$", "", title) if page_match and not DOTS.search(text) else title
        if row["numimg"] is not None:
            if row["gothic"] and row["size"] >= 10.8:
                level = 1
            elif row["numimg"] < 62:
                level = 2
            else:
                level = 3
            entries.append({"level": level, "title": title, "page": None})
        elif entries and abs(row["x"] - 108) < 4 and row["size"] < 10.5:
            # 目次で折り返した見出しの続き（目次の後の略称表は拾わない）
            entries[-1]["title"] += title
        else:
            continue
        if page_match and (DOTS.search(text) or row["numimg"] is None):
            entries[-1]["page"] = int(page_match.group(1))
    numbers = [0, 0, 0]
    for entry in entries:
        level = entry["level"]
        numbers[level - 1] += 1
        for deeper in range(level, 3):
            numbers[deeper] = 0
        entry["no"] = ".".join(str(n) for n in numbers[:level])
    return entries


def is_heading(row):
    return row["gothic"] and row["numimg"] is not None and row["size"] >= 10.4 and not CITE.match(nfkc(row["text"]))


def is_caption(row):
    return row["gothic"] and bool(CITE.match(nfkc(row["text"])))


def match_toc(body, toc):
    """目次の見出しが、本文のどの行に当たるかを決める。"""
    found = {}
    start = 0
    missing = []
    for entry in toc:
        want = key(entry["title"])
        hit = None
        for index in range(start, len(body)):
            row = body[index]
            if not is_heading(row):
                continue
            if row["page"] and entry["page"] and row["page"] > entry["page"] + 2:
                break
            if row["page"] and entry["page"] and row["page"] < entry["page"] - 1:
                continue
            have = key(row["text"])
            if have and (want.startswith(have[:12]) or have.startswith(want[:12])):
                hit = index
                break
        if hit is None:
            missing.append(entry)
            continue
        found[hit] = entry
        start = hit + 1
    return found, missing


def paragraphs(rows):
    """行を段落にまとめる。字下げ・書体の切り替わり・図表の題で段落を分ける。"""
    out = []
    current = None
    prev = None
    for row in rows:
        text = row["text"]
        table = row["size"] < 10.4
        caption = is_caption(row)
        new = (
            current is None
            or caption
            or current["caption"]
            or table != current["table"]
            or (prev["text"].endswith(("。", "）", ")")) and row["x"] - prev["x"] >= 6)
            or (prev["pdf"] == row["pdf"] and row["y"] - prev["y"] > 40)
            or re.match(r"^[①-⑳・※＊\*]", text)
        )
        if new:
            if current:
                out.append(current)
            current = {"text": text, "pages": {row["label"]}, "pdf": row["pdf"], "table": table, "caption": caption}
        else:
            sep = " " if table else ""
            current["text"] = current["text"] + sep + text if sep else join_text(current["text"], text)
            current["pages"].add(row["label"])
        prev = row
    if current:
        out.append(current)
    return out


def page_order(label):
    numbers = [int(n) for n in re.findall(r"\d+", label)]
    return (label.startswith("Appendix"), numbers)


def split_long(text):
    if len(text) <= LIMIT:
        return [text]
    parts = []
    buf = ""
    for sentence in re.split(r"(?<=。)", text):
        if not sentence:
            continue
        if buf and len(buf) + len(sentence) > TARGET:
            parts.append(buf)
            buf = ""
        buf += sentence
    if buf:
        parts.append(buf)
    return parts


def build():
    lines = load_lines()
    toc = parse_toc(lines)
    body = [row for row in lines if row["pdf"] >= BODY_FIRST]
    found, missing = match_toc(body, toc)

    sections = []  # {path:[...], no, rows:[...]}
    toc_path = {}
    subs = []  # [(x, text)]
    current = None
    appendix = False
    index = 0
    while index < len(body):
        row = body[index]
        text = row["text"]
        if row["size"] >= 13.5 and re.match(r"Appendix\s*[-－‐―ー−]?\s*\d+", nfkc(text)):
            appendix = True
            title = text
            while index + 1 < len(body) and body[index + 1]["size"] >= 13.5 and body[index + 1]["pdf"] == row["pdf"]:
                index += 1
                title += body[index]["text"]
            name = nfkc(title).replace("Appendix -", "Appendix-").strip()
            no = re.match(r"Appendix\s*[-－‐―ー−]?\s*(\d+)", name).group(1)
            toc_path = {1: f"Appendix-{no}" + re.sub(r"^Appendix\s*[-－‐―ー−]?\s*\d+\.?", ". ", name).rstrip()}
            subs = []
            current = {"no": f"A{no}", "path": [toc_path[1]], "rows": []}
            sections.append(current)
            index += 1
            continue
        if not appendix and index in found:
            entry = found[index]
            toc_path = {k: v for k, v in toc_path.items() if k < entry["level"]}
            toc_path[entry["level"]] = entry["title"]
            subs = []
            current = {"no": entry["no"], "path": [toc_path[k] for k in sorted(toc_path)], "rows": []}
            sections.append(current)
            # 折り返した見出しの続きの行を飛ばす
            while (
                index + 1 < len(body)
                and body[index + 1]["gothic"]
                and body[index + 1]["numimg"] is None
                and key(entry["title"]).find(key(body[index + 1]["text"])[:6]) > 0
            ):
                index += 1
            index += 1
            continue
        if current is None:
            index += 1
            continue
        heading = is_heading(row) or (
            appendix and row["gothic"] and row["size"] >= 10.4 and len(text) <= 40 and not text.endswith("。") and not is_caption(row)
        )
        if heading:
            title = text
            while (
                index + 1 < len(body)
                and body[index + 1]["gothic"]
                and body[index + 1]["numimg"] is None
                and not is_caption(body[index + 1])
                and body[index + 1]["pdf"] == row["pdf"]
                and 0 < body[index + 1]["y"] - body[index]["y"] < 20
                and abs(body[index + 1]["x"] - row["x"]) < 30
                and not title.endswith(("。", "）"))
            ):
                index += 1
                title += body[index]["text"]
            subs = [(x, t) for x, t in subs if x < row["x"] - 1]
            subs.append((row["x"], title))
            current = {
                "no": current["no"],
                "path": current["path"][: len(current["path"]) - len(current.get("subs", []))] + [t for _, t in subs],
                "subs": [t for _, t in subs],
                "rows": [],
            }
            sections.append(current)
            index += 1
            continue
        current["rows"].append(row)
        index += 1

    chunks = []
    for section in sections:
        paras = paragraphs(section["rows"])
        pieces = []
        for para in paras:
            for part in split_long(para["text"]):
                pieces.append({**para, "text": part})
        buf = []
        size = 0

        def flush():
            nonlocal buf, size
            if not buf:
                return
            text = LEADER.sub(" … ", "\n".join(p["text"] for p in buf))
            pages = sorted({pg for p in buf for pg in p["pages"] if pg}, key=page_order)
            pdfs = [p["pdf"] for p in buf]
            plain = norm_num(text)
            chunks.append(
                {
                    "id": f"c{len(chunks) + 1:04d}",
                    "no": section["no"],
                    "path": section["path"],
                    "page": pages[0] if pages else None,
                    "pageEnd": pages[-1] if pages else None,
                    "pdf": pdfs[0],
                    "text": text,
                    "figures": sorted({norm_num(m) for m in CITE.findall(nfkc(text))}),
                    "refs": sorted(set(REF.findall(plain))),
                }
            )
            buf = []
            size = 0

        for piece in pieces:
            if buf and size + len(piece["text"]) > TARGET:
                flush()
            buf.append(piece)
            size += len(piece["text"])
        flush()
    return toc, found, missing, chunks


def main():
    toc, found, missing, chunks = build()
    OUT.write_text(json.dumps(chunks, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    figures = json.loads(FIGURES.read_text(encoding="utf-8"))
    known = {norm_num(row["number"]) for row in figures if row.get("number")}
    cited = {f for c in chunks for f in c["figures"]}
    lengths = sorted(len(c["text"]) for c in chunks)
    paged = sum(1 for c in chunks if c["page"])
    print(f"toc entries {len(toc)} matched {len(found)} missing {len(missing)}")
    for entry in missing[:15]:
        print(f"  missing {entry['no']} {entry['title'][:40]} p{entry['page']}")
    print(
        f"chunks {len(chunks)} chars {sum(lengths)} median {lengths[len(lengths) // 2]} max {lengths[-1]} "
        f"with page {paged / len(chunks):.1%}"
    )
    print(f"chunks citing figures {sum(1 for c in chunks if c['figures'])} figures cited {len(cited)} known {len(known & cited)}/{len(known)}")
    print(f"-> {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
