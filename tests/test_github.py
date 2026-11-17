import pytest

from fake_github import TOKEN, FakeGitHub
from github_client import GitHub, GitHubError, GitHubRejected, check_path, scan_secrets


@pytest.fixture
def world():
    fake = FakeGitHub(files={
        "README.md": "# Game\n",
        "src/player.js": "export const speed = 5;\nfunction jump() { return speed * 2; }\n",
        "src/enemy.js": "export const hp = 10;\n",
        "assets/logo.png": b"\x89PNG\x00\x00binary",
        "node_modules/x/index.js": "skip me",
    })
    gh = GitHub(TOKEN, ["me/game"], api_url=fake.url, timeout=5)
    yield gh, fake
    fake.stop()


def snap(gh, fake):
    branch, sha = gh.head("me/game")
    return gh.snapshot("me/game", sha), sha


def test_snapshot_find_and_grep(world):
    gh, fake = world
    s, sha = snap(gh, fake)
    assert sha == fake.refs["heads/main"]
    assert "src/player.js" in s.paths and "assets/logo.png" in s.paths
    assert not any("node_modules" in p for p in s.paths)              # служебные папки не попадают в поиск
    assert "assets/logo.png" not in s.text                              # бинарные файлы не читаются как текст
    assert gh.find(s, "player") == ["src/player.js"]
    assert gh.find(s, "*.js") == ["src/enemy.js", "src/player.js"]
    hits = gh.grep(s, "speed")
    assert [(h["path"], h["line"]) for h in hits] == [("src/player.js", 1), ("src/player.js", 2)]
    assert gh.grep(s, "speed", prefix="assets") == []
    assert gh.grep(s, r"hp\s*=", regex=True)[0]["path"] == "src/enemy.js"
    with pytest.raises(GitHubRejected):
        gh.grep(s, "(", regex=True)


def test_token_is_sent_to_api_but_never_to_download_host(world):
    gh, fake = world
    snap(gh, fake)
    api = [a for m, p, a in fake.requests if not p.startswith("/codeload")]
    dl = [a for m, p, a in fake.requests if p.startswith("/codeload")]
    assert api and all(a == f"Bearer {TOKEN}" for a in api)
    assert dl == [""]                                                   # на хост скачивания токен не уходит


def test_snapshot_is_cached_per_commit(world):
    gh, fake = world
    _, sha = snap(gh, fake)
    n = len(fake.requests)
    gh.snapshot("me/game", sha)
    assert len(fake.requests) == n


def test_full_change_flow_creates_branch_commit_and_pr_without_touching_main(world):
    gh, fake = world
    _, sha = snap(gh, fake)
    gh.ensure_branch("me/game", "gdai/fix-1", sha)
    commit = gh.commit_files("me/game", "gdai/fix-1", "Быстрее бег", {
        "src/player.js": "export const speed = 8;\n", "src/new.js": "// new\n", "src/enemy.js": None})
    pr = gh.open_pr("me/game", "gdai/fix-1", "main", "Быстрее бег", "описание")
    assert pr == {"number": 1, "url": "https://github.example/me/game/pull/1"}
    assert fake.refs["heads/gdai/fix-1"] == commit and fake.refs["heads/main"] == sha   # main не тронут
    branch_files = fake.head_files("gdai/fix-1")
    assert branch_files["src/player.js"] == "export const speed = 8;\n" and "src/enemy.js" not in branch_files
    assert branch_files["src/new.js"] == "// new\n" and branch_files["README.md"] == "# Game\n"
    assert fake.head_files("main")["src/player.js"].startswith("export const speed = 5")


def test_retry_after_crash_is_idempotent(world):
    gh, fake = world
    _, sha = snap(gh, fake)
    gh.ensure_branch("me/game", "gdai/x", sha)
    gh.ensure_branch("me/game", "gdai/x", sha)                          # ветка уже есть: не ошибка
    gh.commit_files("me/game", "gdai/x", "m", {"a.txt": "1"})
    a = gh.open_pr("me/game", "gdai/x", "main", "t", "b")
    b = gh.open_pr("me/game", "gdai/x", "main", "t", "b")               # PR уже открыт: возвращается тот же
    assert a == b and len(fake.pulls) == 1


def test_protection_rules(world):
    gh, fake = world
    _, sha = snap(gh, fake)
    with pytest.raises(GitHubRejected):
        gh.ensure_branch("me/game", "main", sha)                         # только gdai/*
    with pytest.raises(GitHubRejected):
        gh.commit_files("me/game", "main", "m", {"a.txt": "1"})
    gh.ensure_branch("me/game", "gdai/ok", sha)
    for bad in ({".github/workflows/x.yml": "on: push"}, {"../evil.py": "1"}, {"a/.git/config": "1"},
                {"k.py": "KEY = 'gsk_" + "A" * 30 + "'"}, {"d.py": "postgresql://user:pass@host/db"},
                {"big.txt": "x" * 200_001}, {f"f{i}.txt": "1" for i in range(31)}, {}):
        with pytest.raises(GitHubRejected):
            gh.commit_files("me/game", "gdai/ok", "m", bad)
    assert fake.head_files("gdai/ok") == fake.head_files("main")        # ничего не записалось
    with pytest.raises(GitHubRejected):
        gh.head("evil/other")                                            # чужой репозиторий
    with pytest.raises(GitHubRejected):
        gh.open_pr("me/game", "feature", "main", "t", "b")


def test_secret_and_path_helpers():
    assert scan_secrets("x = 'ghp_" + "b" * 36 + "'") == "токен GitHub"
    assert scan_secrets("-----BEGIN RSA PRIVATE KEY-----") == "приватный ключ"
    assert scan_secrets("обычный текст про токены и ключи") is None
    assert check_path("\\a\\b.txt") == "a/b.txt"


def test_errors_are_clear_and_never_leak_the_token(world):
    gh, fake = world
    bad = GitHub("wrong-token-123", ["me/game"], api_url=fake.url, timeout=5)
    with pytest.raises(GitHubError) as e:
        bad.default_branch("me/game")
    assert e.value.status == 401 and "GITHUB_TOKEN" in str(e.value) and "wrong-token-123" not in str(e.value)
    gh2 = GitHub(TOKEN, ["me/other"], api_url=fake.url, timeout=5)
    with pytest.raises(GitHubError) as e:
        gh2.default_branch("me/other")
    assert e.value.status == 404 and "доступ" in str(e.value)


def test_transient_503_on_reads_is_retried(world):
    gh, fake = world
    fake.fail_next = 2
    assert gh.default_branch("me/game") == "main"


def test_unreachable_github_gives_readable_error():
    gh = GitHub(TOKEN, ["me/game"], api_url="http://127.0.0.1:1", timeout=1)
    with pytest.raises(GitHubError) as e:
        gh.default_branch("me/game")
    assert "недоступен" in str(e.value) and TOKEN not in str(e.value)
