import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from analysis.signal import Signal
from config.settings import settings
from notifications.formatter import format_signal

log = structlog.get_logger()


class TelegramNotifier:
    def __init__(self) -> None:
        self._bot = Bot(token=settings.telegram_bot_token.get_secret_value())

    async def send_signal(self, sig: Signal) -> bool:
        message = format_signal(sig)
        try:
            await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            log.info(
                "telegram_signal_sent",
                signal_type=sig.signal_type,
                player=sig.player_name,
                confidence=sig.confidence,
                odds=sig.current_odds,
            )
            return True
        except TelegramError:
            log.exception("telegram_send_failed", signal_type=sig.signal_type)
            return False

    async def send_text(self, text: str) -> bool:
        """Send a plain-text message (for health alerts, startup notices, etc.)."""
        try:
            await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
            )
            return True
        except TelegramError:
            log.exception("telegram_text_send_failed")
            return False
