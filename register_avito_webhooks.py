"""Register the bridge webhook URL for one or more Avito API profiles."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from avito_telegram_bridge import load_dotenv


API_BASE = "https://api.avito.ru"


@dataclass(frozen=True)
class Account:
    name: str
    client_id: str
    client_secret: str


def request_json(url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    encoded = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {error.code}: {details}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Сетевая ошибка: {error.reason}") from error


def get_token(account: Account) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": account.client_id,
            "client_secret": account.client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/token/",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Авторизация Avito: HTTP {error.code}: {details}") from error
    token = result.get("access_token")
    if not token:
        raise RuntimeError("Avito не вернул access_token")
    return str(token)


def load_accounts() -> list[Account]:
    raw = os.getenv("AVITO_ACCOUNTS_JSON", "[]")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("AVITO_ACCOUNTS_JSON содержит некорректный JSON") from error
    if not isinstance(values, list) or not values:
        raise ValueError("AVITO_ACCOUNTS_JSON должен содержать непустой JSON-массив")

    accounts = []
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"Профиль {index}: ожидается JSON-объект")
        missing = [key for key in ("name", "client_id", "client_secret") if not value.get(key)]
        if missing:
            raise ValueError(f"Профиль {index}: не заполнены {', '.join(missing)}")
        accounts.append(Account(str(value["name"]), str(value["client_id"]), str(value["client_secret"])))
    return accounts


def webhook_url() -> str:
    base = os.getenv("PUBLIC_WEBHOOK_BASE_URL", "").strip().rstrip("/")
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not base.startswith("https://"):
        raise ValueError("PUBLIC_WEBHOOK_BASE_URL должен начинаться с https://")
    if len(secret) < 32:
        raise ValueError("WEBHOOK_SECRET должен содержать не менее 32 символов")
    return f"{base}/webhooks/avito/{secret}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Регистрация webhook Avito для всех профилей")
    parser.add_argument("--dry-run", action="store_true", help="только проверить конфигурацию")
    args = parser.parse_args()
    load_dotenv()

    try:
        accounts = load_accounts()
        url = webhook_url()
    except ValueError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 2

    print(f"Профилей: {len(accounts)}")
    print(f"Webhook: {url}")
    if args.dry_run:
        print("Конфигурация корректна; запросы в Avito не отправлялись.")
        return 0

    failed = False
    for account in accounts:
        try:
            token = get_token(account)
            request_json(
                f"{API_BASE}/messenger/v3/webhook",
                data={"url": url},
                headers={"Authorization": f"Bearer {token}"},
            )
            print(f"✓ {account.name}: webhook подключён")
        except Exception as error:
            failed = True
            print(f"✗ {account.name}: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

