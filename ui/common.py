"""Pieces every page shares: look & feel, toast messages and the navigation bar."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

STATIC = Path(__file__).resolve().parent.parent / "static"
TEAL = "#0F7D86"

#: (label, url, icon) of the pages
PAGES = (
    ("PCS die file", "/", "description"),
    ("Tasks", "/tasks", "list_alt"),
    ("Workers", "/workers", "dns"),
)


def asset_version() -> int:
    """Changes whenever a file in static/ does, so browsers never keep an old script or style sheet."""
    return int(max(p.stat().st_mtime for p in STATIC.iterdir()))


def add_assets(*names: str) -> None:
    """Link static/ files (``.css`` / ``.js``) into the page's head."""
    version = asset_version()
    tags = []
    for name in names:
        url = f"/pcs/{name}?v={version}"
        tags.append(
            f'<script src="{url}"></script>'
            if name.endswith(".js")
            else f'<link rel="stylesheet" href="{url}">'
        )
    ui.add_head_html("".join(tags))


def notify(message: str, kind: str = "info") -> None:
    """Corner toast: quick ones fade fast, warnings / errors stay a little longer."""
    slow = kind in ("warning", "negative")
    ui.notify(
        message,
        type=kind,
        position="bottom-right",
        timeout=6000 if slow else 2500,
        multi_line=slow,
    )


def nav_bar(active: str) -> None:
    """Teal bar with the page links (PCS die file / Tasks / Workers) of the operations pages."""
    with ui.row().classes("ops-nav w-full items-center no-wrap"):
        ui.label("PCS 1-Stop").classes("ops-title")
        for label, url, icon in PAGES:
            with ui.link(target=url).classes(
                "ops-link" + (" active" if label == active else "")
            ):
                ui.icon(icon).classes("text-lg")
                ui.label(label)
