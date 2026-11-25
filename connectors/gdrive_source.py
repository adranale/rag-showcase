import json
import os
import re
from collections.abc import Callable, Iterator

import httpx

from connectors.base import (
    GOOGLE_DOC_MIME,
    MAX_FILE_BYTES,
    ConnectorError,
    RemoteFile,
    classify,
)

API_URL = "https://www.googleapis.com/drive/v3"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# drive.google.com/drive/folders/<id>, .../drive/u/0/folders/<id>, ...?id=<id>
FOLDER_LINK_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
)
TIMEOUT = 60.0
FIELDS = "nextPageToken,files(id,name,mimeType,md5Checksum,version,size,webViewLink)"


def parse_folder_id(value: str) -> str | None:
    """The folder ID from a Google Drive folder link or a bare ID, else None."""
    value = value.strip()
    if FOLDER_ID_PATTERN.match(value):
        return value
    if not value.startswith(("https://drive.google.com/", "drive.google.com/")):
        return None
    for pattern in FOLDER_LINK_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def service_account_token_provider(info: dict) -> Callable[[], str]:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )

    def provider() -> str:
        if not credentials.valid:
            credentials.refresh(Request())
        return credentials.token

    return provider


class OAuthTokenProvider:
    """Access tokens from a user's refresh token; `reset()` forces a new one."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        client: httpx.Client,
    ) -> None:
        self._data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        self._client = client
        self._token: str | None = None

    def __call__(self) -> str:
        if self._token is None:
            response = self._client.post(OAUTH_TOKEN_URL, data=self._data)
            if response.status_code != 200:
                raise ConnectorError(
                    f"Google token refresh failed (HTTP {response.status_code})"
                )
            self._token = response.json()["access_token"]
        return self._token

    def reset(self) -> None:
        self._token = None


def oauth_token_provider(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    client: httpx.Client,
) -> OAuthTokenProvider:
    return OAuthTokenProvider(client_id, client_secret, refresh_token, client)


class GoogleDriveSource:
    """Read-only Google Drive source (scope: drive.readonly).

    ``folder`` is a Drive folder ID. Per-user access uses the user's OAuth refresh
    token (`for_user`); with a service account (`from_env`), the folder must be
    shared with the service account's e-mail address.
    """

    provider = "google_drive"

    def __init__(
        self,
        folder: str,
        token_provider: Callable[[], str],
        client: httpx.Client | None = None,
    ) -> None:
        if not FOLDER_ID_PATTERN.match(folder):
            raise ConnectorError(
                "The Google Drive folder ID is invalid (letters, digits, - and _)"
            )
        self._folder = folder
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=TIMEOUT)

    @classmethod
    def for_user(
        cls,
        folder: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        client: httpx.Client | None = None,
    ):
        client = client or httpx.Client(timeout=TIMEOUT)
        provider = OAuthTokenProvider(client_id, client_secret, refresh_token, client)
        return cls(folder, provider, client)

    @classmethod
    def from_env(cls, folder: str, client: httpx.Client | None = None):
        client = client or httpx.Client(timeout=TIMEOUT)
        service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if service_account_json:
            try:
                info = json.loads(service_account_json)
                provider = service_account_token_provider(info)
            except (ValueError, KeyError) as exc:
                raise ConnectorError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is not a valid service account key"
                ) from exc
            return cls(folder, provider, client)

        names = (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_REFRESH_TOKEN",
        )
        values = {name: os.getenv(name, "") for name in names}
        if not all(values.values()):
            raise ConnectorError(
                "Set GOOGLE_SERVICE_ACCOUNT_JSON, or all of: " + ", ".join(names)
            )
        provider = oauth_token_provider(*(values[name] for name in names), client)
        return cls(folder, provider, client)

    @property
    def account_identifier(self) -> str:
        return self._folder

    def _request(self, path: str, params: dict) -> httpx.Response:
        for attempt in range(2):
            response = self._client.get(
                f"{API_URL}{path}",
                params={"supportsAllDrives": "true", **params},
                headers={"Authorization": f"Bearer {self._token_provider()}"},
            )
            reset = getattr(self._token_provider, "reset", None)
            if response.status_code == 401 and attempt == 0 and reset is not None:
                # The access token (valid for about an hour) expired during a long run.
                reset()
                continue
            return response
        return response

    def _get(self, path: str, params: dict) -> httpx.Response:
        response = self._request(path, params)
        if response.status_code != 200:
            raise ConnectorError(
                f"Google Drive request failed (HTTP {response.status_code})"
            )
        return response

    def check_folder(self) -> str | None:
        """The folder's name, or None if it is not a folder the user can read."""
        response = self._request(
            f"/files/{self._folder}", {"fields": "id,name,mimeType,trashed"}
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ConnectorError(
                f"Google Drive request failed (HTTP {response.status_code})"
            )
        item = response.json()
        if item.get("mimeType") != FOLDER_MIME or item.get("trashed"):
            return None
        return item.get("name") or self._folder

    def _children(self, folder_id: str) -> Iterator[dict]:
        page_token: str | None = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": FIELDS,
                "pageSize": "1000",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            page = self._get("/files", params).json()
            yield from page.get("files", [])
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def list_files(self) -> Iterator[RemoteFile]:
        pending = [self._folder]
        visited = set(pending)
        while pending:
            for item in self._children(pending.pop()):
                if item["mimeType"] == FOLDER_MIME:
                    if item["id"] not in visited:
                        visited.add(item["id"])
                        pending.append(item["id"])
                    continue
                remote = self._to_remote(item)
                if remote is not None:
                    yield remote

    @staticmethod
    def _to_remote(item: dict) -> RemoteFile | None:
        export_mime = None
        if item["mimeType"] == GOOGLE_DOC_MIME:
            kind, mime_type = "text", "text/plain"
            export_mime = "text/plain"
        else:
            classified = classify(item["name"])
            if classified is None:
                return None
            kind, mime_type = classified
        size = int(item["size"]) if "size" in item else None
        if size is not None and size > MAX_FILE_BYTES:
            return None
        version = item.get("md5Checksum") or str(item.get("version", ""))
        if not version:
            return None
        return RemoteFile(
            file_id=item["id"],
            name=item["name"],
            version=version,
            kind=kind,
            mime_type=mime_type,
            size=size,
            url=item.get("webViewLink"),
            export_mime=export_mime,
        )

    def download(self, remote: RemoteFile) -> bytes:
        if remote.export_mime:
            return self._get(
                f"/files/{remote.file_id}/export", {"mimeType": remote.export_mime}
            ).content
        return self._get(f"/files/{remote.file_id}", {"alt": "media"}).content
