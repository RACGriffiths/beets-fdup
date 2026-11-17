"""Shared pytest fixtures."""

from pathlib import Path

import beets
import pytest
from beets.test import helper

import beetsplug

# beets' test helpers (written for beets' own test suite) point beetsplug.__path__ at beets' bundled plugins only.
# Add this project's plugin directory back, so `beetsplug.fdup` can still be imported.
_PLUGIN_DIR = str(Path(__file__).resolve().parents[1] / "beetsplug")
if _PLUGIN_DIR not in beetsplug.__path__:
    beetsplug.__path__.append(_PLUGIN_DIR)


@pytest.fixture(autouse=True)
def fresh_config():
    """The plugin's config lives in beets' global config: reset it so settings don't leak between tests."""
    beets.config.clear()
    beets.config.read(user=False, defaults=True)
    yield
    beets.config.clear()
    beets.config.read(user=False, defaults=True)


class BeetsEnv(helper.PluginMixin, helper.TestHelper):
    """beets' own test helpers: a temporary in-memory library and config, with the fdup plugin loaded.

    Useful methods: add_item(**fields), run_command(*args), config (beets.config), lib (the Library)."""

    plugin = "fdup"


@pytest.fixture
def beets_env():
    env = BeetsEnv()
    env.setup_beets()
    yield env
    env.teardown_beets()
