"""Connector registry for research-atlas.

Every source connector subclasses :class:`atlas.connectors.base.Connector` and
registers here so the CLI / build scripts can discover it by its ``source`` key.
"""

from atlas.connectors.base import Connector
from atlas.connectors.nsf import NsfConnector

# source key -> connector class
REGISTRY: dict[str, type[Connector]] = {
    NsfConnector.source: NsfConnector,
}

__all__ = ["Connector", "NsfConnector", "REGISTRY"]
