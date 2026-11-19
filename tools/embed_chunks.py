"""本文チャンクの意味ベクトル（埋め込み）を作る。ハイブリッド検索の「意味の近さ」側。

  python tools/embed_chunks.py     # GEMINI_API_KEY（環境変数か .env）が必要。全体で数十円以下

出力:
  data/vectors.i8    … チャンクごとに 768 個の int8（約2MB。評価のハイブリッド比較だけで使う。公開しない）
  data/vectors.json  … モデル名・次元・チャンク id の並び
作り直しを安くするため、文章が同じチャンクは data/build/embed_cache.json から使い回す。
"""

import hashlib
import json
import math
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.run_eval import api_key  # noqa: E402

MODEL = "gemini-embedding-001"
DIMS = 768
BATCH = 100
CHUNKS = ROOT / "data" / "chunks.json"
OUT_VEC = ROOT / "data" / "vectors.i8"
OUT_META = ROOT / "data" / "vectors.json"
CACHE = ROOT / "data" / "build" / "embed_cache.json"


def doc_text(chunk):
    """見出しの道すじ＋本文。意味の近さで探すとき、どの項の話かも効くようにする。"""
    return " › ".join(chunk["path"]) + "\n" + chunk["text"][:1800]


def embed(texts, task, key):
    body = {
        "requests": [
            {
                "model": f"models/{MODEL}",
                "content": {"parts": [{"text": text}]},
                "taskType": task,
                "outputDimensionality": DIMS,
            }
            for text in texts
        ]
    }
    request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:batchEmbedContents",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    for attempt in range(5):
        try:
            with urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
            return [item["values"] for item in data["embeddings"]]
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").replace(key, "***")[:200]
            if error.code in (429, 500, 503) and attempt < 4:
                time.sleep(2 ** attempt * 3)
                continue
            raise SystemExit(f"埋め込みに失敗しました: {error.code} {detail}")
    return []


def quantize(values):
    """長さ1にそろえてから int8 にする。向き（＝意味）は保たれる。"""
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    unit = [v / norm for v in values]
    top = max(abs(v) for v in unit) or 1.0
    return bytes((round(v / top * 127)) & 0xFF for v in unit)


def main():
    key = api_key()
    if not key:
        sys.exit("GEMINI_API_KEY がありません。")
    chunks = json.loads(CHUNKS.read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    texts = [doc_text(c) for c in chunks]
    hashes = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
    todo = [i for i, h in enumerate(hashes) if h not in cache]
    print(f"chunks {len(chunks)} cached {len(chunks) - len(todo)} to embed {len(todo)}")
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        vectors = embed([texts[i] for i in batch], "RETRIEVAL_DOCUMENT", key)
        for index, vector in zip(batch, vectors):
            cache[hashes[index]] = quantize(vector).hex()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
        print(f"  {min(start + BATCH, len(todo))}/{len(todo)}")
    OUT_VEC.write_bytes(b"".join(bytes.fromhex(cache[h]) for h in hashes))
    OUT_META.write_text(
        json.dumps({"model": MODEL, "dims": DIMS, "count": len(chunks), "ids": [c["id"] for c in chunks]}),
        encoding="utf-8",
    )
    print(f"-> {OUT_VEC.relative_to(ROOT)} ({OUT_VEC.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
