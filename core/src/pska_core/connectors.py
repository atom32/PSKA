from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pska_core.enums import Visibility
from pska_core.models import DEFAULT_TENANT_ID, ChannelIngestPayload, ConnectorState


CONNECTOR_RECORD_SCHEMA_VERSION = "pska.connector_record.v1"
CONNECTOR_STATE_SCHEMA_VERSION = "pska.connector_state.v1"


@dataclass(slots=True)
class ConnectorRecord:
    schema_version: str
    connector_id: str
    external_id: str
    source_uri: str | None
    title: str
    body: str
    owner_user_id: str
    space_id: str
    tenant_id: str = DEFAULT_TENANT_ID
    visibility: Visibility = Visibility.PRIVATE
    visible_team_ids: list[str] = field(default_factory=list)
    record_type: str = "document"
    created_at: str | None = None
    updated_at: str | None = None
    captured_at: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    permission_metadata: dict[str, Any] = field(default_factory=dict)
    scan_cursor: str | None = None
    content_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ConnectorRecord":
        schema_version = str(data.get("schema_version") or CONNECTOR_RECORD_SCHEMA_VERSION)
        if schema_version != CONNECTOR_RECORD_SCHEMA_VERSION:
            raise ValueError(f"Unsupported connector record schema_version: {schema_version}")
        body = str(data.get("body") or data.get("text") or "")
        if not body:
            raise ValueError("connector record requires body")
        return cls(
            schema_version=schema_version,
            connector_id=str(data["connector_id"]),
            external_id=str(data["external_id"]),
            source_uri=data.get("source_uri") or data.get("url"),
            title=str(data.get("title") or data.get("external_id") or ""),
            body=body,
            owner_user_id=str(data.get("owner_user_id") or "user_primary"),
            space_id=str(data.get("space_id") or "private_primary"),
            tenant_id=str(data.get("tenant_id") or DEFAULT_TENANT_ID),
            visibility=Visibility(data.get("visibility") or Visibility.PRIVATE),
            visible_team_ids=list(data.get("visible_team_ids") or []),
            record_type=str(data.get("record_type") or "document"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            captured_at=data.get("captured_at"),
            artifacts=dict(data.get("artifacts") or data.get("artifact_refs") or {}),
            permission_metadata=dict(data.get("permission_metadata") or data.get("permissions") or {}),
            scan_cursor=data.get("scan_cursor"),
            content_hash=data.get("content_hash"),
            metadata=dict(data.get("metadata") or {}),
        )


def connector_state_id(connector_id: str, owner_user_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    if tenant_id == DEFAULT_TENANT_ID:
        return f"conn_{owner_user_id}_{connector_id}".replace("/", "_").replace(" ", "_")
    return f"conn_{tenant_id}_{owner_user_id}_{connector_id}".replace("/", "_").replace(" ", "_")


def connector_state_from_mapping(data: dict[str, Any]) -> ConnectorState:
    schema_version = str(data.get("schema_version") or CONNECTOR_STATE_SCHEMA_VERSION)
    if schema_version != CONNECTOR_STATE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported connector state schema_version: {schema_version}")
    connector_id = str(data["connector_id"])
    owner_user_id = str(data.get("owner_user_id") or "user_primary")
    tenant_id = str(data.get("tenant_id") or DEFAULT_TENANT_ID)
    return ConnectorState(
        connector_state_id=str(data.get("connector_state_id") or connector_state_id(connector_id, owner_user_id, tenant_id)),
        connector_id=connector_id,
        owner_user_id=owner_user_id,
        enabled=bool(data.get("enabled", True)),
        scan_cursor=data.get("scan_cursor"),
        sync_status=str(data.get("sync_status") or "idle"),
        last_error=data.get("last_error"),
        permission_scope=dict(data.get("permission_scope") or {}),
        config=dict(data.get("config") or {}),
        tenant_id=tenant_id,
    )


def connector_record_to_payload(record: ConnectorRecord | dict[str, Any]) -> ChannelIngestPayload:
    if isinstance(record, dict):
        record = ConnectorRecord.from_mapping(record)
    return ChannelIngestPayload(
        schema_version="pska.channel_ingest.v1",
        source_channel=record.connector_id,
        record_type=record.record_type,
        source_id=record.external_id,
        owner_user_id=record.owner_user_id,
        space_id=record.space_id,
        tenant_id=record.tenant_id,
        visibility=record.visibility,
        visible_team_ids=record.visible_team_ids,
        url=record.source_uri,
        title=record.title,
        content={
            "text": record.body,
            "connector_id": record.connector_id,
            "external_id": record.external_id,
            "source_uri": record.source_uri,
            "content_hash": record.content_hash,
        },
        created_at=record.created_at,
        captured_at=record.captured_at,
        raw_paths=record.artifacts,
        extra={
            "connector": {
                "schema_version": record.schema_version,
                "connector_id": record.connector_id,
                "external_id": record.external_id,
                "source_uri": record.source_uri,
                "record_type": record.record_type,
                "updated_at": record.updated_at,
                "scan_cursor": record.scan_cursor,
                "content_hash": record.content_hash,
            },
            "permission_metadata": record.permission_metadata,
            **record.metadata,
        },
    )
