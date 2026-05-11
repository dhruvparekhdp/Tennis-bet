import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, InvalidToken, TelegramError

from analysis.signal import Signal
from config.settings import settings
from notifications.formatter import format_signal

log = structlog.get_logger()


class TelegramNotifier:
    def __init__(self) -> None:
        token = settings.telegram_bot_token
        if token is None:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not set — copy .env.example to .env and fill it in"
            )
        self._bot = Bot(token=token.get_secret_value())

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
        except TelegramError as e:
            log.error("telegram_send_failed", signal_type=sig.signal_type,
                      error=str(e), error_type=type(e).__name__)
            return False

    async def send_text(self, text: str) -> bool:
        """Send a plain-text message (for health alerts, startup notices, etc.)."""
        try:
            await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
            )
            return True
        except InvalidToken as e:
            log.error("telegram_invalid_token",
                      hint="Bot token is wrong — regenerate via @BotFather",
                      error=str(e))
        except Forbidden as e:
            log.error("telegram_forbidden",
                      hint="Chat ID wrong, or you haven't sent /start to your bot yet",
                      chat_id=settings.telegram_chat_id,
                      error=str(e))
        except TelegramError as e:
            log.error("telegram_text_send_failed", error=str(e), error_type=type(e).__name__,
                      chat_id=settings.telegram_chat_id)
        return False

    async def verify(self) -> bool:
        """Called at startup to validate credentials before anything else runs."""
        try:
            me = await self._bot.get_me()
            log.info("telegram_bot_verified", bot_username=me.username, bot_id=me.id)
            return True
        except InvalidToken:
            log.error("telegram_invalid_token",
                      hint="TELEGRAM_BOT_TOKEN is wrong — go to @BotFather and get a fresh token")
            return False
        except TelegramError as e:
            log.error("telegram_verify_failed", error=str(e))
            return False
