#!/usr/bin/env python3
"""Publish build files to a draft in the Moaz Elgabry Releases WordPress plugin."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_API_ORIGIN = "https://moazelgabry.com/wp-json/moaz-releases/v1"
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TARGET_PART = re.compile(r"^[a-z0-9_-]+$")


class PublishError(RuntimeError):
    """A safe-to-display publishing error."""


@dataclass(frozen=True)
class Artifact:
    platform: str
    architecture: str
    path: Path
    size: int
    sha256: str


def _redact(message: str, values: tuple[str, ...]) -> str:
    for value in values:
        if value:
            message = message.replace(value, "[REDACTED]")
    return message.replace("Authorization", "[REDACTED-HEADER]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "WordPress API redirected the request", headers, fp)


class WordPressReleaseClient:
    def __init__(self, api_origin: str, username: str, password: str) -> None:
        self.api_origin = api_origin.rstrip("/")
        if not self.api_origin.startswith("https://"):
            raise PublishError("WordPress API origin must use HTTPS")
        if not username or not password:
            raise PublishError("MOAZ_ER_USERNAME and MOAZ_ER_APPLICATION_PASSWORD are required")
        self.username = username
        self.password = password
        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "User-Agent": "Moaz-Release-Publisher/1",
        }
        self._redactions = (username, password, basic)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _open(self, request: urllib.request.Request, timeout: int) -> dict[str, object]:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", "replace")
            except OSError:
                detail = "HTTP request failed"
            detail = _redact(detail, self._redactions)
            response_context: list[str] = []
            if error.headers is not None:
                for header in (
                    "Content-Type",
                    "Server",
                    "X-Hcdn-Request-Id",
                    "X-Request-Id",
                    "CF-Ray",
                    "X-LiteSpeed-Cache",
                ):
                    value = error.headers.get(header)
                    if value:
                        response_context.append(f"{header}={value[:160]}")
            context = f" ({'; '.join(response_context)})" if response_context else ""
            raise PublishError(f"WordPress API returned HTTP {error.code}{context}: {detail[:500]}") from None
        except (urllib.error.URLError, OSError) as error:
            detail = _redact(str(error), self._redactions)
            raise PublishError(f"WordPress API request failed: {detail[:500]}") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PublishError("WordPress API returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise PublishError("WordPress API returned an invalid response object")
        return payload

    def _json(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            self.api_origin + path,
            data=body,
            headers={**self._headers, "Content-Type": "application/json", "Content-Length": str(len(body))},
            method="POST",
        )
        return self._open(request, timeout=120)

    def check_health(self) -> None:
        request = urllib.request.Request(
            self.api_origin + "/automation/health",
            headers=self._headers,
            method="GET",
        )
        health = self._open(request, timeout=30)
        if health.get("ready") is not True:
            raise PublishError("WordPress release storage is not ready")

    @staticmethod
    def _get_release_id(response: Mapping[str, object]) -> int:
        release = response.get("release")
        try:
            release_id = int(release["id"]) if isinstance(release, Mapping) else 0
        except (KeyError, TypeError, ValueError):
            release_id = 0
        if release_id < 1:
            raise PublishError("WordPress API create response did not contain a valid release ID")
        return release_id

    def _upload(self, release_id: int, artifact: Artifact) -> None:
        boundary = "----MoazRelease" + os.urandom(12).hex()
        filename = artifact.path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="artifact"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        content_length = len(prefix) + artifact.size + len(suffix)
        # Spool to disk so large build artifacts do not need to fit in runner RAM.
        with tempfile.TemporaryFile() as body:
            body.write(prefix)
            with artifact.path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    body.write(chunk)
            body.write(suffix)
            body.seek(0)
            request = urllib.request.Request(
                f"{self.api_origin}/automation/releases/{release_id}/artifacts/{artifact.platform}/{artifact.architecture}",
                data=body,  # type: ignore[arg-type]
                headers={
                    **self._headers,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(content_length),
                    "X-Artifact-Size": str(artifact.size),
                    "X-Artifact-SHA256": artifact.sha256,
                },
                method="POST",
            )
            self._open(request, timeout=300)

    def publish_draft(
        self,
        project_slug: str,
        version: str,
        source_commit: str,
        source_run: str,
        artifacts: list[Artifact],
        channel: str = "dev",
    ) -> tuple[int, list[str]]:
        self.check_health()
        release_id = self.create_draft(project_slug, version, channel)
        self.upload_artifacts(release_id, artifacts)
        missing = self.complete_draft(release_id, source_commit, source_run)
        return release_id, missing

    def create_draft(self, project_slug: str, version: str, channel: str) -> int:
        if channel not in {"dev", "private"}:
            raise PublishError("channel must be dev or private")
        created = self._json(
            "/automation/releases",
            {"channel": channel, "plugin": project_slug, "version": version},
        )
        return self._get_release_id(created)

    def upload_artifacts(self, release_id: int, artifacts: list[Artifact]) -> None:
        if release_id < 1:
            raise PublishError("release-id must be a positive integer")
        for artifact in artifacts:
            self._upload(release_id, artifact)

    def complete_draft(self, release_id: int, source_commit: str, source_run: str) -> list[str]:
        if release_id < 1:
            raise PublishError("release-id must be a positive integer")
        completed = self._json(
            f"/automation/releases/{release_id}/complete",
            {"source_commit": source_commit, "source_run": source_run},
        )
        missing = completed.get("missing", [])
        if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
            raise PublishError("WordPress API returned invalid draft readiness details")
        return missing


def _load_artifacts(raw: str) -> list[Artifact]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PublishError(f"artifacts must be a JSON array: {error.msg}") from None
    if not isinstance(items, list) or not items:
        raise PublishError("artifacts must be a non-empty JSON array")

    artifacts: list[Artifact] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PublishError(f"artifact {index} must be an object")
        platform = item.get("platform")
        architecture = item.get("architecture")
        raw_path = item.get("path")
        if not isinstance(platform, str) or not _TARGET_PART.fullmatch(platform):
            raise PublishError(f"artifact {index} has an invalid platform")
        if not isinstance(architecture, str) or not _TARGET_PART.fullmatch(architecture):
            raise PublishError(f"artifact {index} has an invalid architecture")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise PublishError(f"artifact {index} has no file path")
        target = (platform, architecture)
        if target in seen:
            raise PublishError(f"duplicate artifact target: {platform}/{architecture}")
        seen.add(target)
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise PublishError(f"artifact file does not exist: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        if size == 0:
            raise PublishError(f"artifact file is empty: {path}")
        artifacts.append(Artifact(platform, architecture, path, size, digest.hexdigest()))
    return artifacts


def main() -> int:
    project_slug = os.environ.get("INPUT_PROJECT_SLUG", "")
    version = os.environ.get("INPUT_VERSION", "").strip()
    source_commit = os.environ.get("INPUT_SOURCE_COMMIT", "").strip()
    source_run = os.environ.get("INPUT_SOURCE_RUN", "").strip()
    channel = os.environ.get("INPUT_CHANNEL", "dev").strip() or "dev"
    phase = os.environ.get("INPUT_PHASE", "publish").strip() or "publish"
    raw_release_id = os.environ.get("INPUT_RELEASE_ID", "").strip()
    username = os.environ.get("MOAZ_ER_USERNAME", "")
    password = os.environ.get("MOAZ_ER_APPLICATION_PASSWORD", "")
    basic = base64.b64encode(f"{username}:{password}".encode()).decode("ascii") if username and password else ""
    redactions = (username, password, basic)
    try:
        if not _SLUG.fullmatch(project_slug):
            raise PublishError("project-slug must be a registered lowercase slug using letters, digits, and hyphens")
        client = WordPressReleaseClient(
            DEFAULT_API_ORIGIN,
            username,
            password,
        )
        release_id = 0
        missing: list[str] = []
        if phase == "health":
            client.check_health()
        elif phase == "create":
            if not version or any(char in version for char in "\r\n"):
                raise PublishError("version is required and must be a single line when creating a release draft")
            release_id = client.create_draft(project_slug, version, channel)
        elif phase == "upload":
            if not raw_release_id.isdigit() or int(raw_release_id) < 1:
                raise PublishError("release-id must be a positive integer for artifact upload")
            artifacts = _load_artifacts(os.environ.get("INPUT_ARTIFACTS", ""))
            release_id = int(raw_release_id)
            client.upload_artifacts(release_id, artifacts)
        elif phase == "complete":
            if not raw_release_id.isdigit() or int(raw_release_id) < 1:
                raise PublishError("release-id must be a positive integer to complete a release draft")
            if not source_commit or not source_run:
                raise PublishError("source-commit and source-run are required to complete a draft")
            release_id = int(raw_release_id)
            missing = client.complete_draft(release_id, source_commit, source_run)
        elif phase == "publish":
            if not version or any(char in version for char in "\r\n") or not source_commit or not source_run:
                raise PublishError("version, source-commit and source-run are required")
            artifacts = _load_artifacts(os.environ.get("INPUT_ARTIFACTS", ""))
            release_id, missing = client.publish_draft(
                project_slug, version, source_commit, source_run, artifacts, channel
            )
        else:
            raise PublishError("phase must be health, create, upload, complete, or publish")
    except (OSError, PublishError) as error:
        print(f"release draft upload failed: {_redact(str(error), redactions)}", file=sys.stderr)
        return 2

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"release-id={release_id}\n")
            output.write(f"missing-targets={','.join(missing)}\n")
    print(f"Release draft phase '{phase}' completed" + (f" for ID {release_id}." if release_id else "."))
    if missing:
        print(f"::warning::Draft is missing configured target(s): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
