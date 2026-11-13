#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korosten Railways — локальный сервер сайта + приём идей/пожеланий.

Запуск:      python server.py
Без браузера: python server.py --no-open

Сообщения всегда сохраняются в папку inbox/.
Если в config.json заполнены bot_token и chat_id — они также уходят в Telegram.
"""

import base64
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
INBOX = os.path.join(ROOT, "inbox")
MAX_BODY = 14 * 1024 * 1024  # ~14 МБ JSON (фото до 8 МБ превращается в ~10.7 МБ base64)

DEFAULT_CONFIG = {
    "port": 8787,
    "bot_token": "",
    "chat_id": "",
    "note": "Вставь токен бота от @BotFather и свой chat_id. chat_id можно узнать у @userinfobot."
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    except (ValueError, OSError) as e:
        print("[config] не удалось прочитать config.json:", e)
    return cfg


def tg_multipart(token, method, fields, files=None):
    """Вызов Telegram Bot API через multipart/form-data без внешних зависимостей."""
    boundary = "----KorostenRailways" + uuid.uuid4().hex
    body = bytearray()

    for key, value in fields.items():
        body += ("--%s\r\n" % boundary).encode()
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode()
        body += str(value).encode("utf-8") + b"\r\n"

    for key, (filename, mime, data) in (files or {}).items():
        body += ("--%s\r\n" % boundary).encode()
        body += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (key, filename)).encode("utf-8")
        body += ("Content-Type: %s\r\n\r\n" % mime).encode()
        body += data + b"\r\n"

    body += ("--%s--\r\n" % boundary).encode()

    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ok_json(data):
    text = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return 200, text


def format_text(payload):
    kind = (payload.get("kind") or "Другое").strip()[:40]
    name = (payload.get("name") or "").strip()[:60] or "аноним"
    text = (payload.get("text") or "").strip()
    when = time.strftime("%d.%m.%Y %H:%M")
    if (payload.get("type") or "idea") == "coop":
        return "🤝 Запрос на сотрудничество · %s\n\n%s\n\n— %s\n%s" % (kind, text, name, when)
    return "🚆 Korosten Railways · %s\n\n%s\n\n— %s\n%s" % (kind, text, name, when)


def save_inbox(payload, photo_bytes, photo_ext):
    os.makedirs(INBOX, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    folder = os.path.join(INBOX, stamp + "_" + uuid.uuid4().hex[:6])
    os.makedirs(folder, exist_ok=True)

    with open(os.path.join(folder, "message.txt"), "w", encoding="utf-8") as f:
        f.write(format_text(payload))

    meta = dict(payload)
    meta.pop("photo", None)
    meta["has_photo"] = bool(photo_bytes)
    with open(os.path.join(folder, "data.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    if photo_bytes:
        with open(os.path.join(folder, "photo" + photo_ext), "wb") as f:
            f.write(photo_bytes)

    return folder


def ext_from_mime(mime):
    mapping = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic",
    }
    return mapping.get((mime or "").lower(), ".jpg")


def handle_send(payload, cfg):
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "Неверный формат данных"}

    text = (payload.get("text") or "").strip()
    if len(text) < 3:
        return 400, {"ok": False, "error": "Сообщение слишком короткое"}
    if len(text) > 3000:
        text = text[:3000]
        payload["text"] = text

    photo_bytes = None
    photo_ext = ""
    photo = payload.get("photo")
    if isinstance(photo, dict) and photo.get("data"):
        try:
            photo_bytes = base64.b64decode(photo["data"], validate=True)
        except Exception:
            return 400, {"ok": False, "error": "Не удалось прочитать фото"}
        if len(photo_bytes) > 8 * 1024 * 1024:
            return 400, {"ok": False, "error": "Фото больше 8 МБ"}
        photo_ext = ext_from_mime(photo.get("mime"))

    folder = save_inbox(payload, photo_bytes, photo_ext)

    token = str(cfg.get("bot_token") or "").strip()
    chat_id = str(cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return ok_json({"ok": True, "mode": "local", "id": os.path.basename(folder)})

    warnings = []
    sent = False
    try:
        res = tg_multipart(token, "sendMessage", {
            "chat_id": chat_id,
            "text": format_text(payload),
            "disable_web_page_preview": "true",
        })
        sent = bool(res.get("ok"))
        if not sent:
            warnings.append("Telegram: " + str(res.get("description", "ошибка sendMessage")))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("description", str(e))
        except Exception:
            detail = str(e)
        warnings.append("Telegram: " + detail)
    except Exception as e:
        warnings.append("Telegram: " + str(e))

    if photo_bytes:
        try:
            caption = "📷 Фото к сообщению · %s" % ((payload.get("kind") or "Другое")[:40])
            res = tg_multipart(
                token, "sendPhoto",
                {"chat_id": chat_id, "caption": caption},
                {"photo": ("photo" + photo_ext, (photo.get("mime") or "image/jpeg"), photo_bytes)},
            )
            if not res.get("ok"):
                warnings.append("Telegram: " + str(res.get("description", "ошибка sendPhoto")))
            else:
                sent = True
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("description", str(e))
            except Exception:
                detail = str(e)
            warnings.append("Фото: " + detail)
        except Exception as e:
            warnings.append("Фото: " + str(e))

    mode = "telegram" if sent else "local"
    result = {"ok": True, "mode": mode, "id": os.path.basename(folder)}
    if warnings:
        result["warning"] = " | ".join(warnings)[:500]
    return ok_json(result)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def end_headers(self):
        if self.path.endswith(".html") or self.path == "/":
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/status"):
            cfg = load_config()
            return self.json_response({"ok": True, "bot": bool(cfg.get("bot_token") and cfg.get("chat_id"))})
        super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/send"):
            self.send_error(404, "Not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self.json_response({"ok": False, "error": "Слишком большое сообщение или пустой запрос"}, 413)

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.json_response({"ok": False, "error": "Не удалось разобрать запрос"}, 400)

        try:
            code, data = handle_send(payload, load_config())
            return self.json_response(data, code)
        except Exception as e:
            print("[send] ошибка:", repr(e))
            return self.json_response({"ok": False, "error": "Внутренняя ошибка сервера"}, 500)

    def json_response(self, data, code=200):
        if isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        else:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    cfg = load_config()
    try:
        port = int(cfg.get("port") or 8787)
    except (TypeError, ValueError):
        port = 8787

    host = "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/" % (host, port)

    bot_ready = bool(str(cfg.get("bot_token") or "").strip() and str(cfg.get("chat_id") or "").strip())
    print("Korosten Railways · локальный сервер", flush=True)
    print("Сайт:  " + url, flush=True)
    print("Бот:   " + ("подключён" if bot_ready else "не настроен — сообщения пойдут в папку inbox/"), flush=True)
    print("Стоп:  Ctrl+C", flush=True)

    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
