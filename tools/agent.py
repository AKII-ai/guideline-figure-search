"""目次からたどるエージェント方式（評価専用。アプリには入れない）。

人が紙のガイドラインを引くときと同じ流れを Gemini にさせる。呼び出しは最大3回。
  1. 目次（章・節・項、約1万字）を見て、関係しそうな項を最大3つ選ぶ
  2. 選んだ項の本文から質問に近い段落を抜き出し、その項の図表と一緒に見せて図表を選ぶ
     本文に答えが無いと判断したら、別の項を挙げてもらう
  3. 挙がった項で 2 をもう一度だけ行う

本文の段落の選び方（BM25）と抜粋は tools/rag.py の Guideline をそのまま使う。
"""

from collections import defaultdict

from tools.rag import CANDIDATES, Guideline, base_number, call_gemini, page_id  # noqa: F401

MAX_ITEMS = 3  # 1回に読む項の数
PER_ITEM = 2  # 1つの項から見せる段落の数
EXCERPT = 300  # 段落の抜粋の長さ（本文を読ませるので RAG の 220 字より長め）
MAX_CALLS = 3
HINTS = 6  # 混合型で見せる「本文検索で当たった項」の数
HINT_QUOTE = 100  # その抜粋の長さ（文字）


def toc_text(guide):
    """章 › 節 › 項 の目次を字下げで書く。各チャンクの path から作る（PDF の目次と同じ並び）。"""
    seen = set()
    lines = []
    for chunk in guide.chunks:
        no = chunk["no"]
        if no in seen:
            continue
        seen.add(no)
        path = chunk["path"]
        if no.startswith("A"):
            lines.append(f"{no} {path[0]}")
            continue
        parts = no.split(".")
        # 章・節の見出しは初めて出たときだけ書く
        for depth in range(1, len(parts)):
            head = ".".join(parts[:depth])
            if head not in seen and depth - 1 < len(path):
                seen.add(head)
                lines.append(f"{'  ' * (depth - 1)}{head} {path[depth - 1]}")
        title = path[len(parts) - 1] if len(parts) - 1 < len(path) else path[-1]
        lines.append(f"{'  ' * (len(parts) - 1)}{no} {title}")
    return "\n".join(lines)


NAV_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {"type": "ARRAY", "items": {"type": "STRING"}},
        "reason": {"type": "STRING"},
    },
    "required": ["items"],
}

READ_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "found": {"type": "BOOLEAN"},
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
        },
        "more_items": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["found", "matches"],
}


class Agent:
    def __init__(self, guide: Guideline, hints=False):
        """hints=True で混合型: 目次と一緒に、本文の BM25 検索でよく当たった項を見せる。"""
        self.guide = guide
        self.hints = hints
        self.toc = toc_text(guide)
        self.known = {c["no"] for c in guide.chunks}
        self.chunks_of = defaultdict(list)
        for index, chunk in enumerate(guide.chunks):
            self.chunks_of[chunk["no"]].append(index)

    def pick_items(self, values, skip=()):
        """Gemini が返した項番号のうち、実在するものだけ。節（2.3）が返れば、その下の項も含む。"""
        result = []
        for value in values or []:
            no = str(value).strip().split(" ")[0]
            if no in skip or no in result:
                continue
            if no in self.known:
                result.append(no)
            elif any(k.startswith(no + ".") for k in self.known):
                result.append(no)
            if len(result) >= MAX_ITEMS:
                break
        return result

    def navigate(self, query, api_key):
        prompt = (
            "土壌汚染対策法ガイドラインの目次です。実務者の質問に答えるために読むべき項を選んでください。\n"
            f"項番号（例: 2.3.1、付録は A12）を、読む順に最大{MAX_ITEMS}つ返してください。目次にない番号は返さない。\n\n"
            + self.hint_text(query)
            + f"質問: {query}\n\n目次:\n{self.toc}"
        )
        parsed, usage = call_gemini(prompt, NAV_SCHEMA, api_key, max_tokens=256)
        return self.pick_items(parsed.get("items")), usage

    def hint_text(self, query):
        """本文の言葉で探して当たった項（上位 HINTS 個）。見出しと短い抜粋を付ける。"""
        if not self.hints:
            return ""
        lines, seen = [], set()
        for _, chunk in self.guide.search_chunks(query):
            if chunk["no"] in seen:
                continue
            seen.add(chunk["no"])
            quote = self.guide.quote(chunk, query, HINT_QUOTE)
            lines.append(f"- {chunk['no']} {' › '.join(chunk['path'][-2:])}：{quote}")
            if len(lines) >= HINTS:
                break
        if not lines:
            return ""
        return (
            "参考：本文を言葉で検索して当たった項です（当たった順）。言葉が似ているだけのこともあるので、"
            "目次の並びと見比べて判断してください。\n" + "\n".join(lines) + "\n\n"
        )

    def gather(self, query, items):
        """選んだ項の本文から段落と図表の候補を集める。"""
        g = self.guide
        bag, _ = g.expand(query)
        scores = g.chunk_index.scores(bag)
        passages, chunks = [], []
        for no in items:
            indexes = [i for k, ids in self.chunks_of.items() if k == no or k.startswith(no + ".") for i in ids]
            indexes.sort(key=lambda i: (-scores.get(i, 0.0), i))
            for index in indexes[:PER_ITEM]:
                chunk = g.chunks[index]
                chunks.append((scores.get(index, 0.0), chunk))
                passages.append(
                    {
                        "id": chunk["id"],
                        "no": chunk["no"],
                        "title": chunk["path"][-1],
                        "page": chunk.get("page"),
                        "quote": g.quote(chunk, query, EXCERPT),
                    }
                )
        # 候補 = その項の図表＋段落が触れている図表。並びは RAG と同じ点数の順
        wanted = {}
        for no in items:
            for key, rows in g.by_item.items():
                key = str(key)
                # その項の下の図表と、親の節に直接ついている図表（図2.3-1、表3.3-1 など）
                if key and (key == no or key.startswith(no + ".") or no.startswith(key + ".")):
                    for row in rows:
                        wanted.setdefault(page_id(row["url"]), "")
        for _, chunk in chunks:
            for number in chunk["figures"]:
                for row in g.by_base.get(base_number(number), []):
                    wanted.setdefault(page_id(row["url"]), chunk["id"])
        order = {page_id(row["url"]): rank for rank, (row, _, _) in enumerate(g.rank_figures(query))}
        ids = sorted(wanted, key=lambda i: order.get(i, 10**6))[:CANDIDATES]
        rows = []
        for item_id in ids:
            row = g.by_id[item_id]
            rows.append({"id": item_id, "number": row.get("number") or "", "name": (row.get("name") or "")[:70], "chunk": wanted[item_id]})
        return passages, rows

    def read(self, query, passages, rows, can_retry, api_key):
        lines = []
        for p in passages:
            page = f"p.{p['page']}" if p["page"] else ""
            lines.append(f"[{p['id']}] {p['no']} {p['title']} {page}\n{p['quote']}")
        figures = [f"{r['id']}\t{r['number']}\t{r['name']}\t{r['chunk']}" for r in rows]
        retry = (
            "抜粋に質問の答えが無ければ found=false にし、more_items に目次から別の項番号を最大3つ挙げてください。\n"
            f"目次:\n{self.toc}\n\n"
            if can_retry
            else "これが最後の機会です。found に関わらず、一番近い図表を選んでください。\n\n"
        )
        prompt = (
            "土壌汚染対策法ガイドラインの実務者の質問に、図表を選んで答えます。\n"
            "あなたが目次から選んだ項の本文の抜粋（[チャンクid] 項番号 見出し ページ）と、その項の図表の候補（id, 番号, 図表名, 関係するチャンクid）を渡します。\n"
            "抜粋の内容から、質問に一番役立つ図表を選んでください。id は候補の先頭列をそのまま返す。最大5件。\n"
            "chunk は根拠にしたチャンクid。reason は日本語で1文。\n"
            + retry
            + f"質問: {query}\n\n本文の抜粋:\n"
            + "\n\n".join(lines)
            + "\n\n図表の候補:\n"
            + "\n".join(figures)
        )
        return call_gemini(prompt, READ_SCHEMA, api_key, max_tokens=1024)

    def search(self, query, api_key):
        """戻り値は Guideline.search と同じ形。usage は全呼び出しの合計、steps に何をしたかを残す。"""
        total = {"prompt": 0, "output": 0, "calls": 0}

        def add(usage):
            total["calls"] += 1
            total["prompt"] += (usage or {}).get("prompt") or 0
            total["output"] += (usage or {}).get("output") or 0

        items, usage = self.navigate(query, api_key)
        add(usage)
        steps = [{"items": items}]
        read_items = list(items)
        best = None
        while items and total["calls"] < MAX_CALLS:
            passages, rows = self.gather(query, items)
            can_retry = total["calls"] + 1 < MAX_CALLS
            parsed, usage = self.read(query, passages, rows, can_retry, api_key)
            add(usage)
            allowed = {r["id"] for r in rows}
            matches = []
            for m in parsed.get("matches") or []:
                item_id = self.guide.resolve(m.get("id"), allowed)
                if item_id and all(x["id"] != item_id for x in matches):
                    matches.append({"id": item_id, "reason": str(m.get("reason") or ""), "chunk": str(m.get("chunk") or "")})
            found = bool(parsed.get("found")) and bool(matches)
            steps[-1].update({"found": found, "figures": len(rows)})
            if best is None or (found and not best["found"]):
                best = {"found": found, "matches": matches[:5], "passages": passages}
            if found or not can_retry:
                break
            items = self.pick_items(parsed.get("more_items"), skip=read_items)
            read_items += items
            steps.append({"items": items})
        best = best or {"matches": [], "passages": []}
        total["steps"] = steps
        return {"matches": best["matches"], "passages": best["passages"], "usage": total, "steps": steps}


if __name__ == "__main__":
    import sys

    from eval.run_eval import api_key

    agent = Agent(Guideline())
    question = " ".join(sys.argv[1:]) or "30m格子の調査で終わったときの評価"
    result = agent.search(question, api_key())
    print(result["steps"], result["usage"])
    for m in result["matches"]:
        print(agent.guide.by_id[m["id"]]["number"], m["reason"])
