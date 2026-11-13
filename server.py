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
import hmac
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
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
INBOX = os.path.join(ROOT, "inbox")
FORUM_PATH = os.path.join(ROOT, "forum.json")
FORUM_LOCK = threading.Lock()
MAX_BODY = 14 * 1024 * 1024  # ~14 МБ JSON (фото до 8 МБ превращается в ~10.7 МБ base64)

FORUM_KINDS = ("question", "idea", "wish", "bug", "coop", "other")
FORUM_STATUSES = ("pending", "progress", "accepted", "declined", "done")
FORUM_KIND_LABELS = {
    "question": "Вопрос", "idea": "Идея", "wish": "Пожелание",
    "bug": "Баг", "coop": "Сотрудничество", "other": "Другое",
}
FORUM_STATUS_LABELS = {
    "pending": "На рассмотрении", "progress": "В работе",
    "accepted": "Принято", "declined": "Отказано", "done": "Решено",
}

DEFAULT_CONFIG = {
    "port": 8787,
    "bot_token": "",
    "chat_id": "",
    "admin_password": "korosten-admin",
    "note": "Вставь токен бота от @BotFather и свой chat_id. chat_id можно узнать у @userinfobot. Пароль создателя форума — admin_password."
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


def load_forum():
    try:
        with open(FORUM_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return []
    if isinstance(data, dict) and isinstance(data.get("topics"), list):
        return data["topics"]
    return []


def save_forum(topics):
    tmp = FORUM_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"topics": topics}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FORUM_PATH)


def admin_ok(cfg, password):
    expected = str(cfg.get("admin_password") or "")
    given = str(password or "")
    if not expected or not given:
        return False
    return hmac.compare_digest(expected, given)


def notify_forum(cfg, topic):
    token = str(cfg.get("bot_token") or "").strip()
    chat_id = str(cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return
    text = "📝 Новая тема на форуме · %s\n\n%s\n\n%s\n\n— %s\n%s" % (
        FORUM_KIND_LABELS.get(topic.get("kind"), "Другое"),
        topic.get("title", ""),
        topic.get("text", ""),
        topic.get("name", "аноним"),
        topic.get("created", ""),
    )
    try:
        tg_multipart(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        })
    except Exception as e:
        print("[forum] telegram:", repr(e))


def handle_forum(payload, cfg):
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "Неверный формат данных"}
    action = str(payload.get("action") or "create").strip()

    if action == "login":
        if admin_ok(cfg, payload.get("password")):
            return ok_json({"ok": True})
        return 403, {"ok": False, "error": "Неверный пароль"}

    if action == "create":
        title = (payload.get("title") or "").strip()[:120]
        text = (payload.get("text") or "").strip()[:3000]
        if len(title) < 3 or len(text) < 3:
            return 400, {"ok": False, "error": "Слишком короткая тема"}
        kind = str(payload.get("kind") or "other").strip()
        if kind not in FORUM_KINDS:
            kind = "other"
        name = (payload.get("name") or "").strip()[:60] or "аноним"
        topic = {
            "id": uuid.uuid4().hex[:10],
            "title": title,
            "text": text,
            "kind": kind,
            "name": name,
            "status": "pending",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        with FORUM_LOCK:
            topics = load_forum()
            topics.insert(0, topic)
            topics = topics[:500]
            save_forum(topics)
        notify_forum(cfg, topic)
        return ok_json({"ok": True, "topic": topic})

    if action in ("status", "delete"):
        if not admin_ok(cfg, payload.get("password")):
            return 403, {"ok": False, "error": "Нужен пароль создателя"}
        topic_id = str(payload.get("id") or "").strip()
        if not topic_id:
            return 400, {"ok": False, "error": "Не указана тема"}
        with FORUM_LOCK:
            topics = load_forum()
            found = next((x for x in topics if x.get("id") == topic_id), None)
            if found is None:
                return 404, {"ok": False, "error": "Тема не найдена"}
            if action == "delete":
                topics = [x for x in topics if x.get("id") != topic_id]
            else:
                status = str(payload.get("status") or "").strip()
                if status not in FORUM_STATUSES:
                    return 400, {"ok": False, "error": "Неизвестный статус"}
                found["status"] = status
                found["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            save_forum(topics)
        return ok_json({"ok": True, "topic": found if action == "status" else None})

    return 400, {"ok": False, "error": "Неизвестное действие"}


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
        if self.path.startswith("/api/forum"):
            with FORUM_LOCK:
                topics = load_forum()
            return self.json_response({"ok": True, "topics": topics})
        super().do_GET()

    def do_POST(self):
        is_send = self.path.startswith("/api/send")
        is_forum = self.path.startswith("/api/forum")
        if not (is_send or is_forum):
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
            if is_forum:
                code, data = handle_forum(payload, load_config())
            else:
                code, data = handle_send(payload, load_config())
            return self.json_response(data, code)
        except Exception as e:
            print("[api] ошибка:", repr(e))
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
