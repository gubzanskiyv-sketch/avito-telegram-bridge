"""Receive Avito Messenger webhooks and forward them to a Telegram channel."""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


LOG = logging.getLogger("avito-telegram")
MAX_BODY_BYTES = 1_000_000


def load_dotenv(path: str = ".env") -> None:
    """Load a small, conventional .env file without overriding real env vars."""
    try:
        with open(path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if value[:1] == value[-1:] and value.startswith(("'", '"')):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    webhook_secret: str
    profile_names: dict[str, str]
    host: str = "0.0.0.0"
    port: int = 8080
    forward_outgoing: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_SECRET"]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError("Не заданы переменные: " + ", ".join(missing))

        secret = os.environ["WEBHOOK_SECRET"]
        if len(secret) < 32:
            raise ValueError("WEBHOOK_SECRET должен содержать не менее 32 символов")

        raw_names = os.getenv("PROFILE_NAMES_JSON", "{}")
        try:
            names = json.loads(raw_names)
        except json.JSONDecodeError as error:
            raise ValueError("PROFILE_NAMES_JSON содержит некорректный JSON") from error
        if not isinstance(names, dict):
            raise ValueError("PROFILE_NAMES_JSON должен быть JSON-объектом")

        return cls(
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            webhook_secret=secret,
            profile_names={str(key): str(value) for key, value in names.items()},
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8080")),
            forward_outgoing=env_bool("FORWARD_OUTGOING"),
        )


class EventDeduplicator:
    def __init__(self, ttl_seconds: int = 24 * 60 * 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        now = time.monotonic()
        with self._lock:
            expired = [key for key, seen_at in self._seen.items() if now - seen_at > self.ttl_seconds]
            for key in expired:
                self._seen.pop(key, None)
            if event_id in self._seen:
                return True
            self._seen[event_id] = now
            return False

    def forget(self, event_id: str) -> None:
        if not event_id:
            return
        with self._lock:
            self._seen.pop(event_id, None)


def _string(value: Any, fallback: str = "—") -> str:
    return fallback if value is None or value == "" else str(value)


def render_event(event: dict[str, Any], config: Config) -> str | None:
    payload = event.get("payload") or {}
    value = payload.get("value") or {}
    if not isinstance(payload, dict) or not isinstance(value, dict):
        return None

    user_id = _string(value.get("user_id"), "неизвестен")
    author_id = _string(value.get("author_id"), "неизвестен")
    if not config.forward_outgoing and user_id == author_id:
        return None

    profile_name = config.profile_names.get(user_id, f"Avito {user_id}")
    event_type = _string(payload.get("type"), "событие")
    message_type = _string(value.get("type"), "неизвестный тип")
    content = value.get("content") or {}
    text = content.get("text") if isinstance(content, dict) else None

    if text:
        body = _string(text)
    elif message_type == "image":
        body = "🖼 Изображение"
    elif message_type in {"voice", "audio"}:
        body = "🎙 Голосовое сообщение"
    elif message_type == "location":
        body = "📍 Геолокация"
    else:
        body = f"Событие типа «{message_type}»"

    def esc(value_to_escape: Any) -> str:
        return html.escape(_string(value_to_escape))

    return (
        f"<b>Новое сообщение Avito</b>\n"
        f"Профиль: <b>{esc(profile_name)}</b>\n"
        f"Отправитель: <code>{esc(author_id)}</code>\n"
        f"Чат: <code>{esc(value.get('chat_id'))}</code>\n\n"
        f"{html.escape(body)}\n\n"
        f"<i>{esc(event_type)} · {esc(message_type)}</i>"
    )


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15) -> None:
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Telegram вернул HTTP {error.code}: {details}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Telegram недоступен: {error.reason}") from error
        if not result.get("ok"):
            raise RuntimeError(f"Telegram отклонил сообщение: {result}")


class DeliveryWorker(threading.Thread):
    def __init__(self, events: queue.Queue[str], telegram: TelegramClient) -> None:
        super().__init__(name="telegram-delivery", daemon=True)
        self.events = events
        self.telegram = telegram
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                for attempt in range(1, 4):
                    try:
                        self.telegram.send(message)
                        LOG.info("Уведомление отправлено в Telegram")
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        LOG.warning("Ошибка Telegram, повтор %s/3", attempt + 1)
                        self.stop_event.wait(2 ** (attempt - 1))
            except Exception:
                LOG.exception("Не удалось отправить уведомление после трёх попыток")
            finally:
                self.events.task_done()


def make_handler(config: Config, events: queue.Queue[str], dedupe: EventDeduplicator):
    expected_path = f"/webhooks/avito/{config.webhook_secret}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "AvitoTelegramBridge/1.0"

        def log_message(self, format_string: str, *args: Any) -> None:
            # Do not put the request path in logs: it contains WEBHOOK_SECRET.
            status = args[1] if len(args) > 1 else "unknown"
            LOG.debug("HTTP response %s", status)

        def send_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self.send_json(200, {"ok": True, "queue_depth": events.qsize()})
            else:
                self.send_json(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path != expected_path:
                self.send_json(404, {"ok": False})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"ok": False, "error": "invalid content length"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self.send_json(413, {"ok": False, "error": "invalid body size"})
                return

            try:
                event = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_json(400, {"ok": False, "error": "invalid json"})
                return
            if not isinstance(event, dict):
                self.send_json(400, {"ok": False, "error": "invalid event"})
                return

            event_id = _string(event.get("id"), "")
            if not dedupe.is_duplicate(event_id):
                rendered = render_event(event, config)
                if rendered:
                    try:
                        events.put_nowait(rendered)
                    except queue.Full:
                        dedupe.forget(event_id)
                        self.send_json(503, {"ok": False, "error": "queue full"})
                        return
            self.send_json(200, {"ok": True})

    return Handler


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    config = Config.from_env()
    events: queue.Queue[str] = queue.Queue(maxsize=1000)
    worker = DeliveryWorker(events, TelegramClient(config.telegram_bot_token, config.telegram_chat_id))
    worker.start()

    server = ThreadingHTTPServer((config.host, config.port), make_handler(config, events, EventDeduplicator()))

    def shutdown(_signum: int, _frame: Any) -> None:
        LOG.info("Остановка сервиса")
        worker.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    LOG.info("Сервис запущен на %s:%s", config.host, config.port)
    try:
        server.serve_forever()
    finally:
        worker.stop_event.set()
        worker.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
