import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cpd", ROOT / "scripts" / "check_private_data.py")
cpd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpd)


def write_denylist(tmp_path: Path) -> Path:
    p = tmp_path / "denylist.txt"
    p.write_text("# comment\n\nZorblax\nCard Q Bank\n1,234.56\n", encoding="utf-8")
    return p


def test_blocks_denylisted_words_case_insensitive(tmp_path):
    deny = write_denylist(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("owner = 'zorblax'\n", encoding="utf-8")
    assert cpd.main([str(f), "--denylist", str(deny)]) == 1


def test_whole_words_only(tmp_path):
    deny = write_denylist(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("x = 'Zorblaxian'  # different word\n", encoding="utf-8")
    assert cpd.main([str(f), "--denylist", str(deny)]) == 0


def test_phrase_with_flexible_whitespace_and_amount(tmp_path):
    deny = write_denylist(tmp_path)
    f = tmp_path / "a.md"
    f.write_text("paid card  q\nbank\n", encoding="utf-8")
    assert cpd.main([str(f), "--denylist", str(deny)]) == 1
    g = tmp_path / "b.md"
    g.write_text("total $1,234.56\n", encoding="utf-8")
    assert cpd.main([str(g), "--denylist", str(deny)]) == 1


def test_blocks_spreadsheets_and_binaries(tmp_path):
    deny = write_denylist(tmp_path)
    xlsx = tmp_path / "x.xlsx"
    xlsx.write_text("not really", encoding="utf-8")
    binf = tmp_path / "blob.dat"
    binf.write_bytes(b"abc\x00def")
    assert cpd.main([str(xlsx), "--denylist", str(deny)]) == 1
    assert cpd.main([str(binf), "--denylist", str(deny)]) == 1


def test_commit_message_checked(tmp_path):
    deny = write_denylist(tmp_path)
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("Add thing for Zorblax\n# zorblax in a comment is ignored\n", encoding="utf-8")
    assert cpd.main(["--commit-msg", str(msg), "--denylist", str(deny)]) == 1
    msg.write_text("Add thing\n# zorblax in a comment is ignored\n", encoding="utf-8")
    assert cpd.main(["--commit-msg", str(msg), "--denylist", str(deny)]) == 0


def test_missing_denylist_fails_closed_outside_ci(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    f = tmp_path / "a.py"
    f.write_text("ok\n", encoding="utf-8")
    assert cpd.main([str(f), "--denylist", str(tmp_path / "missing.txt")]) == 1
    monkeypatch.setenv("CI", "true")
    assert cpd.main([str(f), "--denylist", str(tmp_path / "missing.txt")]) == 0
