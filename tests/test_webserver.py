import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from webserver import CompanionServer


class CompanionServerTests(unittest.TestCase):
    def test_real_http_get_post_and_errors(self):
        calls = []

        def dispatch(method, path, data):
            calls.append((method, path, data))
            if path == "bad":
                raise ValueError("Bad request")
            return {"ok": True}

        server = CompanionServer(dispatch, port=0)
        try:
            base = f"http://127.0.0.1:{server.port}"
            with urlopen(base + "/api/status", timeout=2) as response:
                self.assertEqual(json.load(response), {"ok": True})
            request = Request(base + "/api/stage", data=b'{"stage":"cq"}',
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response), {"ok": True})
            self.assertEqual(calls, [("GET", "status", {}),
                                     ("POST", "stage", {"stage": "cq"})])
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/api/bad", timeout=2)
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(json.load(error.exception), {"error": "Bad request"})
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
