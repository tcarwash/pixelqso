"""Importable editor/cardcasting helpers must ship in wheel as well as source."""
from pathlib import Path
import tomllib


def test_ui_helpers_are_in_setuptools_py_modules():
    config = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    modules = set(config["tool"]["setuptools"]["py-modules"])
    assert {"app", "cardcast_queue", "reception_quality"} <= modules
