"""Уведомления в Telegram. Без токена в окружении — тихо ничего не делает."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
                          timeout=15)
        if not r.ok:
            log.warning("Telegram ответил %s", r.status_code)   # текст ответа не логируем
        return r.ok
    except requests.RequestException as e:
        log.warning("Telegram недоступен: %s", type(e).__name__)
        return False
