"""Run regressions with outbound socket connections blocked in this process."""
from pathlib import Path
import socket
import unittest
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
    with patch.object(socket, "create_connection", blocked), patch.object(socket.socket, "connect", local_only(socket.socket.connect)), patch.object(socket.socket, "connect_ex", local_only(socket.socket.connect_ex)):
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
        result = unittest.TextTestRunner(verbosity=2).run(suite)  # 本项目自订：详细测试输出
    raise SystemExit(0 if result.wasSuccessful() else 1)  # 本项目自订：CI 退出状态
