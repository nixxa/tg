import curses
import logging
from _curses import window
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from tg import config
from tg.colors import get_color, white
from tg.formatters import (
    ChatFormatter,
    FormattedLine,
    HeaderFormatter,
    MessagesHeaderFormatter,
    MsgFormatter,
    PrivateMsgFormatter,
    format_date_separator,
)
from tg.models import Model
from tg.msg import MsgProxy
from tg.utils import enumerate2, num, string_len_dwc

log = logging.getLogger(__name__)

MAX_KEYBINDING_LENGTH = 5
MULTICHAR_KEYBINDINGS = (
    "dd",
    "sd",
    "sp",
    "sa",
    "sv",
    "sn",
    "ns",
    "ng",
    "bp",
)
if config.LAYOUT_MAPPING:
    mapped_bindings = []
    for binding in MULTICHAR_KEYBINDINGS:
        mapped_binding = ""
        for char in binding:
            mapped_binding += config.LAYOUT_MAPPING[char]
        mapped_bindings.append(mapped_binding)
    MULTICHAR_KEYBINDINGS = (*MULTICHAR_KEYBINDINGS, *mapped_bindings)


class Win:
    """Proxy for win object to log error and continue working"""

    def __init__(self, win: window):
        self.win = win

    def addstr(self, y: int, x: int, _str: str, attr: int = 0) -> None:
        try:
            return self.win.addstr(y, x, _str, attr)
        except Exception:
            log.exception(f"Error drawing: {y=}, {x=}, {_str=}, {attr=}")

    def __getattribute__(self, name: str) -> Any:
        if name in ("win", "addstr"):
            return object.__getattribute__(self, name)
        return self.win.__getattribute__(name)


class View:
    def __init__(
        self,
        stdscr: window,
        chat_view: "ChatView",
        msg_view: "MsgView",
        status_view: "StatusView",
    ) -> None:
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        curses.curs_set(0)

        curses.start_color()
        curses.use_default_colors()
        # init white color first to initialize colors correctly
        get_color(white, -1)

        self.stdscr = stdscr
        self.chats = chat_view
        self.msgs = msg_view
        self.status = status_view
        self.max_read = 2048
        self.resize_handler = self.resize

    def resize_stub(self) -> None:
        pass

    def resize(self) -> None:
        curses.endwin()
        self.stdscr.refresh()

    def get_keys(self) -> Tuple[int, str]:
        keys = repeat_factor = ""

        for _ in range(MAX_KEYBINDING_LENGTH):
            ch = self.stdscr.getch()
            if ch == 208 or ch == 209:
                second_ch = self.stdscr.getch()
                key = (ch.to_bytes() + second_ch.to_bytes()).decode("utf-8")
            elif ch in [
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_PPAGE,
                curses.KEY_NPAGE,
            ]:
                key = chr(ch)
            else:
                log.info("raw ch without unctrl: %s", ch)
                try:
                    key = curses.unctrl(ch).decode()
                except Exception:
                    log.warning("cant uncrtl: %s", ch)
                    break
                if key.isdigit():
                    repeat_factor += key
                    continue
            keys += key
            # if match found or there are not any shortcut matches at all
            if all(
                p == keys or not p.startswith(keys)
                for p in MULTICHAR_KEYBINDINGS
            ):
                break

        return cast(int, num(repeat_factor, default=1)), keys or "UNKNOWN"


class AbstractView:
    def __init__(self, win: Win) -> None:
        self.win = win
        self.first_column = 0
        self.w = curses.COLS

    @property
    def width(self) -> int:
        return self.w - self.first_column

    def draw_lines(self, line_num, lines) -> int:
        """
        Draws multiple lines on the window starting from a given line number.
        Args:
            line_num (int): The starting line number where the lines will be drawn.
            lines (list): A list of line objects to be drawn. Each line object should have a 'parts' attribute,
                          which is a list of parts. Each part should have 'text' and 'attributes' attributes.
        Returns:
            int: The line number after the last drawn line.
        """
        for line in lines:
            column = self.first_column
            for part in line.parts:
                self.win.addstr(line_num, column, part.text, part.attributes)
                column += string_len_dwc(part.text)
            line_num += 1
        return line_num


class StatusView:
    def __init__(self, stdscr: window) -> None:
        self.h = 1
        self.w = curses.COLS
        self.y = curses.LINES - 1
        self.x = 0
        self.stdscr = stdscr
        self.win = Win(stdscr.subwin(self.h, self.w, self.y, self.x))
        self._refresh = self.win.refresh

    def resize(self, rows: int, cols: int) -> None:
        self.w = cols - 1
        self.y = rows - 1
        self.win.resize(self.h, self.w)
        self.win.mvwin(self.y, self.x)

    def draw(self, msg: str = "") -> None:
        self.win.clear()
        self.win.addstr(0, 0, msg.replace("\n", " ")[: self.w])
        self._refresh()

    def get_input(self, prefix: str = "") -> Optional[str]:
        curses.curs_set(1)
        buff = ""

        try:
            while True:
                self.win.erase()
                line = buff[-(self.w - 1) :]
                self.win.addstr(0, 0, f"{prefix}{line}")

                key = self.win.get_wch(
                    0, min(string_len_dwc(buff + prefix), self.w - 1)
                )
                key = ord(key)
                if key == 10:  # return
                    break
                elif key == 127 or key == 8:  # del
                    if buff:
                        buff = buff[:-1]
                elif key in (7, 27):  # (^G, <esc>) cancel
                    return None
                elif chr(key).isprintable():
                    buff += chr(key)
        finally:
            self.win.clear()
            curses.curs_set(0)
            curses.cbreak()
            curses.noecho()

        return buff


class ChatView(AbstractView):
    def __init__(
        self,
        stdscr: window,
        model: Model,
        header_fmt: Optional[HeaderFormatter] = HeaderFormatter,
        chat_fmt: Optional[ChatFormatter] = ChatFormatter,
    ) -> None:
        super().__init__(Win(stdscr.subwin(0, 0, 0, 0)))
        self.stdscr = stdscr
        self.h = 0
        self.w = 0
        self._refresh = self.win.refresh
        self.model = model
        self.draw_vline = True
        self.header_formatter = header_fmt
        self.chat_formatter = chat_fmt

    def resize(self, rows: int, cols: int, width: int) -> None:
        self.h = rows - 1
        self.w = width
        self.draw_vline = False if cols == width else True
        self.win.resize(self.h, self.w)

    def draw(
        self, current: int, chats: List[Dict[str, Any]], title: str = "Chats"
    ) -> None:
        self.win.erase()

        width = self.w
        if self.draw_vline:
            # Draw vertical line (sperator between chats and messages)
            width = self.w - 1
            self.win.vline(0, width, curses.ACS_VLINE, self.h)

        # Draw title
        header_fmt = self.header_formatter(title.center(width)[:width])
        header_lines = header_fmt.format(width)
        line_num = self.draw_lines(0, header_lines)

        header_height = len(header_lines)
        offset = 0
        limit = int((self.h - header_height) / 2)
        if current >= limit:
            offset = current - limit + 1

        try:
            for line_num, chat in enumerate2(
                chats[offset : limit + offset],
                header_height,
                self.chat_formatter.height,
            ):
                is_selected = (
                    line_num
                    == (current - offset) * self.chat_formatter.height
                    + header_height
                )
                formatter = self.chat_formatter(chat, self.model, is_selected)
                try:
                    lines = formatter.format(width)
                except Exception:
                    log.exception("Failed to format chat %s", chat.get("id"))
                    continue
                line_num = self.draw_lines(line_num, lines)
        finally:
            self._refresh()


class MsgView(AbstractView):
    def __init__(self, stdscr: window, model: Model) -> None:
        super().__init__(Win(stdscr.subwin(0, 0, 0, 0)))
        self.model = model
        self.stdscr = stdscr
        self.h = 0
        self.w = 0
        self.x = 0
        self._refresh = self.win.refresh
        self.header_formatter = MessagesHeaderFormatter

    def resize(self, rows: int, cols: int, width: int) -> None:
        self.h = rows - 1
        self.w = width
        self.x = cols - self.w
        self.win.resize(self.h, self.w)
        self.win.mvwin(0, self.x)

    def _create_msg_formatter(self, msg: Dict[str, Any], is_selected: bool):
        chat = self.model.chats.chat_by_index(self.model.current_chat)
        if chat["type"]["@type"] in ("chatTypePrivate", "chatTypeSecret"):
            return PrivateMsgFormatter(MsgProxy(msg), self.model, is_selected)
        return MsgFormatter(MsgProxy(msg), self.model, is_selected)

    def _collect_msgs_to_draw(
        self,
        current_msg_idx: int,
        msgs: List[Tuple[int, Dict[str, Any]]],
        _reread: bool = True,
    ) -> List[List[FormattedLine]]:
        collected_items: List[Tuple[Any, List[FormattedLine], int]] = []
        budget = self.h - self.header_formatter.height
        lines_available = budget
        for msg_idx, msg_item in msgs:
            formatter = self._create_msg_formatter(
                msg_item, current_msg_idx == msg_idx
            )
            msg_lines = formatter.format(width=self.width)
            day = datetime.fromtimestamp(int(msg_item.get("date") or 0)).date()
            needed_lines = len(msg_lines)
            lines_available -= needed_lines
            if lines_available < 0:
                if msg_idx == current_msg_idx:
                    selected_lines = msg_lines[: needed_lines + lines_available]
                    collected_items.append(
                        (day, selected_lines, msg_item["id"])
                    )
                    break
                # If currently selected message is not visible on the screen,
                # then remove first collected item and increase line_num
                if msg_idx < current_msg_idx:
                    if len(collected_items) > 0:
                        collected_items.pop(0)
                    lines_available += needed_lines
                else:
                    selected_lines: List[FormattedLine] = []
                    if config.LATEST_MSG_ON_TOP:
                        # Select only lines that fit and cut the tail
                        selected_lines = msg_lines[: abs(lines_available)]
                    else:
                        # Select only lines that fit and cut the head
                        selected_lines_count = needed_lines - abs(
                            lines_available
                        )
                        if selected_lines_count > 0:
                            selected_lines = msg_lines[-selected_lines_count:]
                    if len(selected_lines) > 0:
                        collected_items.append(
                            (day, selected_lines, msg_item["id"])
                        )
                    break
            else:
                collected_items.append((day, msg_lines, msg_item["id"]))
        shown_ids = [msg_id for _day, _lines, msg_id in collected_items]
        if _reread and self.model.note_rendered_msgs(shown_ids):
            return self._collect_msgs_to_draw(
                current_msg_idx, msgs, _reread=False
            )
        visible = [(day, lines) for day, lines, _msg_id in collected_items]
        return self._with_date_separators(visible, budget)

    def _with_date_separators(
        self,
        items: List[Tuple[Any, List[FormattedLine]]],
        budget: int,
    ) -> List[List[FormattedLine]]:
        """Slip a date banner between two visible messages from different days.

        A banner is shown only when a message from the earlier day and a
        message from the later day are both on screen. Room for that banner
        is taken from other messages, never by hiding either side.
        """
        if not config.LATEST_MSG_ON_TOP:
            items = list(reversed(items))
        blocks: List[Tuple[str, List[FormattedLine], Any]] = []
        previous = None
        for day, lines in items:
            if not lines:
                continue
            if previous is not None and day != previous:
                blocks.append(
                    ("date", format_date_separator(day, self.width), day)
                )
            blocks.append(("msg", lines, day))
            previous = day
        self._fit_message_blocks(
            blocks, budget, keep_tail=not config.LATEST_MSG_ON_TOP
        )
        return [lines for _kind, lines, _day in blocks if lines]

    def _fit_message_blocks(
        self,
        blocks: List[Tuple[str, List[FormattedLine], Any]],
        budget: int,
        keep_tail: bool,
    ) -> None:
        """Free rows for boundary banners without hiding either side."""
        budget = max(budget, 0)

        def total() -> int:
            return sum(len(lines) for _, lines, _day in blocks)

        def minimum(index: int) -> int:
            kind, lines, _day = blocks[index]
            if kind != "msg":
                return len(lines)
            keep = 0
            if index > 0 and blocks[index - 1][0] == "date":
                keep = 1
            if index + 1 < len(blocks) and blocks[index + 1][0] == "date":
                keep = 1
            msg_indexes = [
                i for i, block in enumerate(blocks) if block[0] == "msg"
            ]
            if msg_indexes:
                anchor = msg_indexes[-1] if keep_tail else msg_indexes[0]
                if index == anchor:
                    keep = max(keep, 1)
            return keep

        def sacrificable() -> Optional[int]:
            indexes = (
                range(len(blocks))
                if keep_tail
                else range(len(blocks) - 1, -1, -1)
            )
            for index in indexes:
                if blocks[index][0] != "msg":
                    continue
                if len(blocks[index][1]) > minimum(index):
                    return index
            return None

        while total() > budget and blocks:
            index = sacrificable()
            if index is None:
                dates = [
                    i for i, block in enumerate(blocks) if block[0] == "date"
                ]
                if not dates:
                    break
                blocks.pop(dates[0] if keep_tail else dates[-1])
                continue
            kind, lines, day = blocks[index]
            cut = min(len(lines) - minimum(index), total() - budget)
            if keep_tail:
                kept = lines[cut:]
            else:
                kept = lines[: len(lines) - cut]
            if kept:
                blocks[index] = (kind, kept, day)
            else:
                blocks.pop(index)
        blocks[:] = [
            block
            for index, block in enumerate(blocks)
            if block[0] != "date" or self._banner_has_both_sides(blocks, index)
        ]

    def _banner_has_both_sides(
        self,
        blocks: List[Tuple[str, List[FormattedLine], Any]],
        index: int,
    ) -> bool:
        previous = next(
            (block for block in reversed(blocks[:index]) if block[0] == "msg"),
            None,
        )
        following = next(
            (block for block in blocks[index + 1 :] if block[0] == "msg"),
            None,
        )
        if not previous or not following:
            return False
        return bool(
            previous[1] and following[1] and previous[2] != following[2]
        )

    def draw(
        self,
        msgs: List[Tuple[int, Dict[str, Any]]],
        chat: Dict[str, Any],
    ) -> None:
        self.win.erase()

        # Draw title
        header_formatter = self.header_formatter(chat, self.model)
        lines = header_formatter.format(self.w)
        line_num = self.draw_lines(0, lines)

        try:
            current_msg_idx = self.model.get_current_chat_msg_idx()
            msgs_to_draw = self._collect_msgs_to_draw(current_msg_idx, msgs)

            for lines in msgs_to_draw:
                line_num = self.draw_lines(line_num, lines)
        finally:
            self._refresh()
