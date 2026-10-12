import httpx
import pytest

from greek_news.fetching import UnsafeURL, public_addresses, safe_fetch


def test_private_and_metadata_ips_cannot_be_fetched():
    for host in ("127.0.0.1", "::1", "192.168.1.1", "169.254.169.254", "10.0.0.2"):
        with pytest.raises(UnsafeURL):
            public_addresses(host, 443)


def test_redirect_to_private_address_is_rejected_before_second_request():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/latest/meta-data"})

    with pytest.raises(UnsafeURL):
        safe_fetch("https://source.example/feed", allowed_host="source.example",
                   transport=httpx.MockTransport(handler),
                   resolver=lambda host, port: ["93.184.215.14"] if host == "source.example"
                   else ["127.0.0.1"])
    assert calls == ["source.example"]


def test_response_byte_limit_and_valid_public_fixture():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"a" * 12))
    with pytest.raises(ValueError, match="byte limit"):
        safe_fetch("https://source.example/feed", allowed_host="source.example", max_bytes=8,
                   transport=transport, resolver=lambda host, port: ["93.184.215.14"])
    result = safe_fetch("https://source.example/feed", allowed_host="source.example",
                        max_bytes=20, transport=transport,
                        resolver=lambda host, port: ["93.184.215.14"])
    assert result.content == b"a" * 12