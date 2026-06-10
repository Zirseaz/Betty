"""Telegram notifier for PolyAgent alerts, signal emissions, and fills."""

from __future__ import annotations

import logging
from telegram import Bot
from polyagent.config import Settings

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Delivers trading alerts, fills, and daily summaries to a Telegram channel."""

    def __init__(self, settings: Settings) -> None:
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.enabled = bool(self.bot_token and self.chat_id)
        
        self.bot: Bot | None = None
        if self.enabled:
            try:
                # Bot initialization is simple and doesn't make networks requests immediately
                self.bot = Bot(token=self.bot_token)  # type: ignore
                logger.info("Telegram notifier initialized and enabled.")
            except Exception as e:
                logger.error("Failed to initialize Telegram Bot: %s", e)
                self.enabled = False
        else:
            logger.info("Telegram notifier disabled (missing token or chat_id).")

    async def send_message(self, text: str) -> bool:
        """Send a generic text message with markdown formatting."""
        if not self.enabled or not self.bot or not self.chat_id:
            logger.debug("Telegram message suppressed: %s", text)
            return False
            
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return True
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e)
            return False

    async def send_signal(self, signal_type: str, question: str, edge: float, confidence: float, details: str) -> bool:
        """Send a message when a trading signal is generated."""
        emoji = "🚨"
        if "arb" in signal_type.lower():
            emoji = "⚡"
        elif "negative_risk" in signal_type.lower():
            emoji = "🛡️"
        elif "statistical" in signal_type.lower():
            emoji = "📊"

        message = (
            f"{emoji} *NEW SIGNAL DETECTED: {signal_type.upper()}*\n\n"
            f"*Market:* {question}\n"
            f"*Edge:* {edge * 100:.2f}%\n"
            f"*Confidence:* {confidence * 100:.1f}%\n"
            f"*Details:* {details}"
        )
        return await self.send_message(message)

    async def send_trade(self, side: str, size: float, price: float, question: str, strategy: str, order_id: str) -> bool:
        """Send a message when a trade order is executed/filled."""
        emoji = "🟢" if side.upper() == "BUY" else "🔴"
        total = size * price
        
        message = (
            f"{emoji} *TRADE FILLED ({strategy.upper()})*\n\n"
            f"*Action:* {side.upper()} {size:.2f} shares\n"
            f"*Price:* ${price:.4f}  |  *Total:* ${total:.2f}\n"
            f"*Market:* {question}\n"
            f"*Order ID:* `{order_id}`"
        )
        return await self.send_message(message)

    async def send_alert(self, message_text: str, level: str = "INFO") -> bool:
        """Send a system alert (e.g. Risk Manager pauses trading, database issues)."""
        emoji = "⚠️"
        if level.upper() == "CRITICAL":
            emoji = "❌"
        elif level.upper() == "WARNING":
            emoji = "🚨"
            
        message = f"{emoji} *SYSTEM ALERT ({level.upper()})*\n\n{message_text}"
        return await self.send_message(message)

    async def send_daily_report(self, metrics: dict[str, Any]) -> bool:
        """Sends a daily performance report."""
        pnl = metrics.get("total_pnl", 0.0)
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        
        message = (
            f"📅 *DAILY PERFORMANCE SUMMARY*\n\n"
            f"{pnl_emoji} *Net P&L:* ${pnl:.2f}\n"
            f"🏆 *Win Rate:* {metrics.get('win_rate', 0.0) * 100:.1f}%\n"
            f"📊 *Sharpe Ratio:* {metrics.get('sharpe_ratio', 0.0):.2f}\n"
            f"📉 *Max Drawdown:* {metrics.get('max_drawdown', 0.0) * 100:.1f}%\n"
            f"📂 *Open Positions:* {metrics.get('open_positions_count', 0)}\n"
            f"⚙️ *Daily Volume:* ${metrics.get('daily_volume', 0.0):.2f}"
        )
        return await self.send_message(message)
