"""Temp filtering, extensions, traversal guards."""
from pathlib import Path

import pytest

from app.services.filters import (
    is_supported,
    is_temp_file,
    resolve_within,
    sanitize_filename,
)


@pytest.mark.parametrize("name", [
    "doc.tmp", "file.TMP", "a.crdownload", "b.part", "c.download",
    "d.opdownload", "~$report.docx", ".~lock", "notes~", "thumbs.db",
    "desktop.ini", ".DS_Store", "x.bak", "y.swp", "z.lock",
])
def test_temp_names_ignored(name):
    assert is_temp_file(name)


@pytest.mark.parametrize("name", ["scan.pdf", "photo.JPG", "sig.png", "doc.jpeg"])
def test_normal_names_not_temp(name):
    assert not is_temp_file(name)


def test_supported_extensions():
    permitted = {".pdf", ".jpg", ".jpeg", ".png"}
    assert is_supported("a.pdf", permitted)
    assert is_supported("B.JPG", permitted)
    assert not is_supported("evil.exe", permitted)
    assert not is_supported("noext", permitted)


def test_sanitize_blocks_traversal():
    assert sanitize_filename("../../evil.exe") == "evil.exe"
    assert "/" not in sanitize_filename("a/b\\c.pdf")
    assert sanitize_filename("   ") == "file"
    assert len(sanitize_filename("x" * 500 + ".pdf")) <= 120


def test_resolve_within(tmp_path):
    root = tmp_path / "Inbox"
    root.mkdir()
    inside = root / "a.pdf"
    inside.write_bytes(b"x")
    assert resolve_within(inside, root).name == "a.pdf"
    with pytest.raises(ValueError):
        resolve_within(tmp_path / "other.pdf", root)
    with pytest.raises(ValueError):
        resolve_within(root / ".." / "outside.pdf", root)
