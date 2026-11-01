import base64
import logging
import shutil
import sys
import time
from collections import defaultdict, namedtuple
from typing import Any, Dict, List, Optional, Set, Tuple

from tg.client import Client
from tg.msg import MsgProxy
from tg.types import (
    ChatAction,
    ChatType,
    SecretChatState,
    UserStatus,
    UserType,
    get_chat_type,
)
from tg.utils import copy_to_clipboard, pretty_ts

log = logging.getLogger(__name__)


class Model:
    def __init__(self, tg: Client) -> None:
        self.tg = tg
        self.chats = ChatModel(tg)
        self.msgs = MsgModel(tg)
        self.users = UserModel(tg)
        self.current_chat = 0
        self.downloads: Dict[int, Tuple[int, int]] = {}
        self.selected: Dict[int, List[int]] = defaultdict(list)
        self.copied_msgs: Tuple[int, List[int]] = (0, [])

    def get_me(self) -> Dict[str, Any]:
        return self.users.get_me()

    def is_me(self, user_id: int) -> bool:
        if self.users.me:
            return self.users.me.get("id") == user_id
        return user_id == self.tg._me_id

    @property
    def current_chat_id(self) -> Optional[int]:
        return self.chats.id_by_index(self.current_chat)

    def get_current_chat_msg_idx(self) -> Optional[int]:
        current_chat_id = self.chats.id_by_index(self.current_chat)
        if current_chat_id is None:
            return None
        current_chat = self.chats.chat_by_index(self.current_chat)
        current_msg_indx = self.msgs.current_msgs[current_chat_id]
        if current_msg_indx is None:
            # A large unread count would otherwise request that many
            # messages before the first paint.
            unread = (
                0 if current_chat is None else current_chat.get("unread_count")
            )
            unread_count = min(int(unread or 0), 30)
            self.msgs.current_msgs[current_chat_id] = unread_count
            current_msg_indx = unread_count
        return current_msg_indx

    def fetch_msgs(
        self,
        current_position: int = 0,
        page_size: int = 10,
        msgs_left_scroll_threshold: int = 10,
    ) -> List[Tuple[int, Dict[str, Any]]]:
        chat_id = self.chats.id_by_index(self.current_chat)
        if chat_id is None:
            return []
        offset = max(current_position - msgs_left_scroll_threshold, 0)
        msgs = self.msgs.fetch_msgs(chat_id, offset=offset, limit=page_size)
        return msgs
        # As we are fetching messages from 'current_posiiton',
        # so the current message index will be changed and neeed to be updated
        # chat = self.chats.chat_by_index(self.current_chat)
        # if last_read_msg_id := chat.get("last_read_inbox_message_id", None):
        #     for last_read_msg_idx, msg in enumerate(msgs):
        #         if msg["id"] == last_read_msg_id:
        #             self.msgs.current_msgs[chat_id] = last_read_msg_idx
        #             break
        #     next(msgs, lambda msg: msg["id"] == last_read_msg_id)
        #     self.msgs.current_msgs[chat_id] = last_read_msg_idx

    @property
    def current_msg(self) -> Dict[str, Any]:
        chat_id = self.chats.id_by_index(self.current_chat)
        if chat_id is None:
            return {}
        current_msg = self.msgs.current_msgs[chat_id]
        if current_msg is None:
            return {}
        log.info("current-msg: %s", current_msg)
        msg_id = self.msgs.msg_ids[chat_id][current_msg]
        return self.msgs.msgs[chat_id][msg_id]

    @property
    def current_msg_id(self) -> int:
        return self.current_msg["id"]

    def jump_bottom(self) -> bool:
        res = False
        if chat_id := self.chats.id_by_index(self.current_chat):
            res = self.msgs.jump_bottom(chat_id)
        self.view_current_msg()
        return res

    def set_current_chat_by_id(self, chat_id: int) -> bool:
        idx = next(
            iter(
                i
                for i, chat in enumerate(self.chats.chats)
                if chat["id"] == chat_id
            )
        )
        return self.set_current_chat(idx)

    def set_current_chat(self, chat_idx: int) -> bool:
        if 0 < chat_idx < len(self.chats.chats):
            self.current_chat = chat_idx
            return True
        return False

    def next_chat(self, step: int = 1) -> bool:
        new_idx = self.current_chat + step
        if new_idx < len(self.chats.chats):
            self.current_chat = new_idx
            return True
        return False

    def prev_chat(self, step: int = 1) -> bool:
        if self.current_chat == 0:
            return False
        self.current_chat = max(0, self.current_chat - step)
        return True

    def first_chat(self) -> bool:
        if self.current_chat != 0:
            self.current_chat = 0
            return True
        return False

    def view_current_msg(self) -> None:
        msg = MsgProxy(self.current_msg)
        if chat_id := self.chats.id_by_index(self.current_chat):
            self.tg.view_messages(chat_id, [msg.msg_id])

    def note_rendered_msgs(self, msg_ids: List[int]) -> bool:
        """Treat shown messages as read so the "new" badge does not stick.

        Returns True when the read cursor moved and the pane should be
        laid out again.
        """
        if not msg_ids:
            return False
        chat = self.chats.chat_by_index(self.current_chat)
        if not chat:
            return False
        max_id = max(msg_ids)
        last_read = int(chat.get("last_read_inbox_message_id") or 0)
        if max_id <= last_read:
            return False
        chat["last_read_inbox_message_id"] = max_id
        last_message = chat.get("last_message") or {}
        if int(last_message.get("id") or 0) <= max_id:
            chat["unread_count"] = 0
        self.tg.view_messages(chat["id"], [max_id])
        return True

    def view_all_msgs(self) -> None:
        chat = self.chats.chats[self.current_chat]
        last_message = chat.get("last_message")
        if not last_message:
            return
        self.tg.view_messages(chat["id"], [last_message["id"]])

    def next_msg(self, step: int = 1) -> bool:
        chat_id = self.chats.id_by_index(self.current_chat)
        if not chat_id:
            return False
        is_next = self.msgs.next_msg(chat_id, step)
        self.view_current_msg()
        return is_next

    def prev_msg(self, step: int = 1) -> bool:
        chat_id = self.chats.id_by_index(self.current_chat)
        if not chat_id:
            return False
        is_prev = self.msgs.prev_msg(chat_id, step)
        if is_prev:
            self.view_current_msg()
        return is_prev

    def get_chats(
        self,
        current_position: int = 0,
        page_size: int = 10,
        msgs_left_scroll_threshold: int = 10,
    ) -> List[Dict[str, Any]]:
        chats_left = page_size - current_position
        offset = max(msgs_left_scroll_threshold - chats_left, 0)
        limit = offset + page_size
        return self.chats.fetch_chats(offset=offset, limit=limit)

    def send_message(self, text: str) -> bool:
        chat_id = self.chats.id_by_index(self.current_chat)
        if chat_id is None:
            return False
        # order is matter: this should be before send_message
        # otherwise it will view message that was sent
        self.view_all_msgs()
        self.msgs.send_message(chat_id, text)
        return True

    def edit_message(self, text: str) -> bool:
        if chat_id := self.chats.id_by_index(self.current_chat):
            return self.msgs.edit_message(chat_id, self.current_msg_id, text)
        return False

    def can_be_deleted(self, chat_id: int, msg: Dict[str, Any]) -> bool:
        c_id = msg["sender_id"].get("chat_id") or msg["sender_id"].get(
            "user_id"
        )
        tg_result = self.tg.get_message_properties(chat_id, msg["id"])
        tg_result.wait()
        message_properties = tg_result.update
        if chat_id == c_id:
            return message_properties.get("can_be_deleted_only_for_self", False)
        return message_properties.get("can_be_deleted_for_all_users", False)

    def delete_msgs(self) -> bool:
        chat_id = self.chats.id_by_index(self.current_chat)
        if not chat_id:
            return False
        msg_ids = self.selected[chat_id]
        if msg_ids:
            message_ids = msg_ids
            for msg_id in message_ids:
                msg = self.msgs.get_message(chat_id, msg_id)
                if not msg or not self.can_be_deleted(chat_id, msg):
                    return False
        else:
            selected_msg = self.msgs.current_msgs[chat_id]
            if selected_msg is None:
                return False
            msg_id = self.msgs.msg_ids[chat_id][selected_msg]
            msg = self.msgs.msgs[chat_id][msg_id]
            if not self.can_be_deleted(chat_id, msg):
                return False
            message_ids = [msg["id"]]

        log.info(f"Deleting msg from the chat {chat_id}: {message_ids}")
        self.tg.delete_messages(chat_id, message_ids, revoke=True)
        return True

    def forward_msgs(self) -> bool:
        chat_id = self.chats.id_by_index(self.current_chat)
        if not chat_id:
            return False
        from_chat_id, msg_ids = self.copied_msgs
        if not msg_ids:
            return False
        for msg_id in msg_ids:
            msg = self.msgs.get_message(from_chat_id, msg_id)
            if not msg or not msg["can_be_forwarded"]:
                return False

        self.tg.forward_messages(chat_id, from_chat_id, msg_ids)
        self.copied_msgs = (0, [])
        return True

    def copy_msgs_text(self) -> bool:
        """Copies current msg text or path to file if it's file"""
        buffer = []

        from_chat_id, msg_ids = self.copied_msgs
        if not msg_ids:
            return False
        for msg_id in msg_ids:
            _msg = self.msgs.get_message(from_chat_id, msg_id)
            if not _msg:
                return False
            msg = MsgProxy(_msg)
            if msg.file_id and msg.local_path:
                buffer.append(msg.local_path)
            elif msg.is_text:
                buffer.append(msg.text_content)
        copy_to_clipboard("\n".join(buffer))
        return True

    def copy_files(
        self, chat_id: int, msg_ids: List[int], dest_dir: str
    ) -> bool:
        is_copied = False
        for msg_id in msg_ids:
            _msg = self.msgs.get_message(chat_id, msg_id)
            if not _msg:
                return False
            msg = MsgProxy(_msg)
            if msg.file_id and msg.local_path:
                file_path = msg.local_path
                shutil.copy2(file_path, dest_dir)
                is_copied = True
        return is_copied

    def get_private_chat_info(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        user_id = chat["id"]
        user = self.users.get_user(user_id)
        user_info = self.users.get_user_full_info(user_id)
        status = self.users.get_status(user_id)
        return {
            chat["title"]: status,
            "Username": user.get("username", ""),
            "Phone": user.get("phone_number", ""),
            "Bio": user_info.get("bio", ""),
        }

    def get_basic_group_info(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        group_id = chat["type"]["basic_group_id"]
        result = self.tg.get_basic_group_full_info(group_id)
        result.wait()
        chat_info = result.update
        basic = self.tg.get_basic_group(group_id)
        basic.wait()
        basic_info = basic.update
        return {
            chat["title"]: f"{basic_info['member_count']} members",
            "Info": chat_info["description"],
            "Share link": chat_info["invite_link"],
        }

    def get_supergroup_info(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        result = self.tg.get_supergroup_full_info(chat["type"]["supergroup_id"])
        result.wait()
        chat_info = result.update
        return {
            chat["title"]: f"{chat_info['member_count']} members",
            "Info": chat_info["description"],
            "Share link": chat_info["invite_link"],
        }

    def get_channel_info(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        result = self.tg.get_supergroup_full_info(chat["type"]["supergroup_id"])
        result.wait()
        chat_info = result.update
        return {
            chat["title"]: "subscribers",
            "Info": chat_info["description"],
            "Share link": chat_info["invite_link"],
        }

    def get_secret_chat_info(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        result = self.tg.get_secret_chat(chat["type"]["secret_chat_id"])
        result.wait()
        chat_info = result.update
        enc_key = base64.b64decode(chat_info["key_hash"])[:32].hex()
        hex_key = " ".join(
            [enc_key[i : i + 2] for i in range(0, len(enc_key), 2)]
        )

        state = "Unknown"
        try:
            state = SecretChatState[chat_info["state"]["@type"]].value
        except KeyError:
            pass

        user_id = chat_info["user_id"]
        user_info = self.get_user_info(user_id)

        return {**user_info, "State": state, "Encryption Key": hex_key}

    def get_chat_info(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        chat_type = get_chat_type(chat)
        if chat_type is None:
            return {}

        handlers = {
            ChatType.chatTypePrivate: self.get_private_chat_info,
            ChatType.chatTypeBasicGroup: self.get_basic_group_info,
            ChatType.chatTypeSupergroup: self.get_supergroup_info,
            ChatType.channel: self.get_channel_info,
            ChatType.chatTypeSecret: self.get_secret_chat_info,
        }

        info = handlers.get(chat_type, lambda _: dict())(chat)

        info.update({"Type": chat_type.value, "Chat Id": chat["id"]})
        return info

    def get_user_info(self, user_id: int) -> Dict[str, Any]:
        name = self.users.get_user_label(user_id)
        status = self.users.get_status(user_id)
        user = self.users.get_user(user_id)
        user_info = self.users.get_user_full_info(user_id)
        user_type = "Unknown"
        try:
            user_type = UserType[user["type"]["@type"]].value
        except KeyError:
            pass
        info = {
            name: status,
            "Username": user.get("username", ""),
            "Bio": user_info.get("bio", ""),
            "Phone": user.get("phone_number", ""),
            "User Id": user_id,
            "Type": user_type,
        }
        return info


class ChatModel:
    def __init__(self, tg: Client) -> None:
        self.tg = tg
        self.chats: List[Dict[str, Any]] = []
        self.inactive_chats: Dict[int, Dict[str, Any]] = {}
        self.chat_ids: Set[int] = set()
        self.have_full_chat_list = False
        self.title: str = "Chats"
        self.found_chats: List[int] = []
        self.found_chat_idx: int = 0

    def next_found_chat(self, backwards: bool = False) -> int:
        new_idx = self.found_chat_idx + (-1 if backwards else 1)
        new_idx %= len(self.found_chats)

        self.found_chat_idx = new_idx

        return self.found_chats[new_idx]

    def id_by_index(self, index: int) -> Optional[int]:
        if index >= len(self.chats):
            return None
        return self.chats[index]["id"]

    def chat_by_index(self, index: int) -> Optional[Dict[str, Any]]:
        if index >= len(self.chats):
            return None
        return self.chats[index]

    def fetch_chats(
        self, offset: int = 0, limit: int = 10
    ) -> List[Dict[str, Any]]:
        if offset + limit > len(self.chats):
            self._load_next_chats()

        return self.chats[offset:limit]

    def _load_next_chats(self) -> None:
        if self.have_full_chat_list:
            return None
        result = self.tg.get_chats(offset=len(self.chats), limit=30)
        result.wait()
        if result.error:
            log.error("get chats error: %s", result.error_info)
            return None

        chats = result.update.get("chats") or []
        if result.update.get("full") or not chats:
            self.have_full_chat_list = True
        for chat in chats:
            self.add_chat(chat)

    def fetch_chat(self, chat_id: int) -> Dict[str, Any]:
        result = self.tg.get_chat(chat_id)
        result.wait()

        if result.error:
            log.error(f"get chat error: {result.error_info}")
            return {}
        return result.update

    def add_chat(self, chat: Dict[str, Any]) -> None:
        chat_id = chat["id"]
        if chat_id in self.chat_ids:
            return

        if len(chat["positions"]) > 0:
            chat["order"] = chat["positions"][0]["order"]
            chat["is_pinned"] = chat["positions"][0]["is_pinned"]
        else:
            chat["order"] = 0  # str(sys.maxsize)

        if int(chat["order"]) == 0:
            self.inactive_chats[chat_id] = chat
            return
        self.chat_ids.add(chat_id)
        self.chats.append(chat)
        self._sort_chats()

    def _sort_chats(self) -> None:
        self.chats = sorted(
            self.chats,
            # Pinned chats use a higher order so they stay above the rest.
            key=lambda it: (it["order"], it["id"]),
            reverse=True,
        )

    def update_chat(self, chat_id: int, **updates: Any) -> bool:
        for i, chat in enumerate(self.chats):
            if chat["id"] != chat_id:
                continue
            chat.update(updates)
            if int(chat["order"]) == 0:
                self.inactive_chats[chat_id] = chat
                self.chat_ids.discard(chat_id)
                self.chats = [
                    _chat for _chat in self.chats if _chat["id"] != chat_id
                ]
                log.info(f"Removing chat '{chat['title']}'")
            else:
                self._sort_chats()
                log.info(f"Updated chat with keys {list(updates)}")
            return True

        if _chat := self.inactive_chats.get(chat_id):
            _chat.update(updates)
            if int(_chat["order"]) != 0:
                del self.inactive_chats[chat_id]
                self.add_chat(_chat)
                log.info(f"Marked chat '{_chat['title']}' as active")
                return True
            return False

        log.warning(f"Can't find chat {chat_id} in existing chats")
        return False


class MsgModel:
    def __init__(self, tg: Client) -> None:
        self.tg = tg
        self.msgs: Dict[int, Dict[int, Dict]] = defaultdict(dict)
        self.current_msgs: Dict[int, Optional[int]] = defaultdict(lambda: None)
        self.not_found: Set[int] = set()
        self.msg_ids: Dict[int, List[int]] = defaultdict(list)

    def jump_to_msg_by_id(self, chat_id: int, msg_id: int) -> bool:
        if index := self.msg_ids[chat_id].index(msg_id):
            self.current_msgs[chat_id] = index
            return True
        return False

    def next_msg(self, chat_id: int, step: int = 1) -> bool:
        current_msg = self.current_msgs[chat_id]
        if current_msg is None or current_msg == 0:
            return False
        self.current_msgs[chat_id] = max(0, current_msg - step)
        return True

    def jump_bottom(self, chat_id: int) -> bool:
        if self.current_msgs[chat_id] == 0:
            return False
        self.current_msgs[chat_id] = 0
        return True

    def prev_msg(self, chat_id: int, step: int = 1) -> bool:
        current_msg = self.current_msgs[chat_id]
        if current_msg is None:
            current_msg = 0
        new_idx = current_msg + step
        if new_idx < len(self.msg_ids[chat_id]):
            self.current_msgs[chat_id] = new_idx
            return True
        return False

    def get_message(self, chat_id: int, msg_id: int) -> Optional[Dict]:
        if msg_id in self.not_found:
            return None
        if msg := self.msgs[chat_id].get(msg_id):
            return msg
        result = self.tg.get_message(chat_id, msg_id)
        result.wait()
        if result.error:
            self.not_found.add(msg_id)
            return None
        return result.update

    def remove_messages(self, chat_id: int, msg_ids: List[int]) -> None:
        log.info(f"removing msg {msg_ids=}")
        for msg_id in msg_ids:
            try:
                self.msg_ids[chat_id].remove(msg_id)
            except ValueError:
                pass
            self.msgs[chat_id].pop(msg_id, None)

    def add_message(self, chat_id: int, msg: Dict[str, Any]) -> None:
        log.info(f"adding {msg=}")
        msg_id = msg["id"]
        ids = self.msg_ids[chat_id]
        self.msgs[chat_id][msg_id] = msg
        ids.insert(0, msg_id)
        if len(ids) >= 2 and msg_id < ids[1]:
            self.msg_ids[chat_id].sort(reverse=True)

    def update_msg_content_opened(self, chat_id: int, msg_id: int) -> None:
        msg = self.msgs[chat_id].get(msg_id)
        if not msg:
            return
        msg_proxy = MsgProxy(msg)
        if msg_proxy.content_type == "voice":
            msg_proxy.is_listened = True
        elif msg_proxy.content_type == "recording":
            msg_proxy.is_viewed = True
        # TODO: start the TTL timer for self-destructing messages

    def update_msg(
        self, chat_id: int, msg_id: int, **fields: Dict[str, Any]
    ) -> None:
        msg = self.msgs[chat_id].get(msg_id)
        if not msg:
            return
        msg.update(fields)

    def _fetch_msgs_until_limit(
        self, chat_id: int, offset: int = 0, limit: int = 10
    ) -> List[Dict[str, Any]]:
        del offset
        from_message_id = 0
        if self.msg_ids[chat_id]:
            from_message_id = self.msg_ids[chat_id][-1]
        result = self.tg.get_chat_history(
            chat_id,
            from_message_id=from_message_id,
            limit=min(max(limit, 1), 50),
        )
        result.wait()
        if result.error or not result.update:
            return []
        messages = result.update.get("messages") or []
        return [msg for msg in messages if msg["id"] not in self.msgs[chat_id]]

    def fetch_msgs(
        self, chat_id: int, offset: int = 0, limit: int = 10
    ) -> List[Tuple[int, Dict[str, Any]]]:
        if offset + limit > len(self.msg_ids[chat_id]):
            msgs = self._fetch_msgs_until_limit(chat_id, offset, limit)
            for msg in msgs:
                self.add_message(chat_id, msg)

        return [
            (i + offset, self.msgs[chat_id][msg_id])
            for i, msg_id in enumerate(
                self.msg_ids[chat_id][offset : offset + limit]
            )
        ]

    def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        log.info("Editing msg")
        result = self.tg.edit_message_text(chat_id, message_id, text)

        result.wait()
        if result.error:
            log.info(f"send message error: {result.error_info}")
            return False
        else:
            log.info(f"message has been sent: {result.update}")
            return True

    def send_message(self, chat_id: int, text: str) -> None:
        result = self.tg.send_message(chat_id, text)
        result.wait()
        if result.error:
            log.info(f"send message error: {result.error_info}")
        else:
            log.info(f"message has been sent: {result.update}")


User = namedtuple("User", ["id", "name", "status", "order"])


class UserModel:

    types = {
        "userTypeUnknown": "unknown",
        "userTypeBot": "bot",
        "userTypeDeleted": "deleted",
        "userTypeRegular": "regular",
    }

    def __init__(self, tg: Client) -> None:
        self.tg = tg
        self.me: Dict[str, Any] = {}
        self.users: Dict[int, Dict] = {}
        self.groups: Dict[int, Dict] = {}
        self.supergroups: Dict[int, Dict] = {}
        self.actions: Dict[int, Dict] = {}
        self.not_found: Set[int] = set()
        self.contacts: Dict[str, Any] = {}

    def get_me(self) -> Dict[str, Any]:
        if self.me:
            return self.me
        result = self.tg.get_me()
        result.wait()
        if result.error:
            log.error(f"get myself error: {result.error_info}")
            return {}
        self.me = result.update
        return self.me

    def get_user_action(
        self, chat_id: int
    ) -> Tuple[Optional[int], Optional[str]]:
        action = self.actions.get(chat_id)
        if action is None:
            return None, None
        action_type = action["action"]["@type"]
        user_id = action["user_id"]
        try:
            return user_id, ChatAction[action_type].value
        except KeyError:
            log.error(f"ChatAction type {action_type} not implemented")
        return None, None

    def set_status(self, user_id: int, status: Dict[str, Any]) -> None:
        if user_id not in self.users:
            self.get_user(user_id)
        self.users[user_id]["status"] = status

    def get_status(self, user_id: int) -> str:
        if user_id not in self.users:
            return ""
        if self.is_bot(user_id):
            return "bot"
        user_status = self.users[user_id]["status"]

        try:
            status = UserStatus[user_status["@type"]]
        except KeyError:
            log.error(f"UserStatus type {user_status} not implemented")
            return ""

        if status == UserStatus.userStatusEmpty:
            return ""
        elif status == UserStatus.userStatusOnline:
            expires = user_status["expires"]
            if expires < time.time():
                return ""
            return status.value
        elif status == UserStatus.userStatusOffline:
            was_online = user_status["was_online"]
            ago = pretty_ts(was_online)
            return f"last seen {ago}"
        return f"last seen {status.value}"

    def get_user_status_order(self, user_id: int) -> int:
        if user_id not in self.users:
            return sys.maxsize
        user_status = self.users[user_id]["status"]

        try:
            status = UserStatus[user_status["@type"]]
        except KeyError:
            log.error(f"UserStatus type {user_status} not implemented")
            return sys.maxsize
        if status == UserStatus.userStatusOnline:
            return 0
        elif status == UserStatus.userStatusOffline:
            was_online = user_status["was_online"]
            return time.time() - was_online
        order = {
            UserStatus.userStatusRecently: 1,
            UserStatus.userStatusLastWeek: 2,
            UserStatus.userStatusLastMonth: 3,
        }
        return order.get(status, sys.maxsize)

    def is_bot(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if user and user["type"]["@type"] == "userTypeBot":
            return True
        return False

    def is_online(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if (
            user
            and user["type"]["@type"] != "userTypeBot"
            and user["status"]["@type"] == "userStatusOnline"
            and user["status"]["expires"] > time.time()
        ):
            return True
        return False

    def get_user_full_info(self, user_id: int) -> Dict[str, Any]:
        user = self.get_user(user_id)
        if not user:
            return user
        if user.get("full_info"):
            return user["full_info"]

        result = self.tg.get_user_full_info(user_id)
        result.wait()
        if result.error:
            log.warning(f"get user full info error: {result.error_info}")
            return {}
        user["full_info"] = result.update
        return result.update

    def get_user(self, user_id: int) -> Dict[str, Any]:
        if user_id in self.not_found:
            return {}
        if user_id in self.users:
            return self.users[user_id]
        result = self.tg.get_user(user_id)
        result.wait()
        if result.error:
            log.warning(f"get user error: {result.error_info}")
            self.not_found.add(user_id)
            return {}
        self.users[user_id] = result.update
        return result.update

    def get_group_info(self, group_id: int) -> Optional[Dict[str, Any]]:
        if group_id in self.groups:
            return self.groups[group_id]
        result = self.tg.get_basic_group(group_id)
        result.wait()
        if result.error or not result.update:
            return None
        self.groups[group_id] = result.update
        return result.update

    def get_supergroup_info(
        self, supergroup_id: int
    ) -> Optional[Dict[str, Any]]:
        if supergroup_id in self.supergroups:
            return self.supergroups[supergroup_id]
        result = self.tg.get_supergroup(supergroup_id)
        result.wait()
        if result.error or not result.update:
            return None
        self.supergroups[supergroup_id] = result.update
        return result.update

    def get_contacts(self) -> Optional[Dict[str, Any]]:
        if self.contacts:
            return self.contacts

        result = self.tg.get_contacts()
        result.wait()

        if result.error:
            log.error("get contacts error: %s", result.error_info)
            return None
        self.contacts = result.update
        return self.contacts

    def get_user_label(self, user_id: int) -> str:
        if user_id == 0:
            return ""
        user = self.get_user(user_id)
        if user.get("first_name") and user.get("last_name"):
            return f'{user["first_name"]} {user["last_name"]}'[:20]

        if user.get("first_name"):
            return f'{user["first_name"]}'[:20]

        if user.get("username"):
            return "@" + user["username"]
        return "<Unknown>"

    def get_users(self) -> List[User]:
        contacts = self.get_contacts()
        if contacts is None:
            return []
        users = []
        for user_id in contacts["user_ids"]:
            user_name = self.get_user_label(user_id)
            status = self.get_status(user_id)
            order = self.get_user_status_order(user_id)
            users.append(User(user_id, user_name, status, order))
        return users
