from __future__ import annotations

from io import BytesIO
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class DriveScheduleSource:
    def __init__(self, service_account_file: str, folder_id: str) -> None:
        credentials = Credentials.from_service_account_file(
            service_account_file, scopes=[DRIVE_READONLY_SCOPE]
        )
        self.service = build(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )
        self.folder_id = folder_id

    def list_schedule_files(self) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        pending_folders = [self.folder_id]
        visited_folders: set[str] = set()

        while pending_folders:
            folder_id = pending_folders.pop()
            if folder_id in visited_folders:
                continue
            visited_folders.add(folder_id)

            page_token: str | None = None
            while True:
                response: dict[str, Any] = (
                    self.service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields="nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum)",
                        pageSize=1000,
                        pageToken=page_token,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        corpora="allDrives",
                    )
                    .execute()
                )

                for item in response.get("files", []):
                    if item["mimeType"] == FOLDER_MIME_TYPE:
                        pending_folders.append(item["id"])
                    elif item["mimeType"] == XLSX_MIME_TYPE:
                        item["revision"] = item.get("md5Checksum") or item.get(
                            "modifiedTime", ""
                        )
                        files.append(item)

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        return files

    def download(self, file_id: str) -> bytes:
        request = self.service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        )
        stream = BytesIO()
        downloader = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return stream.getvalue()
