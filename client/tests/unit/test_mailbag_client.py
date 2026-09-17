"""Headless tests for the mailbag Hub client (issue #32).

httpx is stubbed with injectable fakes: no network, no display.
"""

from __future__ import annotations

import unittest

from client.system.mailbag_client import MailbagClient


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, resp=None, error=None):
        self.resp = resp
        self.error = error
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers))
        if self.error:
            raise self.error
        return self.resp

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        if self.error:
            raise self.error
        return self.resp


def _client(fake, url="http://hub:9785", secret="s3cret"):
    return MailbagClient(
        resolve_hub_url=lambda: url,
        resolve_hub_secret=lambda: secret,
        http_get=fake.get,
        http_post=fake.post,
    )


class TestCount(unittest.TestCase):
    def test_count_returns_pending(self):
        fake = _FakeHttp(_FakeResp(200, {"pending": 3}))
        c = _client(fake)
        self.assertEqual(c.count(), 3)
        method, url, headers = fake.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://hub:9785/mailbag/count")
        self.assertEqual(headers, {"X-Hub-Secret": "s3cret"})
        self.assertIsNone(c.last_error)

    def test_count_zero_when_no_hub(self):
        fake = _FakeHttp(_FakeResp(200, {"pending": 9}))
        c = _client(fake, url="")
        self.assertEqual(c.count(), 0)
        self.assertEqual(fake.calls, [])

    def test_count_zero_on_network_error(self):
        fake = _FakeHttp(error=ConnectionError("down"))
        c = _client(fake)
        self.assertEqual(c.count(), 0)
        self.assertIn("unreachable", c.last_error)

    def test_count_zero_on_non_200(self):
        fake = _FakeHttp(_FakeResp(403, {"detail": "no"}))
        c = _client(fake)
        self.assertEqual(c.count(), 0)
        self.assertIsNotNone(c.last_error)


class TestPending(unittest.TestCase):
    def test_pending_returns_questions(self):
        qs = [{"question_id": "q1", "soul_id": "s1", "question": "Why?"}]
        fake = _FakeHttp(_FakeResp(200, {"questions": qs}))
        c = _client(fake)
        self.assertEqual(c.pending(), qs)

    def test_pending_empty_offline(self):
        fake = _FakeHttp(error=ConnectionError("down"))
        c = _client(fake)
        self.assertEqual(c.pending(), [])
        self.assertIsNotNone(c.last_error)


class TestAnswer(unittest.TestCase):
    def test_answer_success(self):
        fake = _FakeHttp(
            _FakeResp(200, {"question_id": "q1", "status": "answered"})
        )
        c = _client(fake)
        result = c.answer("q1", "Because.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["question_id"], "q1")
        method, url, body, _headers = fake.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/mailbag/q1/answer"))
        self.assertEqual(body, {"answer": "Because."})

    def test_answer_error_offline(self):
        fake = _FakeHttp(error=ConnectionError("down"))
        c = _client(fake)
        result = c.answer("q1", "Because.")
        self.assertEqual(result["status"], "error")
        self.assertIn("unreachable", result["message"])

    def test_answer_error_http(self):
        fake = _FakeHttp(_FakeResp(404, {"detail": "no such question"}))
        c = _client(fake)
        result = c.answer("q_nope", "x")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "no such question")


if __name__ == "__main__":
    unittest.main()
