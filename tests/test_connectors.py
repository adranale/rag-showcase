import json

import httpx
import pytest

from connectors.base import ConnectorError, RemoteFile
from connectors.chunking import ChunkedDocument
from connectors.dropbox_source import DropboxSource
from connectors.gdrive_source import GoogleDriveSource
from connectors.store import StoredDocument
from connectors.sync import sync


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_dropbox_lists_filters_and_paginates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path == "/2/files/list_folder":
            assert json.loads(request.content)["path"] == "/docs"
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {".tag": "folder", "name": "sub", "id": "id:f"},
                        {
                            ".tag": "file",
                            "name": "a.md",
                            "id": "id:a",
                            "rev": "r1",
                            "size": 10,
                            "path_display": "/docs/a.md",
                        },
                        {".tag": "file", "name": "x.exe", "id": "id:x", "rev": "r"},
                    ],
                    "has_more": True,
                    "cursor": "c1",
                },
            )
        assert json.loads(request.content) == {"cursor": "c1"}
        return httpx.Response(
            200,
            json={
                "entries": [
                    {".tag": "file", "name": "b.PDF", "id": "id:b", "rev": "r2"}
                ],
                "has_more": False,
                "cursor": "c2",
            },
        )

    source = DropboxSource("docs/", "key", "secret", "refresh", make_client(handler))
    files = list(source.list_files())

    assert [(f.file_id, f.kind, f.version) for f in files] == [
        ("id:a", "text", "r1"),
        ("id:b", "pdf", "r2"),
    ]
    assert source.account_identifier == "/docs"
    assert calls.count("/oauth2/token") == 1


def test_dropbox_download_uses_id_and_refreshes_expired_token():
    tokens = iter(["old", "new"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens)})
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        assert json.loads(request.headers["Dropbox-API-Arg"]) == {"path": "id:a"}
        return httpx.Response(200, content=b"hello")

    source = DropboxSource("/", "k", "s", "r", make_client(handler))
    remote = RemoteFile("id:a", "a.txt", "r1", "text", "text/plain")

    assert source.download(remote) == b"hello"


def test_dropbox_errors_do_not_leak_secrets():
    source = DropboxSource(
        "/",
        "k",
        "s",
        "super-secret-refresh",
        make_client(lambda r: httpx.Response(400)),
    )
    with pytest.raises(ConnectorError) as error:
        list(source.list_files())
    assert "super-secret-refresh" not in str(error.value)


def test_dropbox_from_env_requires_all_variables(monkeypatch):
    for name in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DROPBOX_APP_KEY", "k")

    with pytest.raises(ConnectorError, match="DROPBOX_APP_SECRET"):
        DropboxSource.from_env("/")


def test_gdrive_lists_recursively_and_exports_docs():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path == "/drive/v3/files":
            query = request.url.params["q"]
            if "'root1' in parents" in query:
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            {
                                "id": "sub",
                                "name": "sub",
                                "mimeType": "application/vnd.google-apps.folder",
                            },
                            {
                                "id": "f1",
                                "name": "notes.txt",
                                "mimeType": "text/plain",
                                "md5Checksum": "abc",
                                "size": "5",
                            },
                            {
                                "id": "f2",
                                "name": "sheet.xlsx",
                                "mimeType": "application/x",
                                "md5Checksum": "z",
                            },
                        ]
                    },
                )
            assert "'sub' in parents" in query
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "d1",
                            "name": "Policy",
                            "mimeType": "application/vnd.google-apps.document",
                            "version": "7",
                        }
                    ]
                },
            )
        if path == "/drive/v3/files/d1/export":
            assert request.url.params["mimeType"] == "text/plain"
            return httpx.Response(200, content=b"exported")
        assert path == "/drive/v3/files/f1" and request.url.params["alt"] == "media"
        return httpx.Response(200, content=b"raw")

    source = GoogleDriveSource("root1", lambda: "tok", make_client(handler))
    files = {f.file_id: f for f in source.list_files()}

    assert set(files) == {"f1", "d1"}
    assert files["f1"].version == "abc"
    assert files["d1"].version == "7" and files["d1"].export_mime == "text/plain"
    assert source.download(files["d1"]) == b"exported"
    assert source.download(files["f1"]) == b"raw"


def test_gdrive_rejects_folder_that_is_not_an_id():
    with pytest.raises(ConnectorError):
        GoogleDriveSource("x' or '1'='1", lambda: "tok")


def test_gdrive_from_env_requires_credentials(monkeypatch):
    for name in (
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConnectorError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        GoogleDriveSource.from_env("folder1")


def test_gdrive_invalid_service_account_json_is_reported_without_content(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"private_key": "SECRET"}')

    with pytest.raises(ConnectorError) as error:
        GoogleDriveSource.from_env("folder1")
    assert "SECRET" not in str(error.value)


class FakeConnector:
    provider = "dropbox"
    account_identifier = "/docs"

    def __init__(self, files: list[RemoteFile], fail_ids=()):
        self.files = files
        self.fail_ids = set(fail_ids)
        self.downloads: list[str] = []

    def list_files(self):
        yield from self.files

    def download(self, remote: RemoteFile) -> bytes:
        self.downloads.append(remote.file_id)
        if remote.file_id in self.fail_ids:
            raise ConnectorError("boom")
        return remote.version.encode()


class FakeChunker:
    def chunk(self, kind: str, data: bytes) -> ChunkedDocument:
        return ChunkedDocument(data.decode()[:1], "v-test", [{"text": "t"}])


class MemoryStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.synced = 0

    def ensure_connection(self, provider, account_identifier):
        return 1

    def documents(self, connection_id):
        return {
            ext: StoredDocument(i, d["version"], d["hash"], d["status"])
            for i, (ext, d) in enumerate(self.docs.items())
        }

    def save(self, connection_id, remote, document):
        self.docs[remote.file_id] = {
            "version": remote.version,
            "hash": document.content_hash,
            "status": "pending",
        }

    def update_version(self, document_id, version):
        list(self.docs.values())[document_id]["version"] = version

    def mark_failed(self, connection_id, remote):
        self.docs.setdefault(remote.file_id, {"version": None, "hash": None})
        self.docs[remote.file_id]["status"] = "failed"

    def remove(self, connection_id, external_ids, delete):
        for ext in external_ids:
            if delete:
                del self.docs[ext]
            else:
                self.docs[ext]["status"] = "removed"

    def mark_synced(self, connection_id):
        self.synced += 1


def remote(file_id: str, version: str) -> RemoteFile:
    return RemoteFile(file_id, f"{file_id}.txt", version, "text", "text/plain")


def test_sync_ingests_then_skips_unchanged_and_reingests_changed():
    store = MemoryStore()
    first = FakeConnector([remote("a", "1"), remote("b", "2")])
    result = sync(first, store, FakeChunker())
    assert sorted(result.ingested) == ["a", "b"]

    second = FakeConnector([remote("a", "1"), remote("b", "9")])
    result = sync(second, store, FakeChunker())

    assert result.ingested == ["b"] and result.skipped == ["a"]
    assert second.downloads == ["b"]
    assert store.synced == 2


def test_sync_skips_reingest_when_only_version_changed():
    store = MemoryStore()
    sync(FakeConnector([remote("a", "1")]), store, FakeChunker())

    class SameContent(FakeChunker):
        def chunk(self, kind, data):
            return ChunkedDocument("1", "v-test", [{"text": "t"}])

    result = sync(FakeConnector([remote("a", "5")]), store, SameContent())

    assert result.skipped == ["a"] and result.ingested == []
    assert store.docs["a"]["version"] == "5"


def test_sync_marks_removed_files_and_can_delete():
    store = MemoryStore()
    sync(FakeConnector([remote("a", "1"), remote("b", "2")]), store, FakeChunker())

    result = sync(FakeConnector([remote("a", "1")]), store, FakeChunker())
    assert result.removed == ["b"] and store.docs["b"]["status"] == "removed"

    # an already removed file is not reported again
    assert sync(FakeConnector([remote("a", "1")]), store, FakeChunker()).removed == []

    sync(FakeConnector([]), store, FakeChunker(), delete_removed=True, allow_empty=True)
    assert store.docs == {}


def test_sync_refuses_mass_removal_on_empty_listing():
    store = MemoryStore()
    sync(FakeConnector([remote("a", "1")]), store, FakeChunker())

    result = sync(FakeConnector([]), store, FakeChunker())

    assert result.removal_skipped and result.removed == []
    assert store.docs["a"]["status"] == "pending"


def test_sync_failure_is_recorded_retried_and_not_marked_synced():
    store = MemoryStore()
    result = sync(
        FakeConnector([remote("a", "1")], fail_ids={"a"}), store, FakeChunker()
    )

    assert result.failed == ["a"] and store.synced == 0
    assert store.docs["a"]["status"] == "failed"

    retry = sync(FakeConnector([remote("a", "1")]), store, FakeChunker())
    assert retry.ingested == ["a"]


def test_listing_error_does_not_remove_anything():
    store = MemoryStore()
    sync(FakeConnector([remote("a", "1")]), store, FakeChunker())

    class Broken(FakeConnector):
        def list_files(self):
            raise ConnectorError("api down")

    with pytest.raises(ConnectorError):
        sync(Broken([]), store, FakeChunker())
    assert store.docs["a"]["status"] == "pending"
