import logging.handlers
import signal
import threading
from curses import window, wrapper  # type: ignore
from functools import partial
from types import FrameType

from tg import config, update_handlers, utils
from tg.client import Client
from tg.controllers import Controller
from tg.models import Model
from tg.views import ChatView, MsgView, StatusView, View

log = logging.getLogger(__name__)


def run(tg: Client, stdscr: window) -> None:

    # handle ctrl+c, to avoid interrupting tg when subprocess is called
    def interrupt_signal_handler(sig: int, frame: FrameType) -> None:
        # TODO: draw on status pane: to quite press <q>
        log.info("Interrupt signal is handled and ignored on purpose.")

    signal.signal(signal.SIGINT, interrupt_signal_handler)

    model = Model(tg)
    status_view = StatusView(stdscr)
    msg_view = MsgView(stdscr, model)
    chat_view = ChatView(stdscr, model)
    view = View(stdscr, chat_view, msg_view, status_view)
    controller = Controller(model, view, tg)

    # hanlde resize of terminal correctly
    signal.signal(signal.SIGWINCH, controller.resize_handler)

    for msg_type, handler in update_handlers.handlers.items():
        tg.add_update_handler(msg_type, partial(handler, controller))

    thread = threading.Thread(target=controller.run)
    thread.daemon = True
    thread.start()

    controller.draw()


def parse_args() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("-v", "--version"):
        import tg

        print("Terminal Telegram client")
        print("Version:", tg.__version__)
        exit(0)


def main() -> None:
    parse_args()
    utils.cleanup_cache()
    utils.setup_log()
    tg = Client(
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        phone=config.PHONE,
        session_dir=config.FILES_DIR,
    )
    tg.login()

    utils.set_shorter_esc_delay()

    wrapper(partial(run, tg))


if __name__ == "__main__":
    main()
