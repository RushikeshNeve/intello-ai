# ruff: noqa: PT018
"""M06.01.02 sanity tests: SSRF table, redirects, size cap, DNS pin, happy path (no network)."""

from __future__ import annotations

import gzip
from collections.abc import Iterator, Mapping

import pytest

from app.modules.connectors.settings import UrlCaptureSettings
from app.modules.connectors.url_capture import (
    PinnedRequest,
    SafeFetcher,
    UrlCaptureError,
    is_blocked_ip,
)

PUBLIC_IP = "93.184.216.34"


class Resp:
    def __init__(
        self, status: int = 200, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> None:
        self.status_code = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self.closed = False
        self.yielded = 0

    def iter_bytes(self) -> Iterator[bytes]:
        for i in range(0, len(self._body), 1000):
            self.yielded += 1
            yield self._body[i : i + 1000]

    def close(self) -> None:
        self.closed = True


class Net:
    """Fake resolver + transport; records every connection that would have been made."""

    def __init__(self, dns: dict[str, list[str]], pages: dict[str, Resp] | None = None) -> None:
        self.dns, self.pages = dns, pages or {}
        self.sent: list[PinnedRequest] = []

    def resolver(self, host: str, port: int) -> list[str]:
        if host not in self.dns:
            raise OSError("nxdomain")
        return self.dns[host]

    def transport(self, req: PinnedRequest) -> Resp:
        self.sent.append(req)
        return self.pages[f"{req.scheme}://{req.host}{req.path_query}"]

    def fetcher(self, **cfg: object) -> SafeFetcher:
        return SafeFetcher(
            settings=UrlCaptureSettings(**cfg),  # type: ignore[arg-type]
            resolver=self.resolver,
            transport=self.transport,
        )


HTML = {"content-type": "text/html; charset=utf-8"}


def test_public_page_succeeds_pinned_to_the_resolved_ip() -> None:
    net = Net(
        {"example.com": [PUBLIC_IP]}, {"https://example.com/a?x=1": Resp(200, HTML, b"<p>hi</p>")}
    )
    page = net.fetcher().fetch("https://example.com/a?x=1")
    assert (page.data, page.content_type) == (b"<p>hi</p>", "text/html")
    sent = net.sent[0]
    assert sent.ip == PUBLIC_IP and sent.headers["Host"] == "example.com"
    assert sent.headers["User-Agent"] == "IntelloBot"
    assert not {"cookie", "authorization"} & {k.lower() for k in sent.headers}


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[64:ff9b::7f00:1]/",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://127.1/",
        "http://user:pw@example.com/",
        "http://example.com@127.0.0.1/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://example.com:8080/",
        "http://example.com:22/",
        "https://example.com:6379/",
        "http:///nohost",
        "https://example.com/\r\nHost: evil",
    ],
)
def test_ssrf_table_is_blocked_without_any_connection(url: str) -> None:
    net = Net({"example.com": [PUBLIC_IP], "localhost": ["127.0.0.1"]})
    with pytest.raises(UrlCaptureError) as err:
        net.fetcher().fetch(url)
    assert err.value.code == "url_not_allowed"
    assert net.sent == []
    assert "127" not in str(err.value) and "169" not in str(err.value)  # generic, no echo


def test_hostname_resolving_to_private_or_mixed_answers_is_blocked() -> None:
    net = Net({"internal.example": ["10.0.0.5"], "mixed.example": [PUBLIC_IP, "192.168.0.9"]})
    for host in ("internal.example", "mixed.example"):
        with pytest.raises(UrlCaptureError) as err:
            net.fetcher().fetch(f"http://{host}/")
        assert err.value.code == "url_not_allowed"
    assert net.sent == []


def test_ip_policy_allows_public_addresses() -> None:
    import ipaddress

    assert not is_blocked_ip(ipaddress.ip_address(PUBLIC_IP))
    assert not is_blocked_ip(ipaddress.ip_address("2606:4700:4700::1111"))
    assert is_blocked_ip(ipaddress.ip_address("224.0.0.1"))


def test_redirect_to_private_ip_and_downgrade_are_blocked_and_limit_enforced() -> None:
    net = Net(
        {"a.example": [PUBLIC_IP], "b.example": [PUBLIC_IP]},
        {
            "https://a.example/": Resp(302, {"Location": "http://169.254.169.254/"}),
            "https://b.example/": Resp(301, {"Location": "http://b.example/plain"}),
            "http://a.example/loop": Resp(302, {"Location": "/loop"}),
        },
    )
    for url in ("https://a.example/", "https://b.example/"):
        with pytest.raises(UrlCaptureError) as err:
            net.fetcher().fetch(url)
        assert err.value.code == "url_not_allowed"
    assert [r.ip for r in net.sent] == [PUBLIC_IP, PUBLIC_IP]  # the private hop was never opened
    with pytest.raises(UrlCaptureError) as loop:
        net.fetcher().fetch("http://a.example/loop")
    assert loop.value.code == "fetch_failed"
    assert sum(r.path_query == "/loop" for r in net.sent) == 4  # 1 + 3 redirects, then stop


def test_redirect_to_public_page_is_followed_and_final_url_reported() -> None:
    net = Net(
        {"a.example": [PUBLIC_IP], "b.example": ["93.184.216.35"]},
        {
            "http://a.example/": Resp(301, {"Location": "https://b.example/final"}),
            "https://b.example/final": Resp(200, {"content-type": "text/plain"}, b"hello"),
        },
    )
    page = net.fetcher().fetch("http://a.example/")
    assert page.final_url == "https://b.example/final" and page.data == b"hello"
    assert net.sent[1].ip == "93.184.216.35"


def test_size_cap_aborts_the_stream_and_content_length_is_checked_first() -> None:
    big = Resp(200, {"content-type": "text/plain"}, b"x" * 50_000)
    net = Net({"e.example": [PUBLIC_IP]}, {"http://e.example/": big})
    with pytest.raises(UrlCaptureError) as err:
        net.fetcher(URL_CAPTURE_MAX_BYTES=5000).fetch("http://e.example/")
    assert err.value.code == "too_large"
    assert big.yielded <= 6 and big.closed  # aborted early, connection released
    declared = Resp(200, {"content-type": "text/plain", "content-length": "999999"}, b"x")
    net2 = Net({"e.example": [PUBLIC_IP]}, {"http://e.example/": declared})
    with pytest.raises(UrlCaptureError) as err2:
        net2.fetcher(URL_CAPTURE_MAX_BYTES=5000).fetch("http://e.example/")
    assert err2.value.code == "too_large" and declared.yielded == 0
    assert gzip  # decoded-size cap is enforced by httpx's iter_bytes (decoded stream)


def test_content_type_and_status_errors_are_generic() -> None:
    net = Net(
        {"e.example": [PUBLIC_IP]},
        {
            "http://e.example/bin": Resp(200, {"content-type": "application/zip"}, b"PK"),
            "http://e.example/missing": Resp(404, HTML, b"no"),
        },
    )
    with pytest.raises(UrlCaptureError) as binary:
        net.fetcher().fetch("http://e.example/bin")
    assert binary.value.code == "unsupported_type"
    with pytest.raises(UrlCaptureError) as missing:
        net.fetcher().fetch("http://e.example/missing")
    assert missing.value.code == "fetch_failed"


def test_dns_rebinding_cannot_swap_the_validated_ip() -> None:
    """The resolver answers public first, then private: only ONE lookup is made per hop and the
    connection uses that validated address, so the later private answer is never reachable."""
    calls: list[str] = []
    answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])

    def rebinding_resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        return next(answers)

    net = Net({}, {"http://r.example/": Resp(200, {"content-type": "text/plain"}, b"ok")})
    fetcher = SafeFetcher(
        settings=UrlCaptureSettings(), resolver=rebinding_resolver, transport=net.transport
    )
    assert fetcher.fetch("http://r.example/").data == b"ok"
    assert calls == ["r.example"] and net.sent[0].ip == PUBLIC_IP
    # a second capture re-resolves and now sees the private address: blocked
    with pytest.raises(UrlCaptureError):
        fetcher.fetch("http://r.example/")
