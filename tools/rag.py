"""ガイドライン本文を使った図表検索（RAG）。ローカルサーバと評価で使う。

流れ:
  1. 質問を言い換え辞書（data/synonyms.json）で広げる
  2. 本文チャンク（data/chunks.json）を 2文字区切りの BM25 で探す
  3. 上位チャンクが触れている図表・同じ項の図表・図表名に当たる図表を候補にする
  4. Gemini には「質問＋上位チャンクの抜粋＋候補の図表」だけを渡して選ばせる（1回だけ）

proxy/worker.js に同じ処理を JavaScript で書いている。片方を変えたら、もう片方も合わせる。
"""

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
# モデルは固定する（-latest は知らないうちに入れ替わる）。変えたら proxy/worker.js と data/pricing.json も直す
MODEL = "gemini-3.8-flash"
THINKING_LEVEL = "low"  # gemini-3.8-flash の最小（minimal は 400 で断られる）

TOP_CHUNKS = 12  # 図表の候補を集めるチャンク数
PROMPT_CHUNKS = 6  # Gemini に見せるチャンク数
EXCERPT = 220  # Gemini に見せる抜粋の長さ（文字）
CANDIDATES = 20  # Gemini に見せる図表の候補数（15 だと正解が 16〜20 位の質問を落とす）
K1 = 1.2
B = 0.75
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
RRF_K = 60  # 順位を混ぜるときの定数（RRF の標準値）
FUSE_DEPTH = 200  # キーワード側・意味側それぞれ上位何件を混ぜるか

PUNCT = re.compile(r"[\s、。，．,.・･「」『』（）()［］\[\]【】〈〉《》：:；;！!？?\"'“”‘’…‥―ー－\-~〜～/／|｜＊*※]+")
DASH = re.compile(r"[-－‐―ー−]")


def nfkc(text):
    return unicodedata.normalize("NFKC", text or "")


def norm_num(value):
    text = re.sub(r"\s+", "", nfkc(value))
    return DASH.sub("-", text)


def base_number(value):
    """図2.5.1-1(1) → 図2.5.1-1。分割された図表をまとめて扱う。"""
    return re.sub(r"[（(][^）)]*[）)]$", "", norm_num(value))


def page_id(url):
    return url.rstrip("/").split("/")[-1]


def normalize(text):
    return PUNCT.sub("", nfkc(text).lower())


def grams(text):
    text = normalize(text)
    if len(text) < 2:
        return [text] if text else []
    return [text[i : i + 2] for i in range(len(text) - 1)]


class BM25:
    def __init__(self, docs):
        """docs: [(Counter of grams with weights)]"""
        self.docs = docs
        self.lengths = [sum(doc.values()) for doc in docs]
        self.avg = sum(self.lengths) / max(len(docs), 1)
        df = Counter()
        for doc in docs:
            df.update(doc.keys())
        n = len(docs)
        self.idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}
        self.postings = defaultdict(list)
        for index, doc in enumerate(docs):
            for term, tf in doc.items():
                self.postings[term].append((index, tf))

    def scores(self, query):
        """query: {gram: weight}"""
        result = defaultdict(float)
        for term, weight in query.items():
            idf = self.idf.get(term)
            if not idf:
                continue
            for index, tf in self.postings[term]:
                norm = K1 * (1 - B + B * self.lengths[index] / self.avg)
                result[index] += weight * idf * tf * (K1 + 1) / (tf + norm)
        return result


def weighted(parts):
    bag = Counter()
    for text, weight in parts:
        for gram in grams(text):
            bag[gram] += weight
    return bag


class VectorIndex:
    """data/vectors.i8（チャンクごとに int8 × dims）を読み、質問ベクトルとの近さで並べる。"""

    def __init__(self, raw, count, dims):
        self.count = count
        self.dims = dims
        values = [b - 256 if b > 127 else b for b in raw]
        self.rows = [values[i * dims : (i + 1) * dims] for i in range(count)]
        self.norms = [math.sqrt(sum(v * v for v in row)) or 1.0 for row in self.rows]

    @classmethod
    def load(cls, data, ids):
        meta_path = data / "vectors.json"
        vec_path = data / "vectors.i8"
        if not meta_path.exists() or not vec_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("ids") != ids:
            return None  # チャンクを作り直したのにベクトルが古い。tools/embed_chunks.py を実行する
        return cls(vec_path.read_bytes(), meta["count"], meta["dims"])

    def top(self, qvec, n):
        sims = [
            (sum(a * b for a, b in zip(row, qvec)) / norm, index)
            for index, (row, norm) in enumerate(zip(self.rows, self.norms))
        ]
        sims.sort(key=lambda item: (-item[0], item[1]))
        return [index for _, index in sims[:n]]


def embed_query(query, api_key):
    """質問の意味ベクトル（長さ1）。評価のハイブリッド比較だけで使う（公開版には入れない）。"""
    body = json.dumps(
        {
            "model": f"models/{EMBED_MODEL}",
            "content": {"parts": [{"text": query}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": EMBED_DIMS,
        }
    ).encode("utf-8")
    request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            values = json.loads(response.read())["embedding"]["values"]
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise GeminiError(message_from_google(detail, api_key), error.code) from error
    except URLError as error:
        raise GeminiError("Gemini に接続できませんでした。", 502) from error
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class Guideline:
    def __init__(self, root=ROOT):
        data = root / "data"
        self.chunks = json.loads((data / "chunks.json").read_text(encoding="utf-8"))
        self.figures = json.loads((data / "figures.json").read_text(encoding="utf-8"))
        synonyms = json.loads((data / "synonyms.json").read_text(encoding="utf-8"))
        self.synonyms = {normalize(k): v for k, v in synonyms.items() if not k.startswith("_")}
        self.by_id = {page_id(row["url"]): row for row in self.figures}
        self.by_base = defaultdict(list)
        self.by_item = defaultdict(list)
        for row in self.figures:
            self.by_base[base_number(row.get("number"))].append(row)
            self.by_item[row.get("itemNo") or row.get("sectionNo") or ""].append(row)
        # 見出し（path の後ろ2つ）は本文より重く数える
        self.chunk_index = BM25(
            [weighted([(c["text"], 1.0), (" ".join(c["path"][-2:]), 2.0)]) for c in self.chunks]
        )
        self.figure_index = BM25(
            [
                weighted(
                    [
                        (row.get("name") or "", 2.0),
                        (row.get("item") or row.get("section") or "", 1.0),
                        (re.sub(r"^【AI作成】", "", row.get("description") or "")[:200], 0.5),
                    ]
                )
                for row in self.figures
            ]
        )
        self.vectors = VectorIndex.load(data, [c["id"] for c in self.chunks])

    # ---------- 1. 質問を広げる ----------
    def expand(self, query):
        bag = weighted([(query, 1.0)])
        plain = normalize(query)
        added = []
        for word, more in self.synonyms.items():
            if word and word in plain:
                for extra in more:
                    if normalize(extra) not in plain:
                        added.append(extra)
        for gram, weight in weighted([(" ".join(added), 0.5)]).items():
            bag[gram] = max(bag[gram], weight) if gram in bag else weight
        return bag, added

    # ---------- 2. 本文を探す ----------
    def search_chunks(self, query, k=TOP_CHUNKS, qvec=None):
        """qvec（質問の意味ベクトル）があれば、キーワードと意味の近さの順位を RRF で混ぜる。"""
        bag, _ = self.expand(query)
        scores = self.chunk_index.scores(bag)
        if qvec is None or self.vectors is None:
            top = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
            return [(score, self.chunks[index]) for index, score in top]
        fused = defaultdict(float)
        keyword = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:FUSE_DEPTH]
        for rank, (index, _) in enumerate(keyword):
            fused[index] += 1 / (RRF_K + rank + 1)
        for rank, index in enumerate(self.vectors.top(qvec, FUSE_DEPTH)):
            fused[index] += 1 / (RRF_K + rank + 1)
        top = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:k]
        return [(score, self.chunks[index]) for index, score in top]

    # ---------- 3. 図表の候補 ----------
    def allowed(self, kind="", chapter=""):
        ids = set()
        for row in self.figures:
            if kind and row.get("kind") != kind:
                continue
            name = row.get("chapter") or ""
            if chapter == "__none__":
                if name:
                    continue
            elif chapter and name != chapter:
                continue
            ids.add(page_id(row["url"]))
        return ids

    def rank_figures(self, query, kind="", chapter="", hits=None):
        hits = hits if hits is not None else self.search_chunks(query)
        allowed = self.allowed(kind, chapter)
        votes = defaultdict(list)
        evidence = {}
        best = hits[0][0] if hits else 1.0
        for value, chunk in hits:
            weight = value / best
            for number in chunk["figures"]:
                for row in self.by_base.get(base_number(number), []):
                    item_id = page_id(row["url"])
                    votes[item_id].append(weight)
                    evidence.setdefault(item_id, chunk)
            for row in self.by_item.get(chunk["no"], []):
                item_id = page_id(row["url"])
                votes[item_id].append(0.3 * weight)
                evidence.setdefault(item_id, chunk)
        # 一番強いつながり＋残りは 1/4。多くのチャンクに出てくるだけの図表が上に来すぎないようにする
        score = defaultdict(float)
        for item_id, weights in votes.items():
            weights.sort(reverse=True)
            score[item_id] = weights[0] + 0.25 * sum(weights[1:])
        bag, _ = self.expand(query)
        direct = self.figure_index.scores(bag)
        if direct:
            top = max(direct.values())
            for index, value in direct.items():
                item_id = page_id(self.figures[index]["url"])
                score[item_id] += 1.2 * value / top
        ranked = [(item_id, value) for item_id, value in score.items() if item_id in allowed]
        ranked.sort(key=lambda item: -item[1])
        return [(self.by_id[item_id], value, evidence.get(item_id)) for item_id, value in ranked]

    # ---------- 4. Gemini に渡す内容 ----------
    def quote(self, chunk, query, limit=EXCERPT):
        """チャンクの中で、質問の言葉が多く入っている文のあたりを抜き出す。"""
        bag, _ = self.expand(query)
        sentences = [s for line in chunk["text"].split("\n") for s in re.split(r"(?<=。)", line) if s.strip()]
        if not sentences:
            return ""
        best = max(sentences, key=lambda s: sum(bag.get(g, 0) for g in set(grams(s))))
        start = chunk["text"].find(best)
        text = chunk["text"][start:].replace("\n", " ")
        return text[:limit]

    def request(self, query, kind="", chapter="", qvec=None):
        """中継に送る内容（本文の抜粋と候補の id だけ）。rag.js の request と同じ。"""
        hits = self.search_chunks(query, qvec=qvec)
        ranked = self.rank_figures(query, kind, chapter, hits)
        passages = [
            {"id": c["id"], "no": c["no"], "title": c["path"][-1], "page": c.get("page"), "quote": self.quote(c, query)}
            for _, c in hits[:PROMPT_CHUNKS]
        ]
        candidates = [
            {"id": page_id(row["url"]), "chunk": chunk["id"] if chunk else ""} for row, _, chunk in ranked[:CANDIDATES]
        ]
        return {"hits": hits, "ranked": ranked, "passages": passages, "candidates": candidates}

    def answer(self, query, passages, candidates, api_key):
        """受け取った抜粋と候補から Gemini に1回だけ選ばせる。server.py と proxy/worker.js と同じ。"""
        passages, rows = clean_request(passages, candidates, self.by_id)
        if not rows:
            return {"matches": [], "usage": None}
        parsed, usage = call_gemini(build_prompt(query, passages, rows), MATCH_SCHEMA, api_key)
        allowed = {row["id"] for row in rows}
        chunk_of = {row["id"]: row["chunk"] for row in rows}
        known_chunks = {p["id"] for p in passages} | set(chunk_of.values())
        matches = []
        for item in parsed.get("matches") or []:
            item_id = self.resolve(item.get("id"), allowed)
            if not item_id or any(m["id"] == item_id for m in matches):
                continue
            chunk = str(item.get("chunk") or "")
            matches.append(
                {
                    "id": item_id,
                    "reason": str(item.get("reason") or "").strip(),
                    "chunk": chunk if chunk in known_chunks else chunk_of.get(item_id, ""),
                }
            )
            if len(matches) >= 5:
                break
        return {"matches": matches, "usage": usage}

    def search(self, query, kind="", chapter="", api_key="", qvec=None, use_llm=True):
        """RAG 検索。api_key が無いか use_llm=False なら、Gemini を使わず検索の順位だけで並べる。
        qvec があれば本文の検索はハイブリッド（キーワード＋意味の近さ）。"""
        req = self.request(query, kind, chapter, qvec)
        if not api_key or not use_llm or not req["candidates"]:
            matches = [{"id": c["id"], "reason": "", "chunk": c["chunk"]} for c in req["candidates"][:8]]
            return {"matches": matches, "passages": req["passages"], "usage": None}
        result = self.answer(query, req["passages"], req["candidates"], api_key)
        result["passages"] = req["passages"]
        return result

    def passage(self, chunk, query):
        return {
            "id": chunk["id"],
            "no": chunk["no"],
            "path": chunk["path"],
            "page": chunk.get("page"),
            "pdf": chunk.get("pdf"),
            "quote": self.quote(chunk, query, 160),
        }

    def resolve(self, item_id, allowed):
        item_id = str(item_id or "").strip()
        if item_id in allowed:
            return item_id
        for row in self.by_base.get(base_number(item_id), []):
            if page_id(row["url"]) in allowed:
                return page_id(row["url"])
        return None


CHUNK_ID = re.compile(r"^c\d{4}$")


def clean_request(passages, candidates, by_id):
    """ブラウザから来た抜粋と候補を確かめる。数と長さを制限し、図表名は手元の一覧から引き直す。"""
    clean = []
    for item in (passages or [])[:PROMPT_CHUNKS]:
        if not isinstance(item, dict) or not CHUNK_ID.match(str(item.get("id") or "")):
            continue
        clean.append(
            {
                "id": str(item["id"]),
                "no": str(item.get("no") or "")[:20],
                "title": str(item.get("title") or "")[:80],
                "page": str(item.get("page") or "")[:24],
                "quote": str(item.get("quote") or "")[:300],
            }
        )
    rows = []
    for item in (candidates or [])[:CANDIDATES]:
        if not isinstance(item, dict):
            continue
        row = by_id.get(str(item.get("id") or ""))
        if not row or any(r["id"] == item["id"] for r in rows):
            continue
        chunk = str(item.get("chunk") or "")
        rows.append(
            {
                "id": item["id"],
                "number": row.get("number") or "",
                "name": (row.get("name") or "")[:70],
                "chunk": chunk if CHUNK_ID.match(chunk) else "",
            }
        )
    return clean, rows


def build_prompt(query, passages, rows):
    lines = []
    for p in passages:
        page = f"p.{p['page']}" if p["page"] else ""
        lines.append(f"[{p['id']}] {p['no']} {p['title']} {page}\n{p['quote']}")
    figures = [f"{r['id']}\t{r['number']}\t{r['name']}\t{r['chunk']}" for r in rows]
    return (
        "土壌汚染対策法ガイドラインの実務者の質問に、図表を選んで答えます。\n"
        "本文の抜粋（[チャンクid] 項番号 見出し ページ）と、図表の候補（id, 番号, 図表名, 関係するチャンクid）を渡します。\n"
        "抜粋の内容から、質問に一番役立つ図表を選んでください。言葉が似ているだけの図表は下にしてください。\n"
        "id は候補の先頭列をそのまま返す。候補にない id は返さない。最大5件。\n"
        "chunk は根拠にしたチャンクid。reason は日本語で1文（どの項の、どんな図表か）。\n\n"
        f"質問: {query}\n\n"
        "本文の抜粋:\n" + "\n\n".join(lines) + "\n\n"
        "図表の候補:\n" + "\n".join(figures)
    )


MATCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "matches": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "chunk": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["id", "reason"],
            },
        }
    },
    "required": ["matches"],
}


class GeminiError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def redact(text, api_key):
    if api_key and api_key in text:
        return text.replace(api_key, "***")
    return text


def message_from_google(detail, api_key):
    detail = redact(detail, api_key)
    try:
        message = json.loads(detail)["error"]["message"]
    except (json.JSONDecodeError, KeyError, TypeError):
        message = detail.strip()[:300]
    message = redact(message, api_key)
    lowered = message.lower()
    if "api key" in lowered or "api_key" in lowered:
        return "Gemini APIキーが正しくありません。"
    if "quota" in lowered or "rate" in lowered:
        return "Gemini の利用上限に達しました。少し待ってからもう一度検索してください。"
    return message or "Gemini の検索に失敗しました。"


def gemini_payload(prompt, schema, max_tokens):
    """Gemini に送る中身。worker.js の geminiPayload と同じ。temperature は 3.6 以降無視されるので送らない。"""
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "store": False,  # Gemini API のログに質問を残さない
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingLevel": THINKING_LEVEL},
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }


def call_gemini(prompt, schema, api_key, max_tokens=1024):
    """Gemini を1回呼び、(JSON, 使ったtoken数) を返す。"""
    request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(gemini_payload(prompt, schema, max_tokens)).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code == 404:
            raise GeminiError(f"Gemini のモデル {MODEL} を使えませんでした。", 502) from error
        raise GeminiError(message_from_google(detail, api_key), error.code) from error
    except OSError as error:  # URLError・時間切れ・切断
        raise GeminiError("Gemini に接続できませんでした。", 502) from error
    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    if not text:
        reason = candidate.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "empty"
        raise GeminiError(f"Gemini の応答を読み取れませんでした。（{reason}）", 502)
    start, end = text.find("{"), text.rfind("}")
    try:
        parsed = json.loads(text[start : end + 1] if start >= 0 < end else text)
    except json.JSONDecodeError as error:
        raise GeminiError("Gemini の応答を読み取れませんでした。（format）", 502) from error
    meta = data.get("usageMetadata") or {}
    usage = {
        "prompt": meta.get("promptTokenCount"),
        "output": meta.get("candidatesTokenCount"),
        "thoughts": meta.get("thoughtsTokenCount", 0),
        "model": data.get("modelVersion") or MODEL,
    }
    return parsed, usage


if __name__ == "__main__":
    import sys

    guide = Guideline()
    question = " ".join(sys.argv[1:]) or "地歴調査で集める資料"
    for score, chunk in guide.search_chunks(question)[:5]:
        print(f"{score:6.2f} {chunk['id']} {chunk['no']} p{chunk['page']} {' › '.join(chunk['path'][-2:])}")
    print("--")
    for row, score, chunk in guide.rank_figures(question)[:8]:
        print(f"{score:5.2f} {row['number']} {row['name'][:50]}")
