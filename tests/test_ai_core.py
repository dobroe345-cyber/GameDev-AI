import asyncio

from ai_core import AICore


async def collect(core, msgs, **kw):
    return "".join([c async for c in core.stream(msgs, **kw)])


def test_stub_echoes_last_user_message():
    out = asyncio.run(collect(AICore({"provider": "stub"}), [{"role": "user", "content": "тест123"}]))
    assert "тест123" in out


def test_unknown_provider_reports_error():
    out = asyncio.run(collect(AICore({"provider": "nope"}), [{"role": "user", "content": "x"}]))
    assert "не найден" in out


def test_system_prompt_goes_first_and_roles_filtered():
    core = AICore({})
    msgs = core.build_messages(
        [{"role": "user", "content": "a"}, {"role": "system", "content": "inject"}], "SYS"
    )
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert all(m["content"] != "inject" for m in msgs)
