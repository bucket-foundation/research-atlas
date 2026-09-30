from atlas.users.directories.generic import GenericAdapter
from atlas.users.directories.k12 import K12_REGISTRY, detect_k12
from atlas.users.directories.platforms import (
    CmsPeopleAdapter,
    PureAdapter,
    SymplecticAdapter,
    VivoAdapter,
    detect_platform,
    detect_site,
)

REGISTRY: dict[str, type[GenericAdapter]] = {
    cls.platform: cls for cls in (GenericAdapter, PureAdapter, VivoAdapter, SymplecticAdapter, CmsPeopleAdapter)
}
REGISTRY.update(K12_REGISTRY)

__all__ = ["K12_REGISTRY", "REGISTRY", "detect_k12", "detect_platform", "detect_site"]
