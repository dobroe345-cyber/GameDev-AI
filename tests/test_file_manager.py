from pathlib import Path

import pytest

from file_manager import FileManager, FileManagerError


def test_write_read_list_delete(tmp_path):
    fm = FileManager(tmp_path)
    fm.write("src/player.py", "print('hi')")
    assert fm.read("src/player.py") == "print('hi')"
    assert fm.list() == [{"path": "src/player.py", "size": 11}]
    fm.delete("src/player.py")
    assert fm.list() == []


@pytest.mark.parametrize("bad", ["../evil.py", "a/../../x.py", ""])
def test_path_traversal_blocked(tmp_path, bad):
    fm = FileManager(tmp_path / "ws")
    with pytest.raises(FileManagerError):
        fm.write(bad, "x")
    assert not (tmp_path / "evil.py").exists()


def test_absolute_path_stays_inside_workspace(tmp_path):
    fm = FileManager(tmp_path / "ws")
    fm.write("/etc/passwd.txt", "x")
    assert (tmp_path / "ws" / "etc" / "passwd.txt").exists()
    assert not Path("/etc/passwd.txt").exists()


def test_forbidden_extension(tmp_path):
    fm = FileManager(tmp_path)
    with pytest.raises(FileManagerError):
        fm.write("run.exe", "x")


def test_size_limit(tmp_path):
    fm = FileManager(tmp_path)
    with pytest.raises(FileManagerError):
        fm.write("big.txt", "x" * 1_000_001)
