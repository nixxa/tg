from enum import Enum
from typing import Any, Dict, Optional, Union


class ChatAction(Enum):
    chatActionTyping = "typing"
    chatActionCancel = "cancel"
    chatActionRecordingVideo = "recording video"
    chatActionUploadingVideo = "uploading video"
    chatActionRecordingVoiceNote = "recording voice"
    chatActionUploadingVoiceNote = "uploading voice"
    chatActionUploadingPhoto = "uploading photo"
    chatActionUploadingDocument = "uploading document"
    chatActionChoosingLocation = "choosing location"
    chatActionChoosingContact = "choosing contact"
    chatActionStartPlayingGame = "start playing game"
    chatActionRecordingVideoNote = "recording video"
    chatActionUploadingVideoNote = "uploading video"


class ChatType(Enum):
    chatTypePrivate = "private"
    chatTypeBasicGroup = "group"
    chatTypeSupergroup = "supergroup"
    channel = "channel"
    chatTypeSecret = "secret"


class UserStatus(Enum):
    userStatusEmpty = ""
    userStatusOnline = "online"
    userStatusOffline = "offline"
    userStatusRecently = "recently"
    userStatusLastWeek = "last week"
    userStatusLastMonth = "last month"


class UserType(Enum):
    userTypeRegular = ""
    userTypeDeleted = "deleted"
    userTypeBot = "bot"
    userTypeUnknown = "unknownn"


class SecretChatState(Enum):
    secretChatStatePending = "pending"
    secretChatStateReady = "ready"
    secretChatStateClosed = "closed"


def get_chat_type(chat: Dict[str, Any]) -> Optional[ChatType]:
    try:
        chat_type = ChatType[chat["type"]["@type"]]
        if (
            chat_type == ChatType.chatTypeSupergroup
            and chat["type"]["is_channel"]
        ):
            chat_type = ChatType.channel
        return chat_type
    except KeyError:
        pass
    return None


def is_group(chat_type: Union[str, ChatType]) -> bool:
    return chat_type in (
        ChatType.chatTypeSupergroup,
        ChatType.chatTypeBasicGroup,
    )
