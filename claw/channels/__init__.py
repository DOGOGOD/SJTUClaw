"""Channel integrations for external messaging platforms (QQ, etc.).

QQ Bot setup (two-step)::

    1. QR scan to get credentials (one-time):
       python -m claw.channels.qq_onboard

    2. Add to .env and start gateway:
       QQ_ENABLED=true
       QQ_APP_ID=...
       QQ_CLIENT_SECRET=...
"""

from claw.channels.base import BaseChannel, OutboundMessage, MessageHandler

__all__ = ["BaseChannel", "OutboundMessage", "MessageHandler"]
