"""図表検索のローカルサーバ。静的ファイルと Gemini 検索を出す。"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
FIGURES = json.loads((ROOT / "data" / "figures.json").read_text(encoding="utf-8"))
MODELS = ("gemini-2.5-flash", "gemini-flash-latest")
HOST = "127.0.0.1"
PORT = 8765

BY_ID = {}
for row in FIGURES:
    BY_ID[row["url"].rstrip("/").split("/")[-1]] = row


def page_id(url):
    return url.rstrip("/").split("/")[-1]


def clip(text, limit=80):
    text = " ".join((text or "").split())
    return text[:limit]


def catalog(kind, chapter, ids=None):
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
        desc = clip(row.get("context") or row.get("description") or "", 160) if wanted else ""
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
                    desc,
                ]
            )
        )
    return "\n".join(lines)


def redact(text, api_key):
    if api_key and api_key in text:
        return text.replace(api_key, "***")
    return text


def ask_gemini(query, kind, chapter, api_key, ids=None):
    listed = catalog(kind, chapter, ids)
    if not listed:
        return []
    prompt = (
        "土壌汚染対策法ガイドラインの図表索引から、質問に必要な図表だけを選んでください。\n"
        "一覧にない id は返さない。関係が薄いものは入れない。最大8件。\n"
        "reason は日本語で1文。なぜその図表か。\n"
        "一覧の列は id, 種類, 図表番号, ページ, 章, 節, 項, 図表名, ガイドライン上の言及。タブ区切り。\n"
        "渡された候補だけを並べ替え、一覧にない id は返さない。\n\n"
        f"質問: {query}\n\n"
        f"一覧:\n{listed}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
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
            with urlopen(request, timeout=25) as response:
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
        item_id = str(item.get("id") or "").strip()
        if item_id not in BY_ID or item_id in seen:
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
        if self.path.split("?", 1)[0] != "/api/search":
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
        ids = payload.get("ids") if isinstance(payload.get("ids"), list) else None
        if not ids:
            self.send_json(400, {"error": "並べ替える候補がありません。"})
            return
        try:
            matches = ask_gemini(
                query,
                str(payload.get("kind") or ""),
                str(payload.get("chapter") or ""),
                api_key,
                [str(item) for item in ids[:30]] if ids else None,
            )
        except GeminiError as error:
            self.send_json(error.status if 400 <= error.status < 600 else 502, {"error": str(error)})
            return
        except (KeyError, IndexError, json.JSONDecodeError):
            self.send_json(502, {"error": "Gemini の応答を読み取れませんでした。"})
            return
        self.send_json(200, {"matches": matches})

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        message = fmt % args
        if "api/search" in message or "key" in message.lower():
            return
        super().log_message("%s", message)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT}/")
    server.serve_forever()
