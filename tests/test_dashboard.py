"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from scanner import get_db, init_db, upsert_sessions, insert_turns
import dashboard as dashboard_module
from dashboard import (
    get_dashboard_data,
    DashboardHandler,
    HTML_TEMPLATE,
    _resolve_scan_interval,
    _scan_lock,
    DEFAULT_SCAN_INTERVAL_SEC,
)

try:
    from http.server import HTTPServer
except ImportError:
    HTTPServer = None


class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [{
            "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 500,
            "output_tokens": 200, "cache_read_tokens": 50,
            "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
        }]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_truncated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(len(session["session_id"]), 8)

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_rescan_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_index_with_query_string_returns_html(self):
        """URL に ?range=7d のようなクエリが付いてリロードしても 200 になる。"""
        url = f"http://127.0.0.1:{self.port}/?range=7d"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_multiple_params_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/?range=30d&models=claude-sonnet-4-6"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_with_query_string(self):
        url = f"http://127.0.0.1:{self.port}/api/data?t=123"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])


class TestHTMLTemplate(unittest.TestCase):
    def test_template_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", HTML_TEMPLATE)
        self.assertIn("</html>", HTML_TEMPLATE)

    def test_template_has_esc_function(self):
        """Verify XSS protection is present (PR #10)."""
        self.assertIn("function esc(", HTML_TEMPLATE)

    def test_template_has_chart_js(self):
        self.assertIn("chart.js", HTML_TEMPLATE.lower())

    def test_template_has_substring_matching(self):
        """Verify getPricing falls back to substring match for unknown models."""
        self.assertIn("m.includes('opus')", HTML_TEMPLATE)
        self.assertIn("m.includes('sonnet')", HTML_TEMPLATE)
        self.assertIn("m.includes('haiku')", HTML_TEMPLATE)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", HTML_TEMPLATE)


class TestPeriodicScanRespectsProjectsDir(unittest.TestCase):
    """CLI の --projects-dir が定期スキャンでも尊重されることを担保。"""

    def test_projects_dir_is_propagated(self):
        import dashboard
        import scanner

        captured = []

        class _Stop(BaseException):
            """except Exception ではキャッチされず 1 周で抜けるためのマーカー。"""

        def fake_scan(projects_dir=None, verbose=True):
            captured.append({"projects_dir": projects_dir, "verbose": verbose})
            raise _Stop()

        orig_scan = scanner.scan
        orig_sleep = dashboard.time.sleep
        scanner.scan = fake_scan
        dashboard.time.sleep = lambda _: None
        try:
            with self.assertRaises(_Stop):
                dashboard._periodic_scan_loop(1, projects_dir="/custom/path")
        finally:
            scanner.scan = orig_scan
            dashboard.time.sleep = orig_sleep

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["projects_dir"], "/custom/path")
        self.assertFalse(captured[0]["verbose"])

    def test_projects_dir_none_when_not_specified(self):
        import dashboard
        import scanner

        captured = []

        class _Stop(BaseException):
            pass

        def fake_scan(projects_dir=None, verbose=True):
            captured.append(projects_dir)
            raise _Stop()

        orig_scan = scanner.scan
        orig_sleep = dashboard.time.sleep
        scanner.scan = fake_scan
        dashboard.time.sleep = lambda _: None
        try:
            with self.assertRaises(_Stop):
                dashboard._periodic_scan_loop(1)
        finally:
            scanner.scan = orig_scan
            dashboard.time.sleep = orig_sleep

        self.assertEqual(captured, [None])


class TestScanSerialization(unittest.TestCase):
    """定期スキャンと /api/rescan が同じロックで直列化されることを担保。

    両エントリポイント (``_periodic_scan_loop`` と ``DashboardHandler.do_POST``)
    は同一のモジュールロック ``_scan_lock`` を使って ``scan()`` を
    ``with _scan_lock:`` で保護している。本テストは、そのロックが実際に
    同時実行を阻止することを確認する。
    """

    def test_scan_lock_is_a_lock(self):
        self.assertTrue(hasattr(_scan_lock, "acquire"))
        self.assertTrue(hasattr(_scan_lock, "release"))

    def test_concurrent_scans_are_serialized(self):
        import time as _time

        state = {"current": 0, "max": 0}
        counter_lock = threading.Lock()
        total_calls = {"n": 0}

        def instrumented():
            with counter_lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
                total_calls["n"] += 1
            # 他スレッドが並行突入できるだけの時間、ロックを保持
            _time.sleep(0.05)
            with counter_lock:
                state["current"] -= 1

        def worker():
            # dashboard.py と同じイディオムで保護して scan 相当処理を走らせる
            with _scan_lock:
                instrumented()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(total_calls["n"], 4)
        self.assertEqual(state["max"], 1,
                         "共通ロックが効いていない: scan 相当処理が並行実行された")

    def test_periodic_loop_acquires_scan_lock(self):
        """_periodic_scan_loop 内の scan 呼び出しは _scan_lock 保護下で走る。"""
        import scanner

        lock_states = []

        class _Stop(BaseException):
            pass

        def fake_scan(projects_dir=None, verbose=True):
            # scan 実行中はロックが保持されているはず (acquire() すると False)
            acquired = _scan_lock.acquire(blocking=False)
            lock_states.append(acquired)
            if acquired:
                _scan_lock.release()
            raise _Stop()

        orig_scan = scanner.scan
        orig_sleep = dashboard_module.time.sleep
        scanner.scan = fake_scan
        dashboard_module.time.sleep = lambda _: None
        try:
            with self.assertRaises(_Stop):
                dashboard_module._periodic_scan_loop(1)
        finally:
            scanner.scan = orig_scan
            dashboard_module.time.sleep = orig_sleep

        self.assertEqual(lock_states, [False],
                         "_periodic_scan_loop が scan 呼び出し時にロックを保持していない")


class TestResolveScanInterval(unittest.TestCase):
    """SCAN_INTERVAL_SEC のパース失敗でサーバーが落ちないことを担保。"""

    def setUp(self):
        self._orig = os.environ.pop("SCAN_INTERVAL_SEC", None)

    def tearDown(self):
        if self._orig is not None:
            os.environ["SCAN_INTERVAL_SEC"] = self._orig
        else:
            os.environ.pop("SCAN_INTERVAL_SEC", None)

    def test_explicit_value_wins(self):
        self.assertEqual(_resolve_scan_interval(60), 60)
        # None 以外が来たらそのまま通す（0 = 無効化もそのまま通す）
        self.assertEqual(_resolve_scan_interval(0), 0)

    def test_default_when_env_missing(self):
        self.assertEqual(_resolve_scan_interval(None), DEFAULT_SCAN_INTERVAL_SEC)

    def test_env_integer_parsed(self):
        os.environ["SCAN_INTERVAL_SEC"] = "120"
        self.assertEqual(_resolve_scan_interval(None), 120)

    def test_env_invalid_falls_back(self):
        os.environ["SCAN_INTERVAL_SEC"] = "300s"
        self.assertEqual(_resolve_scan_interval(None), DEFAULT_SCAN_INTERVAL_SEC)

    def test_env_garbage_falls_back(self):
        os.environ["SCAN_INTERVAL_SEC"] = "not-a-number"
        self.assertEqual(_resolve_scan_interval(None), DEFAULT_SCAN_INTERVAL_SEC)


class TestPricingParity(unittest.TestCase):
    """Verify CLI and dashboard pricing tables stay in sync."""

    def _extract_js_pricing(self):
        """Extract pricing values from the dashboard JS PRICING object."""
        import re
        prices = {}
        for match in re.finditer(
            r"'(claude-[^']+)':\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)",
            HTML_TEMPLATE
        ):
            model, inp, out = match.group(1), float(match.group(2)), float(match.group(3))
            prices[model] = {"input": inp, "output": out}
        return prices

    def test_all_cli_models_in_dashboard(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertIn(model, js_prices, f"{model} missing from dashboard JS")

    def test_prices_match(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertAlmostEqual(
                CLI_PRICING[model]["input"], js_prices[model]["input"],
                msg=f"{model} input price mismatch"
            )
            self.assertAlmostEqual(
                CLI_PRICING[model]["output"], js_prices[model]["output"],
                msg=f"{model} output price mismatch"
            )


if __name__ == "__main__":
    unittest.main()
