import importlib.util
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mrs", ROOT / "scripts" / "make_readme_screens.py")
mrs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mrs)


def test_screens_are_valid_svg_built_from_the_sample_household(tmp_path):
    written = mrs.build(tmp_path)
    assert sorted(p.stem for p in written) == ["bills", "checks", "home", "quick-add"]
    for path in written:
        root = ElementTree.parse(path).getroot()  # noqa: S314 - our own generated file
        assert root.tag.endswith("svg")
    assert "Alex check" in (tmp_path / "bills.svg").read_text(encoding="utf-8")
