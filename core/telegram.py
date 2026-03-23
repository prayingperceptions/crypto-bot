import os
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

async def send_telegram_message(message: str) -> bool:
    """Send an async message via Telegram bot."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        logger.warning("Telegram credentials not found in env vars. Message not sent.")
        return False
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    return True
                else:
                    logger.error(f"Failed to send Telegram message. Status: {response.status}")
                    return False
    except Exception as e:
        logger.error(f"Error sending telegram message: {e}")
        return False
    return False

async def send_telegram_heartbeat():
    """Send a basic ping to prove the bot is alive."""
    await send_telegram_message("<b>🤖 Crypto-Bot Heartbeat</b>\nSystem is online and actively scanning L2 Books.")

async def send_telegram_pnl(active_exposure: float, daily_pnl: float):
    """Daily push notification for capital status."""
    msg = (f"<b>📊 Daily Bot Metrics</b>\n"
           f"💵 <b>Current Exposure:</b> ${active_exposure:.2f}\n"
           f"📈 <b>Est. Daily PnL:</b> ${daily_pnl:.2f}")
    await send_telegram_message(msg)
