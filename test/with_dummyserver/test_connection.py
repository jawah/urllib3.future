from __future__ import annotations

import typing

import pytest

from dummyserver.testcase import HTTPDummyServerTestCase as server
from urllib3 import HTTPConnectionPool
from urllib3.exceptions import ResponseNotReady
from urllib3.response import HTTPResponse


@pytest.fixture()
def pool() -> typing.Generator[HTTPConnectionPool, None, None]:
    server.setup_class()

    with HTTPConnectionPool(server.host, server.port) as pool:
        yield pool

    server.teardown_class()


def test_returns_urllib3_HTTPResponse(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()

    method = "GET"
    path = "/"

    conn.request(method, path)

    response = conn.getresponse()

    assert isinstance(response, HTTPResponse)
    pool._put_conn(conn)


def test_does_not_release_conn(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()

    method = "GET"
    path = "/"

    conn.request(method, path)

    response = conn.getresponse()

    response.release_conn()
    assert pool.pool.qsize() == 0  # type: ignore[union-attr]
    conn.close()


def test_releases_conn(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()
    assert conn is not None

    method = "GET"
    path = "/"

    conn.request(method, path)

    response = conn.getresponse()
    # If these variables are set by the pool
    # then the response can release the connection
    # back into the pool.
    response._pool = pool
    response._connection = conn
    response._police_officer = pool.pool
    pool.pool.memorize(response, conn)  # type: ignore[union-attr]

    response.release_conn()
    assert pool.pool.qsize() == 1  # type: ignore[union-attr]
    conn.close()


def test_double_getresponse(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()

    method = "GET"
    path = "/"

    conn.request(method, path)

    _ = conn.getresponse()

    # Calling getrepsonse() twice should cause an error
    with pytest.raises(ResponseNotReady):
        conn.getresponse()

    conn.close()


def test_connection_state_properties(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()

    assert conn.is_closed is True
    assert conn.is_connected is False
    assert conn.has_connected_to_proxy is False
    assert conn.is_verified is False
    assert conn.proxy_is_verified is None

    conn.connect()

    assert conn.is_closed is False
    assert conn.is_connected is True
    assert conn.has_connected_to_proxy is False
    assert conn.is_verified is False
    assert conn.proxy_is_verified is None

    conn.request("GET", "/")
    resp = conn.getresponse()
    assert resp.status == 200

    conn.close()

    assert conn.is_closed is True
    assert conn.is_connected is False
    assert conn.has_connected_to_proxy is False
    assert conn.is_verified is False
    assert conn.proxy_is_verified is None


def test_set_tunnel_is_reset(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()

    assert conn.is_closed is True
    assert conn.is_connected is False
    assert conn.has_connected_to_proxy is False
    assert conn.is_verified is False
    assert conn.proxy_is_verified is None

    conn.set_tunnel(host="host", port=8080, scheme="http")

    assert conn._tunnel_host == "host"
    assert conn._tunnel_port == 8080
    assert conn._tunnel_scheme == "http"

    conn.close()

    assert conn._tunnel_host is None
    assert conn._tunnel_port is None
    assert conn._tunnel_scheme is None


def test_invalid_tunnel_scheme(pool: HTTPConnectionPool) -> None:
    conn = pool._get_conn()

    with pytest.raises(ValueError) as e:
        conn.set_tunnel(host="host", port=8080, scheme="socks")
    assert (
        str(e.value)
        == "Invalid proxy scheme for tunneling: 'socks', must be either 'http' or 'https'"
    )


def test_is_connected_false_after_keepalive_delay_elapsed(
    pool: HTTPConnectionPool,
) -> None:
    """Covers ``HTTPConnection.is_connected`` early-return branch when the
    connection has outlived its configured ``keepalive_delay`` window.
    """
    import time

    conn = pool._get_conn()
    conn.connect()

    assert conn.is_connected is True

    # Force the connection to look as if it was established a long time ago
    # and configure a vanishingly small keepalive delay. The next is_connected
    # probe should reject it without performing the os-level liveness check.
    conn._keepalive_delay = 0.001
    conn._connected_at = time.monotonic() - 10.0

    assert conn.is_connected is False

    conn.close()


def test_is_connected_false_when_socket_fileno_invalid(
    pool: HTTPConnectionPool,
) -> None:
    """Covers ``HTTPConnection.is_connected`` early-return at
    ``src/urllib3/connection.py:310`` when the underlying socket has been
    closed at the file-descriptor level (``fileno() == -1``).
    """
    conn = pool._get_conn()
    conn.connect()

    assert conn.is_connected is True

    # Forcefully close the socket without going through conn.close() so that
    # _protocol is preserved and only the fileno()==-1 branch fires.
    assert conn.sock is not None
    conn.sock.close()

    assert conn.is_connected is False

    conn.close()


def test_aws_style_send_override_typeerror_fallback() -> None:
    """Covers ``src/urllib3/connection.py:548-554`` -- when a subclass
    overrides ``send()`` with a signature that rejects ``eot`` (as the
    historic ``botocore.awsrequest.AWSConnection`` did), the request path
    must fall back to ``super().send(b"", eot=True)`` so that uploads still
    terminate cleanly.
    """
    from urllib3.connection import HTTPConnection

    captured: dict[str, int] = {"calls": 0, "fallbacks": 0}

    class AWSStyleHTTPConnection(HTTPConnection):
        def send(
            self,
            data: typing.Any,
            *,
            eot: bool = False,
        ) -> typing.Any:
            captured["calls"] += 1
            if data == b"" and eot:
                captured["fallbacks"] += 1
                raise TypeError("send() got an unexpected keyword argument 'eot'")
            return super().send(data, eot=eot)

    class AWSStylePool(HTTPConnectionPool):
        ConnectionCls = AWSStyleHTTPConnection

    server.setup_class()
    try:
        with AWSStylePool(server.host, server.port) as pool:
            resp = pool.request("POST", "/echo", body=b"hello-aws-style")
            assert resp.status == 200
            assert b"hello-aws-style" in resp.data
            assert captured["fallbacks"] == 1
    finally:
        server.teardown_class()
