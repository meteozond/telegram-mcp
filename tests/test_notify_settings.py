import json
import time
from datetime import datetime, timezone

import pytest
from telethon import functions, types

from telegram_mcp import runtime
from telegram_mcp.tools import chats

INPUT_PEER = types.InputPeerUser(user_id=42, access_hash=1)
MUTE_FOREVER = 2**31 - 1


class _NotifyClient:
    def __init__(self, current=None, exceptions=None):
        self.current = current or types.PeerNotifySettings()
        self.exceptions = exceptions
        self.updates = []

    async def __call__(self, request):
        if isinstance(request, functions.account.GetNotifySettingsRequest):
            return self.current
        if isinstance(request, functions.account.UpdateNotifySettingsRequest):
            self.updates.append(request)
            return True
        if isinstance(request, functions.account.GetNotifyExceptionsRequest):
            return self.exceptions
        raise AssertionError(f"unexpected request {request!r}")


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)


def _use(monkeypatch, client):
    async def fake_resolve(chat_id, client=None):
        return INPUT_PEER

    monkeypatch.setattr(chats, "get_client", lambda account=None: client)
    monkeypatch.setattr(chats, "resolve_input_entity", fake_resolve)
    return client


def _sent(client):
    (request,) = client.updates
    return request.settings


@pytest.mark.asyncio
async def test_mute_keeps_desktop_tone_and_ignores_phone_tones(monkeypatch):
    ringtone = types.NotificationSoundRingtone(id=7)
    client = _use(
        monkeypatch,
        _NotifyClient(
            types.PeerNotifySettings(
                show_previews=True,
                other_sound=ringtone,
                ios_sound=types.NotificationSoundNone(),
                stories_muted=True,
            )
        ),
    )

    result = await chats.mute_chat(chat_id=42)

    settings = _sent(client)
    assert result == "Chat 42 muted."
    assert settings.mute_until == MUTE_FOREVER
    assert settings.sound == ringtone
    assert settings.show_previews is True
    assert settings.stories_muted is True


@pytest.mark.asyncio
async def test_mute_does_not_copy_phone_tone_to_desktop(monkeypatch):
    client = _use(
        monkeypatch,
        _NotifyClient(types.PeerNotifySettings(ios_sound=types.NotificationSoundRingtone(id=7))),
    )

    await chats.mute_chat(chat_id=42)

    assert _sent(client).sound is None


@pytest.mark.asyncio
async def test_mute_for_duration(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())
    before = int(time.time())

    result = await chats.mute_chat(chat_id=42, duration_seconds=7200)

    assert result == "Chat 42 muted for 7200 seconds."
    assert before + 7200 <= _sent(client).mute_until <= int(time.time()) + 7200


@pytest.mark.asyncio
async def test_mute_duration_is_capped_at_forever(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())

    await chats.mute_chat(chat_id=42, duration_seconds=10**10)

    assert _sent(client).mute_until == MUTE_FOREVER


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, -5])
async def test_mute_rejects_non_positive_duration(monkeypatch, duration):
    client = _use(monkeypatch, _NotifyClient())

    result = await chats.mute_chat(chat_id=42, duration_seconds=duration)

    assert "must be positive" in result
    assert client.updates == []


@pytest.mark.asyncio
async def test_unmute_keeps_tone(monkeypatch):
    ringtone = types.NotificationSoundRingtone(id=7)
    client = _use(monkeypatch, _NotifyClient(types.PeerNotifySettings(other_sound=ringtone)))

    await chats.unmute_chat(chat_id=42)

    assert not _sent(client).mute_until
    assert _sent(client).sound == ringtone


@pytest.mark.parametrize(
    "sound, expected",
    [
        ("default", types.NotificationSoundDefault()),
        ("none", types.NotificationSoundNone()),
        ("123", types.NotificationSoundRingtone(id=123)),
        ("-123", types.NotificationSoundRingtone(id=-123)),
    ],
)
def test_parse_sound(sound, expected):
    assert chats._parse_sound(sound) == expected


@pytest.mark.parametrize("sound", ["loud", "", "²", "١٢", str(2**63)])
def test_parse_sound_rejects_invalid(sound):
    assert chats._parse_sound(sound) is None


@pytest.mark.parametrize(
    "sound, expected",
    [
        (None, None),
        (types.NotificationSoundDefault(), "default"),
        (types.NotificationSoundNone(), "none"),
        (types.NotificationSoundRingtone(id=123), "123"),
        (types.NotificationSoundLocal(title="Note", data="note"), "local:Note"),
    ],
)
def test_describe_sound(sound, expected):
    assert chats._describe_sound(sound) == expected


@pytest.mark.asyncio
async def test_set_chat_sound_rejects_invalid_sound(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())

    result = await chats.set_chat_sound(chat_id=42, sound="loud")

    assert result == chats._SOUND_HINT
    assert client.updates == []


@pytest.mark.asyncio
async def test_remove_notify_exception_sends_empty_settings(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())

    result = await chats.remove_notify_exception(chat_id=42)

    (request,) = client.updates
    assert result == "Chat 42 removed from notification exceptions."
    assert request.peer == types.InputNotifyPeer(INPUT_PEER)
    assert request.settings == types.InputPeerNotifySettings()


@pytest.mark.asyncio
async def test_set_default_notify_validates_input(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())

    assert "scope must be one of" in await chats.set_default_notify(scope="dms", enabled=False)
    assert await chats.set_default_notify(scope="users") == (
        "Nothing to change: pass enabled and/or sound."
    )
    assert await chats.set_default_notify(scope="users", sound="loud") == chats._SOUND_HINT
    assert client.updates == []


@pytest.mark.asyncio
async def test_set_default_notify_disables_scope(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())

    await chats.set_default_notify(scope="users", enabled=False, sound="default")

    (request,) = client.updates
    assert request.peer == types.InputNotifyUsers()
    assert request.settings.mute_until == MUTE_FOREVER
    assert request.settings.sound == types.NotificationSoundDefault()


@pytest.mark.asyncio
async def test_set_default_notify_refused_under_allowlist(monkeypatch):
    client = _use(monkeypatch, _NotifyClient())
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")

    result = await chats.set_default_notify(scope="users", enabled=False)

    assert "allowlist" in result
    assert client.updates == []


def _exceptions():
    muted = types.PeerNotifySettings(
        mute_until=datetime.fromtimestamp(MUTE_FOREVER, tz=timezone.utc)
    )
    unmuted = types.PeerNotifySettings(mute_until=datetime.fromtimestamp(0, tz=timezone.utc))
    user = types.User(id=42, first_name="Ann")
    channel = types.Channel(
        id=77, title="Forum", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
    )
    return types.Updates(
        updates=[
            types.UpdateNotifySettings(
                peer=types.NotifyPeer(peer=types.PeerUser(user_id=42)), notify_settings=unmuted
            ),
            types.UpdateNotifySettings(
                peer=types.NotifyForumTopic(peer=types.PeerChannel(channel_id=77), top_msg_id=5),
                notify_settings=muted,
            ),
            types.UpdateNotifySettings(peer=types.NotifyUsers(), notify_settings=muted),
        ],
        users=[user],
        chats=[channel],
        date=None,
        seq=0,
    )


@pytest.mark.asyncio
async def test_list_notify_exceptions(monkeypatch):
    _use(monkeypatch, _NotifyClient(exceptions=_exceptions()))

    records = json.loads(await chats.list_notify_exceptions())

    assert records == [
        {
            "chat_id": 42,
            "topic_id": None,
            "name": "Ann",
            "type": "User",
            "status": "unmuted",
            "mute_until": 0,
        },
        {
            "chat_id": -1000000000077,
            "topic_id": 5,
            "name": "Forum",
            "type": "Supergroup",
            "status": "muted",
            "mute_until": MUTE_FOREVER,
        },
    ]


@pytest.mark.asyncio
async def test_list_notify_exceptions_respects_allowlist(monkeypatch):
    _use(monkeypatch, _NotifyClient(exceptions=_exceptions()))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")

    records = json.loads(await chats.list_notify_exceptions())

    assert [record["chat_id"] for record in records] == [42]
