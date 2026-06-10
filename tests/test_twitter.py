from __future__ import annotations

import pytest

from pska.collectors.twitter import parse_tweet_id


@pytest.mark.parametrize(
    ("url", "tweet_id"),
    [
        ("https://x.com/alice/status/123456789", "123456789"),
        ("https://twitter.com/alice/status/987654321?s=20", "987654321"),
        ("https://mobile.twitter.com/alice/statuses/111", "111"),
    ],
)
def test_parse_tweet_id(url: str, tweet_id: str) -> None:
    assert parse_tweet_id(url) == tweet_id


def test_parse_tweet_id_rejects_other_hosts() -> None:
    with pytest.raises(ValueError):
        parse_tweet_id("https://example.com/alice/status/123")
