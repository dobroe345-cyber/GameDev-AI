"""Тесты хранилища объектов: локальная папка и R2 (через подставной S3-клиент)."""
import datetime

import pytest

from storage import LocalStorage, R2Storage, StorageError, StorageNotFound


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Минимальная имитация boto3-клиента S3 (в памяти)."""

    def __init__(self):
        self.objects = {}
        self.down = False
        self.calls = []

    def _check(self):
        if self.down:
            raise ConnectionError("нет сети")

    def put_object(self, Bucket, Key, Body, ContentType):
        self._check()
        self.calls.append(("put", Bucket, Key, ContentType))
        self.objects[Key] = bytes(Body)

    def get_object(self, Bucket, Key):
        self._check()
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        data = self.objects[Key]
        return {"Body": type("B", (), {"read": lambda s: data})()}

    def delete_object(self, Bucket, Key):
        self._check()
        self.objects.pop(Key, None)

    def head_bucket(self, Bucket):
        self._check()

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                outer._check()
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), 2):  # по 2 объекта на страницу
                    yield {"Contents": [
                        {"Key": k, "Size": len(outer.objects[k]), "LastModified": datetime.datetime.now()}
                        for k in keys[i:i + 2]
                    ]}
        return P()


@pytest.fixture(params=["local", "r2"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalStorage(tmp_path / "s")
    return R2Storage("https://x", "k", "s", "bucket", client=FakeS3())


def test_put_get_delete_list(store):
    store.put("files/a/b.txt", b"hello", "text/plain")
    store.put("files/c.bin", b"\x00\x01")
    store.put("other/x", b"1")
    assert store.get("files/a/b.txt") == b"hello"
    assert [o.key for o in store.list("files/")] == ["files/a/b.txt", "files/c.bin"]
    assert {o.size for o in store.list("files/")} == {5, 2}
    store.delete("files/a/b.txt")
    store.delete("files/a/b.txt")                      # повторное удаление не падает
    with pytest.raises(StorageNotFound):
        store.get("files/a/b.txt")
    assert store.exists("files/c.bin") and not store.exists("files/a/b.txt")
    assert store.ping()


def test_overwrite_replaces_content(store):
    store.put("k.txt", b"old")
    store.put("k.txt", b"new")
    assert store.get("k.txt") == b"new"


def test_local_blocks_key_traversal(tmp_path):
    s = LocalStorage(tmp_path / "root")
    with pytest.raises(StorageError):
        s.put("../evil", b"x")
    assert not (tmp_path / "evil").exists()


def test_local_write_is_atomic_no_temp_leftovers(tmp_path):
    s = LocalStorage(tmp_path / "r")
    s.put("a.txt", b"1")
    assert [p.name for p in (tmp_path / "r").iterdir()] == ["a.txt"]


def test_r2_pagination_and_content_type():
    c = FakeS3()
    s = R2Storage("https://x", "k", "s", "bucket", client=c)
    for i in range(5):
        s.put(f"files/{i}.txt", b"x", "text/plain")
    assert len(s.list("files/")) == 5                  # прочитаны все страницы
    assert c.calls[0] == ("put", "bucket", "files/0.txt", "text/plain")


def test_r2_outage_gives_clear_error_and_ping_false():
    c = FakeS3()
    s = R2Storage("https://x", "k", "s", "bucket", client=c)
    c.down = True
    with pytest.raises(StorageError) as e:
        s.put("a.txt", b"1")
    assert not isinstance(e.value, StorageNotFound)
    with pytest.raises(StorageError):
        s.list("")
    assert s.ping() is False
    c.down = False
    assert s.ping() is True
