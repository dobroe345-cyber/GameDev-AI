"""Поддельный GitHub для тестов: настоящий HTTP-сервер на localhost с той частью API, которой пользуется агент."""
import hashlib
import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "ghp_" + "a" * 36


def _sha(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


class FakeGitHub:
    def __init__(self, repo="me/game", files=None, branch="main"):
        self.repo, self.branch = repo, branch
        self.trees: dict[str, dict] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.requests: list[tuple[str, str, str]] = []   # (метод, путь, Authorization)
        self.fail_next = 0                                 # сколько ближайших запросов вернут 503
        self.token = TOKEN
        c = self._make_commit(files or {"README.md": "# Game\n", "src/player.js": "export const speed = 5;\n"}, [], "init")
        self.refs[f"heads/{branch}"] = c
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, raw=None, headers=None):
                body = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                auth = self.headers.get("Authorization", "")
                outer.requests.append((method, self.path, auth))
                if outer.fail_next > 0:
                    outer.fail_next -= 1
                    return self._send(503, {"message": "unavailable"})
                if self.path.startswith("/codeload/"):                 # скачивание архива: токен здесь не нужен и не должен приходить
                    sha = self.path.split("/")[2].split(".")[0]
                    return self._send(200, raw=outer._zip(sha))
                if auth != f"Bearer {outer.token}":
                    return self._send(401, {"message": "Bad credentials"})
                code, obj, hdr = outer.route(method, self.path, body, f"http://127.0.0.1:{outer.port}")
                self._send(code, obj, headers=hdr)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    # ---------------------------------------------------------------- модель git
    def _make_commit(self, files, parents, message):
        tree = _sha("tree", json.dumps(files, sort_keys=True, default=repr))
        self.trees[tree] = dict(files)
        sha = _sha("commit", tree, parents, message, len(self.commits))
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def head_files(self, branch=None) -> dict:
        return dict(self.trees[self.commits[self.refs[f"heads/{branch or self.branch}"]]["tree"]])

    def _zip(self, sha) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            top = f"{self.repo.replace('/', '-')}-{sha[:7]}"
            files = self.trees[self.commits[sha]["tree"]]
            for p, c in files.items():
                z.writestr(f"{top}/{p}", c if isinstance(c, bytes) else c.encode())
        return buf.getvalue()

    def route(self, method, path, body, base):
        r = f"/repos/{self.repo}"
        if method == "GET" and path == r:
            return 200, {"default_branch": self.branch, "full_name": self.repo}, None
        m = re.fullmatch(rf"{r}/git/ref/(.+)", path)
        if method == "GET" and m:
            sha = self.refs.get(m.group(1))
            return (200, {"object": {"sha": sha}}, None) if sha else (404, {"message": "Not Found"}, None)
        m = re.fullmatch(rf"{r}/git/commits/(\w+)", path)
        if method == "GET" and m:
            c = self.commits.get(m.group(1))
            return (200, {"tree": {"sha": c["tree"]}}, None) if c else (404, {"message": "Not Found"}, None)
        if method == "POST" and path == f"{r}/git/trees":
            files = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                if "content" in e:
                    files[e["path"]] = e["content"]
                elif e.get("sha", 1) is None:
                    files.pop(e["path"], None)
            t = _sha("tree", json.dumps(files, sort_keys=True, default=repr))
            self.trees[t] = files
            return 201, {"sha": t}, None
        if method == "POST" and path == f"{r}/git/commits":
            sha = _sha("commit", body["tree"], body["parents"], body["message"], len(self.commits))
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return 201, {"sha": sha}, None
        if method == "POST" and path == f"{r}/git/refs":
            ref = body["ref"].removeprefix("refs/")
            if ref in self.refs:
                return 422, {"message": "Reference already exists"}, None
            self.refs[ref] = body["sha"]
            return 201, {"ref": body["ref"]}, None
        m = re.fullmatch(rf"{r}/git/refs/(.+)", path)
        if method == "PATCH" and m:
            if m.group(1) not in self.refs:
                return 404, {"message": "Not Found"}, None
            self.refs[m.group(1)] = body["sha"]
            return 200, {"ok": True}, None
        if method == "POST" and path == f"{r}/pulls":
            if any(p["head"] == body["head"] and p["state"] == "open" for p in self.pulls):
                return 422, {"message": "A pull request already exists"}, None
            pr = {"number": len(self.pulls) + 1, "state": "open", "html_url": f"https://github.example/{self.repo}/pull/{len(self.pulls) + 1}", **body}
            self.pulls.append(pr)
            return 201, pr, None
        if method == "GET" and path.startswith(f"{r}/pulls?"):
            head = re.search(r"head=([^&]+)", path).group(1).replace("%3A", ":")
            return 200, [p for p in self.pulls if f"{self.repo.split('/')[0]}:{p['head']}" == head and p["state"] == "open"], None
        m = re.fullmatch(rf"{r}/zipball/(\w+)", path)
        if method == "GET" and m:
            return 302, {}, {"Location": f"{base}/codeload/{m.group(1)}.zip"}
        return 404, {"message": "Not Found"}, None

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
