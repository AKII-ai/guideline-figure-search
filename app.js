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
  passages: null,
  clarify: null, // AI が確認を求めたとき { question, options }
  pending: null, // 確認中に1回目で選んでいた図表（「このまま検索」で出す）
  retried: "", // 言い換えて探し直した検索語
  standards: null, // 基準値アプリへの案内 { substance, medium }
  agentNote: "",
  record: null, // 試験評価の記録（いまの検索だけ。端末には残さない）
  normal: null, // 通常検索で使った検索 { engine: "exact" | "bm25", reason }
  pricing: null, // data/pricing.json（費用の目安）
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
const aiSetup = document.getElementById("ai-setup");
const aiConnected = document.getElementById("ai-connected");
const aiCheckBtn = document.getElementById("ai-check");
const aiDisconnectBtn = document.getElementById("ai-disconnect");
const aiKeyStatus = document.getElementById("ai-key-status");
const modeBar = document.querySelector(".mode");
const publicAi = "https://guideline-figure-search.kogama089.workers.dev";
const localAi = location.hostname === "localhost" || location.hostname === "127.0.0.1";
const aiOn = localAi || Boolean(publicAi);
const STANDARDS_URL = "https://akii-ai.github.io/standards-web/";
// 試験評価の記録（端末には残さない。公開サイトでは中継を通して担当者の Notion に保存する）
const NOTION_LOG = location.origin === "https://akii-ai.github.io" ? `${publicAi}/log` : "";
const remoteLog = new Map(); // 記録の id → 送信の順番待ち（中身は Notion の行の handle）
const OLD_LOG_KEY = "searchLog"; // 前の版が端末に残した記録。開いたときに消す
const DEVICE_KEY = "searchDeviceId";
const SEQ_KEY = "searchLogSeq";
// 評価のポップアップと保存失敗の知らせは、ここで作る（ブラウザに古い index.html が残っていても動くように）
const rateEl = document.createElement("div");
rateEl.id = "rate";
rateEl.className = "rate";
rateEl.hidden = true;
rateEl.setAttribute("role", "dialog");
rateEl.setAttribute("aria-labelledby", "rate-text");
rateEl.innerHTML = `<div class="rate-box"><p id="rate-text" tabindex="-1"></p><div class="feedback">` +
  ["役立った", "違った", "よくわからない"].map((label) => `<button type="button" data-feedback="${label}">${label}</button>`).join("") +
  `<button type="button" class="rate-back" data-rate-back hidden>結果に戻る</button></div></div>`;
document.body.appendChild(rateEl);
const logWarn = document.createElement("p");
logWarn.className = "log-warn";
logWarn.setAttribute("role", "alert");
statusEl.before(logWarn);
let afterRate = null; // 評価が済むまで止めている操作
let accepted = !NOTION_LOG; // Notion に送らない環境（手元）では説明を出さない
const EXPERIENCES = ["3年未満", "3〜10年", "10年以上"];
let experience = ""; // 実務経験（開くたびに説明の画面で選ぶ。端末には残さない）
const NORMAL_BM25_LIMIT = 15; // 通常検索で本文から探したときに出す図表の数

examplesEl.innerHTML = EXAMPLES.map(
  (text) => `<button type="button" data-example="${escapeHtml(text)}">${escapeHtml(text)}</button>`
).join("");

if (localAi) {
  state.mode = localStorage.getItem("searchMode") === "ai" ? "ai" : "normal";
  if (storedKey()) {
    showConnected(true);
  } else {
    apiKeyInput.value = localStorage.getItem("geminiApiKey") || "";
    showConnected(false);
  }
} else {
  localStorage.removeItem("geminiApiKey");
  localStorage.removeItem("geminiConnected");
  localStorage.removeItem("notionToken");
  if (publicAi) {
    state.mode = localStorage.getItem("searchMode") === "ai" ? "ai" : "normal";
  } else {
    state.mode = "normal";
    localStorage.removeItem("searchMode");
    modeBar.hidden = true;
  }
}
applyMode();
applyNotion();

document.getElementById("notion-save").addEventListener("click", () => {
  if (!localAi) return;
  const token = document.getElementById("notion-token").value.trim();
  if (!token) return;
  localStorage.setItem("notionToken", token);
  document.getElementById("notion-token").value = "";
  applyNotion();
  loadThumbs();
});
document.getElementById("notion-clear").addEventListener("click", () => {
  localStorage.removeItem("notionToken");
  revokePreviews();
  applyNotion();
  render();
});
document.getElementById("preview-close").addEventListener("click", () => {
  document.getElementById("preview").close();
});
document.getElementById("preview").addEventListener("click", (event) => {
  if (event.target.id === "preview") event.currentTarget.close();
});

aiCheckBtn.addEventListener("click", () => checkConnection());
aiDisconnectBtn.addEventListener("click", () => disconnectKey());
apiKeyInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    checkConnection();
  }
});

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
  // 日本語変換を確定する Enter では検索しない（途中の言葉が記録され、評価も求められるため）
  if (event.key === "Enter" && !event.repeat && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    search();
  }
});

// ダブルクリックの2回目では検索しない（結果を見る前に評価を求められるため）
searchBtn.addEventListener("click", (event) => {
  if (event.detail <= 1) search();
});

modeBar.addEventListener("click", (event) => {
  const button = event.target.closest("[data-mode]");
  if (!aiOn || !button || button.dataset.mode === state.mode) return;
  afterRating(() => changeMode(button.dataset.mode));
});

function changeMode(mode) {
  state.mode = mode;
  localStorage.setItem("searchMode", state.mode);
  applyMode();
  if (state.mode === "ai" && state.rows.length) ensureGuide().catch(() => {});
  cancelSearch();
  resetAgent();
  state.normal = null;
  render();
}

clearBtn.addEventListener("click", () => {
  afterRating(() => {
    qInput.value = "";
    clearResults();
    qInput.focus();
  });
});

function clearResults() {
  cancelSearch();
  state.query = "";
  state.hits = null;
  state.passages = null;
  state.error = "";
  state.normal = null;
  resetAgent();
  render();
}

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
  const opened = event.target.closest("[data-open]");
  if (opened) {
    recordOpened(opened.dataset.open);
    return;
  }
  const switcher = event.target.closest("[data-switch]");
  if (switcher) {
    switchNormal(switcher.dataset.switch);
    return;
  }
  const option = event.target.closest("[data-clarify]");
  if (option) {
    answerClarify(option.dataset.clarify);
    return;
  }
  if (event.target.closest("[data-clarify-skip]")) {
    skipClarify();
    return;
  }
  const thumb = event.target.closest("[data-preview]");
  if (thumb) {
    const image = thumb.querySelector("img");
    if (!image?.src) return;
    const previewImg = document.getElementById("preview-img");
    previewImg.src = image.src;
    previewImg.alt = image.alt;
    document.getElementById("preview").showModal();
    return;
  }
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

// 中クリックで Notion を開いたときも記録する
resultsEl.addEventListener("auxclick", (event) => {
  const opened = event.target.closest("[data-open]");
  if (opened && event.button === 1) recordOpened(opened.dataset.open);
});

// 「戻る」で前の画面のまま戻ったときも、説明から始めて結果を残さない
window.addEventListener("pageshow", (event) => {
  if (event.persisted) location.reload();
});

storageSet(OLD_LOG_KEY, null); // このキーだけを消す（localStorage は同じドメインの他のアプリと共有）
startNotice();
load();

async function load() {
  try {
    const response = await fetch("data/figures.json");
    if (!response.ok) throw new Error(String(response.status));
    state.rows = await response.json();
    fetch("data/pricing.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((pricing) => { state.pricing = pricing; })
      .catch(() => {});
    fillChapters(state.rows);
    render();
    if (state.mode === "ai") ensureGuide().catch(() => {});
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

function resetAgent() {
  state.clarify = null;
  state.pending = null;
  state.retried = "";
  state.standards = null;
  state.agentNote = "";
  remember(null);
}

// options.clarified: 確認の答えを足してやり直した質問（もう確認しない）
// options.parent: もとの検索の通し番号（ボタンでの切り替え・確認の選択肢でやり直したとき）
// options.engine / options.reason: 通常検索で「本文からも探す」「完全一致に戻す」を押したとき
async function search(options = {}) {
  if (!accepted) return;
  if (needsRating()) {
    const typed = qInput.value;
    afterRating(() => {
      qInput.value = typed;
      search(options);
    });
    return;
  }
  logWarn.textContent = "";
  const started = performance.now();
  options = { ...options, sid: randomId() }; // 検索1回の印（中継の回数制限で1回に数える）
  state.query = qInput.value;
  clearBtn.hidden = state.query.trim() === "";
  state.error = "";
  resetAgent();
  state.normal = null;
  const query = state.query.trim();
  if (!query) {
    state.hits = null;
    state.loading = false;
    render();
    return;
  }
  const seq = ++searchSeq;
  if (state.controller) state.controller.abort();
  const elapsed = () => Math.round(performance.now() - started) / 1000;
  if (state.mode !== "ai" || !aiOn) {
    await normalSearch(query, seq, options, elapsed);
    return;
  }
  // 図表番号が入っている質問は、AI を呼ばずに通常検索で返す
  if (GuideRag.hasFigureNumber(query)) {
    state.hits = localHits(query);
    state.passages = null;
    state.loading = false;
    state.agentNote = "図表番号が入っているので、AI を使わずに通常検索で表示しています。";
    remember(addRecord({
      query, mode: "AI", engine: "完全一致", reason: "", parent: options.parent, sid: options.sid,
      flow: "図表番号（AIなし）", shown: state.hits, seconds: elapsed(),
    }));
    render();
    return;
  }
  const apiKey = localAi ? storedKey() : "";
  if (localAi && !apiKey) {
    state.hits = null;
    state.loading = false;
    state.error = "接続確認をしてから検索してください。";
    render();
    return;
  }
  state.hits = null;
  state.passages = null;
  state.loading = true;
  render();
  const controller = new AbortController();
  state.controller = controller;
  // 1) 本文の検索はブラウザで行う（rag.js）。該当箇所は AI を待たずに表示する
  let guide;
  let req;
  try {
    guide = await ensureGuide();
    if (seq !== searchSeq) return;
    req = guide.request(query, state.kind, state.chapter);
    state.passages = req.hits.slice(0, 3).map(([, chunk]) => guide.passage(chunk, query));
    render();
  } catch (error) {
    if (seq !== searchSeq) return;
    state.loading = false;
    state.error = "ガイドライン本文のデータを読み込めませんでした。";
    render();
    return;
  }
  const usage = { prompt: 0, output: 0, thoughts: 0, calls: 0, models: new Set() };
  let limited = false;
  const flow = [];
  const toHits = (matches) => {
    const byId = new Map(state.rows.map((row) => [pageId(row.url), row]));
    return (matches || [])
      .map((match) => {
        const row = byId.get(match.id);
        const chunk = guide.chunkById.get(match.chunk);
        return row ? { ...row, reason: match.reason || "", basis: chunk ? guide.passage(chunk, query) : null } : null;
      })
      .filter(Boolean);
  };
  const ask = async (request, attempt, extra) => {
    const payload = {
      query,
      kind: state.kind,
      chapter: state.chapter,
      passages: request.passages,
      candidates: request.candidates,
      attempt,
      sid: options.sid,
      ...extra,
    };
    if (localAi) payload.apiKey = apiKey;
    const response = await fetch(localAi ? "/api/search" : publicAi, {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (data.limited) limited = true;
    if (!response.ok) throw new Error(data.error || "検索に失敗しました。");
    usage.calls += 1;
    usage.prompt += Number(data.usage?.prompt) || 0;
    usage.output += Number(data.usage?.output) || 0;
    usage.thoughts += Number(data.usage?.thoughts) || 0;
    if (data.usage?.model) usage.models.add(data.usage.model);
    return data;
  };
  let firstHits = null;
  try {
    // 2) 1回目：図表選びと次の一手（ok / clarify / retry / standards）を同時に答えさせる
    const first = await ask(req, 1, { allowClarify: !options.clarified });
    if (seq !== searchSeq) return;
    const next = first.next || { status: "ok" };
    flow.push(next.status);
    firstHits = toHits(first.matches);
    if (next.status === "clarify" && next.clarify) {
      state.clarify = next.clarify;
      state.pending = firstHits;
      state.hits = null;
    } else if (next.status === "retry" && next.retry_query) {
      // 3) 言い換えて、ブラウザで探し直す（AI なし）。2回目は ok に固定され、それ以上は探さない
      state.retried = next.retry_query;
      const retryReq = guide.request(GuideRag.retryQuery(next.retry_query, query), state.kind, state.chapter);
      const previous = firstHits.map((row) => row.number).filter(Boolean);
      render();
      const second = await ask(retryReq, 2, { allowClarify: false, previous });
      if (seq !== searchSeq) return;
      flow.push((second.next || { status: "ok" }).status);
      const secondHits = toHits(second.matches);
      if (secondHits.length) {
        state.hits = secondHits;
        state.passages = retryReq.hits.slice(0, 3).map(([, chunk]) => guide.passage(chunk, query));
      } else {
        state.hits = firstHits;
      }
    } else {
      if (next.status === "standards") state.standards = next.standards || { substance: "", medium: "" };
      state.hits = firstHits;
    }
  } catch (error) {
    if (error.name === "AbortError" || seq !== searchSeq) return;
    if (firstHits) {
      // 2回目だけ失敗したときは、1回目の結果を出す
      state.hits = firstHits;
    } else {
      // Gemini が使えないときは、本文検索だけの順位を出す
      state.hits = req.ranked.slice(0, 8).map(([row, , chunk]) => ({
        ...row,
        reason: "",
        basis: chunk ? guide.passage(chunk, query) : null,
      }));
    }
    const message = error.message === "Failed to fetch" ? "Gemini に接続できませんでした。" : (error.message || "検索に失敗しました。");
    state.error = firstHits ? `${message} 1回目の結果を表示しています。` : `${message} 本文検索だけの結果を表示しています。`;
    flow.push("error");
  } finally {
    if (seq === searchSeq) {
      state.loading = false;
      remember(addRecord({
        query,
        mode: "AI",
        engine: "AI",
        reason: "",
        parent: options.parent,
        sid: options.sid,
        send: !limited, // 回数制限で止まった検索は Notion に送らない（評価も求めない）
        flow: flow.join(" → "),
        calls: usage.calls,
        prompt: usage.prompt,
        output: usage.output,
        thoughts: usage.thoughts,
        model: [...usage.models].join(" "),
        shown: state.hits || [],
        seconds: elapsed(),
      }));
      render();
    }
  }
}

// ---------- 通常検索（AI なし） ----------
// 図表番号・空白で区切った短い単語 → 完全一致。文章のとき、または完全一致で0件のとき → 本文 BM25
async function normalSearch(query, seq, options, elapsed) {
  let engine = options.engine || "";
  let reason = options.reason || "";
  if (!engine) {
    if (GuideRag.hasFigureNumber(query)) [engine, reason] = ["exact", "図表番号"];
    else if (GuideRag.isSentence(query)) [engine, reason] = ["bm25", "文章のため自動"];
    else [engine, reason] = ["exact", "単語"];
  }
  state.passages = null;
  state.loading = false;
  if (engine === "exact") {
    state.hits = localHits(query);
    if (!state.hits.length && !options.engine) [engine, reason] = ["bm25", "0件のため自動"];
  }
  if (engine === "bm25") {
    state.hits = null;
    state.loading = true;
    state.normal = { engine, reason };
    render();
    let guide;
    try {
      guide = await ensureGuide();
    } catch {
      if (seq !== searchSeq) return;
      state.loading = false;
      state.error = "ガイドライン本文のデータを読み込めませんでした。";
      render();
      return;
    }
    if (seq !== searchSeq) return;
    const req = guide.request(query, state.kind, state.chapter);
    state.passages = req.hits.slice(0, 3).map(([, chunk]) => guide.passage(chunk, query));
    state.hits = req.ranked.slice(0, NORMAL_BM25_LIMIT).map(([row, , chunk]) => ({
      ...row,
      reason: "",
      basis: chunk ? guide.passage(chunk, query) : null,
    }));
    state.loading = false;
  }
  state.normal = { engine, reason };
  remember(addRecord({
    query,
    mode: "通常",
    engine: engine === "exact" ? "完全一致" : "本文BM25",
    reason,
    parent: options.parent,
    sid: options.sid,
    flow: "",
    shown: state.hits,
    seconds: elapsed(),
  }));
  render();
}

// 「本文からも探す」「完全一致に戻す」：同じ言葉で検索をやり直す（新しい行として記録する）
function switchNormal(engine) {
  const query = state.query;
  const parent = state.record?.seq;
  const reason = engine === "bm25" ? "「本文からも探す」ボタン" : "「完全一致に戻す」ボタン";
  afterRating(() => {
    qInput.value = query;
    search({ engine, reason, parent });
  });
}

function normalNoteHtml() {
  if (state.mode === "ai" || !state.normal || state.normal.engine !== "bm25") return "";
  const why = state.normal.reason === "0件のため自動" ? "（言葉どおりの図表がなかったため）" : "";
  return `<p class="agent-note">本文から探しました${why}。</p>`;
}

function switchHtml() {
  if (state.mode === "ai" || !state.normal || state.loading) return "";
  if (state.normal.engine === "exact") {
    return `<div class="switch"><button type="button" data-switch="bm25">本文からも探す</button></div>`;
  }
  if (state.normal.reason === "0件のため自動") return "";
  return `<div class="switch"><button type="button" data-switch="exact">完全一致に戻す</button></div>`;
}

// 確認（clarify）の選択肢を選んだら、元の質問に足して新しい質問としてやり直す
function answerClarify(option) {
  const base = state.query.trim();
  const parent = state.record?.seq;
  updateRecord({ flow: "clarify → 選択肢" });
  qInput.value = option ? `${base} ${option}` : base;
  search({ clarified: true, parent });
}

// 「このまま検索」：1回目に選んだ図表をそのまま出す（AI は呼ばない）
function skipClarify() {
  state.hits = state.pending || [];
  state.clarify = null;
  state.pending = null;
  updateRecord({ flow: "clarify → このまま検索", shown: shownNumbers(state.hits) });
  render();
}

// ---------- 試験評価のための記録（公開サイトでは担当者の Notion） ----------

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // 保存できない環境では記録しない
  }
}

function randomId() {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// 開くたびに説明を出す。実務経験を選んで「わかりました」を押すまで使えない
function startNotice() {
  if (accepted) return;
  const notice = document.getElementById("first-notice");
  const behind = document.querySelectorAll("body > header, body > main");
  behind.forEach((element) => { element.inert = true; });
  notice.hidden = false;
  const button = document.getElementById("first-notice-ok");
  // ブラウザに古い index.html が残っていても選べるように、無ければここで作る
  if (!document.querySelector('input[name="experience"]')) {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "experience-choice";
    fieldset.innerHTML = "<legend>実務経験</legend>" + EXPERIENCES.map(
      (value) => `<label><input type="radio" name="experience" value="${value}">${value}</label>`
    ).join("");
    button.before(fieldset);
  }
  const choices = [...document.querySelectorAll('input[name="experience"]')];
  button.disabled = true;
  for (const choice of choices) choice.addEventListener("change", () => { button.disabled = false; });
  button.addEventListener("click", () => {
    const chosen = choices.find((choice) => choice.checked);
    if (!chosen) return;
    experience = chosen.value;
    accepted = true;
    notice.hidden = true;
    behind.forEach((element) => { element.inert = false; });
    qInput.focus();
  });
  (choices[0] || button).focus();
}

// 端末ID：初回に作る乱数（名前や会社名は入れない）
function deviceId() {
  let id = storageGet(DEVICE_KEY);
  if (!id) {
    id = randomId();
    storageSet(DEVICE_KEY, id);
  }
  return id;
}

function nextSeq() {
  const seq = (Number(storageGet(SEQ_KEY)) || 0) + 1;
  storageSet(SEQ_KEY, String(seq));
  return seq;
}

// 表示した図表番号（上位5件）
function shownNumbers(rows) {
  return (rows || []).filter(passesFilter).slice(0, 5).map((row) => row.number || "").filter(Boolean).join(" ");
}

function remember(record) {
  state.record = record;
}

function addRecord({ sid, send = true, query, mode, engine, reason, parent, flow, calls = 0, prompt = 0, output = 0, thoughts = 0, model = "", shown, seconds }) {
  const record = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    time: localTime(new Date()),
    sid,
    device: deviceId(),
    experience,
    seq: nextSeq(),
    parent: parent ?? "",
    mode,
    engine,
    reason,
    query,
    flow,
    calls,
    model,
    prompt,
    output,
    thoughts,
    shown: shownNumbers(shown),
    opened: "",
    feedback: "",
    seconds,
  };
  if (send) sendRecord(record);
  // 保存を止めた検索には、評価を求めない
  return { ...record, rateable: send };
}

// Notion に送る。送れなくても検索は止めない（端末に控えが無いので、画面で知らせる）。
// 混雑時のやり直しは中継がする。
async function postLog(body) {
  try {
    const response = await fetch(NOTION_LOG, {
      method: "POST",
      keepalive: true, // 検索の直後に画面を閉じても届くように
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.ok) return await response.json();
  } catch {
    // 下で知らせる
  }
  logWarn.textContent = "Notion に記録を保存できませんでした。";
  return null;
}

function sendRecord(record) {
  if (!NOTION_LOG) return;
  const { id, query, ...rest } = record;
  // 長い貼り付けで中継の上限を超えないように切る
  const sent = { ...rest, query: query.slice(0, 300), cost: costYen(record) };
  remoteLog.set(id, postLog({ record: sent }).then((data) => data?.handle || ""));
}

// あとから変わった列（評価・開いた図表など）は、行ができるのを待ってから順に直す
function sendUpdate(id, patch) {
  if (!remoteLog.has(id)) return;
  remoteLog.set(id, remoteLog.get(id).then(async (handle) => {
    if (handle) await postLog({ handle, patch });
    return handle;
  }));
}

function updateRecord(patch) {
  const record = state.record;
  if (!record) return;
  const changed = typeof patch === "function" ? patch(record) : patch;
  Object.assign(record, changed);
  sendUpdate(record.id, changed);
}

function localTime(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// 費用の目安（円）。単価と為替は data/pricing.json から読む（コードに数字を書かない）。
// 出力の単価は「出力 token ＋ 思考 token」に掛ける。単価は検索した日で選ぶ。分からなければ空欄（0円と書かない）
function costYen({ time, model, prompt, output, thoughts }) {
  if (!prompt && !output && !thoughts) return (0).toFixed(2);
  const p = state.pricing;
  if (!p || (model && model !== p.model)) return "";
  const day = String(time || "").slice(0, 10);
  const price = (p.periods || []).find((x) => (!x.from || day >= x.from) && (!x.until || day <= x.until));
  if (!price) return "";
  const usd = (Number(prompt) || 0) / 1e6 * Number(price.input_usd_per_million) +
    ((Number(output) || 0) + (Number(thoughts) || 0)) / 1e6 * Number(price.output_usd_per_million);
  return (usd * Number(p.jpy_per_usd)).toFixed(2);
}

// ---------- 評価のポップアップ ----------
// 結果が出たら画面下に出す（結果は見られる）。未評価で次へ進もうとしたら中央に出して止める。
// 評価したら結果を消して、止めていた操作を続ける。
function needsRating() {
  return Boolean(state.record?.rateable && !state.record.feedback && !state.loading && !state.clarify);
}

function afterRating(action) {
  if (!needsRating()) {
    action();
    return;
  }
  afterRate = action;
  renderRating();
  // ボタンは選んだ状態にしない（Enter の二度押しで「役立った」が入らないように）
  rateEl.querySelector("#rate-text").focus();
}

function renderRating() {
  const shown = needsRating();
  if (!shown) afterRate = null;
  const blocking = Boolean(afterRate);
  rateEl.hidden = !shown;
  rateEl.classList.toggle("is-blocking", blocking);
  rateEl.setAttribute("aria-modal", String(blocking));
  rateEl.querySelector("#rate-text").textContent = blocking ? "先に、この検索の評価をお願いします" : "この結果は役に立ちましたか";
  rateEl.querySelector("[data-rate-back]").hidden = !blocking;
  document.body.classList.toggle("has-rate", shown);
  // 説明を出している間は、後ろの操作止めに手を触れない
  if (accepted) {
    document.querySelectorAll("body > header, body > main").forEach((element) => { element.inert = blocking; });
  }
}

function rate(feedback) {
  if (!needsRating()) return;
  const action = afterRate;
  updateRecord({ feedback });
  qInput.value = "";
  clearResults();
  window.scrollTo(0, 0);
  if (action) {
    action();
    return;
  }
  qInput.focus();
  statusEl.textContent = "評価を受け付けました。次の検索ができます。";
}

function backToResults() {
  afterRate = null;
  renderRating();
}

rateEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-feedback]");
  if (button) rate(button.dataset.feedback);
  else if (event.target.closest("[data-rate-back]")) backToResults();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && afterRate) backToResults();
});

// Notion で開いた図表番号を、その検索の行に足す
function recordOpened(number) {
  if (!number) return;
  updateRecord((record) => {
    const opened = String(record.opened || "").split(" ").filter(Boolean);
    if (!opened.includes(number)) opened.push(number);
    return { opened: opened.join(" ") };
  });
}

// AI 検索の途中経過（確認・探し直し・基準値の案内）
function agentHtml() {
  if (state.mode !== "ai") return "";
  const parts = [];
  if (state.agentNote) parts.push(`<p class="agent-note">${escapeHtml(state.agentNote)}</p>`);
  if (state.retried) parts.push(`<p class="agent-note">言い換えて探し直しました：${escapeHtml(state.retried)}</p>`);
  if (state.standards) {
    const { substance, medium } = state.standards;
    const what = [substance, medium].filter(Boolean).join("・");
    parts.push(
      `<div class="agent-box"><p>${what ? `${escapeHtml(what)}の` : ""}基準値は、法令の基準値アプリで確かめてください。` +
        `${substance ? `（アプリで「${escapeHtml(substance)}」を探してください）` : ""}</p>` +
        `<p><a href="${STANDARDS_URL}" target="_blank" rel="noopener">基準値アプリを開く</a></p></div>`
    );
  }
  if (state.clarify) {
    const options = state.clarify.options
      .map((option) => `<button type="button" data-clarify="${escapeHtml(option)}">${escapeHtml(option)}</button>`)
      .join("");
    parts.push(
      `<div class="agent-box clarify"><p>${escapeHtml(state.clarify.question)}</p>` +
        `<div class="clarify-options">${options}<button type="button" data-clarify-skip>このまま検索</button></div></div>`
    );
  }
  return parts.join("");
}

let guidePromise = null;

// ガイドライン本文（data/chunks.json）を読み、検索の索引を作る。最初の1回だけ
function ensureGuide() {
  if (!guidePromise) {
    guidePromise = (async () => {
      const [chunks, synonyms] = await Promise.all([
        fetch("data/chunks.json").then((response) => {
          if (!response.ok) throw new Error(String(response.status));
          return response.json();
        }),
        fetch("data/synonyms.json").then((response) => (response.ok ? response.json() : {})),
      ]);
      const guide = new GuideRag.Guideline(chunks, state.rows, synonyms);
      guide.chunkById = new Map(chunks.map((chunk) => [chunk.id, chunk]));
      return guide;
    })().catch((error) => {
      guidePromise = null;
      throw error;
    });
  }
  return guidePromise;
}

function pageLabel(passage) {
  if (passage.page && /^\d+$/.test(String(passage.page))) return `p.${passage.page}`;
  if (passage.page) return String(passage.page);
  return `PDF ${passage.pdf}ページ目`;
}

function whereLabel(passage) {
  const number = passage.no && !passage.no.startsWith("A") ? `${passage.no} ` : "";
  return `${number}${passage.path.slice(-2).join(" › ")}（${pageLabel(passage)}）`;
}

function passagesHtml(tokens) {
  if (!state.passages || !state.passages.length) return "";
  if (state.mode !== "ai" && state.normal?.engine !== "bm25") return "";
  const items = state.passages
    .map(
      (passage) => `<li><span class="passage-where">${escapeHtml(whereLabel(passage))}</span>` +
        `<span class="passage-quote">${highlight(`${passage.quote}…`, tokens)}</span></li>`
    )
    .join("");
  return `<section class="passages" aria-label="本文の該当箇所"><h2>本文の該当箇所</h2><ol>${items}</ol></section>`;
}

function render() {
  renderRating();
  const tokens = tokenize(state.query);
  clearBtn.hidden = state.query.trim() === "";
  if (!state.query.trim()) {
    statusEl.textContent = "内容を入れて検索すると、該当する図表を表示します。";
    resultsEl.innerHTML = "";
    return;
  }
  if (state.loading && state.mode !== "ai") {
    statusEl.textContent = `「${state.query.trim()}」 ガイドライン本文から探しています。`;
    resultsEl.innerHTML = "";
    return;
  }
  if (state.mode === "ai" && state.loading) {
    statusEl.textContent = state.passages
      ? `「${state.query.trim()}」 本文の該当箇所から、図表を選んでいます。`
      : `「${state.query.trim()}」 ガイドライン本文から探しています。`;
    resultsEl.innerHTML = agentHtml() + passagesHtml(tokens);
    return;
  }
  if (state.mode === "ai" && state.clarify) {
    statusEl.textContent = `「${state.query.trim()}」 どちらの意味か選んでください。`;
    resultsEl.innerHTML = agentHtml() + passagesHtml(tokens);
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
  const note = state.error && state.hits.length ? ` ${state.error}` : "";
  statusEl.textContent = `「${state.query.trim()}」 ${matched.length}件${note}`;

  if (matched.length === 0) {
    const detail = state.mode === "ai"
      ? "ガイドラインの構成からは、合う図表を選べませんでした。"
      : "言い方を変えるか、章の指定を外してみてください。";
    resultsEl.innerHTML = `${agentHtml()}${normalNoteHtml()}${passagesHtml(tokens)}
      <div class="empty">
        <h2>見つかりませんでした</h2>
        <p>${detail}</p>
      </div>${switchHtml()}`;
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
          : snippet && !row.basis
            ? `<span class="snippet">${highlight(snippet, tokens)}</span>`
            : ""
      }${row.basis ? `<span class="basis">根拠：${escapeHtml(whereLabel(row.basis))}</span>` : ""}${thumbFor(row)}</td>
      <td>${highlight(row.chapter || "—", tokens)}</td>
      <td>${highlight(row.section || "—", tokens)}</td>
      <td>${highlight(row.item || row.itemNo || "—", tokens)}</td>
      <td class="page-cell">${row.page == null || row.page === "" ? "—" : escapeHtml(String(row.page))}</td>
      <td class="actions">
        <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener" data-open="${escapeHtml(row.number || "")}">Notionで開く</a>
        <button type="button" data-copy="${escapeHtml(cite)}">引用をコピー</button>
      </td>
    </tr>`;
  }).join("");

  resultsEl.innerHTML = `${agentHtml()}${normalNoteHtml()}${passagesHtml(tokens)}<div class="table-wrap"><table>
    <caption class="visually-hidden">検索結果</caption>
    <thead><tr>
      <th>図表番号</th><th>図表名</th><th>章</th><th>節</th><th>項</th><th>ページ</th><th>操作</th>
    </tr></thead>
    <tbody>${body}</tbody>
  </table></div>${switchHtml()}`;
  loadThumbs();
}

function thumbFor(row) {
  if (!canShowThumbs()) return "";
  const id = pageId(row.url);
  const label = row.number || "図表";
  return `<button type="button" class="thumb" data-preview="${escapeHtml(id)}"><img alt="${escapeHtml(label)}"></button>`;
}

function canShowThumbs() {
  if (localAi) return Boolean(notionToken());
  return Boolean(publicAi);
}

const THUMB_CACHE_MAX = 120; // 端末に持っておく画像の数
const THUMB_PARALLEL = 4; // 同時に読む数
const THUMB_RETRY_MS = [2000, 5000, 15000, 30000]; // Notion が混んでいたときの待ち時間
const thumbCache = new Map(); // 図表のページ ID → blob の URL の約束
let previewUrls = [];
let thumbObserver = null;

function notionToken() {
  if (!localAi) return "";
  return localStorage.getItem("notionToken") || "";
}

function applyNotion() {
  const on = Boolean(notionToken());
  document.getElementById("notion-setup").hidden = on;
  document.getElementById("notion-on").hidden = !on;
}

function revokePreviews() {
  for (const url of previewUrls) URL.revokeObjectURL(url);
  previewUrls = [];
  thumbCache.clear();
  const preview = document.getElementById("preview");
  if (preview?.open) preview.close();
}

async function fetchThumb(id) {
  const token = notionToken();
  const path = `figure?id=${encodeURIComponent(id)}`;
  for (let attempt = 0; ; attempt++) {
    const response = await fetch(token ? `/api/${path}` : `${publicAi}/${path}`, {
      headers: token ? { "X-Notion-Token": token } : {},
    });
    if (response.ok) {
      const url = URL.createObjectURL(await response.blob());
      previewUrls.push(url);
      return url;
    }
    if (response.status !== 429 || attempt >= THUMB_RETRY_MS.length) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "画像を読めませんでした。");
    }
    await new Promise((resolve) => setTimeout(resolve, THUMB_RETRY_MS[attempt]));
  }
}

// 画面に近づいた画像から読む（通常検索は30件出るので、一度に読むと Notion の回数制限に当たる）。
// Gemini は呼ばない。中継の回数制限にも数えない。
function loadThumbs() {
  thumbObserver?.disconnect();
  thumbObserver = null;
  if (!canShowThumbs()) return;
  if (previewUrls.length > THUMB_CACHE_MAX) revokePreviews();
  const waiting = [];
  let active = 0;
  let failed = "";
  let reported = false;

  const show = async (button) => {
    const id = button.dataset.preview;
    try {
      if (!thumbCache.has(id)) thumbCache.set(id, fetchThumb(id));
      const url = await thumbCache.get(id);
      const image = button.querySelector("img");
      if (image) image.src = url;
    } catch (error) {
      thumbCache.delete(id);
      if (!button.isConnected) return;
      button.remove();
      failed = error instanceof TypeError ? "画像を読めませんでした。" : error.message;
    }
  };
  const next = () => {
    while (active < THUMB_PARALLEL && waiting.length) {
      const button = waiting.shift();
      if (!button.isConnected) continue; // もう別の検索に変わった
      active += 1;
      show(button).finally(() => {
        active -= 1;
        next();
        // 読もうとした画像が全部だめだったときだけ知らせる
        if (active || !failed || reported || resultsEl.querySelector(".thumb img[src]")) return;
        reported = true;
        statusEl.textContent = `${statusEl.textContent} ${failed}`;
      });
    }
  };
  const want = (button) => {
    if (thumbCache.has(button.dataset.preview)) show(button);
    else waiting.push(button);
  };

  const buttons = [...resultsEl.querySelectorAll("[data-preview]")];
  if (!("IntersectionObserver" in window)) {
    buttons.forEach(want);
    next();
    return;
  }
  thumbObserver = new IntersectionObserver((entries, observer) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      observer.unobserve(entry.target);
      want(entry.target);
    }
    next();
  }, { rootMargin: "250px" });
  buttons.forEach((button) => thumbObserver.observe(button));
}

function storedKey() {
  if (localStorage.getItem("geminiConnected") !== "1") return "";
  return localStorage.getItem("geminiApiKey") || "";
}

function showConnected(connected) {
  aiSetup.hidden = connected;
  aiConnected.hidden = !connected;
  if (connected) aiKeyStatus.textContent = "";
}

function disconnectKey() {
  localStorage.removeItem("geminiApiKey");
  localStorage.removeItem("geminiConnected");
  apiKeyInput.value = "";
  aiKeyStatus.textContent = "";
  showConnected(false);
  cancelSearch();
  apiKeyInput.focus();
}

async function checkConnection() {
  if (!localAi) return;
  const apiKey = apiKeyInput.value.trim();
  if (!apiKey) {
    aiKeyStatus.textContent = "APIキーを入力してください。";
    return;
  }
  aiCheckBtn.disabled = true;
  aiKeyStatus.textContent = "接続を確認しています。";
  try {
    const response = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apiKey }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "接続できませんでした。");
    localStorage.setItem("geminiApiKey", apiKey);
    localStorage.setItem("geminiConnected", "1");
    apiKeyInput.value = "";
    showConnected(true);
  } catch (error) {
    localStorage.removeItem("geminiConnected");
    aiKeyStatus.textContent = error.message === "Failed to fetch"
      ? "Gemini に接続できませんでした。"
      : (error.message || "接続できませんでした。");
  } finally {
    aiCheckBtn.disabled = false;
  }
}

function applyMode() {
  const ai = state.mode === "ai";
  aiKeyBox.hidden = !ai || !localAi;
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

// 比べる形（NFKC・小文字）に直した文と、その各文字が元の文のどこからどこまでか。
// 「…」→「...」、「㎡」→「m2」、「ｶﾞ」→「ガ」のように長さが変わるので、位置をそのまま使うとずれる。
function foldWithPositions(raw) {
  let folded = "";
  const starts = [];
  const ends = [];
  for (const found of raw.matchAll(/.[\p{M}\uFF9E\uFF9F]*/gsu)) {
    const part = found[0].normalize("NFKC").toLowerCase();
    folded += part;
    for (let i = 0; i < part.length; i += 1) {
      starts.push(found.index);
      ends.push(found.index + found[0].length);
    }
  }
  return { folded, starts, ends };
}

function highlight(text, tokens) {
  const raw = text == null ? "" : String(text);
  if (!raw || tokens.length === 0) return escapeHtml(raw);
  const { folded, starts, ends } = foldWithPositions(raw);
  const ranges = [];
  for (const token of tokens) {
    const needle = token.normalize("NFKC").toLowerCase();
    if (!needle) continue;
    let from = 0;
    while (from < folded.length) {
      const at = folded.indexOf(needle, from);
      if (at < 0) break;
      ranges.push([starts[at], ends[at + needle.length - 1]]);
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
