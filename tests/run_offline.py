"""Run both pytest and unittest regressions with outbound sockets blocked here."""
from pathlib import Path
import socket
from unittest.mock import patch


def blocked(*args, **kwargs):
    raise AssertionError("Offline regression attempted an external connection; mock the transport")


def local_only(original):
    def connect(sock, address):
        # Windows asyncio uses a loopback socket pair internally.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(sock, address)
        return blocked()
    return connect


if __name__ == "__main__":
    try:
        import pytest
    except ModuleNotFoundError as exc:
        if exc.name != 'pytest':
            raise
        raise SystemExit('缺少测试依赖：在仓库目录用当前解释器运行 python -m pip install ".[test]"。') from exc
    with patch.object(socket, "create_connection", blocked), patch.object(socket.socket, "connect", local_only(socket.socket.connect)), patch.object(socket.socket, "connect_ex", local_only(socket.socket.connect_ex)):
        raise SystemExit(pytest.main(['-q', '-rs', '-p', 'no:cacheprovider', str(Path(__file__).resolve().parent)]))
