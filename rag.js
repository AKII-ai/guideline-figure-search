// ガイドライン本文を使った図表検索（RAG）のブラウザ版。
// tools/rag.py と同じ処理。片方を変えたら、もう片方も合わせる（tools/check_parity.js で確かめる）。
//
// 1. 質問を言い換え辞書で広げる
// 2. 本文チャンクを 2文字区切りの BM25 で探す
// 3. 上位チャンクが触れている図表・同じ項の図表・図表名に当たる図表を候補にする
// 4. 中継（または local server）に「質問＋上位チャンクの抜粋＋候補の図表 id」だけを送り、Gemini に選ばせる
// 5. 記載箇所：章（1〜6）ごとの候補もあわせて送り、当てはまる章の項を Gemini に選ばせる（呼び出しは増やさない）

(function (root) {
  const TOP_CHUNKS = 12;
  const PROMPT_CHUNKS = 6;
  const EXCERPT = 220;
  const CANDIDATES = 20;
  // 記載箇所の候補：上位 SECTION_DEPTH チャンクから、章（1〜6）ごとに項の違うものを SECTION_PER_CHAPTER 件まで
  const SECTION_DEPTH = 60;
  const SECTION_PER_CHAPTER = 3;
  const SECTION_QUOTE = 120;
  const CHAPTERS = ["1", "2", "3", "4", "5", "6"];
  const K1 = 1.2;
  const B = 0.75;

  const PUNCT = /[\s、。，．,.・･「」『』（）()［］\[\]【】〈〉《》：:；;！!？?"'“”‘’…‥―ー－\-~〜～/／|｜＊*※]+/g;
  const DASH = /[-－‐―ー−]/g;

  function nfkc(text) {
    return String(text || "").normalize("NFKC");
  }

  function normNum(value) {
    return nfkc(value).replace(/\s+/g, "").replace(DASH, "-");
  }

  function baseNumber(value) {
    return normNum(value).replace(/[（(][^）)]*[）)]$/, "");
  }

  function pageId(url) {
    return String(url || "").replace(/\/+$/, "").split("/").pop();
  }

  function normalize(text) {
    return nfkc(text).toLowerCase().replace(PUNCT, "");
  }

  function grams(text) {
    const value = normalize(text);
    if (value.length < 2) return value ? [value] : [];
    const out = [];
    for (let i = 0; i < value.length - 1; i += 1) out.push(value.slice(i, i + 2));
    return out;
  }

  function weighted(parts) {
    const bag = new Map();
    for (const [text, weight] of parts) {
      for (const gram of grams(text)) bag.set(gram, (bag.get(gram) || 0) + weight);
    }
    return bag;
  }

  class BM25 {
    constructor(docs) {
      this.lengths = docs.map((doc) => {
        let total = 0;
        for (const value of doc.values()) total += value;
        return total;
      });
      this.avg = this.lengths.reduce((a, b) => a + b, 0) / Math.max(docs.length, 1);
      const df = new Map();
      for (const doc of docs) for (const term of doc.keys()) df.set(term, (df.get(term) || 0) + 1);
      const n = docs.length;
      this.idf = new Map();
      for (const [term, count] of df) this.idf.set(term, Math.log(1 + (n - count + 0.5) / (count + 0.5)));
      this.postings = new Map();
      docs.forEach((doc, index) => {
        for (const [term, tf] of doc) {
          if (!this.postings.has(term)) this.postings.set(term, []);
          this.postings.get(term).push([index, tf]);
        }
      });
    }

    scores(query) {
      const result = new Map();
      for (const [term, weight] of query) {
        const idf = this.idf.get(term);
        if (!idf) continue;
        for (const [index, tf] of this.postings.get(term)) {
          const norm = K1 * (1 - B + (B * this.lengths[index]) / this.avg);
          result.set(index, (result.get(index) || 0) + (weight * idf * tf * (K1 + 1)) / (tf + norm));
        }
      }
      return result;
    }
  }

  function sortedEntries(map) {
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }

  class Guideline {
    constructor(chunks, figures, synonyms) {
      this.chunks = chunks;
      this.figures = figures;
      this.synonyms = [];
      for (const [key, value] of Object.entries(synonyms || {})) {
        if (!key.startsWith("_")) this.synonyms.push([normalize(key), value]);
      }
      this.byId = new Map(figures.map((row) => [pageId(row.url), row]));
      this.byBase = new Map();
      this.byItem = new Map();
      for (const row of figures) {
        const base = baseNumber(row.number);
        if (!this.byBase.has(base)) this.byBase.set(base, []);
        this.byBase.get(base).push(row);
        const item = row.itemNo || row.sectionNo || "";
        if (!this.byItem.has(item)) this.byItem.set(item, []);
        this.byItem.get(item).push(row);
      }
      this.chunkIndex = new BM25(
        chunks.map((c) => weighted([[c.text, 1.0], [c.path.slice(-2).join(" "), 2.0]]))
      );
      this.figureIndex = new BM25(
        figures.map((row) =>
          weighted([
            [row.name || "", 2.0],
            [row.item || row.section || "", 1.0],
            [String(row.description || "").replace(/^【AI作成】/, "").slice(0, 200), 0.5],
          ])
        )
      );
    }

    expand(query) {
      const bag = weighted([[query, 1.0]]);
      const plain = normalize(query);
      const added = [];
      for (const [word, more] of this.synonyms) {
        if (word && plain.includes(word)) {
          for (const extra of more) if (!plain.includes(normalize(extra))) added.push(extra);
        }
      }
      for (const [gram, weight] of weighted([[added.join(" "), 0.5]])) {
        bag.set(gram, bag.has(gram) ? Math.max(bag.get(gram), weight) : weight);
      }
      return { bag, added };
    }

    searchChunks(query, k = TOP_CHUNKS) {
      const { bag } = this.expand(query);
      return sortedEntries(this.chunkIndex.scores(bag))
        .slice(0, k)
        .map(([index, score]) => [score, this.chunks[index]]);
    }

    allowed(kind = "", chapter = "") {
      const ids = new Set();
      for (const row of this.figures) {
        if (kind && row.kind !== kind) continue;
        const name = row.chapter || "";
        if (chapter === "__none__") {
          if (name) continue;
        } else if (chapter && name !== chapter) {
          continue;
        }
        ids.add(pageId(row.url));
      }
      return ids;
    }

    rankFigures(query, kind = "", chapter = "", hits = null) {
      hits = hits || this.searchChunks(query);
      const allowed = this.allowed(kind, chapter);
      const votes = new Map();
      const evidence = new Map();
      const vote = (id, weight, chunk) => {
        if (!votes.has(id)) votes.set(id, []);
        votes.get(id).push(weight);
        if (!evidence.has(id)) evidence.set(id, chunk);
      };
      const best = hits.length ? hits[0][0] : 1.0;
      for (const [value, chunk] of hits) {
        const weight = value / best;
        for (const number of chunk.figures) {
          for (const row of this.byBase.get(baseNumber(number)) || []) vote(pageId(row.url), weight, chunk);
        }
        for (const row of this.byItem.get(chunk.no) || []) vote(pageId(row.url), 0.3 * weight, chunk);
      }
      const score = new Map();
      for (const [id, weights] of votes) {
        weights.sort((a, b) => b - a);
        score.set(id, weights[0] + 0.25 * weights.slice(1).reduce((a, b) => a + b, 0));
      }
      const { bag } = this.expand(query);
      const direct = this.figureIndex.scores(bag);
      if (direct.size) {
        const top = Math.max(...direct.values());
        for (const [index, value] of direct) {
          const id = pageId(this.figures[index].url);
          score.set(id, (score.get(id) || 0) + (1.2 * value) / top);
        }
      }
      return sortedEntries(score)
        .filter(([id]) => allowed.has(id))
        .map(([id, value]) => [this.byId.get(id), value, evidence.get(id) || null]);
    }

    quote(chunk, query, limit = EXCERPT) {
      const { bag } = this.expand(query);
      const sentences = chunk.text
        .split("\n")
        .flatMap((line) => line.split(/(?<=。)/))
        .filter((s) => s && s.trim());
      if (!sentences.length) return "";
      let best = sentences[0];
      let bestScore = -1;
      for (const sentence of sentences) {
        let value = 0;
        for (const gram of new Set(grams(sentence))) value += bag.get(gram) || 0;
        if (value > bestScore) {
          best = sentence;
          bestScore = value;
        }
      }
      const start = chunk.text.indexOf(best);
      return chunk.text.slice(start).replace(/\n/g, " ").slice(0, limit);
    }

    passage(chunk, query, limit = 160) {
      return {
        id: chunk.id,
        no: chunk.no,
        path: chunk.path,
        page: chunk.page,
        pdf: chunk.pdf,
        quote: this.quote(chunk, query, limit),
      };
    }

    // 記載箇所の候補（章ごと）。どの章が当てはまるか・どの項かは AI が選ぶ。付録は入れない
    sectionCandidates(query, deep) {
      const byChapter = new Map(CHAPTERS.map((ch) => [ch, []]));
      for (const [, chunk] of deep) {
        const list = byChapter.get(String(chunk.no || "").split(".")[0]);
        if (!list || list.length >= SECTION_PER_CHAPTER || list.some((c) => c.no === chunk.no)) continue;
        list.push(chunk);
      }
      return CHAPTERS.flatMap((ch) =>
        byChapter.get(ch).map((chunk) => ({
          id: chunk.id,
          ch,
          no: chunk.no,
          title: chunk.path.slice(-2).join(" › "),
          page: chunk.page,
          quote: this.quote(chunk, query, SECTION_QUOTE),
        }))
      );
    }

    // 中継に送る内容（本文の抜粋と候補の id だけ）
    request(query, kind = "", chapter = "") {
      const deep = this.searchChunks(query, SECTION_DEPTH);
      const hits = deep.slice(0, TOP_CHUNKS);
      const ranked = this.rankFigures(query, kind, chapter, hits);
      const passages = hits.slice(0, PROMPT_CHUNKS).map(([, chunk]) => ({
        id: chunk.id,
        no: chunk.no,
        title: chunk.path[chunk.path.length - 1],
        page: chunk.page,
        quote: this.quote(chunk, query),
      }));
      const candidates = ranked.slice(0, CANDIDATES).map(([row, , chunk]) => ({
        id: pageId(row.url),
        chunk: chunk ? chunk.id : "",
      }));
      return { hits, ranked, passages, candidates, sections: this.sectionCandidates(query, deep) };
    }
  }

  // ---------- 軽量エージェント（tools/agent_lite.py と同じ決まり） ----------
  // 図表番号（表2.3.1-2、図5.10.7-2、図2.3-1 など）を含む質問は、AI を呼ばずに通常検索で返す
  const FIGURE_NUMBER = /[図表]\s*\d+(?:\s*[.．]\s*\d+)+\s*[-－‐―ー−]\s*\d+/;

  function hasFigureNumber(query) {
    return FIGURE_NUMBER.test(nfkc(query));
  }

  // ---------- 通常検索（tools/normal_search.py と同じ決まり） ----------
  // 文章かどうか：？。、！ があるか、空白で区切った1語が16字以上か、9字以上でひらがなを含むなら文章
  const SENTENCE_MARK = /[?？。、!！]/;
  const HIRAGANA = /[ぁ-ゟ]/g;

  function isSentence(query) {
    const text = nfkc(query).trim();
    if (!text) return false;
    if (SENTENCE_MARK.test(text)) return true;
    return text.split(/\s+/).some((token) => {
      const length = [...token].length;
      const hiragana = (token.match(HIRAGANA) || []).length;
      return length >= 16 || (length >= 9 && hiragana >= 1);
    });
  }

  // 探し直すときの検索語：言い換えた言葉＋元の質問
  function retryQuery(retry, query) {
    return `${retry} ${query}`;
  }

  const api = {
    Guideline,
    normalize,
    grams,
    baseNumber,
    normNum,
    pageId,
    hasFigureNumber,
    isSentence,
    retryQuery,
    PROMPT_CHUNKS,
    CANDIDATES,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.GuideRag = api;
})(typeof self !== "undefined" ? self : this);
