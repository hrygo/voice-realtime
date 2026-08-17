"""字幕事件消费演示/桥接入口：连接 WhisperLiveKit WS 并打印事件。

`vr-subtitle-events [--url ws://127.0.0.1:8001] [--language Chinese]`

partial 就地刷新（浅色行），confirmed 独立成行输出。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from voice_realtime.subtitles.events import SubtitleStream


async def _run(url: str, language: str) -> None:
    stream = SubtitleStream(url, language=language)
    await stream.connect()
    print(f"已连接 {stream._uri}", file=sys.stderr)
    try:
        async for event in stream.events():
            if event.kind == "partial":
                print(f"\r[partial] {event.text}", end="", flush=True)
            elif event.kind == "confirmed":
                print(f"\n[confirmed] ({event.start}→{event.end}) {event.text}", flush=True)
            elif event.kind == "config":
                print(f"\n[config] {event.raw}", file=sys.stderr)
            elif event.kind == "error":
                print(f"\n[error] {event.text}", file=sys.stderr)
            else:
                print(f"\n[{event.kind}] {event.raw}", file=sys.stderr)
    finally:
        await stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="消费 WhisperLiveKit 字幕事件")
    parser.add_argument("--url", default="ws://127.0.0.1:8001", help="字幕服务地址")
    parser.add_argument("--language", default="Chinese", help="语言")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.url, args.language))


if __name__ == "__main__":
    main()
