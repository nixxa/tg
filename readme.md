# tg

Terminal Telegram client. The interface is curses. Telegram is reached through [Telethon](https://docs.telethon.dev/).

![tg screenshot](tg-screenshot.png)

## Features

- View photos, video, voice and video notes, documents, and stickers
- Send text, photos, documents, audio, video, and animations
- Reply, edit, forward, and delete messages
- Record and send voice messages
- Notifications
- Automatic download of small files
- Pin, mute, and mark chats read or unread
- Message history
- Contacts and user status
- Secret chats
- Contact search

Not implemented: full-text message search, and bot keyboards.

## Requirements

- Python 3.14 or newer
- [less](https://en.wikipedia.org/wiki/Less_(Unix)), used to show profiles, chat info, and help

Telethon and [telethon-secret-chat](https://pypi.org/project/telethon-secret-chat/) are installed with the package.

## Optional tools

- A font with emoji, such as [Noto](https://fonts.google.com/noto)
- [ffmpeg](https://ffmpeg.org/) to record voice messages
- [fzf](https://github.com/junegunn/fzf) to pick users when creating a group or a secret chat
- [ranger](https://github.com/ranger/ranger) or [nnn](https://github.com/jarun/nnn) as the file picker (`FILE_PICKER_CMD`)
- `urlview`, or another program set as `URL_VIEW`, when a message contains several links
- A notifier. The default command is `terminal-notifier` on macOS. On Linux, point `NOTIFY_CMD` at [dunst](https://github.com/dunst-project/dunst) or another program
- `wl-copy` or `xclip` so yank can reach the clipboard. macOS uses `pbcopy`

## Install

```sh
git clone https://github.com/nixxa/tg.git
cd tg
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/tg
```

### Docker

Voice recording and desktop notifications do not work in the container. Mount the config and cache directories so the login survives the container.

```sh
docker build -t tg .
docker run -it --rm \
  -v "$HOME/.config/tg:/root/.config/tg" \
  -v "$HOME/.cache/tg:/root/.cache/tg" \
  tg
```

## First run

If `~/.config/tg/conf.py` does not exist, tg asks for a phone number and writes it there. Telethon then asks for the login code. The session is stored at `~/.cache/tg/telethon.session`.

This client does not read a TDLib database. An existing TDLib login cannot be reused.

Get `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org) if you do not want the built-in application credentials.

## Configuration

`~/.config/tg/conf.py` is a Python file. Names in uppercase replace the defaults in `tg/config.py`.

```python
import os

def get_pass(key):
    return os.popen("pass show {} | head -n 1".format(key)).read().strip()

PHONE = get_pass("telegram-phone")

# https://my.telegram.org
API_ID = "123456"
API_HASH = "api hash"

LOG_LEVEL = "INFO"
LOG_PATH = os.path.expanduser("~/.local/share/tg/")

NOTIFY_CMD = "dunstify {title} {msg}"

# Opus audio in an Ogg container, one channel.
VOICE_RECORD_CMD = "ffmpeg -f alsa -i hw:0 -c:a libopus -b:a 32k {file_path}"

CHAT_FLAGS = {
    "online": "●",
    "pinned": "P",
    "muted": "M",
    "unread": "U",
    "unseen": "✓",
    "secret": "🔒",
    "seen": "✓✓",
}
MSG_FLAGS = {
    "selected": "*",
    "forwarded": "F",
    "new": "N",
    "unseen": "U",
    "edited": "E",
    "pending": "...",
    "failed": "x",
    "seen": "✓✓",
}

URL_VIEW = "urlview"
USERS_COLORS = tuple(range(2, 16))
KEEP_MEDIA = 7  # days, or None to keep files
FILE_PICKER_CMD = "ranger --choosefile={file_path}"
DOWNLOAD_DIR = os.path.expanduser("~/Downloads/")
EDITOR = "vim"
VIEW_TEXT_CMD = "less"
LONG_MSG_CMD = "vim + -c 'startinsert' {file_path}"
```

Files and logs:

| Path | Contents |
| --- | --- |
| `~/.config/tg/conf.py` | Configuration |
| `~/.cache/tg/telethon.session` | Login session and secret-chat keys |
| `~/.cache/tg/secret_messages.json` | Decrypted secret-chat history |
| `~/.cache/tg/` | Downloaded media |
| `~/.local/share/tg/` | Logs |

`KEEP_MEDIA` is how many days downloaded files are kept. `MAX_DOWNLOAD_SIZE` (default `10MB`) is the limit for automatic downloads.

Opening a file uses `xdg-open` on Linux and `open` on macOS (`DEFAULT_OPEN`). Plain text is opened with `EDITOR`.

## Secret chats

`ns` asks for a contact and starts a secret chat. Incoming secret chats are accepted automatically. The chat is pending until the other side accepts it, and sending is blocked until then.

Text, photos, documents, audio, and video can be sent. Replies, local delete, and close (`dd` on the chat) work. Secret messages cannot be edited or forwarded.

Chat info (`c` in the message view) shows a SHA-256 digest of the shared key. The official Telegram app shows a different fingerprint, so the two displays will not match.

Keys live in the Telethon session. Decrypted messages are stored in `~/.cache/tg/secret_messages.json` on this machine.

## Keybindings

Movement also accepts the arrow keys. A count works: `4j` moves four lines.

`q` quits. `?` opens the binding list for the current pane.

### Chats

- `j`, `k`: move
- `J`, `K`: move ten chats
- `g`: first chat
- `l` or Enter: open the chat
- `m`: mute or unmute
- `p`: pin or unpin
- `u`: mark read or unread
- `r`: mark the chat read
- `c`: list contacts
- `/`: search contacts, then jump to the chat
- `n`, `N`: next and previous search hit
- `ng`: new group
- `ns`: new secret chat
- `dd`: leave a group or channel, or delete a private or secret chat

### Messages

- `j`, `k`: move
- `J`, `K`: move ten messages
- `G`: last message
- `]`, `[`: next and previous chat
- `i`, `a`: write a message
- `I`, `A`: write a long message in `LONG_MSG_CMD`
- `r`, `R`: reply, or reply with the long-message editor
- `e`: edit the current text message
- `dd`: delete the current message, or the selected ones
- Space: select and move down
- Ctrl-Space: select and move up
- `y`: copy selected messages for forwarding, and copy the current text or file path
- `p`: forward the yanked messages into this chat
- `D`: download the file
- `O`: copy the file into `DOWNLOAD_DIR`
- `l`: open the file, or the text in `VIEW_TEXT_CMD`
- `!`: open the file with a command that contains `%s`
- `o`: open a link. Several links are passed to `URL_VIEW`
- `S`: pick a file and send it
- `sp`, `sd`, `sa`, `sv`, `sn`: send a picture, document, audio, video, or animation
- `v`: record and send a voice message
- `u`: user info
- `c`: chat info, including the secret-chat key when the chat is secret
