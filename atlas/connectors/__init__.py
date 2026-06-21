"""Connector registry for research-atlas.

Every source connector subclasses :class:`atlas.connectors.base.Connector` and
registers here so the CLI / build scripts can discover it by a registry key.

Note: several connectors share a provenance ``source`` key on purpose so their
rows merge into the same node namespace (e.g. ``nsf.py`` is the small-sample
reference connector and ``nsf_bulk.py`` is the full-scale connector -- both emit
``source="nsf"``). The REGISTRY is therefore keyed by an explicit *registry name*
(which may differ from ``.source``) so both are discoverable.
"""

from atlas.connectors.base import Connector
from atlas.connectors.cordis import CordisConnector
from atlas.connectors.czi import CziConnector
from atlas.connectors.dfg import DfgConnector
from atlas.connectors.erc import ErcConnector
from atlas.connectors.gates import GatesConnector
from atlas.connectors.nih import NihConnector
from atlas.connectors.nsf import NsfConnector
from atlas.connectors.nsf_bulk import NsfBulkConnector
from atlas.connectors.sloan import SloanConnector
from atlas.connectors.ukri import UkriConnector
from atlas.connectors.wellcome import WellcomeConnector

# registry name -> connector class
REGISTRY: dict[str, type[Connector]] = {
    "nsf": NsfConnector,
    "nsf_bulk": NsfBulkConnector,
    "nih": NihConnector,
    "cordis": CordisConnector,
    "ukri": UkriConnector,
    "erc": ErcConnector,
    "dfg": DfgConnector,
    "gates": GatesConnector,
    "wellcome": WellcomeConnector,
    "sloan": SloanConnector,
    "czi": CziConnector,
}

__all__ = [
    "Connector",
    "NsfConnector",
    "NsfBulkConnector",
    "NihConnector",
    "CordisConnector",
    "UkriConnector",
    "ErcConnector",
    "DfgConnector",
    "GatesConnector",
    "WellcomeConnector",
    "SloanConnector",
    "CziConnector",
    "REGISTRY",
]
