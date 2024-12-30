from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from tg import config
from tg.colors import get_color, cyan, yellow, white, magenta, bold, reverse, dim
from tg.models import Model, UserModel
from tg.msg import MsgProxy
from tg.tdlib import ChatType, get_chat_type, is_group
from tg.utils import flatten, get_color_by_str, split_string_dwc, string_len_dwc, truncate_to_len


class FormattedText:
    def __init__(self, text: str, attributes: int):
        self.text = text
        self.attributes = attributes

class FormattedLine:
    def __init__(self, parts: List[FormattedText] = []):
        self.parts = parts

    def add_part(self, text: str, attributes: int) -> None:
        self.parts.append(FormattedText(text, attributes))

class MsgFormatter:
    def __init__(self, msg: MsgProxy, model: Model, selected: bool) -> None:
        self.msg = msg
        self.model = model
        self.selected = selected
        self.chat = self.model.chats.chat_by_index(self.model.current_chat)
        self.states = {
            "messageSendingStateFailed": "failed",
            "messageSendingStatePending": "pending",
        }

    @property
    def time(self) -> str:
        return self.msg.date.strftime("%H:%M:%S")
    
    @property
    def sender(self) -> str:
        sender = "<Unknown>"
        if 'is_channel' in self.chat['type'] and self.chat['type']['is_channel']:
            sender = self.chat['title']
        else:
            sender = self.model.users.get_user_label(self.msg.sender_id)
        return sender
    
    @property
    def flags(self) -> str:
        flags = []

        if self.msg.msg_id in self.model.selected[self.chat["id"]]:
            flags.append("selected")

        if self.msg.forward is not None:
            flags.append("forwarded")

        if (
            not self.model.is_me(self.msg.sender_id)
            and self.msg.msg_id > self.chat["last_read_inbox_message_id"]
        ):
            flags.append("new")
        elif (
            self.model.is_me(self.msg.sender_id)
            and self.msg.msg_id > self.chat["last_read_outbox_message_id"]
        ):
            if not self.model.is_me(self.chat["id"]):
                flags.append("unseen")
        elif (
            self.model.is_me(self.msg.sender_id)
            and self.msg.msg_id <= self.chat["last_read_outbox_message_id"]
        ):
            flags.append("seen")
        if state := self.msg.msg.get("sending_state"):
            state_type = state["@type"]
            flags.append(self.states.get(state_type, state_type))
        if self.msg.msg["edit_date"]:
            flags.append("edited")

        return " ".join(config.MSG_FLAGS.get(flag, flag) for flag in flags) if flags else ""
    
    @property
    def time_color(self) -> int:
        return get_color(cyan, -1)
    
    @property
    def sender_color(self) -> int:
        return get_color(get_color_by_str(self.sender), -1)
    
    @property
    def flags_color(self) -> int:
        return get_color(yellow, -1)
    
    @property
    def text_color(self) -> int:
        return get_color(white, -1)
    
    def _format_attributes(self, attributes: int) -> int:
        return attributes | reverse if self.selected else attributes

    def format(self, width: int) -> str:
        msg = self._parse_msg()
        if caption := self.msg.caption:
            msg += "\n" + caption.replace("\n", " ")
        msg += self._format_url()
        if reply_to := self.msg.reply_msg_id:
            msg = self._format_reply_msg(
                self.msg.chat_id, msg, reply_to, width
            )
        if reply_markup := self._format_reply_markup():
            msg += reply_markup
        
        if links := self.msg.links_from_entities:
            msg += links

        msg_lines = flatten([split_string_dwc(msg_line, width) for msg_line in msg.split("\n")])

        result = []
        header_line = FormattedLine([])
        header_line.add_part(f" {self.time}", self._format_attributes(self.time_color))
        header_line.add_part(f" {self.sender}", self._format_attributes(self.sender_color))
        header_line.add_part(f" {self.flags}", self._format_attributes(self.flags_color))
        result.append(header_line)

        for line in msg_lines:
            formatted_line = FormattedLine([FormattedText(line, self.text_color)])
            result.append(formatted_line)

        return result
    
    def _format_url(self) -> str:
        msg_proxy = self.msg
        if not msg_proxy.is_text or "web_page" not in msg_proxy.msg["content"]:
            return ""
        web = msg_proxy.msg["content"]["web_page"]
        page_type = web["type"]
        if page_type == "photo":
            return f"\n | photo: {web['url']}"
        name = web["site_name"]
        title = web["title"]
        description = web["description"]["text"].replace("\n", "")
        url = f"\n | {name}: {title}"
        if description:
            url += f"\n | {description}"
        return url
    
    def _format_reply_markup(self) -> str:
        msg = ""
        msg_proxy = self.msg
        reply_markup = msg_proxy.reply_markup
        if not reply_markup:
            return msg
        for row in msg_proxy.reply_markup_rows:
            msg += "\n"
            for item in row:
                text = item.get("text")
                if not text:
                    continue
                _type = item.get("type", {})
                if _type.get("@type") == "inlineKeyboardButtonTypeUrl":
                    if url := _type.get("url"):
                        text = f"{text} ({url})"
                msg += f"| {text} "
            msg += "|"
        return msg
    
    def _format_reply_msg(
        self, chat_id: int, msg: str, reply_to: int, width_limit: int
    ) -> str:
        _msg = self.model.msgs.get_message(chat_id, reply_to)
        if not _msg:
            return msg
        reply_msg = MsgProxy(_msg)
        if reply_msg_content := self._parse_msg(reply_msg):
            reply_sender = self.model.users.get_user_label(reply_msg.sender_id)
            sender_name = f" {reply_sender}:" if reply_sender else ""
            reply_line = f">{sender_name} {reply_msg_content}"
            if len(reply_line) >= width_limit:
                reply_line = f"{reply_line[:width_limit - 4]}..."
            msg = f"{reply_line}\n{msg}"
        return msg
    
    def _parse_msg(self) -> str:
        if self.msg.is_message:
            return self._parse_content()
        return "unknown msg type: " + str(self.msg["content"])
    
    def _parse_content(self) -> str:
        msg = self.msg
        users = self.model.users

        if msg.is_text:
            return msg.text_content

        content = msg["content"]
        _type = content["@type"]

        if _type == "messageBasicGroupChatCreate":
            return f"[created the group \"{content['title']}\"]"
        if _type == "messageChatAddMembers":
            user_ids = content["member_user_ids"]
            if user_ids[0] == msg.sender_id:
                return "[joined the group]"
            users_name = ", ".join(
                users.get_user_label(user_id) for user_id in user_ids
            )
            return f"[added {users_name}]"
        if _type == "messageChatDeleteMember":
            user_id = content["user_id"]
            if user_id == msg.sender_id:
                return "[left the group]"
            user_name = users.get_user_label(user_id)
            return f"[removed {user_name}]"
        if _type == "messageChatChangeTitle":
            return f"[changed the group name to \"{content['title']}\"]"

        if not msg.content_type:
            # not implemented
            return f"[{_type}]"

        content_text = ""
        if msg.is_poll:
            content_text = f"\n {msg.poll_question}"
            for option in msg.poll_options:
                content_text += f"\n * {option['voter_count']} ({option['vote_percentage']}%) | {option['text']}"

        fields = dict(
            name=msg.file_name,
            download=self._get_download(msg.local, msg.size),
            size=msg.human_size,
            duration=msg.duration,
            listened=self._format_bool(msg.is_listened),
            viewed=self._format_bool(msg.is_viewed),
            animated=msg.is_animated,
            emoji=msg.sticker_emoji,
            closed=msg.is_closed_poll,
        )
        info = ", ".join(f"{k}={v}" for k, v in fields.items() if v is not None)

        return f"[{msg.content_type}: {info}]{content_text}"
    
    def _format_bool(self, value: Optional[bool]) -> str:
        if value is None:
            return None
        return "yes" if value else "no"
    
    def _get_download(self, local: Dict[str, Union[str, bool, int]], size: Optional[int]) -> Optional[str]:
        if not size:
            return None
        elif local["is_downloading_completed"]:
            return "yes"
        elif local["is_downloading_active"]:
            d = int(local["downloaded_size"])
            percent = int(d * 100 / size)
            return f"{percent}%"
        return "no"

class ChatFormatter:
    height = 2

    def __init__(self, chat: Dict[str, Any], model: Model, selected: bool) -> None:
        self.chat = chat
        self.model = model
        self.selected = selected
        self.title = self.chat["title"]

    @property
    def date(self) -> str:
        last_msg = self.chat.get("last_message")
        if not last_msg:
            return "<No date>"
        dt = datetime.fromtimestamp(last_msg["date"])
        date_fmt = "%d %b %y"
        if datetime.today().date() == dt.date():
            date_fmt = "%H:%M"
        elif datetime.today().year == dt.year:
            date_fmt = "%d %b"
        return dt.strftime(date_fmt)

    @property
    def date_color(self) -> int:
        color = get_color(cyan, -1) | dim
        return color | reverse if self.selected else color
    
    @property
    def flags_color(self) -> int:
        color = get_color(magenta, -1)
        return color | reverse if self.selected else color
    
    @property
    def title_color(self) -> int:
        color = get_color(cyan, -1) if not config.USE_CHAT_RANDOM_COLORS else get_color(get_color_by_str(self.title), -1)
        return color | reverse if self.selected else color

    @property
    def subtitle_color(self) -> int:
        color = get_color(white, -1)
        return color | dim
    
    def sender_color(self, sender: str) -> int:
        color = get_color(get_color_by_str(sender), -1)
        return color | dim

    def format(self, width: int) -> List[FormattedLine]:
        last_msg_sender, last_msg = self._get_last_msg_data()
        sender = f" {last_msg_sender} " if last_msg_sender else ""

        flags = self._get_flags()
        spacer_len = width - string_len_dwc(self.date) - string_len_dwc(self.title) - string_len_dwc(flags) - 3

        first_line = FormattedLine([])
        first_line.add_part(f" {self.date}", self.date_color)
        first_line.add_part(f" {self.title}", self.title_color)
        first_line.add_part(" " * spacer_len, self.title_color)
        first_line.add_part(f"{flags} ", self.flags_color)

        second_line = FormattedLine([])
        second_line.add_part(" " * 7, self.subtitle_color)
        second_line.add_part(sender, self.sender_color(sender))
        second_line.add_part(truncate_to_len(last_msg, width - string_len_dwc(sender) - 10), self.subtitle_color)

        return [first_line, second_line]
    
    def _get_flags(self) -> str:
        flags = []

        msg = self.chat.get("last_message")
        if (
            msg
            and self.model.is_me(msg["sender_id"].get("user_id"))
            and msg["id"] > self.chat["last_read_outbox_message_id"]
            and not self.model.is_me(self.chat["id"])
        ):
            # last msg haven't been seen by recipient
            flags.append("unseen")
        elif (
            msg
            and self.model.is_me(msg["sender_id"].get("user_id"))
            and msg["id"] <= self.chat["last_read_outbox_message_id"]
        ):
            flags.append("seen")

        if action_label := self._get_action_label():
            flags.append(action_label)

        if self.model.users.is_online(self.chat["id"]):
            flags.append("online")

        if "is_pinned" in self.chat and self.chat["is_pinned"]:
            flags.append("pinned")

        if self.chat["notification_settings"]["mute_for"]:
            flags.append("muted")

        if self.chat["is_marked_as_unread"]:
            flags.append("unread")
        elif self.chat["unread_count"]:
            unread_count = min(999, int(self.chat["unread_count"]))
            flags.append(f"{unread_count: >4}")
        else:
            flags.append("    ")

        if get_chat_type(self.chat) == ChatType.chatTypeSecret:
            flags.append("secret")

        label = " ".join(config.CHAT_FLAGS.get(flag, flag) for flag in flags)
        if label:
            return f" {label}"
        return label
        
    def _get_action_label(self) -> Optional[str]:
        actioner, action = self.model.users.get_user_action(self.chat["id"])
        if actioner and action:
            label = f"{action}..."
            chat_type = get_chat_type(self.chat)
            if chat_type and is_group(chat_type):
                user_label = self.model.users.get_user_label(actioner)
                label = f"{user_label} {label}"
            return label
        return None
    
    def _get_last_msg_data(self) -> Tuple[Optional[str], Optional[str]]:
        user, last_msg = self._get_last_msg()
        last_msg = last_msg.replace("\n", " ")
        if user:
            last_msg_sender = self.model.users.get_user_label(user)
            chat_type = get_chat_type(self.chat)
            if chat_type and is_group(chat_type):
                return last_msg_sender, last_msg

        return None, last_msg

    def _get_last_msg(self) -> Tuple[Optional[int], str]:
        last_msg = self.chat.get("last_message")
        if not last_msg:
            return None, "<No messages yet>"
        formatter = MsgFormatter(MsgProxy(last_msg), self.model, False)
        return (
            last_msg["sender_id"].get("user_id"),
            formatter._parse_content(),
        )

class HeaderFormatter:
    def __init__(self, title: str):
        self.text = title

    def format(self, width: int) -> List[FormattedLine]:
        lines = []
        lines.append(FormattedLine([FormattedText(self.text, get_color(cyan, -1) | bold)]))
        lines.append(FormattedLine([FormattedText("-" * width, get_color(cyan, -1))]))
        return lines

class MessagesHeaderFormatter(HeaderFormatter):
    height = 2
    
    def __init__(self, chat: Dict[str, Any], model: Model):
        super().__init__("Messages")
        self.chat = chat
        self.model = model
    
    def format(self, width: int) -> List[FormattedLine]:
        self.text = self._msg_title(width)
        return super().format(width)
    
    def _msg_title(self, width: int) -> str:
        chat_type = get_chat_type(self.chat)
        status = ""

        if action_label := self._get_action_label():
            status = action_label
        elif chat_type == ChatType.chatTypePrivate:
            status = self.model.users.get_status(self.chat["id"])
        elif chat_type == ChatType.chatTypeBasicGroup:
            if group := self.model.users.get_group_info(
                self.chat["type"]["basic_group_id"]
            ):
                status = f"{group['member_count']} members"
        elif chat_type == ChatType.chatTypeSupergroup:
            if supergroup := self.model.users.get_supergroup_info(
                self.chat["type"]["supergroup_id"]
            ):
                status = f"{supergroup['member_count']} members"
        elif chat_type == ChatType.channel:
            if supergroup := self.model.users.get_supergroup_info(
                self.chat["type"]["supergroup_id"]
            ):
                status = f"{supergroup['member_count']} subscribers"

        return f"{self.chat['title']}: {status}".center(width)[: width]
    
    def _get_action_label(self) -> Optional[str]:
        actioner, action = self.model.users.get_user_action(self.chat["id"])
        if actioner and action:
            label = f"{action}..."
            chat_type = get_chat_type(self.chat)
            if chat_type and is_group(chat_type):
                user_label = self.model.users.get_user_label(actioner)
                label = f"{user_label} {label}"
            return label
        return None