from connectors.base import ConnectorError, SourceConnector

PROVIDERS = ("dropbox", "google_drive")


def create_connector(provider: str, folder: str) -> SourceConnector:
    if provider == "dropbox":
        from connectors.dropbox_source import DropboxSource

        return DropboxSource.from_env(folder)
    if provider == "google_drive":
        from connectors.gdrive_source import GoogleDriveSource

        return GoogleDriveSource.from_env(folder)
    raise ConnectorError(f"Unknown provider: {provider}")
