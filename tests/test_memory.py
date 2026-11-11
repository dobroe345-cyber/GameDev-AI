from memory import Memory


def test_add_and_get_history(tmp_path):
    m = Memory(tmp_path / "t.db")
    m.add_message("c1", "user", "привет")
    m.add_message("c1", "assistant", "здравствуй")
    h = m.get_history("c1")
    assert [x["role"] for x in h] == ["user", "assistant"]
    assert h[0]["content"] == "привет"


def test_chats_are_isolated_and_clearable(tmp_path):
    m = Memory(tmp_path / "t.db")
    m.add_message("a", "user", "1")
    m.add_message("b", "user", "2")
    assert {c["chat_id"] for c in m.list_chats()} == {"a", "b"}
    assert m.clear("a") == 1
    assert m.get_history("a") == []
    assert len(m.get_history("b")) == 1


def test_history_limit_keeps_latest(tmp_path):
    m = Memory(tmp_path / "t.db")
    for i in range(5):
        m.add_message("c", "user", str(i))
    assert [x["content"] for x in m.get_history("c", limit=2)] == ["3", "4"]
