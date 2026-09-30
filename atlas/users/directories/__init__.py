from atlas.users.directories.generic import GenericAdapter
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

__all__ = ["REGISTRY", "detect_platform", "detect_site"]
