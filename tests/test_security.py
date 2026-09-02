from __future__ import annotations

import ipaddress

import pytest

from bigbot.security import UnsafeUrlError, forum_title, plain_text, validate_feed_url


async def public_resolver(host: str, port: int) -> set[ipaddress.IPv4Address]:
    assert host == "example.com"
    assert port == 443
    return {ipaddress.ip_address("93.184.216.34")}


async def private_resolver(host: str, port: int) -> set[ipaddress.IPv4Address]:
    return {ipaddress.ip_address("127.0.0.1")}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/rss",
        "https://user:pass@example.com/rss",
        "https://example.com:8443/rss",
    ],
)
async def test_validate_feed_url_rejects_unsafe_authorities(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_feed_url(url, resolver=public_resolver)


async def test_validate_feed_url_rejects_private_dns() -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_feed_url("https://example.com/rss", resolver=private_resolver)


async def test_validate_feed_url_accepts_public_https() -> None:
    assert (
        await validate_feed_url("https://example.com/rss", resolver=public_resolver)
        == "https://example.com/rss"
    )


def test_plain_text_strips_markup_and_neutralizes_mentions() -> None:
    assert plain_text("<b>Hello</b> @everyone <i>world</i>", limit=100) == (
        "Hello @\u200beveryone world"
    )


def test_plain_text_preserves_bare_ampersands_at_end_of_title() -> None:
    title = "Rising numbers of children in mental health crisis ending up in A&E"
    assert plain_text(title, limit=100) == title
    assert plain_text("AT&T reports", limit=100) == "AT&T reports"
    assert plain_text("Markets &amp; Economy", limit=100) == "Markets & Economy"


def test_forum_title_is_bounded_and_never_empty() -> None:
    assert len(forum_title("x" * 200)) == 100
    assert forum_title(" <br> ") == "New feed item"


def test_forum_title_drops_bullet_content() -> None:
    value = "Market closes higher - point one\n- point two\n- point three"
    assert forum_title(value) == "Market closes higher"
    assert forum_title("## Clean headline\n* detail") == "Clean headline"
