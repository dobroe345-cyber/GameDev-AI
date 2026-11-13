import io
import zipfile

import pytest

from db import Database
from file_manager import (
    FileNotFound, FilePathError, FileService, FileTooLarge, FileTypeError, normalize_path,
)
from storage import LocalStorage, R2Storage
from test_storage import FakeS3


def make(tmp_path, kind="local", max_bytes=1_000_000):
    db = Database(tmp_path / "f.db")
    db.migrate()
    st = LocalStorage(tmp_path / "st") if kind == "local" else R2Storage("https://x", "k", "s", "b", client=FakeS3())
    return FileService(db, st, max_bytes), db, st


@pytest.fixture(params=["local", "r2"])
def svc(request, tmp_path):
    return make(tmp_path, request.param)[0]


def test_write_read_list_delete(svc):
    meta = svc.write("src/player.py", "print('hi')")
    assert meta["size"] == 11 and len(meta["sha256"]) == 64
    assert svc.read_text("src/player.py") == "print('hi')"
    assert [f["path"] for f in svc.list()] == ["src/player.py"]
    assert [f["path"] for f in svc.list("src")] == ["src/player.py"]
    assert svc.list("sr") == []                        # префикс — это папка, а не подстрока
    svc.delete("src/player.py")
    assert svc.list() == []
    with pytest.raises(FileNotFound):
        svc.read("src/player.py")


def test_overwrite_updates_meta(svc):
    svc.write("a.md", "v1")
    svc.write("a.md", "version two")
    assert svc.meta("a.md")["size"] == 11
    assert len(svc.list()) == 1


@pytest.mark.parametrize("bad", ["../evil.py", "a/../../x.py", "a/../b.py", "", "   ", "/", "a/\x00b.py"])
def test_path_traversal_blocked(svc, bad):
    with pytest.raises(FilePathError):
        svc.write(bad, "x")


def test_absolute_path_becomes_relative():
    assert normalize_path("/etc/passwd.txt") == "etc/passwd.txt"
    assert normalize_path("a\\b\\c.py") == "a/b/c.py"
    assert normalize_path("./a//b.py") == "a/b.py"


def test_forbidden_extension_and_reserved_folder(svc):
    for bad in ("run.exe", "script.sh", "noext", ".bashrc"):
        with pytest.raises(FileTypeError):
            svc.write(bad, "x")
    with pytest.raises(FilePathError):
        svc.write("archives/x.zip", b"x")


def test_size_limit(tmp_path):
    svc, *_ = make(tmp_path, max_bytes=10)
    svc.write("ok.txt", "x" * 10)
    with pytest.raises(FileTooLarge):
        svc.write("big.txt", "x" * 11)


def test_binary_assets_roundtrip(svc):
    data = bytes(range(256))
    svc.write("assets/hero.png", data)
    assert svc.read("assets/hero.png")[0] == data
    with pytest.raises(FileTypeError):
        svc.read_text("assets/hero.png")


def test_archive_contains_project_and_is_stored(svc):
    svc.write("game/main.js", "1")
    svc.write("game/lvl/a.json", "{}")
    svc.write("other/x.md", "x")
    meta = svc.archive("game", "my game!")
    assert meta["path"].startswith("archives/my_game") and meta["kind"] == "archive"
    data, _ = svc.read_archive(meta["path"])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ["game/lvl/a.json", "game/main.js"]
    assert [a["path"] for a in svc.list_archives()] == [meta["path"]]
    assert all(not f["path"].startswith("archives") for f in svc.list())   # архивы не смешиваются с файлами


def test_archive_of_empty_prefix_is_error(svc):
    with pytest.raises(FileNotFound):
        svc.archive("пусто")


def test_files_survive_server_restart_and_db_loss(tmp_path):
    """Главный сценарий: Render перезапущен, а затем потеряна и сама БД. Файлы возвращаются из хранилища."""
    svc, db, st = make(tmp_path, "r2")
    svc.write("p/a.py", "print(1)")
    svc.write("p/img.png", b"\x89PNG")
    arch = svc.archive("p")

    # 1) перезапуск: новые объекты поверх тех же БД и хранилища
    svc2 = FileService(db, st)
    assert svc2.read_text("p/a.py") == "print(1)"

    # 2) потеряна БД: новая пустая база, то же хранилище
    db2 = Database(tmp_path / "new.db")
    db2.migrate()
    svc3 = FileService(db2, st)
    assert svc3.list() == []
    assert svc3.reindex() == {"added": 3, "removed": 0}
    assert sorted(f["path"] for f in svc3.list()) == ["p/a.py", "p/img.png"]
    assert svc3.read_text("p/a.py") == "print(1)"
    assert [a["path"] for a in svc3.list_archives()] == [arch["path"]]


def test_reindex_drops_entries_whose_objects_vanished(tmp_path):
    svc, db, st = make(tmp_path)
    svc.write("a.txt", "1")
    st.delete("files/a.txt")                           # объект пропал мимо сервиса
    with pytest.raises(FileNotFound):
        svc.read("a.txt")
    assert svc.reindex() == {"added": 0, "removed": 1}
    assert svc.list() == []


def test_db_failure_after_upload_is_healed_by_reindex(tmp_path):
    svc, db, st = make(tmp_path)
    st.put("files/orphan.md", b"data")                 # объект записан, а запись в БД не удалась
    assert svc.list() == []
    svc.reindex()
    assert svc.read_text("orphan.md") == "data"
