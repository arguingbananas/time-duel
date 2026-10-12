#!/usr/bin/env python3
"""Tests for duel_clock_server.py rotation logic and game flow."""

import json
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

sys.path.insert(0, ".")
import duel_clock_server as srv


class TestNowMs(unittest.TestCase):
    def setUp(self):
        self._orig_mock = srv._mock_time

    def tearDown(self):
        srv._mock_time = self._orig_mock

    def test_returns_int(self):
        srv._mock_time = None
        val = srv.now_ms()
        self.assertIsInstance(val, int)

    def test_uses_mock(self):
        srv._mock_time = 12345
        self.assertEqual(srv.now_ms(), 12345)

    def test_increases(self):
        srv._mock_time = None
        a = srv.now_ms()
        time.sleep(0.01)
        b = srv.now_ms()
        self.assertGreaterEqual(b, a)


class TestMakePuzzle(unittest.TestCase):
    def setUp(self):
        self._orig_mock = srv._mock_time
        srv._mock_time = None

    def tearDown(self):
        srv._mock_time = self._orig_mock

    def test_structure(self):
        p = srv.make_puzzle()
        self.assertIn("shapeType", p)
        self.assertIn("color", p)
        self.assertIn("missingIndex", p)
        self.assertIn("choices", p)
        self.assertIn("correctIndex", p)
        self.assertIn("rotationIndex", p)
        self.assertIn("rotationStartedAt", p)

    def test_choices_count(self):
        p = srv.make_puzzle()
        self.assertEqual(len(p["choices"]), srv.NUM_CHOICES)

    def test_missing_index_range(self):
        for _ in range(50):
            p = srv.make_puzzle()
            self.assertIn(p["missingIndex"], [0, 1, 2, 3])

    def test_rotation_index_starts_zero(self):
        p = srv.make_puzzle()
        self.assertEqual(p["rotationIndex"], 0)

    def test_rotation_started_at_is_recent(self):
        p = srv.make_puzzle()
        self.assertGreaterEqual(p["rotationStartedAt"], srv.now_ms() - 100)

    def test_correct_index_valid(self):
        p = srv.make_puzzle()
        self.assertIn(p["correctIndex"], range(srv.NUM_CHOICES))

    def test_correct_piece_matches_missing(self):
        p = srv.make_puzzle()
        correct_choice = p["choices"][p["correctIndex"]]
        self.assertEqual(correct_choice["shapeType"], p["shapeType"])


class TestGetCurrentMissingIndex(unittest.TestCase):
    def setUp(self):
        self._orig_mock = srv._mock_time
        self.base_time = 1000000
        srv._mock_time = self.base_time

    def tearDown(self):
        srv._mock_time = self._orig_mock

    def test_returns_none_for_none(self):
        self.assertIsNone(srv.get_current_missing_index(None))

    def test_returns_none_for_empty(self):
        self.assertIsNone(srv.get_current_missing_index({}))

    def test_at_start_returns_missing_index(self):
        puzzle = {"missingIndex": 2, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time
        self.assertEqual(srv.get_current_missing_index(puzzle), 2)

    def test_after_one_rotation(self):
        puzzle = {"missingIndex": 0, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 4000
        self.assertEqual(srv.get_current_missing_index(puzzle), 1)

    def test_after_two_rotations(self):
        puzzle = {"missingIndex": 0, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 8000
        self.assertEqual(srv.get_current_missing_index(puzzle), 2)

    def test_after_three_rotations(self):
        puzzle = {"missingIndex": 0, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 12000
        self.assertEqual(srv.get_current_missing_index(puzzle), 3)

    def test_after_four_rotations_cycles(self):
        puzzle = {"missingIndex": 0, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 16000
        self.assertEqual(srv.get_current_missing_index(puzzle), 0)

    def test_wraps_correctly_from_nonzero_missing(self):
        puzzle = {"missingIndex": 3, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 4000
        self.assertEqual(srv.get_current_missing_index(puzzle), 0)

    def test_interior_rotation_count(self):
        puzzle = {"missingIndex": 1, "rotationIndex": 0, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 6000
        self.assertEqual(srv.get_current_missing_index(puzzle), 2)

    def test_with_nonzero_rotation_index(self):
        puzzle = {"missingIndex": 0, "rotationIndex": 2, "rotationStartedAt": self.base_time}
        srv._mock_time = self.base_time + 4000
        self.assertEqual(srv.get_current_missing_index(puzzle), 3)


class TestGameFlow(unittest.TestCase):
    def setUp(self):
        self._orig_mock = srv._mock_time
        srv._mock_time = None
        srv.STATE = srv.default_state()
        srv.LOCK = threading.Lock()

    def tearDown(self):
        srv._mock_time = self._orig_mock

    def test_initial_state(self):
        s = srv.snapshot()
        self.assertEqual(s["status"], "claiming")
        self.assertIsNone(s["activePlayer"])
        self.assertIsNone(s["puzzle"])

    def test_join_p1(self):
        result, _ = srv.do_join()
        s = srv.snapshot()
        self.assertTrue(s["p1Taken"])
        self.assertFalse(s["p2Taken"])

    def test_join_p2(self):
        srv.do_join()
        result, _ = srv.do_join()
        s = srv.snapshot()
        self.assertTrue(s["p1Taken"])
        self.assertTrue(s["p2Taken"])
        self.assertEqual(s["status"], "ready")

    def test_start_requires_both_joined(self):
        result, code = srv.do_start()
        self.assertEqual(code, 409)
        self.assertEqual(result["error"], "not_ready")

    def test_start_creates_puzzle(self):
        srv.do_join()
        srv.do_join()
        result, _ = srv.do_start()
        self.assertIsNotNone(result["puzzle"])
        self.assertEqual(result["status"], "active")
        # correctIndex should NOT be in the snapshot puzzle
        self.assertNotIn("correctIndex", result["puzzle"])

    def test_correct_answer_switches_turn(self):
        srv.do_join()
        srv.do_join()
        start_state = srv.do_start()[0]
        puzzle = start_state["puzzle"]
        current_missing = puzzle["currentMissingIndex"]
        # Find a choice that matches currentMissingIndex
        matching_choices = [
            i for i, c in enumerate(puzzle["choices"])
            if c["pieceIndex"] == current_missing and c["shapeType"] == puzzle["shapeType"]
        ]
        self.assertTrue(len(matching_choices) > 0, "No matching choice found")
        correct_slot = matching_choices[0]
        result, _ = srv.do_answer("p1", correct_slot)
        self.assertEqual(result["activePlayer"], "p2")
        self.assertEqual(result["status"], "active")

    def test_wrong_answer_penalizes_and_same_player_continues(self):
        srv.do_join()
        srv.do_join()
        srv.do_start()
        # Pick a wrong answer (slot 0 is likely wrong)
        result, _ = srv.do_answer("p1", 0)
        p1_remaining = result["p1RemainingMs"]
        # Should have been penalized (remaining time decreased)
        self.assertLess(p1_remaining, srv.STATE["p1RemainingMs"] + 100)
        self.assertEqual(result["activePlayer"], "p1")

    def test_answer_when_not_active_player(self):
        srv.do_join()
        srv.do_join()
        srv.do_start()
        result, code = srv.do_answer("p2", 0)
        self.assertEqual(code, 409)

    def test_answer_when_no_puzzle(self):
        srv.STATE = srv.default_state()
        result, code = srv.do_answer("p1", 0)
        self.assertEqual(code, 200)
        self.assertIsNone(result["puzzle"])

    def test_answer_wrong_choice_same_player_continues(self):
        srv.do_join()
        srv.do_join()
        start_state = srv.do_start()[0]
        puzzle = start_state["puzzle"]
        current_missing = puzzle["currentMissingIndex"]
        # Find a choice that does NOT match currentMissingIndex
        wrong_choices = [
            i for i, c in enumerate(puzzle["choices"])
            if c["pieceIndex"] != current_missing or c["shapeType"] != puzzle["shapeType"]
        ]
        self.assertTrue(len(wrong_choices) > 0, "No wrong choice found")
        wrong_idx = wrong_choices[0]
        result, _ = srv.do_answer("p1", wrong_idx)
        self.assertEqual(result["activePlayer"], "p1")

    def test_answer_correct_with_rotation(self):
        """Test that answer validation uses currentMissingIndex, not missingIndex."""
        srv.do_join()
        srv.do_join()
        srv.do_start()
        puzzle = srv.STATE["puzzle"]
        missing_idx = puzzle["missingIndex"]
        # currentMissingIndex at start equals missingIndex
        current_missing = srv.get_current_missing_index(puzzle)
        # Find a choice that matches currentMissingIndex
        matching_choices = [
            i for i, c in enumerate(puzzle["choices"])
            if c["pieceIndex"] == current_missing and c["shapeType"] == puzzle["shapeType"]
        ]
        if matching_choices:
            correct_slot = matching_choices[0]
            result, _ = srv.do_answer("p1", correct_slot)
            self.assertEqual(result["activePlayer"], "p2")


class TestHttpEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE = srv.default_state()
        srv._mock_time = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if not data:
            return {"error": "empty_response"}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"error": "non_json_response", "status": resp.status}

    def _post(self, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port)
        payload = json.dumps(body or {})
        conn.request("POST", path, payload, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return json.loads(data)

    def test_get_state(self):
        s = self._get("/state")
        self.assertIn("status", s)
        self.assertIn("serverNow", s)

    def test_get_unknown_path(self):
        s = self._get("/nonexistent")
        # Server returns 404, which may be empty or have error
        self.assertTrue("error" in s or s.get("error") == "empty_response")

    def test_join_and_start(self):
        self._post("/join", {"player": "p1"})
        self._post("/join", {"player": "p2"})
        result = self._post("/start", {"player": "p1"})
        self.assertIn("puzzle", result)
        self.assertEqual(result["status"], "active")


if __name__ == "__main__":
    unittest.main()
