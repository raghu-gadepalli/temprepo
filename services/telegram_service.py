#!/usr/bin/env python3

import json
import requests
from typing import Any, Dict

from schemas.telegram_templates import TEMPLATES


class TelegramService:
    """
    Onestop Telegram client + template formatter.
    Usage:
        tg = TelegramService()
        tg.send("signal.entry", {  })
    """

    #  your existing bot creds
    BOT_TOKEN = "7548477415:AAFJLYg3WK_yOmNTsG0FMRsoFekF9sz83fU"
    CHAT_ID   = "-4515076223"

    def send(self, key: str, context: Dict[str, Any]) -> None:
        """
        Render the template identified by `key` with `context`, then POST to Telegram.
        """
        tpl = TEMPLATES.get(key)
        if not tpl:
            raise ValueError(f"No Telegram template configured for key: {key}")

        # 1) render message
        message = tpl.template.format(**context)

        # 2) call Telegram API
        url = f"https://api.telegram.org/bot{self.BOT_TOKEN}/sendMessage"
        payload = {"chat_id": self.CHAT_ID, "text": message}
        headers = {"Content-Type": "application/json"}

        resp = requests.post(url, data=json.dumps(payload), headers=headers)
        # optional: check resp.status_code / resp.json() for errors
        return
