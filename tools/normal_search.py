"""新しい通常検索（AI なし）の Python 版。app.js の通常検索と rag.js の isSentence と同じ処理。

  1. 図表番号を含む・空白で区切った短い単語 → 完全一致（図表一覧に言葉が入っているか）
  2. 文章のとき、または完全一致で0件のとき → 本文 BM25（言い換え辞書・本文 BM25・図表の候補集め。AI なし）

完全一致の並べ方は app.js の localHits／scoreRow と同じ。tools/keyword_search.py（昨年方式の再現。凍結）とは別に持つ。
app.js の通常検索を変えたら、ここも合わせ、python tools/check_parity.py で確かめる。
"""

import re
import unicodedata

SENTENCE_MARK = re.compile(r"[?？。、!！]")
HIRAGANA = re.compile(r"[ぁ-ゟ]")
FIGURE_NUMBER = re.compile(r"[図表]\s*\d+(?:\s*[.．]\s*\d+)+\s*[-－‐―ー−]\s*\d+", re.ASCII)


def is_sentence(query):
    """文章かどうか：？。、！ があるか、空白で区切った1語が16字以上か、9字以上でひらがなを含むなら文章。"""
    text = unicodedata.normalize("NFKC", query or "").strip()
    if not text:
        return False
    if SENTENCE_MARK.search(text):
        return True
    for token in text.split():
        hiragana = len(HIRAGANA.findall(token))
        if len(token) >= 16 or (len(token) >= 9 and hiragana >= 1):
            return True
    return False


def has_figure_number(query):
    return bool(FIGURE_NUMBER.search(unicodedata.normalize("NFKC", query or "")))


# ---------- 完全一致（app.js の localHits／scoreRow と同じ） ----------
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


def exact_hits(figures, query, limit=30):
    tokens = tokenize(query)
    compact_query = compact(query)
    scored = [(row, score_row(row, tokens, compact_query)) for row in figures]
    scored = [(row, s) for row, s in scored if s > 0]
    scored.sort(key=lambda item: (-item[1], item[0].get("chapterNo") if item[0].get("chapterNo") is not None else 999))
    return [row for row, _ in scored[:limit]]


def plan(query):
    """(使う検索, 理由)。理由は記録の「切り替えの理由」と同じ言葉。0件のときの切り替えは search で決める。"""
    if has_figure_number(query):
        return "exact", "図表番号"
    if is_sentence(query):
        return "bm25", "文章のため自動"
    return "exact", "単語"


def normal_search(guide, query, limit=15):
    """戻り値：(図表の行のリスト, 使った検索, 理由, 本文の抜粋の上位3チャンク)"""
    engine, reason = plan(query)
    if engine == "exact":
        rows = exact_hits(guide.figures, query)
        if rows:
            return rows, engine, reason, []
        engine, reason = "bm25", "0件のため自動"
    hits = guide.search_chunks(query)
    ranked = guide.rank_figures(query, hits=hits)
    return [row for row, _, _ in ranked[:limit]], engine, reason, [chunk for _, chunk in hits[:3]]
