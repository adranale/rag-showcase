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
TIMEOUT = 60.0
FIELDS = "nextPageToken,files(id,name,mimeType,md5Checksum,version,size,webViewLink)"


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


def oauth_token_provider(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    client: httpx.Client,
) -> Callable[[], str]:
    cached: dict[str, str] = {}

    def provider() -> str:
        if "token" not in cached:
            response = client.post(
                OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                },
            )
            if response.status_code != 200:
                raise ConnectorError(
                    f"Google token refresh failed (HTTP {response.status_code})"
                )
            cached["token"] = response.json()["access_token"]
        return cached["token"]

    return provider


class GoogleDriveSource:
    """Read-only Google Drive source (scope: drive.readonly).

    ``folder`` is a Drive folder ID. With a service account, the folder must be
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
                "--folder must be a Google Drive folder ID (letters, digits, - and _)"
            )
        self._folder = folder
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=TIMEOUT)

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

    def _get(self, path: str, params: dict) -> httpx.Response:
        response = self._client.get(
            f"{API_URL}{path}",
            params={"supportsAllDrives": "true", **params},
            headers={"Authorization": f"Bearer {self._token_provider()}"},
        )
        if response.status_code != 200:
            raise ConnectorError(
                f"Google Drive request failed (HTTP {response.status_code})"
            )
        return response

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
