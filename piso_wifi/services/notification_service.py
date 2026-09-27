"""Webhook notification service for Piso-WiFi owner alerts and reports."""

import json
import logging
import threading
from typing import Any, Dict, Optional
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


class NotificationService:
    """Dispatches webhook alerts and daily sales recaps to Telegram, Discord, or generic endpoints."""

    def __init__(self, default_webhook_url: Optional[str] = None):
        self.default_webhook_url = default_webhook_url

    def _send_http_post(self, url: str, payload: Dict[str, Any], timeout: int = 5) -> bool:
        """Send JSON payload via HTTP POST."""
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "PisoWiFi-Notifier/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status in (200, 201, 204)
        except Exception as e:
            logger.error(f"Failed sending webhook to {url}: {e}")
            return False

    def send_alert(
        self,
        title: str,
        message: str,
        webhook_url: Optional[str] = None,
        blocking: bool = False,
    ) -> bool:
        """Send an urgent alert to the operator (WAN drop, coin box full, tamper attempt)."""
        target_url = webhook_url or self.default_webhook_url
        if not target_url:
            logger.debug("No webhook URL configured; skipping alert dispatch")
            return False

        payload: Dict[str, Any]
        if "discord.com" in target_url:
            payload = {
                "content": f"⚠️ **[Piso-WiFi Alert]** {title}\n{message}",
                "embeds": [{
                    "title": title,
                    "description": message,
                    "color": 15158332, # Red
                }]
            }
        else:
            payload = {
                "event": "alert",
                "title": title,
                "message": message,
            }

        if blocking:
            return self._send_http_post(target_url, payload)

        thread = threading.Thread(
            target=self._send_http_post,
            args=(target_url, payload),
            daemon=True,
        )
        thread.start()
        return True

    def send_daily_report(
        self,
        summary: Dict[str, Any],
        webhook_url: Optional[str] = None,
        blocking: bool = False,
    ) -> bool:
        """Dispatch a daily sales summary to the operator's chat/webhook."""
        target_url = webhook_url or self.default_webhook_url
        if not target_url:
            logger.debug("No webhook URL configured; skipping report dispatch")
            return False

        rev = summary.get("total_revenue", 0.0)
        tx = summary.get("total_transactions", 0)
        users = summary.get("unique_users", 0)
        period = summary.get("period", "day")

        text = (
            f"📊 **Piso-WiFi Sales Report ({period.capitalize()})**\n"
            f"• Gross Revenue: ₱{rev:.2f}\n"
            f"• Total Transactions: {tx}\n"
            f"• Unique Devices: {users}\n"
            f"• Average Ticket: ₱{summary.get('average_ticket', 0.0):.2f}"
        )

        payload: Dict[str, Any]
        if "discord.com" in target_url:
            payload = {
                "content": text,
                "embeds": [{
                    "title": f"Sales Report - {period.capitalize()}",
                    "description": text,
                    "color": 3066993, # Green
                }]
            }
        else:
            payload = {
                "event": "sales_report",
                "summary": summary,
                "message": text,
            }

        if blocking:
            return self._send_http_post(target_url, payload)

        thread = threading.Thread(
            target=self._send_http_post,
            args=(target_url, payload),
            daemon=True,
        )
        thread.start()
        return True

    def test_webhook(self, webhook_url: str) -> bool:
        """Test sending a verification ping to the specified webhook."""
        return self.send_alert(
            title="Test Connection",
            message="This is a test notification from your Piso-WiFi appliance. Webhook connected successfully!",
            webhook_url=webhook_url,
            blocking=True,
        )
