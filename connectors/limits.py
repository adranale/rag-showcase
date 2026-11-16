from collections.abc import Iterator

from connectors.base import ConnectorError, RemoteFile, SourceConnector


class LimitedConnector:
    """Refuses to sync more files than a per-user quota allows.

    The listing is checked as a whole (never truncated): a partial listing would
    make the sync treat the remaining files as removed.
    """

    def __init__(self, inner: SourceConnector, max_files: int) -> None:
        self._inner = inner
        self._max_files = max_files
        self.provider = inner.provider

    @property
    def account_identifier(self) -> str:
        return self._inner.account_identifier

    def list_files(self) -> Iterator[RemoteFile]:
        files = list(self._inner.list_files())
        if len(files) > self._max_files:
            raise ConnectorError(
                f"The folder contains {len(files)} supported files; "
                f"your limit is {self._max_files}. Choose a smaller folder."
            )
        yield from files

    def download(self, remote: RemoteFile) -> bytes:
        return self._inner.download(remote)
