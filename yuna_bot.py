#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ゆなちゃん大好きボット（常時オンライン版）

Discord に常時接続して「オンライン」表示を保ちながら、
config.json で指定したチャンネルへ一定間隔でメッセージを送信します。

    python yuna_bot.py            # 送信スタート
    python yuna_bot.py --once     # 1通だけ送って終了
    python yuna_bot.py --check    # 設定・トークン・チャンネルを確認（送信しない）

クラウドでは環境変数 DISCORD_BOT_TOKEN にトークンを設定してください。
PORT が設定されている場合は、ヘルスチェック用の HTTP サーバーも起動します。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    print(
        "requests が必要です。次のコマンドを実行してください:\n"
        "    pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

try:
    import discord
except ImportError:  # pragma: no cover
    print(
        "discord.py が必要です。次のコマンドを実行してください:\n"
        "    pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = BASE_DIR / "token.txt"

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "YunaDaisukiBot/2.0 (Discord bot)"
PLACEHOLDER_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
MAX_CONTENT_LENGTH = 2000
MIN_INTERVAL = 0.1

DEFAULT_CONFIG: dict[str, Any] = {
    "channel_id": "",
    "message": "ゆなちゃんだいすき",
    "interval_seconds": 1.0,
    "max_messages": 0,
}


class BotError(Exception):
    """設定ミスや権限不足など、続行できないエラー。"""


class TemporaryError(Exception):
    """通信エラーなど、時間をおけば再試行できるエラー。"""


_console_configured = False


def setup_console() -> None:
    """Windows のコンソールでも日本語が化けないようにする。"""
    global _console_configured
    if _console_configured:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    _console_configured = True


def log(message: str) -> None:
    setup_console()
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise BotError(
            "config.json が見つからなかったので新規作成しました。\n"
            "  channel_id を設定してから、もう一度実行してください。"
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise BotError(
            "config.json の書式が正しくありません"
            f"（{exc.lineno}行目付近）。カンマや引用符を確認してください。"
        ) from exc

    if not isinstance(raw, dict):
        raise BotError("config.json は { ... } の形で書いてください。")

    config = {**DEFAULT_CONFIG, **raw}

    channel_id = str(config["channel_id"]).strip()
    if not channel_id:
        raise BotError(
            "config.json の channel_id が空です。\n"
            "  Discord でチャンネルを右クリック →「チャンネルIDをコピー」して貼り付けてください。\n"
            "  （Discord の設定 → 詳細設定 → 開発者モード をONにすると表示されます）"
        )
    if not channel_id.isdigit():
        raise BotError(
            "channel_id は数字だけで指定してください。\n"
            "  チャンネル名ではなく「チャンネルID」を貼り付けてください。"
        )

    message = config["message"]
    if not isinstance(message, str):
        raise BotError("message は文字列で指定してください。")
    if not message.strip():
        raise BotError("message が空です。送信する文章を入れてください。")
    if len(message) > MAX_CONTENT_LENGTH:
        raise BotError(
            f"message が長すぎます（{len(message)}文字）。"
            f"Discord の上限は {MAX_CONTENT_LENGTH} 文字です。"
        )

    interval = config["interval_seconds"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise BotError("interval_seconds は数値で指定してください（例: 1.0）。")
    interval = float(interval)
    if interval < MIN_INTERVAL:
        raise BotError(f"interval_seconds は {MIN_INTERVAL} 秒以上にしてください。")

    max_messages = config["max_messages"]
    if isinstance(max_messages, bool) or not isinstance(max_messages, int):
        raise BotError("max_messages は 0 以上の整数で指定してください（0 = 無制限）。")
    if max_messages < 0:
        raise BotError("max_messages は 0 以上で指定してください（0 = 無制限）。")

    return {
        "channel_id": channel_id,
        "message": message,
        "interval_seconds": interval,
        "max_messages": max_messages,
    }


def load_token(path: Path = TOKEN_PATH) -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

    if not token and path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().strip('"').strip("'")
            if not line or line.startswith("#") or line == PLACEHOLDER_TOKEN:
                continue
            token = line
            break

    if not token:
        raise BotError(
            "Bot トークンが設定されていません。\n"
            "  token.txt を開いて、Discord Developer Portal で発行した Bot トークンを貼り付けてください。\n"
            "  （環境変数 DISCORD_BOT_TOKEN でも指定できます）"
        )

    if "." not in token:
        log("⚠ トークンの形式がいつもと違うようです。そのまま試します。")

    return token


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}時間{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def discord_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": USER_AGENT,
    }


def fetch_bot_username(token: str) -> str:
    try:
        response = requests.get(
            f"{API_BASE}/users/@me",
            headers=discord_headers(token),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise TemporaryError(f"Discord に接続できませんでした: {exc}") from exc

    if response.status_code == 401:
        raise BotError(
            "Bot トークンが無効です。discord.com/developers/applications で確認してください。"
        )
    if response.status_code != 200:
        raise TemporaryError(
            f"ユーザー情報を取得できませんでした（HTTP {response.status_code}）。"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise TemporaryError("Discord から予期しない形式の応答が返りました。") from exc

    if not isinstance(data, dict):
        raise TemporaryError("Discord から予期しない形式の応答が返りました。")

    name = str(data.get("username", "?"))
    discriminator = str(data.get("discriminator", "0"))
    if discriminator not in ("", "0"):
        name = f"{name}#{discriminator}"
    return name


def fetch_channel_name(token: str, channel_id: str) -> str:
    try:
        response = requests.get(
            f"{API_BASE}/channels/{channel_id}",
            headers=discord_headers(token),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise TemporaryError(f"Discord に接続できませんでした: {exc}") from exc

    if response.status_code == 401:
        raise BotError("Bot トークンが無効です。")
    if response.status_code == 403:
        raise BotError(
            "チャンネルを見る権限がありません。\n"
            "  Bot に「チャンネルを見る」権限を付けてください。"
        )
    if response.status_code == 404:
        raise BotError(
            "チャンネルが見つかりません。\n"
            "  channel_id が正しいか、Bot がそのサーバーに参加しているか確認してください。"
        )
    if response.status_code != 200:
        raise TemporaryError(
            f"チャンネル情報を取得できませんでした（HTTP {response.status_code}）。"
        )

    try:
        data = response.json()
    except ValueError:
        return "?"
    if not isinstance(data, dict):
        return "?"
    return str(data.get("name") or "DM")


class YunaBot(discord.Client):
    """Discord に常時接続し、指定チャンネルへ定期送信するクライアント。"""

    def __init__(self, config: dict[str, Any], once: bool = False) -> None:
        intents = discord.Intents.default()
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="ゆなちゃん",
        )
        super().__init__(
            intents=intents,
            activity=activity,
            status=discord.Status.online,
        )
        self.config = config
        self.once = once
        self.sent = 0
        self.started_at = time.monotonic()
        self._send_task: asyncio.Task[None] | None = None
        self._health_runner: Any = None

    async def setup_hook(self) -> None:
        port = os.environ.get("PORT")
        if not port:
            return
        try:
            await self._start_health_server(int(port))
        except (ValueError, OSError) as exc:
            log(f"ヘルスチェックサーバーを起動できませんでした: {exc}")

    async def _start_health_server(self, port: int) -> None:
        # aiohttp は discord.py と一緒にインストールされる
        from aiohttp import web

        app = web.Application()

        async def health(request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app.router.add_get("/", health)
        app.router.add_get("/health", health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        self._health_runner = runner
        log(f"ヘルスチェック用サーバーを起動しました（ポート {port}）")

    async def on_ready(self) -> None:
        log(f"{self.user} としてログインしました（オンライン表示中）")
        if self._send_task is None or self._send_task.done():
            self._send_task = asyncio.create_task(self._send_loop())

    async def _resolve_channel(self) -> Any:
        channel_id = int(self.config["channel_id"])
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.Forbidden:
                log("❌ チャンネルを見る権限がありません。")
                return None
            except discord.NotFound:
                log("❌ チャンネルが見つかりません。channel_id を確認してください。")
                return None
            except discord.HTTPException as exc:
                log(f"❌ チャンネル情報を取得できませんでした（HTTP {exc.status}）。")
                return None

        if not hasattr(channel, "send"):
            log("❌ 指定されたIDはメッセージを送信できるチャンネルではありません。")
            return None
        return channel

    async def _send_loop(self) -> None:
        try:
            channel = await self._resolve_channel()
            if channel is None:
                return

            interval = float(self.config["interval_seconds"])
            limit = 1 if self.once else int(self.config["max_messages"])
            next_at = time.monotonic()

            log("送信を開始しました。")
            while not self.is_closed():
                if limit and self.sent >= limit:
                    break

                now = time.monotonic()
                if next_at > now:
                    await asyncio.sleep(next_at - now)

                send_started = time.monotonic()
                try:
                    message = await channel.send(self.config["message"])
                except discord.RateLimited as exc:
                    wait = float(exc.retry_after) + 0.5
                    log(f"レート制限中です。{wait:.1f}秒待ちます…")
                    await asyncio.sleep(wait)
                    next_at = time.monotonic()
                    continue
                except discord.Forbidden:
                    log("❌ メッセージを送信する権限がありません。")
                    return
                except discord.NotFound:
                    log("❌ チャンネルが見つかりません。")
                    return
                except discord.HTTPException as exc:
                    status = getattr(exc, "status", "?")
                    log(f"送信に失敗しました（HTTP {status}）。5秒後に再試行します…")
                    await asyncio.sleep(5)
                    next_at = time.monotonic()
                    continue

                self.sent += 1
                elapsed = time.monotonic() - self.started_at
                log(
                    f"{self.sent}通目を送信しました"
                    f"（id: {message.id} / 経過 {format_duration(elapsed)}）"
                )
                next_at = send_started + interval

            log(f"送信を終了しました。送信したメッセージ: {self.sent}通")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 予期しないエラーでもログに残す
            log(f"❌ 予期しないエラー: {exc}")
        finally:
            await self.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discord に常時接続して、指定チャンネルへ一定間隔でメッセージを送信します。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="1通だけ送って終了します（動作確認用）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="設定・トークン・チャンネルを確認するだけで、送信はしません",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
        token = load_token()
    except BotError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print("💗 ゆなちゃん大好きボット（常時オンライン版）")
    print(f"  チャンネルID : {config['channel_id']}")
    print(f"  メッセージ   : {config['message']}")
    print(f"  送信間隔     : {config['interval_seconds']}秒")

    if args.once:
        count_label = "1回（--once）"
    elif config["max_messages"]:
        count_label = f"{config['max_messages']}回"
    else:
        count_label = "無制限（Ctrl+C で停止）"
    print(f"  送信回数     : {count_label}")
    print("  オンライン表示: あり")

    if config["interval_seconds"] < 1.0:
        print("  ⚠ 1秒未満の間隔は Discord のレート制限で安定しません。")

    sys.stdout.flush()

    if args.check:
        try:
            username = fetch_bot_username(token)
            channel_name = fetch_channel_name(token, config["channel_id"])
        except (BotError, TemporaryError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print(f"✅ トークンOK: {username} として接続できます")
        print(f"✅ チャンネルOK: #{channel_name}")
        print("✅ 設定OK（--check のため送信していません）")
        return 0

    bot = YunaBot(config, once=args.once)
    try:
        bot.run(token)
    except discord.LoginFailure:
        print(
            "❌ Bot トークンが無効です。\n"
            "  Discord Developer Portal → Bot → Reset Token で再発行し、\n"
            "  token.txt または環境変数 DISCORD_BOT_TOKEN に設定してください。",
            file=sys.stderr,
        )
        return 1
    except discord.PrivilegedIntentsRequired:
        print(
            "❌ Privileged Gateway Intents が有効になっていません。\n"
            "  Developer Portal → Bot → Privileged Gateway Intents を確認してください。",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print()
        log(f"停止しました。送信したメッセージ: {bot.sent}通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
