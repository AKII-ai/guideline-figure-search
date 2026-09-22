// Cloudflare Workers に置く中継。
// Gemini のキーは秘密 GEMINI_API_KEY、Notion のトークンは秘密 NOTION_TOKEN にだけ入れる。
// このファイルと GitHub にはキーを書かない。

const FIGURES_URL = "https://akii-ai.github.io/guideline-figure-search/data/figures.json";
const MODELS = ["gemini-2.5-flash", "gemini-flash-latest"];
const ALLOWED = new Set([
  "https://akii-ai.github.io",
  "http://127.0.0.1:8765",
  "http://localhost:8765",
]);

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
      const { listed, allowed } = figureMap(figures, payload.kind, payload.chapter);
      if (!listed) {
        return withCors(json({ matches: [] }), origin);
      }
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
  const source = await notionImageUrl(figureId, token);
  if (!source) {
    return json({ error: "このページに画像がありません。" }, 404);
  }
  if (!allowedImageUrl(source)) {
    return json({ error: "この画像の置き場所は読めません。" }, 404);
  }
  const response = await fetch(source, { redirect: "manual", headers: { "User-Agent": "figure-search" } });
  if (!response.ok) {
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
    temperature: 0.2,
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
  const body = JSON.stringify({
    contents: [{ role: "user", parts: [{ text: prompt }] }],
    generationConfig,
  });
  let lastStatus = 502;
  let lastDetail = "";
  let lastReason = "";
  for (const model of MODELS) {
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-api-key": apiKey,
        },
        body,
      }
    );
    if (response.ok) {
      const data = await response.json();
      const text = visibleText(data);
      if (text) return text;
      lastReason = data.candidates?.[0]?.finishReason || data.promptFeedback?.blockReason || "empty";
      continue;
    }
    lastStatus = response.status;
    lastDetail = await response.text();
    if (response.status === 404) continue;
    throw Object.assign(new Error(messageFromGoogle(lastDetail, apiKey)), { status: clientStatus(response.status) });
  }
  if (lastReason) {
    throw Object.assign(new Error(`Gemini の応答を読み取れませんでした。（${lastReason}）`), { status: 502 });
  }
  throw Object.assign(new Error(messageFromGoogle(lastDetail, apiKey) || "Gemini のモデルを使えませんでした。"), {
    status: clientStatus(lastStatus),
  });
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
