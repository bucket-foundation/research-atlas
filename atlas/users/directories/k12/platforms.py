from __future__ import annotations

from atlas.users.directories.generic import Node, Person, mailto_emails
from atlas.users.directories.k12.base import K12Adapter, find, first_text


class FinalsiteAdapter(K12Adapter):
    platform = "k12_finalsite"
    signatures = ("finalsite", "fsconstituentitem", "fspagelayout", "resources.finalsite.net")
    card_classes = ("fsConstituentItem",)
    name_classes = ("fsFullName",)
    title_classes = ("fsTitles",)
    dept_classes = ("fsDepartments",)


class BlackboardAdapter(K12Adapter):
    platform = "k12_blackboard"
    signatures = ("schoolwires", "blackboard web community manager", "/cms/lib/", "sw-channel")
    card_classes = ("staff",)
    name_classes = ("staffname",)
    title_classes = ("staffjob",)
    dept_classes = ("staffdepartment",)

    def cards(self, root: Node) -> list[Node]:
        return [n for n in find(root, *self.card_classes) if find(n, *self.name_classes)]


class EdlioAdapter(K12Adapter):
    platform = "k12_edlio"
    signatures = ("edlio", "/apps/staff/", "edliocdn")
    card_classes = ("staff-card", "user-info")
    name_classes = ("user-name", "name")
    title_classes = ("user-position", "position")
    dept_classes = ("user-department", "department")


class ApptegyAdapter(K12Adapter):
    platform = "k12_apptegy"
    signatures = ("apptegy", "thrillshare", "cmsv2-assets")
    card_classes = ("staff-directory-card",)
    name_classes = ("staff-name",)
    title_classes = ("staff-title",)
    dept_classes = ("staff-department",)

    def card_person(self, card: Node) -> Person | None:
        p = super().card_person(card)
        if p is not None and not p.emails:
            p.emails = mailto_emails(card) or [a for a in [card.attrs.get("data-email", "")] if "@" in a]
        if p is not None and not p.school:
            p.school = first_text(card, "staff-building")
        return p


DETECT_ORDER: tuple[type[K12Adapter], ...] = (FinalsiteAdapter, BlackboardAdapter, EdlioAdapter, ApptegyAdapter)
K12_REGISTRY: dict[str, type[K12Adapter]] = {cls.platform: cls for cls in DETECT_ORDER}


def detect_k12(*bodies: str) -> type[K12Adapter] | None:
    for cls in DETECT_ORDER:
        if any(cls.matches(b) for b in bodies if b):
            return cls
    return None
