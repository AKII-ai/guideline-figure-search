"""図表検索のローカルサーバ。静的ファイルと Gemini 検索を出す。"""

import json
import re
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
FIGURES = json.loads((ROOT / "data" / "figures.json").read_text(encoding="utf-8"))
MODELS = ("gemini-2.5-flash", "gemini-flash-latest")
HOST = "127.0.0.1"
PORT = 8765
NOTION_VERSION = "2022-06-28"
IMAGE_CACHE = {}
IMAGE_LOCK = threading.Lock()


def norm_num(value):
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"\s+", "", text)
    return text.replace("‐", "-").replace("－", "-").replace("ー", "-")


BY_ID = {}
BY_NUMBER = {}
for row in FIGURES:
    BY_ID[row["url"].rstrip("/").split("/")[-1]] = row
    BY_NUMBER.setdefault(norm_num(row.get("number")), []).append(row)


def page_id(url):
    return url.rstrip("/").split("/")[-1]


def focus(text, query, limit=520):
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    at = -1
    for token in re.split(r"\s+", query or ""):
        if not token:
            continue
        found = compact.find(token)
        if found >= 0 and (at < 0 or found < at):
            at = found
    if at < 0:
        return compact[:limit]
    start = max(0, at - 90)
    return compact[start:start + limit]


def catalog(kind, chapter, ids=None, query=""):
    wanted = {item for item in (ids or []) if item}
    lines = []
    for row in FIGURES:
        item_id = page_id(row["url"])
        if wanted and item_id not in wanted:
            continue
        if kind and row.get("kind") != kind:
            continue
        chapter_name = row.get("chapter") or ""
        if chapter == "__none__":
            if chapter_name:
                continue
        elif chapter and chapter_name != chapter:
            continue
        mention = focus(row.get("context") or "", query) if wanted else ""
        description = " ".join((row.get("description") or "").split())
        if wanted and description and description not in mention:
            mention = f"{mention} {description[:180]}".strip()
        lines.append(
            "\t".join(
                [
                    page_id(row["url"]),
                    row.get("kind") or "",
                    row.get("number") or "",
                    str(row.get("page") or ""),
                    chapter_name,
                    row.get("section") or "",
                    row.get("item") or "",
                    row.get("name") or "",
                    mention,
                ]
            )
        )
    return "\n".join(lines)


def redact(text, api_key):
    if api_key and api_key in text:
        return text.replace(api_key, "***")
    return text


class NotionImageError(Exception):
    def __init__(self, status):
        super().__init__(str(status))
        self.status = status


def hyphenate(page_id):
    text = page_id.replace("-", "")
    return f"{text[0:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:32]}"


def clean_token(token):
    token = token.strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def notion_image_url(page_id, token):
    url = f"https://api.notion.com/v1/blocks/{hyphenate(page_id)}/children?page_size=20"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        error.read()
        raise NotionImageError(error.code) from error
    for block in payload.get("results") or []:
        if block.get("type") != "image":
            continue
        image = block.get("image") or {}
        kind = image.get("type")
        source = image.get(kind) or {}
        found = source.get("url")
        if found:
            return found
    return None


def load_figure_image(page_id, token):
    now = time.time()
    with IMAGE_LOCK:
        cached = IMAGE_CACHE.get(page_id)
        if cached and cached[0] > now:
            return cached[1], cached[2]
    source = notion_image_url(page_id, token)
    if not source:
        return None
    request = Request(source, headers={"User-Agent": "figure-search"})
    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        data = response.read(12_000_000)
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    with IMAGE_LOCK:
        IMAGE_CACHE[page_id] = (now + 240, content_type, data)
    return content_type, data


def check_gemini(api_key):
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": api_key},
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response.read(64)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise GeminiError(message_from_google(detail, api_key), error.code) from error
    except URLError as error:
        raise GeminiError("Gemini に接続できませんでした。", 502) from error


def resolve_id(item_id, allowed):
    item_id = str(item_id or "").strip()
    if item_id in allowed and item_id in BY_ID:
        return item_id
    for row in BY_NUMBER.get(norm_num(item_id), []):
        found = page_id(row["url"])
        if found in allowed:
            return found
    return None


def figure_map(kind, chapter):
    lines = []
    allowed = set()
    for row in FIGURES:
        if kind and row.get("kind") != kind:
            continue
        chapter_name = row.get("chapter") or ""
        if chapter == "__none__":
            if chapter_name:
                continue
        elif chapter and chapter_name != chapter:
            continue
        item_id = page_id(row["url"])
        allowed.add(item_id)
        description = " ".join((row.get("description") or "").split())[:120]
        lines.append(
            "\t".join(
                [
                    item_id,
                    row.get("number") or "",
                    chapter_name,
                    row.get("section") or "",
                    row.get("item") or "",
                    row.get("name") or "",
                    description,
                ]
            )
        )
    return "\n".join(lines), allowed


def ask_gemini(query, kind, chapter, api_key):
    listed, allowed = figure_map(kind, chapter)
    if not listed:
        return []
    prompt = (
        "これは土壌汚染対策法ガイドラインの図表の目次です。\n"
        "列は id, 図表番号, 章, 節, 項, 図表名, 説明。タブ区切り。\n"
        "質問は図表名と同じ言葉になっていないことがあります。\n"
        "言い換え（深度と深さ、ボーリングと試料採取など）と、章・節・項のまとまりから、質問が指す図を選んでください。\n"
        "言葉が含まれる図を機械的に並べないでください。その調査の流れの中で、質問の内容を説明する図を上にしてください。\n"
        "id は一覧の先頭列をそのまま返す。一覧にない id は返さない。最大8件。\n"
        "reason は日本語で1文。どの節の、どんな図か。\n\n"
        f"質問: {query}\n\n"
        f"目次:\n{listed}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 768,
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "matches": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "id": {"type": "STRING"},
                                "reason": {"type": "STRING"},
                            },
                            "required": ["id", "reason"],
                        },
                    }
                },
                "required": ["matches"],
            },
        },
    }
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        request = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8")
            break
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            last_error = (error.code, detail)
            if error.code == 404:
                continue
            raise GeminiError(message_from_google(detail, api_key), error.code) from error
        except URLError as error:
            raise GeminiError("Gemini に接続できませんでした。", 502) from error
    else:
        code, detail = last_error or (502, "")
        raise GeminiError(message_from_google(detail, api_key) or "Gemini のモデルを使えませんでした。", code)

    data = json.loads(raw)
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    matches = []
    seen = set()
    for item in parsed.get("matches") or []:
        item_id = resolve_id(item.get("id"), allowed)
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        matches.append({"id": item_id, "reason": str(item.get("reason") or "").strip()})
        if len(matches) >= 8:
            break
    return matches


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


class GeminiError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/figure":
            self.figure_image()
            return
        if path == "/":
            path = "/index.html"
        file_path = (ROOT / path.lstrip("/")).resolve()
        if not file_path.is_relative_to(ROOT) or not file_path.is_file():
            self.send_error(404)
            return
        content_type = "text/plain; charset=utf-8"
        if file_path.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif file_path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif file_path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif file_path.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/check":
            self.check_key()
            return
        if path != "/api/search":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json(400, {"error": "検索内容を読み取れませんでした。"})
            return
        query = str(payload.get("query") or "").strip()
        api_key = str(payload.get("apiKey") or "").strip()
        if not query:
            self.send_json(400, {"error": "検索する内容を入力してください。"})
            return
        if not api_key:
            self.send_json(400, {"error": "Gemini APIキーを入力してください。"})
            return
        try:
            matches = ask_gemini(
                query,
                str(payload.get("kind") or ""),
                str(payload.get("chapter") or ""),
                api_key,
            )
        except GeminiError as error:
            self.send_json(error.status if 400 <= error.status < 600 else 502, {"error": str(error)})
            return
        except (KeyError, IndexError, json.JSONDecodeError):
            self.send_json(502, {"error": "Gemini の応答を読み取れませんでした。"})
            return
        self.send_json(200, {"matches": matches})

    def figure_image(self):
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        page_id = ""
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "id":
                page_id = value
        page_id = page_id.replace("-", "")
        token = clean_token(self.headers.get("X-Notion-Token") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", page_id) or not token:
            self.send_json(404, {"error": "画像の取得先が分かりません。"})
            return
        try:
            loaded = load_figure_image(page_id.lower(), token)
        except NotionImageError as error:
            print(f"figure notion {error.status}", flush=True)
            message = {
                401: "Notionトークンが違います。Configuration のアクセストークンを貼り直してください。",
                403: "「コンテンツを読み取る」をオンにして、エンジニアリングシートにこの接続を追加してください。",
                404: "エンジニアリングシートを開き、右上の…から「接続」で、この内部接続を追加してください。",
            }.get(error.status, "Notionから画像を読めませんでした。")
            self.send_json(error.status if 400 <= error.status < 500 else 502, {"error": message})
            return
        except (URLError, TimeoutError, json.JSONDecodeError, KeyError):
            self.send_json(502, {"error": "Notionから画像を読めませんでした。"})
            return
        if not loaded:
            self.send_json(404, {"error": "このページに画像がありません。"})
            return
        content_type, data = loaded
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=120")
        self.end_headers()
        self.wfile.write(data)

    def check_key(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json(400, {"error": "接続内容を読み取れませんでした。"})
            return
        api_key = str(payload.get("apiKey") or "").strip()
        if not api_key:
            self.send_json(400, {"error": "Gemini APIキーを入力してください。"})
            return
        try:
            check_gemini(api_key)
        except GeminiError as error:
            self.send_json(error.status if 400 <= error.status < 600 else 502, {"error": str(error)})
            return
        self.send_json(200, {"ok": True})

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        message = fmt % args
        if "api/search" in message or "api/check" in message or "key" in message.lower():
            return
        super().log_message("%s", message)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT}/")
    server.serve_forever()
