"""Samuga AI modular startup only."""
import signal
import atexit
from config import log_startup, log
from cards import check_fonts
from db import init_database
from state import restore_state, save_state
from publishing import start_social_worker
from website_api import start_api
from commands import start_telegram_listener
from scheduler import start_scheduler


def graceful_shutdown(signum=None, frame=None):
    log.info("🛑 Shutdown signal received — saving state...")
    try:
        save_state()
        log.info("✅ State saved")
    except Exception as e:
        log.error(f"Shutdown save failed: {e}")


def main():
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    atexit.register(graceful_shutdown)

    log_startup()
    check_fonts()
    init_database()
    restore_state()
    start_social_worker()
    start_api()
    start_telegram_listener()
    start_scheduler()


if __name__ == "__main__":
    main()
