// Cloudflare Workers に置く中継。
// Gemini のキーは秘密 GEMINI_API_KEY、Notion のトークンは秘密 NOTION_TOKEN にだけ入れる。
// 検索の記録用は別の秘密：NOTION_LOG_TOKEN（追加と更新だけ。読む権限なし）と NOTION_LOG_DB（保存先の ID）。
// このファイルと GitHub にはキーを書かない。質問文・送信元を console に書かない（Cloudflare のログに残さない）。
// 設定（回数制限の置き場所・ログなし・プレビュー URL なし）はリポジトリ直下の wrangler.jsonc。

const FIGURES_URL = "https://akii-ai.github.io/guideline-figure-search/data/figures.json";
// モデルは固定する（-latest は知らないうちに入れ替わる）。変えたら tools/rag.py と data/pricing.json も直す
const MODEL = "gemini-3.8-flash";
const THINKING_LEVEL = "low"; // gemini-3.8-flash の最小（minimal は 400 で断られる）
const ALLOWED = new Set([
  "https://akii-ai.github.io",
  "http://127.0.0.1:8765",
  "http://localhost:8765",
]);

// 回数制限。1回 = 検索1回（同じ検索の探し直しと記録は、合わせて1回に数える）
const PER_MINUTE = 30; // 同じ送信元から
const PER_DAY = 500; // 全体で。日本時間の0時に戻る
const EDITS_PER_RECORD = 30; // 1行の記録を直せる回数（評価・開いた図表）

// Notion の画像の URL は1時間有効。使い回して Notion API の回数（1分180回）を節約する
const IMAGE_URL_MS = 45 * 60 * 1000;
const imageUrls = new Map();

let figuresCache = null;
let figuresAt = 0;
const imageCache = new Map();

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }), origin);
    }
    if (!ALLOWED.has(origin)) {
      return withCors(json({ error: "このサイトからは検索できません。" }, 403), origin);
    }
    if (request.method === "GET" && url.pathname === "/figure") {
      try {
        return withCors(await figureImage(url, env), origin);
      } catch (error) {
        const status = error.status || 502;
        return withCors(json({ error: error.message || "Notionから画像を読めませんでした。" }, status), origin);
      }
    }
    if (request.method !== "POST") {
      return withCors(json({ error: "検索は POST で送ってください。" }, 405), origin);
    }
    if (url.pathname === "/log") {
      return withCors(await saveLog(request, env, origin), origin);
    }
    const apiKey = String(env.GEMINI_API_KEY || "").trim();
    if (!apiKey) {
      return withCors(json({ error: "中継に Gemini のキーがまだ入っていません。" }, 500), origin);
    }
    let payload;
    try {
      payload = await request.json();
    } catch {
      return withCors(json({ error: "検索内容を読み取れませんでした。" }, 400), origin);
    }
    const query = String(payload.query || "").trim();
    if (!query) {
      return withCors(json({ error: "検索する内容を入力してください。" }, 400), origin);
    }
    if (query.length > 200) {
      return withCors(json({ error: "検索文が長すぎます。" }, 400), origin);
    }
    try {
      const figures = await loadFigures(env.FIGURES_URL || FIGURES_URL);
      if (Array.isArray(payload.passages)) {
        // RAG：本文の検索はブラウザ（rag.js）が済ませている。ここでは Gemini に1回だけ選ばせる
        const { passages, rows } = cleanRequest(payload.passages, payload.candidates, figures);
        if (payload.attempt != null) {
          // 軽量エージェント：図表選びと次の一手（ok/clarify/retry/standards）を1回で答えさせる
          if (!rows.length) {
            return withCors(json({ matches: [], next: { status: "ok" } }), origin);
          }
          const attempt = Number(payload.attempt) === 2 ? 2 : 1;
          const allowClarify = attempt === 1 && payload.allowClarify !== false;
          const previous = cleanPrevious(payload.previous, figures);
          const prompt = buildAgentPrompt(query, passages, rows, attempt, allowClarify, previous.map((p) => p.number));
          const schema = attempt === 1 ? AGENT_SCHEMA : MATCH_SCHEMA;
          const stopped = await checkLimit(request, env, attempt === 1 ? "ai1" : "ai2", payload.sid);
          if (stopped) return withCors(stopped, origin);
          const { text, usage } = await callGemini(prompt, schema, apiKey, 1024);
          const parsed = parseJson(text);
          const matches = pickRagMatches(parsed, rows, passages, previous);
          return withCors(json({ matches, next: cleanNext(parsed, attempt, allowClarify), usage }), origin);
        }
        if (!rows.length) {
          return withCors(json({ matches: [] }), origin);
        }
        const stopped = await checkLimit(request, env, "ai1", payload.sid);
        if (stopped) return withCors(stopped, origin);
        const { text, usage } = await callGemini(buildPrompt(query, passages, rows), MATCH_SCHEMA, apiKey, 1024);
        return withCors(json({ matches: pickRagMatches(parseJson(text), rows, passages), usage }), origin);
      }
      // 以前の方式（図表の目次全体を渡す）。古い画面と比較のために残す
      const { listed, allowed } = figureMap(figures, payload.kind, payload.chapter);
      if (!listed) {
        return withCors(json({ matches: [] }), origin);
      }
      const stopped = await checkLimit(request, env, "ai1", payload.sid);
      if (stopped) return withCors(stopped, origin);
      const text = await askGemini(query, listed, apiKey);
      const matches = pickMatches(text, figures, allowed);
      return withCors(json({ matches }), origin);
    } catch (error) {
      const status = error.status || 502;
      return withCors(json({ error: error.message || "Gemini の検索に失敗しました。" }, status), origin);
    }
  },
};

async function figureImage(url, env) {
  const token = cleanToken(env.NOTION_TOKEN || "");
  if (!token) {
    return json({ error: "中継に Notion のトークンがまだ入っていません。" }, 500);
  }
  const figureId = String(url.searchParams.get("id") || "").replace(/-/g, "").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(figureId)) {
    return json({ error: "画像の取得先が分かりません。" }, 404);
  }
  const figures = await loadFigures(env.FIGURES_URL || FIGURES_URL);
  const known = new Set(figures.map((row) => pageId(row.url).replace(/-/g, "").toLowerCase()));
  if (!known.has(figureId)) {
    return json({ error: "この図表の画像は公開していません。" }, 404);
  }
  const cached = imageCache.get(figureId);
  if (cached && cached.until > Date.now()) {
    return imageResponse(cached.type, cached.bytes);
  }
  let saved = imageUrls.get(figureId);
  if (!saved || saved.until <= Date.now()) {
    saved = { source: await notionImageUrl(figureId, token), until: Date.now() + IMAGE_URL_MS };
    imageUrls.set(figureId, saved);
  }
  const source = saved.source;
  if (!source) {
    imageUrls.delete(figureId);
    return json({ error: "このページに画像がありません。" }, 404);
  }
  if (!allowedImageUrl(source)) {
    return json({ error: "この画像の置き場所は読めません。" }, 404);
  }
  const response = await fetch(source, { redirect: "manual", headers: { "User-Agent": "figure-search" } });
  if (!response.ok) {
    imageUrls.delete(figureId);
    return json({ error: "Notionから画像を読めませんでした。" }, 502);
  }
  const type = (response.headers.get("Content-Type") || "image/jpeg").split(";")[0];
  if (!type.startsWith("image/")) {
    return json({ error: "Notionから画像を読めませんでした。" }, 502);
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > 12_000_000) {
    return json({ error: "画像が大きすぎます。" }, 502);
  }
  imageCache.set(figureId, { until: Date.now() + 240_000, type, bytes });
  if (imageCache.size > 24) {
    const oldest = imageCache.keys().next().value;
    imageCache.delete(oldest);
  }
  return imageResponse(type, bytes);
}

async function notionImageUrl(pageId, token) {
  const response = await fetch(
    `https://api.notion.com/v1/blocks/${hyphenate(pageId)}/children?page_size=20`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        "Notion-Version": "2022-06-28",
      },
    }
  );
  if (!response.ok) {
    const message = {
      401: "Notionトークンが違います。内部インテグレーションのトークンを入れ直してください。",
      403: "「コンテンツを読み取る」をオンにして、エンジニアリングシートにこの接続を追加してください。",
      404: "エンジニアリングシートを開き、右上の…から「接続」で、この内部接続を追加してください。",
    }[response.status] || "Notionから画像を読めませんでした。";
    throw Object.assign(new Error(message), { status: response.status >= 500 ? 502 : response.status });
  }
  const payload = await response.json();
  for (const block of payload.results || []) {
    if (block.type !== "image") continue;
    const image = block.image || {};
    const found = image[image.type]?.url;
    if (found) return found;
  }
  return "";
}

function imageResponse(type, bytes) {
  return new Response(bytes, {
    headers: {
      "Content-Type": type,
      "Cache-Control": "private, max-age=120",
    },
  });
}

function allowedImageUrl(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:") return false;
  const host = parsed.hostname;
  return host.endsWith(".amazonaws.com") || host.endsWith(".notion-static.com") || host.endsWith(".notion.so");
}

function hyphenate(pageId) {
  return `${pageId.slice(0, 8)}-${pageId.slice(8, 12)}-${pageId.slice(12, 16)}-${pageId.slice(16, 20)}-${pageId.slice(20)}`;
}

function cleanToken(token) {
  let value = String(token || "").trim().replace(/^['"]|['"]$/g, "");
  if (value.toLowerCase().startsWith("bearer ")) value = value.slice(7).trim();
  return value;
}

async function loadFigures(url) {
  if (figuresCache && Date.now() - figuresAt < 10 * 60 * 1000) return figuresCache;
  const response = await fetch(url);
  if (!response.ok) {
    throw Object.assign(new Error("図表一覧を読めませんでした。"), { status: 502 });
  }
  figuresCache = await response.json();
  figuresAt = Date.now();
  return figuresCache;
}

function figureMap(figures, kind, chapter) {
  const lines = [];
  const allowed = new Set();
  for (const row of figures) {
    if (kind && row.kind !== kind) continue;
    const chapterName = row.chapter || "";
    if (chapter === "__none__") {
      if (chapterName) continue;
    } else if (chapter && chapterName !== chapter) {
      continue;
    }
    const id = pageId(row.url);
    allowed.add(id);
    const description = String(row.description || "").trim().split(/\s+/).filter(Boolean).join(" ").slice(0, 120);
    lines.push([id, row.number || "", chapterName, row.section || "", row.item || "", row.name || "", description].join("\t"));
  }
  return { listed: lines.join("\n"), allowed };
}

async function askGemini(query, listed, apiKey) {
  const prompt = [
    "これは土壌汚染対策法ガイドラインの図表の目次です。",
    "列は id, 図表番号, 章, 節, 項, 図表名, 説明。タブ区切り。",
    "質問は図表名と同じ言葉になっていないことがあります。",
    "言い換え（深度と深さ、ボーリングと試料採取など）と、章・節・項のまとまりから、質問が指す図を選んでください。",
    "言葉が含まれる図を機械的に並べないでください。その調査の流れの中で、質問の内容を説明する図を上にしてください。",
    "id は一覧の先頭列をそのまま返す。一覧にない id は返さない。最大8件。",
    "reason は日本語で1文。どの節の、どんな図か。",
    "",
    `質問: ${query}`,
    "",
    "目次:",
    listed,
  ].join("\n");
  const generationConfig = {
    maxOutputTokens: 4096,
    responseMimeType: "application/json",
    responseSchema: {
      type: "OBJECT",
      properties: {
        matches: {
          type: "ARRAY",
          items: {
            type: "OBJECT",
            properties: {
              id: { type: "STRING" },
              reason: { type: "STRING" },
            },
            required: ["id", "reason"],
          },
        },
      },
      required: ["matches"],
    },
  };
  const { text } = await callGemini(prompt, generationConfig.responseSchema, apiKey, generationConfig.maxOutputTokens);
  return text;
}

// Gemini に送る中身。tools/rag.py の gemini_payload と同じ。temperature は 3.6 以降無視されるので送らない
function geminiPayload(prompt, schema, maxTokens) {
  return {
    contents: [{ role: "user", parts: [{ text: prompt }] }],
    store: false, // Gemini API のログに質問を残さない
    generationConfig: {
      maxOutputTokens: maxTokens,
      thinkingConfig: { thinkingLevel: THINKING_LEVEL },
      responseMimeType: "application/json",
      responseSchema: schema,
    },
  };
}

// Gemini を1回呼ぶ。{ text, usage } を返す
async function callGemini(prompt, schema, apiKey, maxTokens) {
  const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey,
    },
    body: JSON.stringify(geminiPayload(prompt, schema, maxTokens)),
  });
  if (!response.ok) {
    const detail = await response.text();
    const message = response.status === 404 ? `Gemini のモデル ${MODEL} を使えませんでした。` : messageFromGoogle(detail, apiKey);
    throw Object.assign(new Error(message), { status: clientStatus(response.status) });
  }
  const data = await response.json();
  const text = visibleText(data);
  if (!text) {
    const reason = data.candidates?.[0]?.finishReason || data.promptFeedback?.blockReason || "empty";
    throw Object.assign(new Error(`Gemini の応答を読み取れませんでした。（${reason}）`), { status: 502 });
  }
  const meta = data.usageMetadata || {};
  const usage = {
    prompt: meta.promptTokenCount,
    output: meta.candidatesTokenCount,
    thoughts: meta.thoughtsTokenCount ?? 0,
    model: data.modelVersion || MODEL,
  };
  return { text, usage };
}

// ---------- RAG（tools/rag.py の clean_request / build_prompt と同じ） ----------
const PROMPT_CHUNKS = 6;
const CANDIDATES = 20;
const CHUNK_ID = /^c\d{4}$/;

const MATCH_SCHEMA = {
  type: "OBJECT",
  properties: {
    matches: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: { id: { type: "STRING" }, chunk: { type: "STRING" }, reason: { type: "STRING" } },
        required: ["id", "reason"],
      },
    },
  },
  required: ["matches"],
};

// ブラウザから来た抜粋と候補を確かめる。数と長さを制限し、図表名は手元の一覧から引き直す
function cleanRequest(rawPassages, rawCandidates, figures) {
  const byId = new Map(figures.map((row) => [pageId(row.url), row]));
  const passages = [];
  for (const item of (rawPassages || []).slice(0, PROMPT_CHUNKS)) {
    if (!item || !CHUNK_ID.test(String(item.id || ""))) continue;
    passages.push({
      id: String(item.id),
      no: String(item.no || "").slice(0, 20),
      title: String(item.title || "").slice(0, 80),
      page: String(item.page || "").slice(0, 24),
      quote: String(item.quote || "").slice(0, 300),
    });
  }
  const rows = [];
  for (const item of (Array.isArray(rawCandidates) ? rawCandidates : []).slice(0, CANDIDATES)) {
    const id = String((item && item.id) || "");
    const row = byId.get(id);
    if (!row || rows.some((r) => r.id === id)) continue;
    const chunk = String(item.chunk || "");
    rows.push({
      id,
      number: row.number || "",
      name: String(row.name || "").slice(0, 70),
      chunk: CHUNK_ID.test(chunk) ? chunk : "",
    });
  }
  return { passages, rows };
}

function buildPrompt(query, passages, rows) {
  const lines = passages.map((p) => `[${p.id}] ${p.no} ${p.title} ${p.page ? `p.${p.page}` : ""}\n${p.quote}`);
  const figures = rows.map((r) => `${r.id}\t${r.number}\t${r.name}\t${r.chunk}`);
  return (
    "土壌汚染対策法ガイドラインの実務者の質問に、図表を選んで答えます。\n" +
    "本文の抜粋（[チャンクid] 項番号 見出し ページ）と、図表の候補（id, 番号, 図表名, 関係するチャンクid）を渡します。\n" +
    "抜粋の内容から、質問に一番役立つ図表を選んでください。言葉が似ているだけの図表は下にしてください。\n" +
    "id は候補の先頭列をそのまま返す。候補にない id は返さない。最大5件。\n" +
    "chunk は根拠にしたチャンクid。reason は日本語で1文（どの項の、どんな図表か）。\n\n" +
    `質問: ${query}\n\n` +
    "本文の抜粋:\n" +
    lines.join("\n\n") +
    "\n\n" +
    "図表の候補:\n" +
    figures.join("\n")
  );
}

function parseJson(text) {
  try {
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    return JSON.parse(start >= 0 && end > start ? text.slice(start, end + 1) : text);
  } catch {
    throw Object.assign(new Error("Gemini の応答を読み取れませんでした。（format）"), { status: 502 });
  }
}

// previous（2回目に渡す1回目の図表）も選べるようにする。[{ id, number }]
function pickRagMatches(parsed, rows, passages, previous = []) {
  const chunkOf = new Map(rows.map((r) => [r.id, r.chunk]));
  const byNumber = new Map(rows.map((r) => [baseNumber(r.number), r.id]));
  const extra = new Set();
  for (const p of previous) {
    extra.add(p.id);
    if (!byNumber.has(baseNumber(p.number))) byNumber.set(baseNumber(p.number), p.id);
  }
  const known = new Set([...passages.map((p) => p.id), ...chunkOf.values()]);
  const matches = [];
  for (const item of parsed.matches || []) {
    const raw = String(item.id || "").trim();
    const id = chunkOf.has(raw) || extra.has(raw) ? raw : byNumber.get(baseNumber(raw));
    if (!id || matches.some((m) => m.id === id)) continue;
    const chunk = String(item.chunk || "");
    matches.push({
      id,
      reason: String(item.reason || "").trim(),
      chunk: known.has(chunk) ? chunk : chunkOf.get(id) || "",
    });
    if (matches.length >= 5) break;
  }
  return matches;
}

function baseNumber(value) {
  return normNum(value).replace(/[（(][^）)]*[）)]$/, "");
}

// ---------- 軽量エージェント（tools/agent_lite.py と同じ） ----------
const STATUSES = ["ok", "clarify", "retry", "standards"];
const RETRY_QUERY_MAX = 60;
const CLARIFY_MAX = 80;
const OPTION_MAX = 30;
const OPTIONS_MAX = 3;
const PREVIOUS_MAX = 5;

const AGENT_SCHEMA = {
  type: "OBJECT",
  properties: {
    status: { type: "STRING", enum: STATUSES },
    matches: { ...MATCH_SCHEMA.properties.matches, maxItems: 5 },
    // 入れ子にすると確認の一文が改行の繰り返しで止まらなくなることがあったので、平らにして短く縛る
    clarify_question: { type: "STRING", description: "確認の一文。80字まで。改行なし" },
    clarify_options: { type: "ARRAY", items: { type: "STRING" }, maxItems: 3 },
    retry_query: { type: "STRING", description: "検索語。60字まで" },
    substance: { type: "STRING", description: "物質名" },
    medium: { type: "STRING", description: "土壌／地下水など" },
  },
  required: ["status", "matches"],
  propertyOrdering: ["status", "clarify_question", "clarify_options", "retry_query", "substance", "medium", "matches"],
};

const CLARIFY_RULE =
  "- clarify: 質問の意味が2通り以上に取れ、それぞれ別の項に行き着くときだけ。" +
  "clarify_question に確認の一文（80字まで・改行なし）、clarify_options に選択肢2〜3個（各30字まで）\n";

function statusRules(allowClarify) {
  return (
    "status は次の一手。迷ったら ok。\n" +
    "- ok: 候補の中に質問に合う図表がある\n" +
    (allowClarify ? CLARIFY_RULE : "") +
    "- retry: 候補が質問に合わず、言い方を変えれば当たりそうなとき。retry_query にガイドラインでの言い方の検索語（60字まで）\n" +
    "- standards: 数値の基準値そのもの（〇〇の土壌溶出量基準は何 mg/L か等）を聞いているとき。substance に物質名、medium に土壌／地下水などの別\n" +
    "status が ok 以外でも、候補に役立つ図表があれば matches に入れる。"
  );
}

function buildAgentPrompt(query, passages, rows, attempt, allowClarify, previous) {
  const base = buildPrompt(query, passages, rows);
  if (attempt >= 2) {
    // 2回目には1回目の結果を図表番号だけで渡す（抜粋や理由は渡さない）
    return (
      base +
      "\n\n言い換えて探し直した2回目です。1回目に選んだ図表番号: " +
      (previous.length ? previous.join("、") : "なし") +
      "\n候補の中から選び直してください。1回目の図表のほうが合うなら、その番号を id に返してもよい。"
    );
  }
  return `${base}\n\n${statusRules(allowClarify)}`;
}

function clip(value, limit) {
  return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, limit);
}

// 1回目の図表番号は figures.json にある番号だけ通す
function cleanPrevious(raw, figures) {
  if (!Array.isArray(raw)) return [];
  const byNumber = new Map();
  for (const row of figures) {
    const key = baseNumber(row.number);
    if (key && !byNumber.has(key)) byNumber.set(key, row);
  }
  const out = [];
  for (const value of raw.slice(0, 10)) {
    const row = byNumber.get(baseNumber(clip(value, 30)));
    if (!row || out.some((p) => p.id === pageId(row.url))) continue;
    out.push({ id: pageId(row.url), number: row.number });
    if (out.length >= PREVIOUS_MAX) break;
  }
  return out;
}

// AI が返した次の一手を確かめる。2回目は ok だけ。長さと数を制限する
function cleanNext(parsed, attempt, allowClarify) {
  let status = String(parsed.status || "ok");
  if (!STATUSES.includes(status) || attempt >= 2 || (status === "clarify" && !allowClarify)) status = "ok";
  if (status === "clarify") {
    const options = (Array.isArray(parsed.clarify_options) ? parsed.clarify_options : [])
      .map((o) => clip(o, OPTION_MAX))
      .filter(Boolean)
      .slice(0, OPTIONS_MAX);
    const question = clip(parsed.clarify_question, CLARIFY_MAX);
    if (question && options.length >= 2) return { status, clarify: { question, options } };
    return { status: "ok" };
  }
  if (status === "retry") {
    const retry = clip(parsed.retry_query, RETRY_QUERY_MAX);
    return retry ? { status, retry_query: retry } : { status: "ok" };
  }
  if (status === "standards") {
    return { status, standards: { substance: clip(parsed.substance, 30), medium: clip(parsed.medium, 20) } };
  }
  return { status: "ok" };
}

// ---------- 回数制限 ----------
// 数は Durable Object 1つで数える（中継が何台に分かれても同じ数になる）。
// 残すのは回数と、元に戻せない形にした送信元・検索 ID だけ。質問文は渡さない。日が変わったら全部消す。
export class RateLimit {
  constructor(ctx) {
    this.storage = ctx.storage;
  }

  async fetch(request) {
    const { kind, source, id } = await request.json();
    const now = Date.now();
    const day = new Date(now + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
    let total = await this.storage.get("total");
    if (!total || total.day !== day) {
      await this.storage.deleteAll();
      total = { day, count: 0 };
      await this.storage.put("total", total);
    }
    if (kind === "edit") {
      const edits = (await this.storage.get(`e:${id}`)) || 0;
      if (edits >= EDITS_PER_RECORD) return json({ stop: "repeat" });
      await this.storage.put(`e:${id}`, edits + 1);
      return json({});
    }
    const key = `s:${source}:${id}`;
    const used = id ? await this.storage.get(key) : null;
    if (used) {
      // 数え済みの検索。1回目・2回目・記録のそれぞれ1度ずつだけ通す
      if (used.includes(kind)) return json({ stop: "repeat" });
      await this.storage.put(key, [...used, kind]);
      return json({});
    }
    if (total.count >= PER_DAY) return json({ stop: "day" });
    const recent = ((await this.storage.get(`m:${source}`)) || []).filter((at) => at > now - 60000);
    if (recent.length >= PER_MINUTE) return json({ stop: "minute" });
    await this.storage.put(`m:${source}`, [...recent, now]);
    await this.storage.put("total", { day, count: total.count + 1 });
    if (id) await this.storage.put(key, [kind]);
    return json({});
  }
}

// IPv6 は1人が末尾を変え放題なので、前半（/64）でまとめる
function sourceNetwork(ip) {
  if (!ip.includes(":") || ip.includes(".")) return ip;
  const [head, tail] = ip.split("::");
  const front = head ? head.split(":") : [];
  const back = tail ? tail.split(":") : [];
  const zeros = ip.includes("::") ? Array(Math.max(0, 8 - front.length - back.length)).fill("0") : [];
  return [...front, ...zeros, ...back].slice(0, 4).map((part) => parseInt(part, 16).toString(16)).join(":");
}

// 通してよければ null、止めるなら返す応答。数えられないときも止める（制限なしで動かさない）
async function checkLimit(request, env, kind, rawId) {
  const messages = {
    minute: `回数制限（1分に${PER_MINUTE}回）に達したため、AI検索と記録を止めています。少し待ってからお試しください。`,
    day: `今日の回数制限（全体で${PER_DAY}回）に達したため、AI検索と記録を止めています。日本時間の0時に戻ります。`,
    repeat: "同じ検索の受付回数を超えたため止めました。",
    down: "回数を確かめられないため、AI検索と記録を止めています。",
  };
  let stop;
  try {
    const id = /^[0-9a-f]{12,32}$/.test(String(rawId || "")) ? String(rawId) : "";
    const network = sourceNetwork(request.headers.get("CF-Connecting-IP") || "unknown");
    const secret = String(env.GEMINI_API_KEY || env.NOTION_LOG_TOKEN);
    const mac = await crypto.subtle.sign("HMAC", await logKey(secret), new TextEncoder().encode(network));
    const source = [...new Uint8Array(mac).slice(0, 8)].map((b) => b.toString(16).padStart(2, "0")).join("");
    const counter = env.RATE_LIMIT.get(env.RATE_LIMIT.idFromName("all"));
    const response = await counter.fetch("https://limit/", { method: "POST", body: JSON.stringify({ kind, source, id }) });
    if (!response.ok) throw new Error("limit");
    stop = (await response.json()).stop;
  } catch {
    stop = "down";
  }
  if (!stop) return null;
  return json({ error: messages[stop], limited: stop }, stop === "down" ? 503 : 429);
}

// ---------- 試験評価の記録を Notion に保存する ----------
// 受けるのは2つだけ：{ record }（1行足す）と { handle, patch }（その行の4列だけ直す）。読み出しの口は作らない。
// handle は中継が署名したページ ID。ほかの人の行や、ほかのページは直せない。
const LOG_ORIGIN = "https://akii-ai.github.io"; // 手元のテストの検索は保存しない
const LOG_BODY_MAX = 6000;
// [記録の項目, Notion の列, 長さの上限]
const LOG_TEXTS = [
  ["time", "日時", 19],
  ["device", "端末ID", 12],
  ["participant", "参加者番号", 12], // WG7 モードなら WG7。正しいかは確かめない（長さだけ）
  ["code", "ID", 4],
  ["mode", "検索モード", 10],
  ["engine", "使った検索", 20],
  ["reason", "切り替えの理由", 40],
  ["flow", "AIの動き", 60],
  ["model", "モデル名", 80],
  ["shown", "表示した図表番号（上位5件）", 200],
  ["opened", "Notionで開いた図表番号", 400],
];
// [記録の項目, Notion の列, 値の上限]
const LOG_NUMBERS = [
  ["seq", "検索の通し番号", 1e7],
  ["parent", "もとの検索", 1e7],
  ["calls", "AIの呼び出し回数", 2],
  ["prompt", "入力token", 1e6],
  ["output", "出力token", 1e6],
  ["thoughts", "思考token", 1e6],
  ["cost", "費用の目安（円）", 1e4],
  ["seconds", "最初の結果が出るまでの時間（秒）", 3600],
];
const LOG_FEEDBACK = ["未評価", "役立った", "違った"];
const LOG_EXPERIENCE = ["3年未満", "3〜10年", "10年以上"];
const LOG_EDITABLE = ["flow", "shown", "opened", "feedback"];
const LOG_QUESTION_MAX = 300;

function logProperties(raw) {
  const out = {};
  for (const [key, column, max] of LOG_TEXTS) {
    if (!(key in raw)) continue;
    const content = clip(raw[key], max);
    out[column] = { rich_text: content ? [{ type: "text", text: { content } }] : [] };
  }
  for (const [key, column, max] of LOG_NUMBERS) {
    if (!(key in raw)) continue;
    const value = raw[key] === "" || raw[key] === null ? NaN : Number(raw[key]);
    out[column] = { number: value >= 0 && value <= max ? value : null };
  }
  if ("experience" in raw) {
    out["実務経験"] = { select: LOG_EXPERIENCE.includes(raw.experience) ? { name: raw.experience } : null };
  }
  if ("feedback" in raw) {
    out["評価"] = { select: { name: LOG_FEEDBACK.includes(raw.feedback) ? raw.feedback : "未評価" } };
  }
  return out;
}

async function logKey(token) {
  return crypto.subtle.importKey("raw", new TextEncoder().encode(token), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

async function signHandle(id, token) {
  const mac = await crypto.subtle.sign("HMAC", await logKey(token), new TextEncoder().encode(id));
  return `${id}.${[...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

async function openHandle(handle, token) {
  const found = /^([0-9a-f]{32})\.([0-9a-f]{64})$/.exec(String(handle || ""));
  if (!found) return "";
  const mac = new Uint8Array(found[2].match(/../g).map((h) => parseInt(h, 16)));
  const valid = await crypto.subtle.verify("HMAC", await logKey(token), mac, new TextEncoder().encode(found[1]));
  return valid ? found[1] : "";
}

async function saveLog(request, env, origin) {
  if (origin !== LOG_ORIGIN) return json({ error: "記録は公開サイトからだけ受け取ります。" }, 403);
  const token = cleanToken(env.NOTION_LOG_TOKEN || "");
  const database = String(env.NOTION_LOG_DB || "").replace(/-/g, "").trim();
  if (!token || !database) return json({ error: "記録の保存先がまだ設定されていません。" }, 503);
  try {
    const raw = await request.text();
    if (raw.length > LOG_BODY_MAX) return json({ error: "記録が大きすぎます。" }, 413);
    let body;
    try {
      body = JSON.parse(raw);
    } catch {
      body = null;
    }
    if (!body || typeof body !== "object") return json({ error: "記録を読み取れませんでした。" }, 400);
    let path;
    let method;
    let payload;
    let stopped;
    if ("handle" in body) {
      const id = await openHandle(body.handle, token);
      if (!id) return json({ error: "この記録は直せません。" }, 403);
      const patch = {};
      for (const key of LOG_EDITABLE) {
        if (typeof body.patch?.[key] === "string") patch[key] = body.patch[key];
      }
      if (!Object.keys(patch).length) return json({ error: "直す項目がありません。" }, 400);
      stopped = await checkLimit(request, env, "edit", id);
      [method, path, payload] = ["PATCH", `pages/${hyphenate(id)}`, { properties: logProperties(patch) }];
    } else {
      const record = body.record;
      if (!record || typeof record !== "object") return json({ error: "記録がありません。" }, 400);
      const question = clip(record.query, LOG_QUESTION_MAX);
      if (!question || !/^[0-9a-f]{12}$/.test(String(record.device || ""))) return json({ error: "記録の形が違います。" }, 400);
      const properties = logProperties({ ...record, feedback: record.feedback || "未評価" });
      properties["質問"] = { title: [{ type: "text", text: { content: question } }] };
      stopped = await checkLimit(request, env, "log", record.sid);
      [method, path, payload] = ["POST", "pages", { parent: { database_id: database }, properties }];
    }
    if (stopped) return stopped;
    let response;
    for (let attempt = 0; attempt < 3; attempt++) {
      response = await fetch(`https://api.notion.com/v1/${path}`, {
        method,
        headers: { Authorization: `Bearer ${token}`, "Notion-Version": "2022-06-28", "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (attempt === 2 || (response.status !== 429 && response.status !== 529)) break;
      const wait = Math.min(Number(response.headers.get("Retry-After")) || 1, 5);
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
    }
    if (!response.ok) {
      // Notion の返事には保存先の ID などが入るので、そのまま返さない
      const busy = response.status === 429 || response.status === 529;
      return json({ error: "記録を保存できませんでした。", notion: response.status }, busy ? 429 : 502);
    }
    if (method === "PATCH") return json({ ok: true });
    const page = await response.json();
    return json({ handle: await signHandle(String(page.id).replace(/-/g, ""), token) });
  } catch {
    return json({ error: "記録を保存できませんでした。" }, 502);
  }
}

function visibleText(data) {
  const parts = data.candidates?.[0]?.content?.parts || [];
  return parts.filter((part) => part.text && !part.thought).map((part) => part.text).join("");
}

function pickMatches(text, figures, allowed) {
  let parsed;
  try {
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    parsed = JSON.parse(start >= 0 && end > start ? text.slice(start, end + 1) : text);
  } catch {
    throw Object.assign(new Error("Gemini の応答を読み取れませんでした。（format）"), { status: 502 });
  }
  const byNumber = new Map();
  for (const row of figures) {
    const key = normNum(row.number);
    if (!byNumber.has(key)) byNumber.set(key, []);
    byNumber.get(key).push(row);
  }
  const matches = [];
  const seen = new Set();
  for (const item of parsed.matches || []) {
    const id = resolveId(item.id, allowed, byNumber);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    matches.push({ id, reason: String(item.reason || "").trim() });
    if (matches.length >= 8) break;
  }
  return matches;
}

function resolveId(itemId, allowed, byNumber) {
  const id = String(itemId || "").trim();
  if (allowed.has(id)) return id;
  for (const row of byNumber.get(normNum(id)) || []) {
    const found = pageId(row.url);
    if (allowed.has(found)) return found;
  }
  return "";
}

function pageId(url) {
  return String(url || "").replace(/\/+$/, "").split("/").pop();
}

function normNum(value) {
  return String(value || "")
    .normalize("NFKC")
    .replace(/\s+/g, "")
    .replace(/[‐－ー]/g, "-");
}

function messageFromGoogle(detail, apiKey) {
  let message = redact(detail, apiKey);
  try {
    message = JSON.parse(detail).error.message;
  } catch {
    message = message.trim().slice(0, 300);
  }
  message = redact(message, apiKey);
  const lowered = message.toLowerCase();
  if (lowered.includes("api key") || lowered.includes("api_key")) return "Gemini APIキーが正しくありません。";
  if (lowered.includes("quota") || lowered.includes("rate")) {
    return "Gemini の利用上限に達しました。少し待ってからもう一度検索してください。";
  }
  return message || "Gemini の検索に失敗しました。";
}

function clientStatus(status) {
  if (status === 429) return 429;
  if (status === 400 || status === 401 || status === 403) return 401;
  return 502;
}

function redact(text, apiKey) {
  const value = String(text || "");
  return apiKey ? value.split(apiKey).join("***") : value;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function withCors(response, origin) {
  const headers = new Headers(response.headers);
  if (ALLOWED.has(origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Vary", "Origin");
    headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Content-Type");
    headers.set("Access-Control-Max-Age", "86400");
  }
  return new Response(response.body, { status: response.status, headers });
}
