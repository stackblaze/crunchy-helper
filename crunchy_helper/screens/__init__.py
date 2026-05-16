"""Textual screens for pg-db-manager.

Each module exports exactly one Screen subclass. Cross-screen navigation
goes through ``app.push_screen()`` / ``app.pop_screen()`` rather than
direct imports between screens, so the dependency graph stays a tree
rooted at MainMenuScreen.
"""

from .main_menu import MainMenuScreen
from .databases import DatabasesScreen
from .users import UsersScreen
from .primary import PrimaryScreen
from .restore import RestoreFlowScreen

__all__ = [
    "MainMenuScreen",
    "DatabasesScreen",
    "UsersScreen",
    "PrimaryScreen",
    "RestoreFlowScreen",
]
