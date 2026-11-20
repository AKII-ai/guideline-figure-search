"""昨年のキーワード検索の再現（評価の方式 keyword／keyword_words）。

昨年の Notion キーワード検索に相当する比較用の方式。図表一覧の番号・図表名・場所・説明に
質問の言葉（空白で区切ったもの）がそのまま入っているかだけで並べる。本文も AI も使わない。
昨年方式の再現として固定（2026-11-20 凍結）。app.js の通常検索とは別物。中身を変えない。
新しい通常検索の Python 版は tools/normal_search.py。
"""

import re
import unicodedata


def normalize(value):
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).lower()
    return re.sub(r"\s+", " ", text).strip()


def compact(value):
    return normalize(value).replace(" ", "")


def tokenize(query):
    return [t for t in normalize(query).split(" ") if t]


def field_hits(text, tokens):
    folded = compact(text)
    if not folded:
        return 0
    return sum(1 for token in tokens if token in folded)


def haystack(row):
    keys = ("number", "name", "section", "item", "itemNo", "description", "context")
    return compact(" ".join(str(row[k]) for k in keys if row.get(k) not in (None, "")))


def score_row(row, tokens, compact_query):
    number = compact(row.get("number"))
    score = 0
    if compact_query and number and compact_query == number:
        score += 100
    if any(token == number for token in tokens):
        score += 100
    name_hits = field_hits(row.get("name"), tokens)
    place_hits = field_hits(f"{row.get('section') or ''} {row.get('item') or ''} {row.get('itemNo') or ''}", tokens)
    desc_hits = field_hits(row.get("description"), tokens)
    context_hits = field_hits(row.get("context"), tokens)
    score += name_hits * 8 + place_hits * 5 + desc_hits * 4 + context_hits * 2
    if len(tokens) > 1:
        if name_hits == len(tokens):
            score += 12
        if place_hits == len(tokens):
            score += 12
        if desc_hits == len(tokens):
            score += 6
        if context_hits == len(tokens):
            score += 3
        hay = haystack(row)
        if all(token in hay for token in tokens):
            score += 10
    return score


def keyword_search(figures, query, limit=30):
    tokens = tokenize(query)
    compact_query = compact(query)
    scored = [(row, score_row(row, tokens, compact_query)) for row in figures]
    scored = [(row, s) for row, s in scored if s > 0]
    # app.js と同じ：点数の高い順、同点なら章の番号順（sort は安定）
    scored.sort(key=lambda item: (-item[1], item[0].get("chapterNo") if item[0].get("chapterNo") is not None else 999))
    return [row for row, _ in scored[:limit]]
