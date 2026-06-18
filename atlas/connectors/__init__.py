"""Connector registry for research-atlas.

Every source connector subclasses :class:`atlas.connectors.base.Connector` and
registers here so the CLI / build scripts can discover it by its ``source`` key.
"""

from atlas.connectors.base import Connector
from atlas.connectors.dfg import DfgConnector
from atlas.connectors.erc import ErcConnector
from atlas.connectors.nsf import NsfConnector
from atlas.connectors.ukri import UkriConnector

# source key -> connector class
REGISTRY: dict[str, type[Connector]] = {
    NsfConnector.source: NsfConnector,
    UkriConnector.source: UkriConnector,
    ErcConnector.source: ErcConnector,
    DfgConnector.source: DfgConnector,
}

__all__ = [
    "Connector",
    "NsfConnector",
    "UkriConnector",
    "ErcConnector",
    "DfgConnector",
    "REGISTRY",
]
