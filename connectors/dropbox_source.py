import json
import os
from collections.abc import Iterator

import httpx

from connectors.base import (
    MAX_FILE_BYTES,
    ConnectorError,
    RemoteFile,
    classify,
)

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
API_URL = "https://api.dropboxapi.com/2"
CONTENT_URL = "https://content.dropboxapi.com/2"
TIMEOUT = 60.0


class DropboxSource:
    """Read-only Dropbox source (scopes: files.metadata.read, files.content.read)."""

    provider = "dropbox"

    def __init__(
        self,
        folder: str,
        app_key: str,
        app_secret: str,
        refresh_token: str,
        client: httpx.Client | None = None,
    ) -> None:
        stripped = folder.strip().strip("/")
        self._folder = f"/{stripped}" if stripped else ""
        self._app_key = app_key
        self._app_secret = app_secret
        self._refresh_token = refresh_token
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._access_token: str | None = None

    @classmethod
    def from_env(cls, folder: str, client: httpx.Client | None = None):
        names = ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
        values = {name: os.getenv(name, "") for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConnectorError("Missing environment variables: " + ", ".join(missing))
        return cls(folder, *(values[name] for name in names), client=client)

    @property
    def account_identifier(self) -> str:
        return self._folder or "/"

    def _token(self) -> str:
        if self._access_token is None:
            response = self._client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                auth=(self._app_key, self._app_secret),
            )
            if response.status_code != 200:
                raise ConnectorError(
                    f"Dropbox token refresh failed (HTTP {response.status_code})"
                )
            self._access_token = response.json()["access_token"]
        return self._access_token

    def _post(self, url: str, **kwargs) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token()}", **extra_headers}
            response = self._client.post(url, headers=headers, **kwargs)
            if response.status_code == 401 and attempt == 0:
                # The short-lived access token expired during a long run.
                self._access_token = None
                continue
            break
        if response.status_code != 200:
            raise ConnectorError(
                f"Dropbox request failed (HTTP {response.status_code})"
            )
        return response

    def list_files(self) -> Iterator[RemoteFile]:
        response = self._post(
            f"{API_URL}/files/list_folder",
            json={"path": self._folder, "recursive": True, "limit": 500},
        )
        while True:
            page = response.json()
            for entry in page["entries"]:
                remote = self._to_remote(entry)
                if remote is not None:
                    yield remote
            if not page.get("has_more"):
                return
            response = self._post(
                f"{API_URL}/files/list_folder/continue",
                json={"cursor": page["cursor"]},
            )

    @staticmethod
    def _to_remote(entry: dict) -> RemoteFile | None:
        if entry.get(".tag") != "file":
            return None
        kind = classify(entry["name"])
        size = entry.get("size")
        if kind is None or (size is not None and size > MAX_FILE_BYTES):
            return None
        return RemoteFile(
            file_id=entry["id"],
            name=entry["name"],
            version=entry["rev"],
            kind=kind[0],
            mime_type=kind[1],
            size=size,
            url="https://www.dropbox.com/home" + entry.get("path_display", ""),
        )

    def download(self, remote: RemoteFile) -> bytes:
        response = self._post(
            f"{CONTENT_URL}/files/download",
            headers={"Dropbox-API-Arg": json.dumps({"path": remote.file_id})},
        )
        return response.content
