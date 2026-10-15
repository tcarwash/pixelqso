import unittest
from unittest.mock import Mock

from app import RigctlClient


class BufferedSocket:
    def __init__(self):
        self.data = b""

    def canReadLine(self):
        return b"\n" in self.data

    def readLine(self):
        line, self.data = self.data.split(b"\n", 1)
        return line + b"\n"


class RigctlLineTests(unittest.TestCase):
    def test_fragmented_replies_wait_for_complete_lines(self):
        for chunks, lines, expected in [
            ([b"145", b"000000", b"\n"], 1, ["145000000"]),
            ([b"US", b"B\n24", b"00\r\n"], 2, ["USB", "2400"]),
            ([b"RPR", b"T -1\n"], 2, ["RPRT -1"]),
        ]:
            with self.subTest(chunks=chunks):
                client = Mock()
                client.socket = BufferedSocket()
                client.pending = ["query", lines, Mock(), []]
                client._finish.side_effect = lambda result: setattr(client, "pending", None)
                for chunk in chunks[:-1]:
                    client.socket.data += chunk
                    RigctlClient._read(client)
                    client._finish.assert_not_called()
                client.socket.data += chunks[-1]
                RigctlClient._read(client)
                client._finish.assert_called_once_with(expected)
