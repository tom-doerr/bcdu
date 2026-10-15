from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Select

from bcdu.app import BcduApp, PolicyEditor, Report
from bcdu.demo import ROOT


@pytest.mark.parametrize("terminal_size", [(150, 45), (80, 24)])
async def test_keyboard_navigation_and_filter(terminal_size):
    app = BcduApp(Path("/"), demo=True)
    async with app.run_test(size=terminal_size) as pilot:
        await pilot.pause()
        assert app.selected().path.name == "media"
        await pilot.press("down", "right")
        await pilot.pause()
        assert app.path == ROOT / "backups"
        await pilot.press("left")
        await pilot.pause()
        assert app.path == ROOT and app.selected().path.name == "backups"
        await pilot.press("/", "v", "i", "r", "enter")
        assert len(app.rows) == 1 and app.selected().path.name == "virtual-machines"
        await pilot.press("escape", "s")
        assert len(app.rows) == 7 and app.sort_key == "allocated"
        await pilot.press("f")
        assert isinstance(app.screen, Report)
        await pilot.press("escape")
        assert app.query_one("#files", DataTable).has_focus


async def test_policy_requires_preview_and_explicit_apply():
    app = BcduApp(Path("/"), demo=True)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        selected = app.selected().path
        await pilot.press("e")
        assert isinstance(app.screen, PolicyEditor)
        app.screen.query_one("#option", Select).value = "data_replicas"
        app.screen.query_one("#value", Input).value = "3"
        await pilot.pause()
        await pilot.click("#preview-button")
        await pilot.pause()
        assert app.backend.policy(selected).effective["data_replicas"] == "2"
        assert app.screen.edit.value == "3"
        await pilot.click("#apply")
        await pilot.pause()
        assert app.backend.policy(selected).effective["data_replicas"] == "3"
        assert isinstance(app.screen, Report)


async def test_cancel_and_read_only_do_not_apply():
    app = BcduApp(Path("/"), demo=True, read_only=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        assert not isinstance(app.screen, PolicyEditor)
        assert not app.backend.overrides
