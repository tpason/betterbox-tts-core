"""Fire-and-forget reader notifications via story-reader /api/realtime/broadcast."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


def _reader_realtime_config() -> tuple[str, str]:
    # READER_REALTIME_DEV_URL overrides production URL while developing UI on host :3003.
    base = (
        os.getenv("READER_REALTIME_DEV_URL")
        or os.getenv("READER_REALTIME_URL")
        or ""
    ).strip().rstrip("/")
    token = (os.getenv("READER_REALTIME_TOKEN") or "").strip()
    return base, token


def broadcast_reader_event(
    *,
    event_type: str = "notification_update",
    story_id: str | None = None,
    chapter_number: int | None = None,
    message: str | None = None,
    timeout: float = 3.0,
) -> bool:
    base, token = _reader_realtime_config()
    if not base:
        return False

    payload: dict[str, Any] = {"type": event_type}
    if story_id:
        payload["storyId"] = story_id
    if chapter_number is not None:
        payload["chapterNumber"] = chapter_number
    if message:
        payload["message"] = message

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.post(
            f"{base}/api/realtime/broadcast",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.debug("reader realtime broadcast failed: %s", exc)
        return False

    if response.ok:
        return True

    logger.debug(
        "reader realtime broadcast rejected: status=%s body=%s",
        response.status_code,
        response.text[:200],
    )
    return False


def broadcast_chapter_update(*, story_id: str, chapter_number: int) -> bool:
    return broadcast_reader_event(
        event_type="chapter_update",
        story_id=story_id,
        chapter_number=chapter_number,
    )


def broadcast_story_update(*, story_id: str, message: str | None = None) -> bool:
    return broadcast_reader_event(
        event_type="story_update",
        story_id=story_id,
        message=message,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="POST a reader realtime broadcast event.")
    parser.add_argument("--type", default="notification_update", help="Event type (default: notification_update)")
    parser.add_argument("--story-id", default="", help="Story UUID")
    parser.add_argument("--chapter-number", type=int, default=0, help="Chapter number for chapter_update")
    parser.add_argument("--message", default="", help="Optional message")
    args = parser.parse_args(argv)

    ok = broadcast_reader_event(
        event_type=args.type,
        story_id=args.story_id or None,
        chapter_number=args.chapter_number or None,
        message=args.message or None,
    )
    if ok:
        print("broadcast ok")
        return 0
    print("broadcast failed (check READER_REALTIME_URL / READER_REALTIME_TOKEN)", file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
