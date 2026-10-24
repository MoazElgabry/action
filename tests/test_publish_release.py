from __future__ import annotations

import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.publish_release as publisher


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class PublishActionTests(unittest.TestCase):
    def test_complete_phase_does_not_require_version(self) -> None:
        with patch.dict(publisher.os.environ, {
            "INPUT_PROJECT_SLUG": "bgrade",
            "INPUT_PHASE": "complete",
            "INPUT_RELEASE_ID": "6",
            "INPUT_SOURCE_COMMIT": "abc123",
            "INPUT_SOURCE_RUN": "40100081749",
            "MOAZ_ER_USERNAME": "account",
            "MOAZ_ER_APPLICATION_PASSWORD": "app-password",
        }, clear=True):
            client = unittest.mock.Mock()
            client.complete_draft.return_value = []
            with patch.object(publisher, "WordPressReleaseClient", return_value=client):
                self.assertEqual(publisher.main(), 0)

        client.complete_draft.assert_called_once_with(6, "abc123", "40100081749")

    def test_http_errors_report_safe_host_diagnostics(self) -> None:
        headers = Message()
        headers["Content-Type"] = "text/html"
        headers["Server"] = "hcdn"
        headers["X-Hcdn-Request-Id"] = "trace-123"
        error = HTTPError(
            "https://example.test/api/upload",
            403,
            "Forbidden",
            headers,
            None,
        )

        with patch.object(publisher.urllib.request.OpenerDirector, "open", side_effect=error):
            client = publisher.WordPressReleaseClient("https://example.test/api", "account", "app-password")
            request = Request("https://example.test/api/upload")
            with self.assertRaisesRegex(
                publisher.PublishError,
                r"HTTP 403 \(Content-Type=text/html; Server=hcdn; X-Hcdn-Request-Id=trace-123\)",
            ):
                client._open(request, timeout=30)

    def test_generic_project_draft_uploads_artifacts_and_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact_path = Path(temporary) / "tool.pkg"
            artifact_path.write_bytes(b"sample package")
            requests: list[tuple[str, str, bytes, dict[str, str]]] = []

            def fake_open(request: Request, timeout: int = 0) -> _Response:
                body = request.data
                raw = body.read() if hasattr(body, "read") else body or b""
                requests.append((request.full_url, request.method, raw, dict(request.header_items())))
                if request.full_url.endswith("/automation/health"):
                    return _Response({"ready": True, "free_bytes": 1024, "message": "ready"})
                if request.full_url.endswith("/automation/releases"):
                    return _Response({"release": {"id": 27}})
                if request.full_url.endswith("/complete"):
                    return _Response({"release": {"id": 27}, "missing": []})
                return _Response({"artifact": {"id": 80}})

            with patch.object(publisher.urllib.request.OpenerDirector, "open", side_effect=fake_open):
                client = publisher.WordPressReleaseClient("https://example.test/api", "account", "app-password")
                artifacts = publisher._load_artifacts(json.dumps([
                    {"platform": "windows", "architecture": "arm64", "path": str(artifact_path)}
                ]))
                release_id, missing = client.publish_draft("future-tool", "0.4.0-dev.2", "abc123", "91", artifacts)

        self.assertEqual(release_id, 27)
        self.assertEqual(missing, [])
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[0][1], "GET")
        self.assertTrue(requests[0][0].endswith("/automation/health"))
        self.assertTrue(requests[2][0].endswith("/artifacts/windows/arm64"))
        self.assertIn(b"sample package", requests[2][2])
        self.assertEqual(requests[2][3]["X-artifact-size"], str(len(b"sample package")))
        self.assertEqual(requests[3][3]["Content-type"], "application/json")
        self.assertEqual(json.loads(requests[3][2]), {"source_commit": "abc123", "source_run": "91"})

    def test_health_phase_checks_wordpress_without_creating_a_draft(self) -> None:
        requests: list[Request] = []

        def fake_open(request: Request, timeout: int = 0) -> _Response:
            requests.append(request)
            return _Response({"ready": True, "free_bytes": 2048, "message": "ready"})

        with patch.object(publisher.urllib.request.OpenerDirector, "open", side_effect=fake_open):
            client = publisher.WordPressReleaseClient("https://example.test/api", "account", "app-password")
            client.check_health()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertTrue(requests[0].full_url.endswith("/automation/health"))

    def test_unready_wordpress_storage_stops_before_draft_creation(self) -> None:
        def fake_open(request: Request, timeout: int = 0) -> _Response:
            return _Response({"ready": False, "free_bytes": 0, "message": "not ready"})

        with patch.object(publisher.urllib.request.OpenerDirector, "open", side_effect=fake_open):
            client = publisher.WordPressReleaseClient("https://example.test/api", "account", "app-password")
            with self.assertRaisesRegex(publisher.PublishError, "storage is not ready"):
                client.publish_draft("future-tool", "0.4.0-dev.2", "abc123", "91", [])

    def test_private_release_uses_numeric_version_and_private_channel(self) -> None:
        requests: list[dict[str, object]] = []

        def fake_open(request: Request, timeout: int = 0) -> _Response:
            requests.append(json.loads(request.data.decode("utf-8")))
            return _Response({"release": {"id": 88}})

        with patch.object(publisher.urllib.request.OpenerDirector, "open", side_effect=fake_open):
            client = publisher.WordPressReleaseClient("https://example.test/api", "account", "app-password")
            release_id = client.create_draft("chromonotgen", "0.4.0", "private")

        self.assertEqual(release_id, 88)
        self.assertEqual(requests, [{"channel": "private", "plugin": "chromonotgen", "version": "0.4.0"}])

    def test_unknown_automation_channel_is_rejected_before_api_request(self) -> None:
        client = publisher.WordPressReleaseClient("https://example.test/api", "account", "app-password")
        with self.assertRaisesRegex(publisher.PublishError, "channel must be dev or private"):
            client.create_draft("chromonotgen", "0.4.0", "stable")

    def test_duplicate_target_is_rejected_before_any_api_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact_path = Path(temporary) / "package.zip"
            artifact_path.write_bytes(b"package")
            with self.assertRaisesRegex(publisher.PublishError, "duplicate artifact target"):
                publisher._load_artifacts(json.dumps([
                    {"platform": "linux", "architecture": "x86_64", "path": str(artifact_path)},
                    {"platform": "linux", "architecture": "x86_64", "path": str(artifact_path)},
                ]))

    def test_non_https_api_origin_is_rejected(self) -> None:
        with self.assertRaisesRegex(publisher.PublishError, "must use HTTPS"):
            publisher.WordPressReleaseClient("http://example.test/api", "account", "app-password")


if __name__ == "__main__":
    unittest.main()
