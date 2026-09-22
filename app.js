const EXAMPLES = ["特定有害物質", "自然由来 盛土", "一時的免除", "地下水"];

const state = {
  rows: [],
  query: "",
  kind: "",
  chapter: "",
  hits: null,
  error: "",
  loading: false,
  mode: "normal",
};

const qInput = document.getElementById("q");
const searchBtn = document.getElementById("search");
const apiKeyInput = document.getElementById("apikey");
const clearBtn = document.getElementById("clear");
const chapterSelect = document.getElementById("chapter");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const examplesEl = document.getElementById("examples");
const aiKeyBox = document.getElementById("ai-key");
const modeBar = document.querySelector(".mode");
const localAi = location.hostname === "localhost" || location.hostname === "127.0.0.1";

examplesEl.innerHTML = EXAMPLES.map(
  (text) => `<button type="button" data-example="${escapeHtml(text)}">${escapeHtml(text)}</button>`
).join("");

if (localAi) {
  state.mode = localStorage.getItem("searchMode") === "ai" ? "ai" : "normal";
  apiKeyInput.value = localStorage.getItem("geminiApiKey") || "";
  apiKeyInput.addEventListener("change", () => {
    localStorage.setItem("geminiApiKey", apiKeyInput.value.trim());
  });
} else {
  state.mode = "normal";
  localStorage.removeItem("geminiApiKey");
  localStorage.removeItem("searchMode");
  modeBar.hidden = true;
}
applyMode();

examplesEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-example]");
  if (!button) return;
  qInput.value = button.dataset.example;
  clearBtn.hidden = false;
  qInput.focus();
});

qInput.addEventListener("input", () => {
  clearBtn.hidden = qInput.value.trim() === "";
});

qInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    search();
  }
});

searchBtn.addEventListener("click", () => search());

modeBar.addEventListener("click", (event) => {
  const button = event.target.closest("[data-mode]");
  if (!localAi || !button || button.dataset.mode === state.mode) return;
  state.mode = button.dataset.mode;
  localStorage.setItem("searchMode", state.mode);
  applyMode();
  cancelSearch();
  render();
});

clearBtn.addEventListener("click", () => {
  qInput.value = "";
  state.query = "";
  state.hits = null;
  state.error = "";
  render();
  qInput.focus();
});

document.querySelector(".kind").addEventListener("click", (event) => {
  const button = event.target.closest("[data-kind]");
  if (!button) return;
  state.kind = button.dataset.kind;
  for (const item of document.querySelectorAll(".kind button")) {
    const on = item === button;
    item.classList.toggle("is-on", on);
    item.setAttribute("aria-pressed", on ? "true" : "false");
  }
  render();
});

chapterSelect.addEventListener("change", () => {
  state.chapter = chapterSelect.value;
  render();
});

resultsEl.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const text = button.dataset.copy;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  const previous = button.textContent;
  button.textContent = "コピーしました";
  button.classList.add("is-copied");
  setTimeout(() => {
    button.textContent = previous;
    button.classList.remove("is-copied");
  }, 1400);
});

load();

async function load() {
  try {
    const response = await fetch("data/figures.json");
    if (!response.ok) throw new Error(String(response.status));
    state.rows = await response.json();
    fillChapters(state.rows);
    render();
  } catch (error) {
    statusEl.textContent = "索引データを読み込めませんでした。";
    resultsEl.innerHTML = `<div class="empty"><p>${escapeHtml(String(error.message || error))}</p></div>`;
  }
}

function fillChapters(rows) {
  const chapters = new Map();
  for (const row of rows) {
    const name = row.chapter || "";
    if (!chapters.has(name)) chapters.set(name, row.chapterNo ?? 999);
  }
  const names = [...chapters.keys()].sort((a, b) => chapters.get(a) - chapters.get(b) || a.localeCompare(b, "ja"));
  for (const name of names) {
    const option = document.createElement("option");
    option.value = name || "__none__";
    option.textContent = name || "章なし";
    chapterSelect.appendChild(option);
  }
}

let searchSeq = 0;

function cancelSearch() {
  searchSeq += 1;
  if (state.controller) {
    state.controller.abort();
    state.controller = null;
  }
  state.loading = false;
}

async function search() {
  state.query = qInput.value;
  clearBtn.hidden = state.query.trim() === "";
  state.error = "";
  const query = state.query.trim();
  if (!query) {
    state.hits = null;
    state.loading = false;
    render();
    return;
  }
  const seq = ++searchSeq;
  if (state.controller) state.controller.abort();
  const local = localHits(query);
  state.hits = local;
  if (state.mode !== "ai" || !localAi) {
    state.loading = false;
    state.error = "";
    render();
    return;
  }
  const apiKey = apiKeyInput.value.trim();
  localStorage.setItem("geminiApiKey", apiKey);
  if (!apiKey) {
    state.loading = false;
    state.error = state.hits.length ? "" : "Gemini APIキーを入力してください。";
    render();
    return;
  }
  if (!local.length) {
    state.loading = false;
    render();
    return;
  }
  state.loading = true;
  render();
  const controller = new AbortController();
  state.controller = controller;
  try {
    const response = await fetch("/api/search", {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        kind: state.kind,
        chapter: state.chapter,
        apiKey,
        ids: local.slice(0, 20).map((row) => pageId(row.url)),
      }),
    });
    const data = await response.json();
    if (seq !== searchSeq) return;
    if (!response.ok) throw new Error(data.error || "検索に失敗しました。");
    const byId = new Map(state.rows.map((row) => [pageId(row.url), row]));
    const refined = (data.matches || [])
      .map((match) => {
        const row = byId.get(match.id);
        return row ? { ...row, reason: match.reason || "" } : null;
      })
      .filter(Boolean);
    if (refined.length) state.hits = refined;
  } catch (error) {
    if (error.name === "AbortError" || seq !== searchSeq) return;
    state.error = state.hits.length
      ? "Gemini にはつなげませんでした。通常の検索結果を表示しています。"
      : (error.message || "検索に失敗しました。");
  } finally {
    if (seq === searchSeq) {
      state.loading = false;
      render();
    }
  }
}

function render() {
  const tokens = tokenize(state.query);
  clearBtn.hidden = state.query.trim() === "";
  if (!state.query.trim()) {
    statusEl.textContent = "内容を入れて検索すると、該当する図表を表示します。";
    resultsEl.innerHTML = "";
    return;
  }
  if (state.error && !state.hits) {
    statusEl.textContent = state.error;
    resultsEl.innerHTML = "";
    return;
  }
  if (!state.hits) {
    statusEl.textContent = "検索ボタンで探します。";
    resultsEl.innerHTML = "";
    return;
  }
  const matched = state.hits.filter(passesFilter);
  const waiting = state.mode === "ai" && state.loading ? " Gemini で確認しています。" : "";
  const note = state.error && state.hits.length && !state.loading ? ` ${state.error}` : "";
  statusEl.textContent = `「${state.query.trim()}」 ${matched.length}件${waiting}${note}`;

  if (matched.length === 0) {
    resultsEl.innerHTML = `
      <div class="empty">
        <h2>見つかりませんでした</h2>
        <p>言い方を変えるか、章の指定を外してみてください。</p>
      </div>`;
    return;
  }

  const body = matched.map((row) => {
    const cite = citation(row);
    const snippet = snippetFor(row, tokens);
    return `<tr>
      <td class="num">${highlight(row.number || "—", tokens)}</td>
      <td><span class="name">${highlight(row.name || "（名称なし）", tokens)}</span>${
        row.reason
          ? `<span class="snippet">${escapeHtml(row.reason)}</span>`
          : snippet
            ? `<span class="snippet">${highlight(snippet, tokens)}</span>`
            : ""
      }</td>
      <td>${highlight(row.chapter || "—", tokens)}</td>
      <td>${highlight(row.section || "—", tokens)}</td>
      <td>${highlight(row.item || row.itemNo || "—", tokens)}</td>
      <td class="page-cell">${row.page == null || row.page === "" ? "—" : escapeHtml(String(row.page))}</td>
      <td class="actions">
        <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">Notionで開く</a>
        <button type="button" data-copy="${escapeHtml(cite)}">引用をコピー</button>
      </td>
    </tr>`;
  }).join("");

  resultsEl.innerHTML = `<div class="table-wrap"><table>
    <caption class="visually-hidden">検索結果</caption>
    <thead><tr>
      <th>図表番号</th><th>図表名</th><th>章</th><th>節</th><th>項</th><th>ページ</th><th>操作</th>
    </tr></thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

function applyMode() {
  const ai = state.mode === "ai";
  aiKeyBox.hidden = !ai;
  for (const button of document.querySelectorAll("[data-mode]")) {
    const on = button.dataset.mode === state.mode;
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

function passesFilter(row) {
  if (state.kind && row.kind !== state.kind) return false;
  if (state.chapter === "__none__") return !row.chapter;
  if (state.chapter && (row.chapter || "") !== state.chapter) return false;
  return true;
}

function localHits(query) {
  const tokens = tokenize(query);
  const compactQuery = compact(query);
  return state.rows
    .filter(passesFilter)
    .map((row) => ({ row, score: scoreRow(row, tokens, compactQuery) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || (a.row.chapterNo ?? 999) - (b.row.chapterNo ?? 999))
    .slice(0, 30)
    .map((item) => item.row);
}

function scoreRow(row, tokens, compactQuery) {
  const number = compact(row.number);
  let score = 0;
  if (compactQuery && number && compactQuery === number) score += 100;
  if (tokens.some((token) => token === number)) score += 100;
  const nameHits = fieldHits(row.name, tokens);
  const placeHits = fieldHits(`${row.section || ""} ${row.item || ""} ${row.itemNo || ""}`, tokens);
  const descHits = fieldHits(row.description, tokens);
  const contextHits = fieldHits(row.context, tokens);
  score += nameHits * 8 + placeHits * 5 + descHits * 4 + contextHits * 2;
  if (tokens.length > 1) {
    if (nameHits === tokens.length) score += 12;
    if (placeHits === tokens.length) score += 12;
    if (descHits === tokens.length) score += 6;
    if (contextHits === tokens.length) score += 3;
    const covered = tokens.filter((token) => haystack(row).includes(token));
    if (covered.length === tokens.length) score += 10;
  }
  return score;
}

function fieldHits(text, tokens) {
  const folded = compact(text);
  if (!folded) return 0;
  return tokens.reduce((count, token) => count + (folded.includes(token) ? 1 : 0), 0);
}

function pageId(url) {
  return String(url || "").replace(/\/$/, "").split("/").pop();
}

function citation(row) {
  const number = row.number || "図表";
  const name = row.name ? ` ${row.name}` : "";
  const loc = [];
  if (row.page != null && row.page !== "") loc.push(`p.${row.page}`);
  if (row.itemNo) loc.push(String(row.itemNo));
  else if (row.sectionNo != null && row.sectionNo !== "") loc.push(String(row.sectionNo));
  const where = loc.length ? `（${loc.join("、")}）` : "";
  return `${number}${name}${where}`;
}

function haystack(row) {
  return compact([
    row.number,
    row.name,
    row.section,
    row.item,
    row.itemNo,
    row.description,
    row.context,
  ].filter((value) => value != null && value !== "").join(" "));
}

function snippetFor(row, tokens) {
  const context = row.context || "";
  const contextHas = tokens.some((token) => compact(context).includes(token));
  const source = contextHas ? context : [row.description, row.context].filter(Boolean).join(" ");
  if (!source) return "";
  if (tokens.length === 0) return clip(source, 90);
  const folded = normalize(source);
  let at = -1;
  for (const token of tokens) {
    const found = folded.indexOf(token);
    if (found >= 0 && (at < 0 || found < at)) at = found;
  }
  if (at < 0) return clip(source, 90);
  const start = Math.max(0, at - 24);
  const slice = [...folded].slice(start, start + 110).join("");
  return `${start > 0 ? "…" : ""}${slice}${start + 110 < [...folded].length ? "…" : ""}`;
}

function clip(text, length) {
  const folded = normalize(text);
  if ([...folded].length <= length) return folded;
  return `${[...folded].slice(0, length).join("")}…`;
}

function tokenize(query) {
  return normalize(query).split(" ").map((token) => token.replaceAll(" ", "")).filter(Boolean);
}

function compact(value) {
  return normalize(value).replaceAll(" ", "");
}

function normalize(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function highlight(text, tokens) {
  const raw = text == null ? "" : String(text);
  if (!raw || tokens.length === 0) return escapeHtml(raw);
  const folded = raw.normalize("NFKC").toLowerCase();
  const ranges = [];
  for (const token of tokens) {
    const needle = token.normalize("NFKC").toLowerCase();
    if (!needle) continue;
    let from = 0;
    while (from < folded.length) {
      const at = folded.indexOf(needle, from);
      if (at < 0) break;
      ranges.push([at, at + needle.length]);
      from = at + needle.length;
    }
  }
  if (ranges.length === 0) return escapeHtml(raw);
  ranges.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  const merged = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (!last || range[0] > last[1]) merged.push(range.slice());
    else last[1] = Math.max(last[1], range[1]);
  }
  let html = "";
  let cursor = 0;
  for (const [start, end] of merged) {
    html += escapeHtml(raw.slice(cursor, start));
    html += `<mark>${escapeHtml(raw.slice(start, end))}</mark>`;
    cursor = end;
  }
  html += escapeHtml(raw.slice(cursor));
  return html;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
