"""Templates and static files must survive `pip install .`.

They are data, not modules, so setuptools does not collect them by default.
The failure mode is invisible in development — the source tree is on the path
either way — and only appears when the container dies on boot, because
StaticFiles raises on the missing directory.
"""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
PACKAGE = Path(__file__).resolve().parents[1] / "clipd"


def test_templates_and_static_are_declared_as_package_data():
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    package_data = raw["tool"]["setuptools"]["package-data"]["clipd"]

    assert "templates/*.html" in package_data
    assert "static/*" in package_data


def test_every_template_on_disk_is_covered_by_the_declaration():
    """A .html added to a subdirectory would not match templates/*.html."""
    stray = [p for p in (PACKAGE / "templates").rglob("*.html")
             if p.parent != PACKAGE / "templates"]
    assert stray == [], f"nested templates are not packaged: {stray}"


def test_every_static_file_on_disk_is_covered_by_the_declaration():
    """`static/*` does not recurse. A font or icon dropped in a subdirectory
    would vanish from the wheel silently, which is this file's whole subject."""
    stray = [p for p in (PACKAGE / "static").rglob("*")
             if p.is_file() and p.parent != PACKAGE / "static"]
    assert stray == [], f"nested static files are not packaged: {stray}"


def test_jinja2_is_a_runtime_dependency():
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert any(d.startswith("jinja2") for d in raw["project"]["dependencies"])
