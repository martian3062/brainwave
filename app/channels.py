"""Encrypted durable storage for x402 batch-settlement channel state.

The SDK's channel record contains the payer's signed cumulative voucher. That
record must survive a web-process restart so `app.cli.close_batch` can claim it,
but it does not belong in the reporting ledger that powers the dashboard.

`DatabaseChannelStorage` implements the SDK's synchronous `ChannelStorage`
protocol over a separate `channel_state` table. The serialized record is
encrypted with Fernet using a key derived from `STORAGE_SECRET`; neither the
signature nor the channel configuration is stored in plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.models import ChannelState, utcnow

__all__ = [
    "DatabaseChannelStorage",
    "channel_storage",
    "claims_for",
    "mark_claimed",
]


_LOCK = threading.RLock()
_MEMORY_STORAGE: Any | None = None
_DATABASE_STORAGE: DatabaseChannelStorage | None = None


def _normalize(channel_id: str) -> str:
    from x402.mechanisms.evm.batch_settlement.utils import normalize_channel_id

    return normalize_channel_id(channel_id)


def _cipher() -> Fernet:
    digest = hashlib.sha256(settings.storage_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _cipher().encrypt(raw).decode("ascii")


def _decrypt(token: str) -> dict[str, Any]:
    try:
        raw = _cipher().decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError(
            "channel state cannot be decrypted with the current STORAGE_SECRET"
        ) from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("decrypted channel state is not a JSON object")
    return parsed


class DatabaseChannelStorage:
    """The SDK `ChannelStorage` protocol backed by encrypted SQL rows."""

    def get(self, channel_id: str):
        from x402.mechanisms.evm.batch_settlement.server.storage import Channel

        key = _normalize(channel_id)
        with _LOCK, session_scope() as db:
            row = db.get(ChannelState, key)
            return Channel.from_dict(_decrypt(row.payload_encrypted)) if row else None

    def list(self) -> list[Any]:
        from x402.mechanisms.evm.batch_settlement.server.storage import Channel

        with _LOCK, session_scope() as db:
            rows = db.exec(select(ChannelState).order_by(ChannelState.updated_at)).all()
            return [Channel.from_dict(_decrypt(row.payload_encrypted)) for row in rows]

    def update_channel(self, channel_id: str, update):
        from x402.mechanisms.evm.batch_settlement.server.storage import (
            Channel,
            ChannelUpdateResult,
        )

        key = _normalize(channel_id)
        with _LOCK, session_scope() as db:
            statement = select(ChannelState).where(ChannelState.channel_id == key).with_for_update()
            row = db.exec(statement).first()
            current = (
                Channel.from_dict(_decrypt(row.payload_encrypted)) if row is not None else None
            )
            next_channel = update(current)

            if next_channel is current:
                return ChannelUpdateResult(channel=current, status="unchanged")

            if next_channel is None:
                if row is None:
                    return ChannelUpdateResult(channel=None, status="unchanged")
                db.delete(row)
                return ChannelUpdateResult(channel=None, status="deleted")

            now = utcnow()
            encrypted = _encrypt(next_channel.to_dict())
            if row is None:
                row = ChannelState(
                    channel_id=key,
                    payload_encrypted=encrypted,
                    created_at=now,
                    updated_at=now,
                )
            else:
                row.payload_encrypted = encrypted
                row.updated_at = now
            db.add(row)
            db.flush()
            return ChannelUpdateResult(channel=next_channel.copy(), status="updated")


def channel_storage():
    """Process-wide storage passed to the SDK's batch-settlement scheme."""
    global _DATABASE_STORAGE, _MEMORY_STORAGE

    if settings.channel_storage_backend == "memory":
        if _MEMORY_STORAGE is None:
            from x402.mechanisms.evm.batch_settlement.server.storage import (
                InMemoryChannelStorage,
            )

            _MEMORY_STORAGE = InMemoryChannelStorage()
        return _MEMORY_STORAGE

    if _DATABASE_STORAGE is None:
        _DATABASE_STORAGE = DatabaseChannelStorage()
    return _DATABASE_STORAGE


def claims_for(channel_id: str | None) -> list[Any]:
    """Return the SDK's claim for one channel, or an empty list when none is due."""
    if not channel_id:
        return []
    from x402.mechanisms.evm.batch_settlement.server.channel_manager_common import (
        get_claimable_vouchers_from_channels,
    )

    channel = channel_storage().get(channel_id)
    if channel is None:
        return []
    return get_claimable_vouchers_from_channels([channel], idle_secs=None)


def mark_claimed(channel_id: str, total_claimed: int | str) -> None:
    """Advance the durable channel after a facilitator accepted its claim."""
    claimed = int(total_claimed)

    def update(current):
        if current is None or claimed <= int(current.total_claimed):
            return current
        next_channel = current.copy()
        next_channel.total_claimed = str(claimed)
        return next_channel

    channel_storage().update_channel(channel_id, update)
