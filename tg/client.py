"""Telethon-backed Telegram client.

The curses UI still consumes chat, user, and message dictionaries. This
module talks to Telegram with Telethon and translates those objects into
that dictionary shape, then pushes the same update names the UI already
handles.
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import threading
from collections import defaultdict
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

from telethon import TelegramClient, events, utils
from telethon.tl import types
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.contacts import GetContactsRequest, SearchRequest
from telethon.tl.functions.messages import (
    CreateChatRequest,
    DeleteChatUserRequest,
    GetFullChatRequest,
    MarkDialogUnreadRequest,
    SetTypingRequest,
    ToggleDialogPinRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    InputDialogPeer,
    InputNotifyPeer,
    InputPeerNotifySettings,
)

from tg import config
from tg.types import ChatAction

log = logging.getLogger(__name__)

UpdateCallback = Callable[[Dict[str, Any]], None]
PINNED_ORDER = 10**12

_STATUS_TYPES = {
    types.UserStatusOnline: "userStatusOnline",
    types.UserStatusOffline: "userStatusOffline",
    types.UserStatusRecently: "userStatusRecently",
    types.UserStatusLastWeek: "userStatusLastWeek",
    types.UserStatusLastMonth: "userStatusLastMonth",
    types.UserStatusEmpty: "userStatusEmpty",
}

_TYPING_ACTIONS = {
    types.SendMessageTypingAction: "chatActionTyping",
    types.SendMessageCancelAction: "chatActionCancel",
    types.SendMessageRecordVideoAction: "chatActionRecordingVideo",
    types.SendMessageUploadVideoAction: "chatActionUploadingVideo",
    types.SendMessageRecordAudioAction: "chatActionRecordingVoiceNote",
    types.SendMessageUploadAudioAction: "chatActionUploadingVoiceNote",
    types.SendMessageUploadPhotoAction: "chatActionUploadingPhoto",
    types.SendMessageUploadDocumentAction: "chatActionUploadingDocument",
    types.SendMessageGeoLocationAction: "chatActionChoosingLocation",
    types.SendMessageChooseContactAction: "chatActionChoosingContact",
    types.SendMessageGamePlayAction: "chatActionStartPlayingGame",
    types.SendMessageRecordRoundAction: "chatActionRecordingVideoNote",
    types.SendMessageUploadRoundAction: "chatActionUploadingVideoNote",
}

_SEND_ACTIONS = {
    ChatAction.chatActionTyping: types.SendMessageTypingAction,
    ChatAction.chatActionCancel: types.SendMessageCancelAction,
    ChatAction.chatActionRecordingVideo: types.SendMessageRecordVideoAction,
    ChatAction.chatActionUploadingVideo: types.SendMessageUploadVideoAction,
    ChatAction.chatActionRecordingVoiceNote: (
        types.SendMessageRecordAudioAction
    ),
    ChatAction.chatActionUploadingVoiceNote: (
        types.SendMessageUploadAudioAction
    ),
    ChatAction.chatActionUploadingPhoto: types.SendMessageUploadPhotoAction,
    ChatAction.chatActionUploadingDocument: (
        types.SendMessageUploadDocumentAction
    ),
    ChatAction.chatActionChoosingLocation: (
        types.SendMessageGeoLocationAction
    ),
    ChatAction.chatActionChoosingContact: (
        types.SendMessageChooseContactAction
    ),
    ChatAction.chatActionStartPlayingGame: types.SendMessageGamePlayAction,
    ChatAction.chatActionRecordingVideoNote: (
        types.SendMessageRecordRoundAction
    ),
    ChatAction.chatActionUploadingVideoNote: (
        types.SendMessageUploadRoundAction
    ),
}


class CallResult:
    """Synchronous result of a Telegram request."""

    def __init__(
        self,
        update: Any = None,
        error: bool = False,
        error_info: str = "",
    ) -> None:
        self.update = {} if update is None else update
        self.error = error
        self.error_info = error_info

    def wait(self) -> "CallResult":
        return self


def _ts(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


_ENTITY_KINDS = {
    "MessageEntityBold": "textEntityTypeBold",
    "MessageEntityItalic": "textEntityTypeItalic",
    "MessageEntityUnderline": "textEntityTypeUnderline",
    "MessageEntityStrike": "textEntityTypeStrikethrough",
    "MessageEntityCode": "textEntityTypeCode",
    "MessageEntityPre": "textEntityTypePre",
    "MessageEntityTextUrl": "textEntityTypeTextUrl",
    "MessageEntityUrl": "textEntityTypeUrl",
    "MessageEntityBlockquote": "textEntityTypeBlockQuote",
    "MessageEntitySpoiler": "textEntityTypeSpoiler",
}


def _python_span(
    text: str, offset: int, length: int
) -> Optional[tuple[int, int]]:
    """Telegram entity offsets count UTF-16 code units."""
    if offset < 0 or length <= 0 or not text:
        return None
    raw = text.encode("utf-16-le")
    start_b = offset * 2
    end_b = (offset + length) * 2
    if end_b > len(raw):
        if offset + length <= len(text):
            return offset, offset + length
        return None
    begin = len(raw[:start_b].decode("utf-16-le"))
    end = begin + len(raw[start_b:end_b].decode("utf-16-le"))
    return begin, end


def _plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    return str(value)


def _title(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return "Unknown"


def _status_dict(status: Any) -> Dict[str, Any]:
    kind = _STATUS_TYPES.get(type(status), "userStatusEmpty")
    if isinstance(status, types.UserStatusOnline):
        return {"@type": kind, "expires": _ts(status.expires)}
    if isinstance(status, types.UserStatusOffline):
        return {"@type": kind, "was_online": _ts(status.was_online)}
    return {"@type": kind}


def _user_dict(user: types.User) -> Dict[str, Any]:
    if user.bot:
        user_type = "userTypeBot"
    elif user.deleted:
        user_type = "userTypeDeleted"
    else:
        user_type = "userTypeRegular"
    return {
        "id": user.id,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "username": user.username or "",
        "phone_number": user.phone or "",
        "type": {"@type": user_type},
        "status": _status_dict(user.status),
    }


def _chat_type(entity: Any) -> Dict[str, Any]:
    if isinstance(entity, types.User):
        return {"@type": "chatTypePrivate", "user_id": entity.id}
    if isinstance(entity, types.Chat):
        return {"@type": "chatTypeBasicGroup", "basic_group_id": entity.id}
    if isinstance(entity, types.Channel):
        return {
            "@type": "chatTypeSupergroup",
            "supergroup_id": entity.id,
            "is_channel": not bool(entity.megagroup),
        }
    return {"@type": "chatTypePrivate", "user_id": 0}


def _can_send(entity: Any) -> bool:
    if isinstance(entity, types.User):
        return True
    if isinstance(entity, types.Chat):
        return not bool(getattr(entity, "deactivated", False)) and not bool(
            getattr(entity, "left", False)
        )
    if isinstance(entity, types.Channel):
        if entity.left:
            return False
        if entity.creator or getattr(entity, "admin_rights", None):
            return True
        if entity.broadcast and not entity.megagroup:
            return False
        banned = entity.default_banned_rights
        if banned and banned.send_messages:
            return False
        return True
    return True


def _mute_for(notify: Any) -> int:
    mute_until = getattr(notify, "mute_until", None)
    if not isinstance(mute_until, datetime):
        return 0
    if mute_until.tzinfo is None:
        mute_until = mute_until.replace(tzinfo=timezone.utc)
    remaining = int(
        mute_until.timestamp() - datetime.now(timezone.utc).timestamp()
    )
    return max(remaining, 0)


def _file_dict(msg: Any) -> Dict[str, Any]:
    size = 0
    if msg.file is not None and msg.file.size:
        size = int(msg.file.size)
    return {
        "id": msg.id,
        "size": size,
        "local": {
            "path": "",
            "is_downloading_completed": False,
            "is_downloading_active": False,
            "downloaded_size": 0,
        },
    }


class Client:
    def __init__(
        self,
        api_id: Any,
        api_hash: str,
        phone: Optional[str],
        session_dir: str,
    ) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.phone = phone
        os.makedirs(session_dir, exist_ok=True)
        self.session = os.path.join(session_dir, "telethon")
        self._handlers: Dict[str, List[UpdateCallback]] = defaultdict(list)
        self._known_chats: set[int] = set()
        self.secrets: Any = None
        self._me_id = 0
        self._secret_seq = 0
        self._secret_users: Dict[int, int] = {}
        self._secret_messages: Dict[int, List[Dict[str, Any]]] = {}
        self._secret_store = os.path.join(session_dir, "secret_messages.json")
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="telethon", daemon=True
        )
        self._thread.start()
        self.client: TelegramClient = self._run(self._create())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()

    def _call(self, coro: Coroutine[Any, Any, Any]) -> CallResult:
        # Waiting here from the client thread deadlocks: the coroutine is
        # queued on that same thread, which cannot run it while it waits.
        if threading.current_thread() is self._thread:
            coro.close()
            log.error("Skipped a Telegram request on the client thread")
            return CallResult(error=True, error_info="client thread is busy")
        try:
            update = self._run(coro)
        except Exception as exc:
            log.exception("Telegram request failed")
            return CallResult(error=True, error_info=str(exc))
        if update is None:
            update = {}
        return CallResult(update=update)

    async def _create(self) -> TelegramClient:
        return TelegramClient(self.session, self.api_id, self.api_hash)

    def login(self) -> None:
        self._install_handlers()
        self._run(self._login())

    async def _login(self) -> None:
        await self.client.connect()
        await self._start_secrets()
        await self.client.start(phone=self.phone)
        me = await self.client.get_me()
        self._me_id = int(getattr(me, "id", 0) or 0)

    def add_update_handler(
        self, update_type: str, callback: UpdateCallback
    ) -> None:
        self._handlers[update_type].append(callback)

    def _emit(self, update_type: str, update: Dict[str, Any]) -> None:
        for callback in self._handlers.get(update_type, []):
            try:
                callback(update)
            except Exception:
                log.exception("Update handler failed: %s", update_type)

    def _install_handlers(self) -> None:
        client = self.client
        client.add_event_handler(self._on_new_message, events.NewMessage())
        client.add_event_handler(self._on_edited, events.MessageEdited())
        client.add_event_handler(self._on_deleted, events.MessageDeleted())
        client.add_event_handler(self._on_user_update, events.UserUpdate())
        client.add_event_handler(self._on_read, events.MessageRead())
        client.add_event_handler(self._on_read, events.MessageRead(inbox=True))
        client.add_event_handler(self._on_inbox_count)

    async def _on_new_message(self, event: Any) -> None:
        msg = self._message_to_dict(event.message)
        if msg["chat_id"] not in self._known_chats:
            entity = await event.message.get_chat()
            chat = self._entity_to_chat(entity, msg)
            self._known_chats.add(chat["id"])
            self._emit("updateNewChat", {"chat": chat})
        self._emit("updateNewMessage", {"message": msg})
        self._emit(
            "updateChatLastMessage",
            {
                "chat_id": msg["chat_id"],
                "last_message": msg,
                "positions": [{"order": msg["date"]}],
            },
        )

    async def _on_edited(self, event: Any) -> None:
        msg = self._message_to_dict(event.message)
        self._emit(
            "updateMessageEdited",
            {
                "chat_id": msg["chat_id"],
                "message_id": msg["id"],
                "edit_date": msg["edit_date"],
            },
        )
        self._emit(
            "updateMessageContent",
            {
                "chat_id": msg["chat_id"],
                "message_id": msg["id"],
                "new_content": msg["content"],
            },
        )

    async def _on_deleted(self, event: Any) -> None:
        if event.chat_id is None:
            return
        self._emit(
            "updateDeleteMessages",
            {
                "chat_id": event.chat_id,
                "message_ids": list(event.deleted_ids),
                "is_permanent": True,
            },
        )

    async def _on_user_update(self, event: Any) -> None:
        if event.status is not None:
            self._emit(
                "updateUserStatus",
                {
                    "user_id": event.user_id,
                    "status": _status_dict(event.status),
                },
            )
        if event.action is None:
            return
        action_type = _TYPING_ACTIONS.get(type(event.action))
        if action_type is None:
            return
        self._emit(
            "updateUserChatAction",
            {
                "chat_id": event.chat_id,
                "user_id": event.user_id or 0,
                "action": {"@type": action_type},
            },
        )

    async def _on_read(self, event: Any) -> None:
        if event.contents:
            for message_id in event.message_ids:
                self._emit(
                    "updateMessageContentOpened",
                    {"chat_id": event.chat_id, "message_id": message_id},
                )
            return
        if not event.max_id:
            return
        if event.outbox:
            self._emit(
                "updateChatReadOutbox",
                {
                    "chat_id": event.chat_id,
                    "last_read_outbox_message_id": event.max_id,
                },
            )

    async def _on_inbox_count(self, update: Any) -> None:
        if isinstance(update, types.UpdateReadHistoryInbox):
            chat_id = utils.get_peer_id(update.peer)
        elif isinstance(update, types.UpdateReadChannelInbox):
            chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
        else:
            return
        self._emit(
            "updateChatReadInbox",
            {
                "chat_id": chat_id,
                "last_read_inbox_message_id": update.max_id,
                "unread_count": int(update.still_unread_count or 0),
            },
        )

    def _message_to_dict(self, msg: Any) -> Dict[str, Any]:
        content = self._content(msg)
        sender_id = msg.sender_id
        if sender_id:
            sender: Dict[str, Any] = {"user_id": sender_id}
        else:
            sender = {"chat_id": msg.chat_id}
        reply = None
        header = getattr(msg, "reply_to", None)
        reply_id = getattr(msg, "reply_to_msg_id", None)
        quote = getattr(header, "quote_text", None) or "" if header else ""
        if reply_id or quote:
            peer = (
                getattr(header, "reply_to_peer_id", None) if header else None
            )
            reply_chat = (
                utils.get_peer_id(peer) if peer is not None else msg.chat_id
            )
            reply = {
                "chat_id": reply_chat,
                "message_id": reply_id,
                "quote": quote,
            }
        text_message = content.get("@type") == "messageText"
        return {
            "@type": "message",
            "id": msg.id,
            "chat_id": msg.chat_id,
            "date": _ts(msg.date),
            "edit_date": _ts(getattr(msg, "edit_date", None)),
            "is_outgoing": bool(msg.out),
            "sender_id": sender,
            "content": content,
            "reply_to": reply,
            "forward_info": {} if getattr(msg, "fwd_from", None) else None,
            "can_be_edited": bool(msg.out and text_message),
            "can_be_forwarded": not bool(getattr(msg, "noforwards", False)),
            "reply_markup": self._reply_markup(msg),
            "sending_state": None,
        }

    def _content(self, msg: Any) -> Dict[str, Any]:
        action = getattr(msg, "action", None)
        if action is not None:
            return self._action_content(msg, action)
        if msg.poll:
            return self._poll_content(msg)
        if msg.sticker:
            return self._sticker_content(msg)
        if msg.voice:
            return self._voice_content(msg)
        if msg.video_note:
            return self._video_note_content(msg)
        if msg.gif:
            return self._named_media(msg, "messageAnimation", "animation")
        if msg.video:
            return self._named_media(msg, "messageVideo", "video")
        if msg.audio:
            return self._named_media(msg, "messageAudio", "audio")
        if msg.photo:
            return {
                "@type": "messagePhoto",
                "photo": {"sizes": [{"photo": {"file": _file_dict(msg)}}]},
                **self._caption(msg),
            }
        if msg.document:
            return self._named_media(msg, "messageDocument", "document")
        return self._text_content(msg)

    def _caption(self, msg: Any) -> Dict[str, Any]:
        text = msg.text or ""
        if not text:
            return {}
        return {
            "caption": {"text": text, "entities": self._text_entities(msg)}
        }

    def _text_entities(self, msg: Any) -> List[Dict[str, Any]]:
        text = msg.text or ""
        entities = []
        for entity in msg.entities or []:
            kind = _ENTITY_KINDS.get(type(entity).__name__)
            if not kind:
                continue
            span = _python_span(text, entity.offset, entity.length)
            if span is None:
                continue
            info: Dict[str, Any] = {"@type": kind}
            url = getattr(entity, "url", None)
            if url:
                info["url"] = url
            entities.append(
                {
                    "@type": "textEntity",
                    "offset": span[0],
                    "length": span[1] - span[0],
                    "type": info,
                }
            )
        return entities

    def _text_content(self, msg: Any) -> Dict[str, Any]:
        entities = self._text_entities(msg)
        content: Dict[str, Any] = {
            "@type": "messageText",
            "text": {
                "@type": "formattedText",
                "text": msg.text or "",
                "entities": entities,
            },
        }
        if msg.web_preview:
            content["web_page"] = {
                "type": getattr(msg.web_preview, "type", "") or "",
                "url": getattr(msg.web_preview, "url", "") or "",
                "site_name": getattr(msg.web_preview, "site_name", "") or "",
                "title": getattr(msg.web_preview, "title", "") or "",
                "description": {
                    "text": _plain(getattr(msg.web_preview, "description", ""))
                },
            }
        return content

    def _named_media(
        self, msg: Any, type_name: str, field: str
    ) -> Dict[str, Any]:
        file_info = msg.file
        outer: Dict[str, Any] = {field: {"file": _file_dict(msg)}}
        if file_info is not None:
            if file_info.name:
                outer["file_name"] = file_info.name
            if file_info.duration:
                outer["duration"] = int(file_info.duration)
        content = {"@type": type_name, field: outer}
        content.update(self._caption(msg))
        return content

    def _voice_content(self, msg: Any) -> Dict[str, Any]:
        duration = int(msg.file.duration or 0) if msg.file else 0
        return {
            "@type": "messageVoiceNote",
            "is_listened": not bool(msg.media_unread),
            "voice_note": {
                "duration": duration,
                "voice": {"file": _file_dict(msg)},
            },
        }

    def _video_note_content(self, msg: Any) -> Dict[str, Any]:
        duration = int(msg.file.duration or 0) if msg.file else 0
        return {
            "@type": "messageVideoNote",
            "is_viewed": not bool(msg.media_unread),
            "video_note": {
                "duration": duration,
                "video": {"file": _file_dict(msg)},
            },
        }

    def _sticker_content(self, msg: Any) -> Dict[str, Any]:
        file_info = msg.file
        mime = file_info.mime_type if file_info else ""
        animated = mime in ("application/x-tgsticker", "video/webm")
        return {
            "@type": "messageSticker",
            "sticker": {
                "emoji": (file_info.emoji if file_info else "") or "",
                "is_animated": animated,
                "thumbnail": {"photo": {"file": _file_dict(msg)}},
            },
        }

    def _poll_content(self, msg: Any) -> Dict[str, Any]:
        media = msg.poll
        poll = media.poll
        results = list(getattr(media.results, "results", None) or [])
        counts = []
        for index, _answer in enumerate(poll.answers):
            voters = 0
            if index < len(results) and results[index] is not None:
                voters = int(results[index].voters or 0)
            counts.append(voters)
        total = sum(counts) or 1
        options = []
        for index, answer in enumerate(poll.answers):
            options.append(
                {
                    "text": _plain(answer.text),
                    "voter_count": counts[index],
                    "vote_percentage": int(counts[index] * 100 / total),
                }
            )
        return {
            "@type": "messagePoll",
            "poll": {
                "question": _plain(poll.question),
                "options": options,
                "is_closed": bool(poll.closed),
            },
        }

    def _action_content(self, msg: Any, action: Any) -> Dict[str, Any]:
        if isinstance(
            action,
            (types.MessageActionChatCreate, types.MessageActionChannelCreate),
        ):
            return {
                "@type": "messageBasicGroupChatCreate",
                "title": action.title,
            }
        if isinstance(action, types.MessageActionChatAddUser):
            return {
                "@type": "messageChatAddMembers",
                "member_user_ids": list(action.users),
            }
        if isinstance(action, types.MessageActionChatJoinedByLink):
            sender = msg.sender_id or 0
            return {
                "@type": "messageChatAddMembers",
                "member_user_ids": [sender],
            }
        if isinstance(action, types.MessageActionChatDeleteUser):
            return {
                "@type": "messageChatDeleteMember",
                "user_id": action.user_id,
            }
        if isinstance(action, types.MessageActionChatEditTitle):
            return {
                "@type": "messageChatChangeTitle",
                "title": action.title,
            }
        return {"@type": type(action).__name__}

    def _reply_markup(self, msg: Any) -> Optional[Dict[str, Any]]:
        try:
            buttons = msg.buttons
        except Exception:
            return None
        if not buttons:
            return None
        rows = []
        for row in buttons:
            items = []
            for button in row:
                item: Dict[str, Any] = {"text": button.text, "type": {}}
                url = getattr(button, "url", None)
                if url:
                    item["type"] = {
                        "@type": "inlineKeyboardButtonTypeUrl",
                        "url": url,
                    }
                items.append(item)
            rows.append(items)
        return {"rows": rows}

    def _entity_to_chat(
        self, entity: Any, last_message: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        chat_id = utils.get_peer_id(entity)
        order = (
            last_message["date"]
            if last_message
            else int(datetime.now().timestamp())
        )
        if order <= 0:
            order = 1
        return self._chat_dict(
            chat_id=chat_id,
            title=_title(entity),
            entity=entity,
            order=order,
            pinned=False,
            unread_count=0,
            unread_mark=False,
            last_message=last_message,
            read_inbox=0,
            read_outbox=0,
            mute_for=0,
        )

    def _dialog_to_chat(self, dialog: Any) -> Dict[str, Any]:
        raw = dialog.dialog
        last_message = None
        if dialog.message is not None:
            last_message = self._message_to_dict(dialog.message)
        order = _ts(dialog.date)
        if order <= 0:
            order = 1
        if dialog.pinned:
            order += PINNED_ORDER
        return self._chat_dict(
            chat_id=dialog.id,
            title=dialog.name,
            entity=dialog.entity,
            order=order,
            pinned=bool(dialog.pinned),
            unread_count=int(dialog.unread_count or 0),
            unread_mark=bool(getattr(raw, "unread_mark", False)),
            last_message=last_message,
            read_inbox=int(getattr(raw, "read_inbox_max_id", 0) or 0),
            read_outbox=int(getattr(raw, "read_outbox_max_id", 0) or 0),
            mute_for=_mute_for(getattr(raw, "notify_settings", None)),
        )

    def _chat_dict(
        self,
        chat_id: int,
        title: str,
        entity: Any,
        order: int,
        pinned: bool,
        unread_count: int,
        unread_mark: bool,
        last_message: Optional[Dict[str, Any]],
        read_inbox: int,
        read_outbox: int,
        mute_for: int,
    ) -> Dict[str, Any]:
        return {
            "id": chat_id,
            "title": title,
            "order": order,
            "is_pinned": pinned,
            "unread_count": unread_count,
            "is_marked_as_unread": unread_mark,
            "last_message": last_message,
            "last_read_inbox_message_id": read_inbox,
            "last_read_outbox_message_id": read_outbox,
            "notification_settings": {"mute_for": mute_for},
            "positions": [{"order": order, "is_pinned": pinned}],
            "type": _chat_type(entity),
            "permissions": {"can_send_basic_messages": _can_send(entity)},
            "can_be_deleted_for_all_users": isinstance(entity, types.User),
        }

    def get_chats(self, offset: int = 0, limit: int = 30) -> CallResult:
        return self._call(self._get_chats(offset, limit))

    async def _get_chats(self, offset: int, limit: int) -> Dict[str, Any]:
        dialogs = await self.client.get_dialogs(limit=offset + limit)
        chunk = list(dialogs)[offset : offset + limit]
        chats = []
        for dialog in chunk:
            chat = self._dialog_to_chat(dialog)
            self._known_chats.add(chat["id"])
            chats.append(chat)
        if offset == 0:
            chats.extend(await self._secret_chat_list())
        return {"chats": chats, "full": len(chunk) < limit}

    def get_chat(self, chat_id: int) -> CallResult:
        return self._call(self._get_chat(chat_id))

    async def _get_chat(self, chat_id: int) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return await self._secret_chat_info(chat_id)
        entity = await self.client.get_entity(chat_id)
        return self._entity_to_chat(entity)

    def get_chat_history(
        self,
        chat_id: int,
        from_message_id: int = 0,
        offset: int = 0,
        limit: int = 20,
    ) -> CallResult:
        return self._call(
            self._get_history(chat_id, from_message_id, offset, limit)
        )

    async def _get_history(
        self,
        chat_id: int,
        from_message_id: int,
        offset: int,
        limit: int,
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {
                "messages": self._secret_history(
                    chat_id, from_message_id, limit
                )
            }
        kwargs: Dict[str, Any] = {"limit": limit}
        if from_message_id:
            kwargs["offset_id"] = from_message_id
        elif offset:
            kwargs["add_offset"] = offset
        messages = await self.client.get_messages(chat_id, **kwargs)
        if messages is None:
            return {"messages": []}
        if not isinstance(messages, list):
            messages = [messages]
        return {
            "messages": [
                self._message_to_dict(msg)
                for msg in messages
                if msg is not None
            ]
        }

    def get_message(self, chat_id: int, msg_id: int) -> CallResult:
        return self._call(self._get_message(chat_id, msg_id))

    async def _get_message(self, chat_id: int, msg_id: int) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            found = self._find_secret_message(chat_id, msg_id)
            if found is None:
                raise ValueError(f"message {msg_id} was not found")
            return found
        msg = await self.client.get_messages(chat_id, ids=msg_id)
        if msg is None:
            raise ValueError(f"message {msg_id} was not found")
        return self._message_to_dict(msg)

    def send_message(self, chat_id: int, text: str) -> CallResult:
        return self._call(self._send_text(chat_id, text))

    def reply_message(
        self, chat_id: int, reply_to_message_id: int, text: str
    ) -> CallResult:
        return self._call(
            self._send_text(chat_id, text, reply_to=reply_to_message_id)
        )

    async def _send_text(
        self, chat_id: int, text: str, reply_to: Optional[int] = None
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return await self._send_secret_text(chat_id, text, reply_to)
        formatting = self._formatting(text)
        msg = await self.client.send_message(
            chat_id,
            formatting[0],
            formatting_entities=formatting[1],
            reply_to=reply_to,
        )
        return self._message_to_dict(msg)

    def _formatting(self, text: str) -> tuple[str, Optional[List[Any]]]:
        try:
            from telethon.extensions import markdown

            return markdown.parse(text)
        except Exception:
            return text, None

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> CallResult:
        return self._call(self._edit_text(chat_id, message_id, text))

    async def _edit_text(
        self, chat_id: int, message_id: int, text: str
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            raise RuntimeError("Secret messages can't be edited")
        parsed, entities = self._formatting(text)
        msg = await self.client.edit_message(
            chat_id,
            message_id,
            parsed,
            formatting_entities=entities,
        )
        return self._message_to_dict(msg)

    def delete_messages(
        self, chat_id: int, message_ids: List[int], revoke: bool = True
    ) -> CallResult:
        return self._call(self._delete_messages(chat_id, message_ids, revoke))

    async def _delete_messages(
        self, chat_id: int, message_ids: List[int], revoke: bool
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            self._drop_secret_messages(chat_id, message_ids)
            self._emit(
                "updateDeleteMessages",
                {
                    "chat_id": chat_id,
                    "message_ids": message_ids,
                    "is_permanent": True,
                },
            )
            return {}
        await self.client.delete_messages(chat_id, message_ids, revoke=revoke)
        return {}

    def forward_messages(
        self, chat_id: int, from_chat_id: int, message_ids: List[int]
    ) -> CallResult:
        return self._call(self._forward(chat_id, from_chat_id, message_ids))

    async def _forward(
        self, chat_id: int, from_chat_id: int, message_ids: List[int]
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id) or self._is_secret(from_chat_id):
            raise RuntimeError("Secret messages can't be forwarded")
        await self.client.forward_messages(chat_id, message_ids, from_chat_id)
        return {}

    def view_messages(
        self, chat_id: int, message_ids: List[int]
    ) -> CallResult:
        if not message_ids:
            return CallResult(update={})
        coro = self._view_messages(chat_id, max(message_ids))
        if threading.current_thread() is self._thread:
            task = self.loop.create_task(coro)
            task.add_done_callback(self._log_task_error)
            return CallResult(update={})
        return self._call(coro)

    def _log_task_error(self, task: "Future[Any]") -> None:
        try:
            task.result()
        except Exception:
            log.exception("Telegram request failed")

    async def _view_messages(
        self, chat_id: int, max_id: int
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {}
        await self.client.send_read_acknowledge(chat_id, max_id=max_id)
        return {}

    def send_chat_action(
        self,
        chat_id: int,
        action: ChatAction,
        progress: Optional[int] = None,
    ) -> CallResult:
        del progress
        action_type = _SEND_ACTIONS.get(action, types.SendMessageTypingAction)
        return self._call(self._typing(chat_id, action_type()))

    async def _typing(self, chat_id: int, action: Any) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {}
        peer = await self.client.get_input_entity(chat_id)
        await self.client(SetTypingRequest(peer=peer, action=action))
        return {}

    def send_doc(self, file_path: str, chat_id: int) -> CallResult:
        return self._send_path(chat_id, file_path, force_document=True)

    def send_photo(self, file_path: str, chat_id: int) -> CallResult:
        return self._send_path(chat_id, file_path)

    def send_audio(self, file_path: str, chat_id: int) -> CallResult:
        return self._send_path(chat_id, file_path, voice_note=False)

    def send_animation(self, file_path: str, chat_id: int) -> CallResult:
        return self._send_path(
            chat_id,
            file_path,
            attributes=[types.DocumentAttributeAnimated()],
        )

    def send_video(
        self,
        file_path: str,
        chat_id: int,
        width: int,
        height: int,
        duration: int,
    ) -> CallResult:
        return self._send_path(
            chat_id,
            file_path,
            attributes=[
                types.DocumentAttributeVideo(
                    duration=int(duration or 0),
                    w=int(width or 0),
                    h=int(height or 0),
                    supports_streaming=True,
                )
            ],
        )

    def send_voice(
        self,
        file_path: str,
        chat_id: int,
        duration: int,
        waveform: str,
    ) -> CallResult:
        del duration, waveform
        return self._send_path(chat_id, file_path, voice_note=True)

    def _send_path(
        self, chat_id: int, file_path: str, **kwargs: Any
    ) -> CallResult:
        return self._call(self._send_file(chat_id, file_path, **kwargs))

    async def _send_file(
        self, chat_id: int, file_path: str, **kwargs: Any
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return await self._send_secret_file(chat_id, file_path, **kwargs)
        msg = await self.client.send_file(chat_id, file_path, **kwargs)
        if isinstance(msg, list):
            msg = msg[-1]
        return self._message_to_dict(msg)

    def download_file(
        self,
        file_id: int,
        chat_id: Optional[int] = None,
        msg_id: Optional[int] = None,
    ) -> None:
        message_id = msg_id if msg_id is not None else file_id
        if chat_id is None:
            log.warning("Cannot download file %s without a chat id", file_id)
            return
        asyncio.run_coroutine_threadsafe(
            self._download(chat_id, message_id), self.loop
        )

    async def _download(self, chat_id: int, message_id: int) -> None:
        if self._is_secret(chat_id):
            return
        msg = await self.client.get_messages(chat_id, ids=message_id)
        if msg is None:
            log.warning(
                "Message %s in chat %s was not found", message_id, chat_id
            )
            return
        path = await self.client.download_media(msg, file=config.FILES_DIR)
        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
        self._emit(
            "updateFile",
            {
                "file": {
                    "id": message_id,
                    "local": {
                        "path": path or "",
                        "is_downloading_completed": bool(path),
                        "is_downloading_active": False,
                        "downloaded_size": size,
                    },
                }
            },
        )

    def open_message_content(
        self, chat_id: int, message_id: int
    ) -> CallResult:
        self._emit(
            "updateMessageContentOpened",
            {"chat_id": chat_id, "message_id": message_id},
        )
        return CallResult(update={})

    def get_message_properties(
        self, chat_id: int, message_id: int
    ) -> CallResult:
        return self._call(self._message_properties(chat_id, message_id))

    async def _message_properties(
        self, chat_id: int, message_id: int
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {
                "can_be_deleted_only_for_self": True,
                "can_be_deleted_for_all_users": False,
            }
        msg = await self.client.get_messages(chat_id, ids=message_id)
        outgoing = bool(msg and msg.out)
        return {
            "can_be_deleted_only_for_self": msg is not None,
            "can_be_deleted_for_all_users": outgoing,
        }

    def get_me(self) -> CallResult:
        return self._call(self._get_me())

    async def _get_me(self) -> Dict[str, Any]:
        me = await self.client.get_me()
        if not isinstance(me, types.User):
            raise RuntimeError("the current account is not a user")
        return _user_dict(me)

    def get_user(self, user_id: int) -> CallResult:
        return self._call(self._get_user(user_id))

    async def _get_user(self, user_id: int) -> Dict[str, Any]:
        entity = await self.client.get_entity(user_id)
        if isinstance(entity, types.User):
            return _user_dict(entity)
        return {
            "id": user_id,
            "first_name": _title(entity),
            "last_name": "",
            "username": getattr(entity, "username", "") or "",
            "phone_number": "",
            "type": {"@type": "userTypeRegular"},
            "status": {"@type": "userStatusEmpty"},
        }

    def get_user_full_info(self, user_id: int) -> CallResult:
        return self._call(self._get_user_full(user_id))

    async def _get_user_full(self, user_id: int) -> Dict[str, Any]:
        entity = await self.client.get_entity(user_id)
        if not isinstance(entity, types.User):
            return {"bio": ""}
        full = await self.client(GetFullUserRequest(entity))
        return {"bio": full.full_user.about or ""}

    def get_contacts(self) -> CallResult:
        return self._call(self._get_contacts())

    async def _get_contacts(self) -> Dict[str, Any]:
        result = await self.client(GetContactsRequest(hash=0))
        return {"user_ids": [user.id for user in result.users]}

    def search_contacts(self, target: str, limit: int = 10) -> CallResult:
        return self._call(self._search(target, limit))

    async def _search(self, query: str, limit: int) -> Dict[str, Any]:
        found = await self.client(SearchRequest(q=query, limit=limit))
        chat_ids = []
        for peer in list(found.my_results) + list(found.results):
            chat_ids.append(utils.get_peer_id(peer))
        return {"chat_ids": chat_ids}

    def get_basic_group(self, basic_group_id: int) -> CallResult:
        return self._call(self._basic_group(basic_group_id))

    def get_basic_group_full_info(self, basic_group_id: int) -> CallResult:
        return self._call(self._basic_group(basic_group_id))

    async def _basic_group(self, basic_group_id: int) -> Dict[str, Any]:
        full = await self.client(GetFullChatRequest(basic_group_id))
        participants = getattr(full.full_chat, "participants", None)
        members = getattr(participants, "participants", None) or []
        invite = getattr(full.full_chat, "exported_invite", None)
        return {
            "id": basic_group_id,
            "member_count": len(members),
            "description": getattr(full.full_chat, "about", "") or "",
            "invite_link": getattr(invite, "link", "") or "",
        }

    def get_supergroup(self, supergroup_id: int) -> CallResult:
        return self._call(self._supergroup(supergroup_id))

    def get_supergroup_full_info(self, supergroup_id: int) -> CallResult:
        return self._call(self._supergroup(supergroup_id))

    async def _supergroup(self, supergroup_id: int) -> Dict[str, Any]:
        entity = await self.client.get_entity(types.PeerChannel(supergroup_id))
        full = await self.client(GetFullChannelRequest(entity))
        invite = getattr(full.full_chat, "exported_invite", None)
        return {
            "id": supergroup_id,
            "member_count": int(full.full_chat.participants_count or 0),
            "description": full.full_chat.about or "",
            "invite_link": getattr(invite, "link", "") or "",
        }

    def get_secret_chat(self, secret_chat_id: int) -> CallResult:
        peer, pending = self._lookup_secret(secret_chat_id)
        if peer is None:
            return CallResult(
                error=True, error_info="Secret chat was not found"
            )
        user_id = peer.user_id or self._secret_users.get(secret_chat_id, 0)
        if pending or not peer.auth_key:
            key_hash = ""
            state = "secretChatStatePending"
        else:
            digest = hashlib.sha256(peer.auth_key).digest()
            key_hash = base64.b64encode(digest).decode()
            state = "secretChatStateReady"
        return CallResult(
            update={
                "user_id": user_id,
                "key_hash": key_hash,
                "state": {"@type": state},
            }
        )

    def create_new_secret_chat(self, user_id: int) -> CallResult:
        return self._call(self._create_secret(user_id))

    def close_secret_chat(self, secret_chat_id: int) -> CallResult:
        return self._call(self._close_secret(secret_chat_id))

    def create_new_basic_group_chat(
        self, user_ids: List[int], title: str
    ) -> CallResult:
        return self._call(self._create_group(user_ids, title))

    async def _create_group(
        self, user_ids: List[int], title: str
    ) -> Dict[str, Any]:
        users = []
        for user_id in user_ids:
            entity = await self.client.get_entity(user_id)
            users.append(utils.get_input_user(entity))
        await self.client(CreateChatRequest(users=users, title=title))
        return {}

    def leave_chat(self, chat_id: int) -> CallResult:
        return self._call(self._leave_chat(chat_id))

    async def _leave_chat(self, chat_id: int) -> Dict[str, Any]:
        entity = await self.client.get_entity(chat_id)
        if isinstance(entity, types.Channel):
            await self.client(LeaveChannelRequest(entity))
        elif isinstance(entity, types.Chat):
            me = await self.client.get_me()
            await self.client(
                DeleteChatUserRequest(
                    chat_id=entity.id, user_id=utils.get_input_user(me)
                )
            )
        return {}

    def delete_chat_history(
        self,
        chat_id: int,
        remove_from_chat_list: bool,
        revoke: bool = False,
    ) -> CallResult:
        return self._call(
            self._delete_history(chat_id, remove_from_chat_list, revoke)
        )

    async def _delete_history(
        self, chat_id: int, remove_from_chat_list: bool, revoke: bool
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            await self._close_secret(chat_id)
            return {}
        if remove_from_chat_list:
            await self.client.delete_dialog(chat_id, revoke=revoke)
        return {}

    def toggle_chat_is_pinned(
        self, chat_id: int, is_pinned: bool
    ) -> CallResult:
        result = self._call(self._pin(chat_id, is_pinned))
        if not result.error:
            order = int(datetime.now().timestamp())
            if is_pinned:
                order += PINNED_ORDER
            self._emit(
                "updateChatIsPinned",
                {
                    "chat_id": chat_id,
                    "is_pinned": is_pinned,
                    "order": order,
                },
            )
        return result

    async def _pin(self, chat_id: int, is_pinned: bool) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {}
        peer = await self.client.get_input_entity(chat_id)
        await self.client(
            ToggleDialogPinRequest(
                peer=InputDialogPeer(peer), pinned=is_pinned
            )
        )
        return {}

    def toggle_chat_is_marked_as_unread(
        self, chat_id: int, is_marked_as_unread: bool
    ) -> CallResult:
        result = self._call(self._mark_unread(chat_id, is_marked_as_unread))
        if not result.error:
            self._emit(
                "updateChatIsMarkedAsUnread",
                {
                    "chat_id": chat_id,
                    "is_marked_as_unread": is_marked_as_unread,
                },
            )
        return result

    async def _mark_unread(self, chat_id: int, unread: bool) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {}
        peer = await self.client.get_input_entity(chat_id)
        await self.client(
            MarkDialogUnreadRequest(peer=InputDialogPeer(peer), unread=unread)
        )
        return {}

    def set_chat_nottification_settings(
        self, chat_id: int, notification_settings: Dict[str, Any]
    ) -> CallResult:
        result = self._call(self._mute(chat_id, notification_settings))
        if not result.error:
            self._emit(
                "updateChatNotificationSettings",
                {
                    "chat_id": chat_id,
                    "notification_settings": notification_settings,
                },
            )
        return result

    async def _mute(
        self, chat_id: int, notification_settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self._is_secret(chat_id):
            return {}
        mute_for = int(notification_settings.get("mute_for") or 0)
        if mute_for:
            mute_until = datetime.now(timezone.utc) + timedelta(
                seconds=mute_for
            )
        else:
            mute_until = datetime.fromtimestamp(0, timezone.utc)
        peer = await self.client.get_input_entity(chat_id)
        await self.client(
            UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer),
                settings=InputPeerNotifySettings(mute_until=mute_until),
            )
        )
        return {}

    async def _start_secrets(self) -> None:
        from telethon_secret_chat import SECRET_TYPES, SecretChatManager
        from telethon_secret_chat.secret_chat_manager import patch_tlobjects

        patch_tlobjects()
        self.secrets = SecretChatManager(
            self.client,
            session=self.client.session,
            auto_accept=True,
            new_chat_created=self._on_secret_ready,
        )
        self.secrets.add_secret_event_handler(
            SECRET_TYPES.decrypt, self._on_secret_message
        )
        self.client.add_event_handler(self._on_secret_raw)
        self._load_secret_store()

    async def _on_secret_raw(self, update: Any) -> None:
        if isinstance(update, types.UpdateEncryption) and isinstance(
            update.chat, types.EncryptedChatDiscarded
        ):
            self._forget_secret_chat(update.chat.id)

    async def _on_secret_ready(
        self, chat: Any, created_by_me: bool = False
    ) -> None:
        try:
            user_id = (
                getattr(chat, "participant_id", 0)
                if created_by_me
                else getattr(chat, "admin_id", 0)
            )
            try:
                stored = self.secrets.get_secret_chat(chat.id)
            except ValueError:
                stored = None
            if stored is None:
                stored, _pending = self._lookup_secret(chat.id)
            if stored is None:
                return
            user_id = stored.user_id or user_id
            self._secret_users[chat.id] = user_id
            self._save_secret_store()
            info = await self._secret_chat_dict(stored, False, user_id)
            self._known_chats.add(chat.id)
            self._emit("updateSecretChat", {"chat": info})
        except Exception:
            log.exception("Failed to publish secret chat %s", chat)

    async def _on_secret_message(self, event: Any) -> None:
        decrypted = event.decrypted_event
        action = getattr(decrypted, "action", None)
        media = None
        if action is not None:
            ttl = getattr(action, "ttl_seconds", None)
            text = f"[message TTL set to {ttl}s]" if ttl is not None else ""
            if not text:
                return
            kind = "text"
        else:
            text = getattr(decrypted, "message", "") or ""
            media = getattr(decrypted, "media", None)
            if media is not None and "Empty" in type(media).__name__:
                media = None
            caption = getattr(media, "caption", "") if media else ""
            if caption and not text:
                text = caption
            kind = _secret_media_kind(media)
        chat_id = int(event.message.chat_id)
        await self._ensure_secret_listed(chat_id)
        path = ""
        size = int(getattr(media, "size", 0) or 0) if media else 0
        if media is not None:
            path, size = await self._save_secret_media(
                chat_id, decrypted, media, size
            )
        random_id = int(getattr(decrypted, "random_id", 0) or 0)
        date = _ts(getattr(event.message, "date", None)) or int(
            datetime.now().timestamp()
        )
        duration = int(getattr(media, "duration", 0) or 0) if media else 0
        msg = self._secret_message(
            chat_id,
            text,
            outgoing=False,
            kind=kind,
            path=path,
            size=size,
            file_name=_secret_file_name(media),
            duration=duration,
            random_id=random_id,
            date=date,
        )
        self._publish_secret_message(msg)

    async def _create_secret(self, user_id: int) -> Dict[str, Any]:
        chat_id = await self.secrets.start_secret_chat(user_id)
        self._secret_users[chat_id] = user_id
        self._save_secret_store()
        temp = self.secrets.session.get_temp_secret_chat_by_id(chat_id)
        info = await self._secret_chat_dict(temp, True, user_id)
        self._known_chats.add(chat_id)
        self._emit("updateSecretChat", {"chat": info})
        return {}

    async def _close_secret(self, chat_id: int) -> Dict[str, Any]:
        peer, _pending = self._lookup_secret(chat_id)
        if peer is not None:
            try:
                await self.secrets.close_secret_chat(peer)
            except Exception:
                log.exception("Failed to discard secret chat %s", chat_id)
        self._forget_secret_chat(chat_id)
        return {}

    def _require_ready_secret(self, chat_id: int) -> None:
        peer, pending = self._lookup_secret(chat_id)
        if peer is None or pending or not peer.auth_key:
            raise RuntimeError("Secret chat is not ready yet")

    async def _send_secret_text(
        self, chat_id: int, text: str, reply_to: Optional[int]
    ) -> Dict[str, Any]:
        self._require_ready_secret(chat_id)
        reply_random = self._secret_random_id(chat_id, reply_to)
        await self.secrets.send_secret_message(
            chat_id, text, reply_to_id=reply_random, parse_mode=None
        )
        msg = self._secret_message(
            chat_id,
            text,
            outgoing=True,
            kind="text",
            reply_to=reply_to,
        )
        self._publish_secret_message(msg)
        return msg

    async def _send_secret_file(
        self, chat_id: int, file_path: str, **kwargs: Any
    ) -> Dict[str, Any]:
        self._require_ready_secret(chat_id)
        size = os.path.getsize(file_path)
        mime, _encoding = mimetypes.guess_type(file_path)
        mime = mime or "application/octet-stream"
        name = os.path.basename(file_path)
        duration, width, height = _media_shape(kwargs.get("attributes"))
        if kwargs.get("voice_note"):
            kind = "voice"
            await self.secrets.send_secret_audio(
                chat_id, file_path, duration, mime, size
            )
        elif mime.startswith("image/") and not kwargs.get("force_document"):
            kind = "photo"
            await self.secrets.send_secret_photo(
                chat_id, file_path, b"", 0, 0, width, height, size
            )
        elif mime.startswith("video/"):
            kind = "video"
            await self.secrets.send_secret_video(
                chat_id,
                file_path,
                b"",
                0,
                0,
                duration,
                mime,
                width,
                height,
                size,
            )
        else:
            kind = "document"
            await self.secrets.send_secret_document(
                chat_id, file_path, b"", 0, 0, name, mime, size, []
            )
        msg = self._secret_message(
            chat_id,
            "",
            outgoing=True,
            kind=kind,
            path=file_path,
            size=size,
            file_name=name,
            duration=duration,
        )
        self._publish_secret_message(msg)
        return msg

    async def _secret_chat_list(self) -> List[Dict[str, Any]]:
        chats = []
        for peer, pending in self._iter_secret_chats():
            user_id = peer.user_id or self._secret_users.get(peer.id, 0)
            chats.append(await self._secret_chat_dict(peer, pending, user_id))
            self._known_chats.add(peer.id)
        return chats

    async def _secret_chat_info(self, chat_id: int) -> Dict[str, Any]:
        peer, pending = self._lookup_secret(chat_id)
        if peer is None:
            raise ValueError(f"secret chat {chat_id} was not found")
        user_id = peer.user_id or self._secret_users.get(chat_id, 0)
        return await self._secret_chat_dict(peer, pending, user_id)

    async def _ensure_secret_listed(self, chat_id: int) -> None:
        if chat_id in self._known_chats:
            return
        info = await self._secret_chat_info(chat_id)
        self._known_chats.add(chat_id)
        self._emit("updateSecretChat", {"chat": info})

    async def _secret_chat_dict(
        self, peer: Any, pending: bool, user_id: int
    ) -> Dict[str, Any]:
        title = "Secret chat"
        if user_id:
            try:
                title = _title(await self.client.get_entity(user_id))
            except Exception:
                title = f"user {user_id}"
        stored = self._secret_messages.get(peer.id, [])
        last_message = (
            max(stored, key=lambda item: item["id"]) if stored else None
        )
        order = last_message["date"] if last_message else _ts(peer.created)
        if order <= 0:
            order = 1
        last_id = last_message["id"] if last_message else 0
        return {
            "id": peer.id,
            "title": title,
            "order": order,
            "is_pinned": False,
            "unread_count": 0,
            "is_marked_as_unread": False,
            "last_message": last_message,
            "last_read_inbox_message_id": last_id,
            "last_read_outbox_message_id": last_id,
            "notification_settings": {"mute_for": 0},
            "positions": [{"order": order, "is_pinned": False}],
            "type": {
                "@type": "chatTypeSecret",
                "secret_chat_id": peer.id,
                "user_id": user_id,
            },
            "permissions": {"can_send_basic_messages": not pending},
            "can_be_deleted_for_all_users": False,
        }

    def _secret_message(
        self,
        chat_id: int,
        text: str,
        outgoing: bool,
        kind: str,
        path: str = "",
        size: int = 0,
        file_name: str = "",
        duration: int = 0,
        random_id: int = 0,
        date: int = 0,
        reply_to: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._secret_seq += 1
        msg_id = int(datetime.now().timestamp() * 1000) * 1000
        msg_id += self._secret_seq % 1000
        user_id = self._me_id if outgoing else self._secret_peer_user(chat_id)
        reply = None
        if reply_to:
            reply = {"chat_id": chat_id, "message_id": reply_to}
        return {
            "@type": "message",
            "id": msg_id,
            "chat_id": chat_id,
            "date": date or int(datetime.now().timestamp()),
            "edit_date": 0,
            "is_outgoing": outgoing,
            "sender_id": {"user_id": user_id},
            "content": _secret_content(
                kind, msg_id, text, path, size, file_name, duration
            ),
            "reply_to": reply,
            "forward_info": None,
            "can_be_edited": False,
            "can_be_forwarded": False,
            "reply_markup": None,
            "sending_state": None,
            "secret_random_id": random_id,
        }

    def _publish_secret_message(self, msg: Dict[str, Any]) -> None:
        chat_id = msg["chat_id"]
        bucket = self._secret_messages.setdefault(chat_id, [])
        random_id = int(msg.get("secret_random_id") or 0)
        if random_id and not msg["is_outgoing"]:
            for existing in bucket:
                if int(existing.get("secret_random_id") or 0) == random_id:
                    return
        bucket.append(msg)
        bucket.sort(key=lambda item: item["id"])
        self._save_secret_store()
        self._emit("updateNewMessage", {"message": msg})
        self._emit(
            "updateChatLastMessage",
            {
                "chat_id": chat_id,
                "last_message": msg,
                "positions": [{"order": msg["date"]}],
            },
        )

    def _secret_history(
        self, chat_id: int, from_message_id: int, limit: int
    ) -> List[Dict[str, Any]]:
        messages = list(self._secret_messages.get(chat_id, []))
        messages.sort(key=lambda item: item["id"], reverse=True)
        if from_message_id:
            messages = [
                item for item in messages if item["id"] < from_message_id
            ]
        return messages[:limit]

    def _find_secret_message(
        self, chat_id: int, msg_id: int
    ) -> Optional[Dict[str, Any]]:
        for msg in self._secret_messages.get(chat_id, []):
            if msg["id"] == msg_id:
                return msg
        return None

    def _secret_random_id(
        self, chat_id: int, msg_id: Optional[int]
    ) -> Optional[int]:
        if not msg_id:
            return None
        msg = self._find_secret_message(chat_id, msg_id)
        if not msg:
            return None
        random_id = int(msg.get("secret_random_id") or 0)
        return random_id or None

    def _drop_secret_messages(
        self, chat_id: int, message_ids: List[int]
    ) -> None:
        wanted = set(message_ids)
        self._secret_messages[chat_id] = [
            msg
            for msg in self._secret_messages.get(chat_id, [])
            if msg["id"] not in wanted
        ]
        self._save_secret_store()

    def _forget_secret_chat(self, chat_id: int) -> None:
        self._secret_messages.pop(chat_id, None)
        self._secret_users.pop(chat_id, None)
        self._known_chats.discard(chat_id)
        self._save_secret_store()
        self._emit(
            "updateChatPosition",
            {"chat_id": chat_id, "position": {"order": 0}},
        )

    def _secret_peer_user(self, chat_id: int) -> int:
        peer, _pending = self._lookup_secret(chat_id)
        if peer is not None and peer.user_id:
            return int(peer.user_id)
        return self._secret_users.get(chat_id, 0)

    def _is_secret(self, chat_id: int) -> bool:
        peer, _pending = self._lookup_secret(chat_id)
        return peer is not None

    def _lookup_secret(self, chat_id: int) -> tuple[Any, bool]:
        if self.secrets is None:
            return None, False
        session = self.secrets.session
        ready = session.get_secret_chat_by_id(chat_id)
        if ready is not None:
            return ready, False
        pending = session.get_temp_secret_chat_by_id(chat_id)
        if pending is not None:
            return pending, True
        return None, False

    def _iter_secret_chats(self) -> List[tuple[Any, bool]]:
        if self.secrets is None:
            return []
        session = self.secrets.session
        conn = getattr(session, "_conn", None)
        found: List[tuple[Any, bool]] = []
        if conn is not None:
            try:
                rows = conn.execute(
                    "select id, temp from plugin_secret_chats"
                ).fetchall()
            except Exception:
                rows = []
            for chat_id, temp in rows:
                pending = bool(temp)
                getter = (
                    session.get_temp_secret_chat_by_id
                    if pending
                    else session.get_secret_chat_by_id
                )
                peer = getter(chat_id)
                if peer is not None:
                    found.append((peer, pending))
            return found
        for peer in session.secret_chats.values():
            found.append((peer, False))
        for peer in session.temp_secret_chat.values():
            found.append((peer, True))
        return found

    async def _save_secret_media(
        self, chat_id: int, decrypted: Any, media: Any, size: int
    ) -> tuple[str, int]:
        try:
            data = await self.secrets.download_secret_media(decrypted)
        except Exception:
            log.exception("Could not download secret media in %s", chat_id)
            return "", size
        if not data:
            return "", size
        mime = getattr(media, "mime_type", "") or ""
        ext = mimetypes.guess_extension(mime) or ""
        self._secret_seq += 1
        path = os.path.join(
            os.path.dirname(self._secret_store),
            f"secret-{chat_id}-{self._secret_seq}{ext}",
        )
        with open(path, "wb") as handle:
            handle.write(data)
        return path, len(data)

    def _load_secret_store(self) -> None:
        try:
            with open(self._secret_store, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        self._secret_seq = int(raw.get("seq") or 0)
        self._secret_users = {
            int(chat_id): int(user_id)
            for chat_id, user_id in (raw.get("users") or {}).items()
        }
        self._secret_messages = {
            int(chat_id): messages
            for chat_id, messages in (raw.get("messages") or {}).items()
        }

    def _save_secret_store(self) -> None:
        payload = {
            "seq": self._secret_seq,
            "users": {
                str(chat_id): user_id
                for chat_id, user_id in self._secret_users.items()
            },
            "messages": {
                str(chat_id): messages
                for chat_id, messages in self._secret_messages.items()
            },
        }
        tmp = self._secret_store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self._secret_store)


def _secret_media_kind(media: Any) -> str:
    if media is None:
        return "text"
    name = type(media).__name__
    if "Photo" in name:
        return "photo"
    if "Video" in name:
        return "video"
    if "Audio" in name:
        return "voice"
    if "Document" in name:
        return "document"
    return "text"


def _secret_file_name(media: Any) -> str:
    if media is None:
        return ""
    for attr in getattr(media, "attributes", None) or []:
        file_name = getattr(attr, "file_name", None)
        if file_name:
            return str(file_name)
    return ""


def _media_shape(attributes: Any) -> tuple[int, int, int]:
    duration = width = height = 0
    for attr in attributes or []:
        duration = int(getattr(attr, "duration", 0) or duration)
        width = int(getattr(attr, "w", 0) or width)
        height = int(getattr(attr, "h", 0) or height)
    return duration, width, height


def _secret_content(
    kind: str,
    msg_id: int,
    text: str,
    path: str,
    size: int,
    file_name: str,
    duration: int,
) -> Dict[str, Any]:
    local = {
        "path": path,
        "is_downloading_completed": bool(path),
        "is_downloading_active": False,
        "downloaded_size": size if path else 0,
    }
    file_info = {"id": msg_id, "size": size, "local": local}
    caption = {"caption": {"text": text}} if text else {}
    if kind == "photo":
        return {
            "@type": "messagePhoto",
            "photo": {"sizes": [{"photo": {"file": file_info}}]},
            **caption,
        }
    if kind == "voice":
        return {
            "@type": "messageVoiceNote",
            "is_listened": False,
            "voice_note": {
                "duration": duration,
                "voice": {"file": file_info},
            },
        }
    if kind == "video":
        return {
            "@type": "messageVideo",
            "video": {
                "file_name": file_name,
                "duration": duration,
                "video": {"file": file_info},
            },
            **caption,
        }
    if kind == "document":
        return {
            "@type": "messageDocument",
            "document": {
                "file_name": file_name,
                "document": {"file": file_info},
            },
            **caption,
        }
    return {
        "@type": "messageText",
        "text": {"@type": "formattedText", "text": text, "entities": []},
    }
