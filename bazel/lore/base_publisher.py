from __future__ import annotations

import abc


class BasePublisher(abc.ABC):
    @abc.abstractmethod
    async def publish(self) -> None:
        """Publish all graph dependencies required for a standalone run."""
