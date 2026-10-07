"""The sidebar: every module, every page in it, and which one a path belongs to.

One definition feeds the rail, the top bar's title and description, and the tests, so a page
cannot be listed in one place and titled differently in another. The layout follows the CAN
utility's workbench: modules are groups, pages are items, and a module that is not built yet is
listed — dimmed, with a pill — so the shape of the utility is visible before it is finished.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    label: str
    href: str
    icon: str
    desc: str
    pill: str = ""                     # "Soon" dims the item; anything else is a plain tag
    # Further paths that belong to this page — POST targets that render it, mostly.
    also: tuple[str, ...] = ()
    # False for a page reached from the top bar rather than the rail — COTA's sign-in, opened
    # from its "Cloud: …" status chip. It still titles the page and claims it for the module.
    in_rail: bool = True
    # Path prefixes that also belong to this page — a job's own page under Jobs.
    prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    items: tuple[Item, ...] = field(default_factory=tuple)


MODULES: tuple[Module, ...] = (
    Module("fota", "Web FOTA", (
        Item("Overview", "/", "grid", "Fleet and rollout health at a glance"),
        Item("Changes", "/changes", "activity", "What moved between fetches"),
        Item("Devices", "/devices", "list", "Every device in the selected snapshot"),
        Item("Pending", "/pending", "clock",
             "Tasks still waiting, and which of those devices are reachable"),
        Item("Firmware", "/firmware", "layers", "Versions per model, and who runs what"),
        Item("Update data", "/update", "refresh",
             "Fetch from the platform, import a file, share history"),
    )),
    Module("web_cota", "Web COTA", (
        Item("Configuration", "/web-cota", "sliders",
             "Configuration over the air through the web platform", pill="Soon"),
    )),
    Module("intouch_cota", "Intouch COTA", (
        Item("Configure", "/cota/console", "message",
             "One device, one command at a time — send it and see what the device says"),
        Item("Jobs", "/cota", "send",
             "Many devices, a sequence of commands each — and where every one of them is",
             prefixes=("/cota/jobs",)),
        Item("Commands", "/cota/commands", "tag",
             "Your names for parameters and commands — every page and export reads them"),
        Item("Devices", "/cota/devices", "cpu", "IMEI to cloud device id — who can be reached"),
        Item("Sign in", "/cota/signin", "key", "Which cloud, and the credentials to reach it",
             also=("/cota/signout",), in_rail=False),
    )),
)

# Pages served but not in the rail still belong to a module, so the right chips show.
_MODULE_PREFIXES = (("/cota", "intouch_cota"), ("/web-cota", "web_cota"))


def _matches(item: Item, path: str) -> bool:
    if path == item.href or path in item.also:
        return True
    if any(path.startswith(p) for p in item.prefixes):
        return True
    # A module's landing page ("/", "/cota") would otherwise claim every page below it.
    if item.href in ("/", "/cota"):
        return False
    return path.startswith(item.href + "/")


@dataclass
class State:
    module: str
    active: Item | None

    @property
    def module_label(self) -> str:
        return next(m.label for m in MODULES if m.key == self.module)


def state(path: str) -> State:
    for module in MODULES:
        for item in module.items:
            if _matches(item, path):
                return State(module.key, item)
    for prefix, key in _MODULE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return State(key, None)
    return State("fota", None)


# Feather-style line icons (MIT), inner markup only — the <svg> wrapper is in the template so
# size and stroke stay in one place.
ICONS = {
    "grid": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>'
            '<rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>'
            '<line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/>'
            '<line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "layers": '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/>'
              '<polyline points="2 12 12 17 22 12"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>'
               '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "sliders": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>'
               '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>'
               '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>'
               '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>'
               '<line x1="17" y1="16" x2="23" y2="16"/>',
    "send": '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/>'
           '<line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/>'
           '<line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/>'
           '<line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/>'
           '<line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
    "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777z'
           'm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>'
                '<line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>'
                '<line x1="3" y1="10" x2="21" y2="10"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "code": '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    "panel": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="15" y1="3" x2="15" y2="21"/>',
    "chevrons-right": '<polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/>',
    "menu": '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/>'
            '<line x1="3" y1="18" x2="21" y2="18"/>',
    "tag": '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/>'
           '<line x1="7" y1="7" x2="7.01" y2="7"/>',
    "chevron-left": '<polyline points="15 18 9 12 15 6"/>',
    "chevron-right": '<polyline points="9 18 15 12 9 6"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "rotate-ccw": '<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
    "pause": '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
    "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "edit": '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
    "book": '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>'
            '<path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
    "mark": '<rect x="3" y="3" width="18" height="18" rx="5"/>'
            '<path d="M8 12a4 4 0 0 1 8 0"/><path d="M5.5 12a6.5 6.5 0 0 1 13 0"/>'
            '<circle cx="12" cy="15" r="1.5"/>',
}
