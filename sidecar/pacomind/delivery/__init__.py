"""PacoMind proactive delivery system.

Bridges the autonomy loop's initiative/insight generation with the gateway's
messaging adapters so PacoMind can proactively reach users when it has something
worth saying.

Components:
- ProactiveDeliveryBridge: Queues and manages pending deliveries
- RateLimiter: Per-person rate limiting (max 3/day, quiet hours, 2h cooldown)
"""

from pacomind.delivery.bridge import ProactiveDeliveryBridge
from pacomind.delivery.rate_limiter import DeliveryRateLimiter
from pacomind.delivery.channels import ChannelRegistry

__all__ = ["ProactiveDeliveryBridge", "DeliveryRateLimiter", "ChannelRegistry"]
