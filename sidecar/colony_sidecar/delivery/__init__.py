"""Colony proactive delivery system.

Bridges the autonomy loop's initiative/insight generation with the gateway's
messaging adapters so Colony can proactively reach users when it has something
worth saying.

Components:
- ProactiveDeliveryBridge: Queues and manages pending deliveries
- RateLimiter: Per-person rate limiting (max 3/day, quiet hours, 2h cooldown)
"""

from colony_sidecar.delivery.bridge import ProactiveDeliveryBridge
from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter
from colony_sidecar.delivery.channels import ChannelRegistry

__all__ = ["ProactiveDeliveryBridge", "DeliveryRateLimiter", "ChannelRegistry"]
