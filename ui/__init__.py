"""NiceGUI pages: the recipe editor ("/"), the Tasks page ("/tasks") and the Workers page ("/workers")."""
from __future__ import annotations

from config import Settings
from services import Services

from . import recipe_page, tasks_page, workers_page


def register_pages(services: Services, settings: Settings) -> None:
    """Define the three pages (call once, before ``ui.run``)."""
    recipe_page.register(services, settings)
    tasks_page.register(services, settings)
    workers_page.register(services, settings)


__all__ = ["register_pages"]
