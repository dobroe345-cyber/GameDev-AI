"""Поддельный почтовый API (Resend/Brevo) на localhost: запоминает «отправленные» письма."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMail:
    def __init__(self):
        self.sent: list[dict] = []        # {"to", "subject", "text", "headers", "path"}
        self.fail_status = 0              # если задан, все запросы получают этот код
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                if outer.fail_status:
                    out, code = json.dumps({"message": "denied"}).encode(), outer.fail_status
                else:
                    to = body["to"][0] if isinstance(body["to"][0], str) else body["to"][0]["email"]
                    outer.sent.append({"to": to, "subject": body.get("subject", ""), "path": self.path, "headers": headers,
                                       "text": body.get("text") or body.get("textContent", ""), "body": body})
                    out, code = json.dumps({"id": "x"}).encode(), 200
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def last_code(self, to=None) -> str:
        mails = [m for m in self.sent if to is None or m["to"] == to]
        return re.search(r"\b(\d{6})\b", mails[-1]["text"]).group(1)

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
