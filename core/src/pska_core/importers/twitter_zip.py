from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from pska_core.adapters.twitter_archive import archive_metadata_to_payload
from pska_core.embeddings import EmbeddingProvider
from pska_core.enums import Visibility
from pska_core.ingest import IngestService
from pska_core.models import DEFAULT_TENANT_ID, SourceItem
from pska_core.store import KnowledgeStore


@dataclass(slots=True)
class TwitterZipImportResult:
    imported: int = 0
    skipped: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    source_item_ids: list[str] = field(default_factory=list)


class TwitterZipImporter:
    def __init__(
        self,
        store: KnowledgeStore,
        *,
        archive_root: Path,
        owner_user_id: str = "user_primary",
        tenant_id: str = DEFAULT_TENANT_ID,
        space_id: str = "private_primary",
        visibility: Visibility = Visibility.PRIVATE,
        visible_team_ids: list[str] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.store = store
        self.archive_root = archive_root
        self.owner_user_id = owner_user_id
        self.tenant_id = tenant_id
        self.space_id = space_id
        self.visibility = visibility
        self.visible_team_ids = visible_team_ids or []
        self.ingest = IngestService(store, embedding_provider=embedding_provider)

    def import_directory(self, input_dir: Path) -> TwitterZipImportResult:
        result = TwitterZipImportResult()
        existing_content_hashes = {
            item.content_hash
            for item in self.store.list_source_items(tenant_id=self.tenant_id)
        }
        for zip_path in sorted(input_dir.glob("*.zip")):
            try:
                content_hash = self._zip_content_hash(zip_path)
                if content_hash in existing_content_hashes:
                    result.skipped += 1
                else:
                    item = self.import_zip(zip_path, content_hash=content_hash)
                    result.imported += 1
                    result.source_item_ids.append(item.source_item_id)
                    existing_content_hashes.add(content_hash)
            except Exception as exc:  # noqa: BLE001 - import should report all failures.
                result.failed.append({"path": str(zip_path), "error": f"{type(exc).__name__}: {exc}"})
        return result

    def import_zip(self, zip_path: Path, *, content_hash: str | None = None) -> SourceItem:
        metadata, metadata_name = self._read_metadata(zip_path)
        source_id = str(metadata.get("source_id") or metadata.get("id") or Path(metadata_name).parts[0])
        target_dir = self.archive_root / "twitter-x"
        target_dir.mkdir(parents=True, exist_ok=True)
        self._extract_safe(zip_path, target_dir)
        archive_dir = target_dir / Path(metadata_name).parent
        payload = archive_metadata_to_payload(
            metadata,
            owner_user_id=self.owner_user_id,
            tenant_id=self.tenant_id,
            space_id=self.space_id,
            visibility=self.visibility,
            visible_team_ids=self.visible_team_ids,
            archive_dir=archive_dir,
        )
        payload.content["content_hash"] = content_hash or self._zip_content_hash(zip_path)
        return self.ingest.ingest_channel_payload(payload)

    def _zip_content_hash(self, zip_path: Path) -> str:
        digest = hashlib.sha256()
        with zip_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def _read_metadata(self, zip_path: Path) -> tuple[dict, str]:
        with zipfile.ZipFile(zip_path) as archive:
            names = [name for name in archive.namelist() if name.endswith("/metadata.json") or name == "metadata.json"]
            if not names:
                raise ValueError("metadata.json not found")
            metadata_name = sorted(names, key=lambda value: value.count("/"))[0]
            return json.loads(archive.read(metadata_name).decode("utf-8")), metadata_name

    def _extract_safe(self, zip_path: Path, target_dir: Path) -> None:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"Unsafe zip path: {member.filename}")
                destination = target_dir / member_path
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)

    def _safe_part(self, value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)[:160] or "unknown"
