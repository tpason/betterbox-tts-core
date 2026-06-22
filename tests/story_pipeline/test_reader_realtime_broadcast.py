from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from scripts.story_pipeline.reader_realtime_broadcast import (
    broadcast_chapter_update,
    broadcast_reader_event,
)


def test_broadcast_skips_when_url_unset(monkeypatch):
    monkeypatch.delenv("READER_REALTIME_URL", raising=False)
    assert broadcast_reader_event(story_id="1") is False


def test_broadcast_posts_with_token(monkeypatch):
    monkeypatch.setenv("READER_REALTIME_URL", "http://story-reader:3000")
    monkeypatch.setenv("READER_REALTIME_TOKEN", "secret")

    response = MagicMock()
    response.ok = True
    mock_post = MagicMock(return_value=response)

    with patch("scripts.story_pipeline.reader_realtime_broadcast.requests.post", mock_post):
        assert broadcast_chapter_update(story_id="42", chapter_number=7) is True

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://story-reader:3000/api/realtime/broadcast"
    assert kwargs["json"] == {
        "type": "chapter_update",
        "storyId": "42",
        "chapterNumber": 7,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_cli_main_success(monkeypatch):
    monkeypatch.setenv("READER_REALTIME_URL", "http://story-reader:3000")
    with patch("scripts.story_pipeline.reader_realtime_broadcast.broadcast_reader_event", return_value=True):
        from scripts.story_pipeline.reader_realtime_broadcast import main

        assert main([]) == 0


def test_cli_main_failure(monkeypatch):
    monkeypatch.setenv("READER_REALTIME_URL", "http://story-reader:3000")
    with patch("scripts.story_pipeline.reader_realtime_broadcast.broadcast_reader_event", return_value=False):
        from scripts.story_pipeline.reader_realtime_broadcast import main

        assert main([]) == 1
