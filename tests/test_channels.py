"""Durable, encrypted storage for signed batch-settlement vouchers."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

from sqlmodel import Session as DBSession  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.channels import DatabaseChannelStorage, claims_for, mark_claimed  # noqa: E402
from app.db import create_all, engine  # noqa: E402
from app.models import ChannelState  # noqa: E402


def _channel():
    from x402.mechanisms.evm.batch_settlement.server.storage import Channel
    from x402.mechanisms.evm.batch_settlement.types import ChannelConfig

    payer = "0x" + "11" * 20
    receiver = "0x" + "22" * 20
    config = ChannelConfig(
        payer=payer,
        payer_authorizer=payer,
        receiver=receiver,
        receiver_authorizer=receiver,
        token="0x" + "33" * 20,
        withdraw_delay=900,
        salt="0x" + "44" * 32,
    )
    return Channel(
        channel_id="0x" + "55" * 32,
        channel_config=config,
        charged_cumulative_amount="7000",
        signed_max_claimable="10000",
        signature="0x" + "ab" * 65,
        balance="10000",
        total_claimed="0",
    )


def test_channel_state_is_encrypted_and_yields_real_sdk_claims():
    create_all()
    storage = DatabaseChannelStorage()
    channel = _channel()

    with DBSession(engine) as db:
        for row in db.exec(select(ChannelState)).all():
            db.delete(row)
        db.commit()

    result = storage.update_channel(channel.channel_id, lambda _current: channel)
    assert result.status == "updated"

    with DBSession(engine) as db:
        row = db.get(ChannelState, channel.channel_id)
        assert row is not None
        assert channel.signature not in row.payload_encrypted
        assert channel.channel_config.payer not in row.payload_encrypted

    loaded = storage.get(channel.channel_id)
    assert loaded.signature == channel.signature
    assert loaded.charged_cumulative_amount == "7000"

    claims = claims_for(channel.channel_id)
    assert len(claims) == 1
    assert claims[0].max_claimable_amount == "10000"
    assert claims[0].total_claimed == "7000"

    mark_claimed(channel.channel_id, claims[0].total_claimed)
    assert claims_for(channel.channel_id) == []
