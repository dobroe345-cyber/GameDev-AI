"""Проверка развёрнутого сервера: python scripts/smoke_test.py https://xxx.onrender.com ТОКЕН"""
import json
import sys
import urllib.request


def get(url, token=None, timeout=90):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    base, token = sys.argv[1].rstrip("/"), sys.argv[2]
    print("health:", get(base + "/api/health"))
    st = get(base + "/api/status?deep=1", token)
    print("база:", st["database"])
    print("файлы:", st["storage"])
    print("модели:", st.get("models_alive"))
    for w in st["warnings"]:
        print("ВНИМАНИЕ:", w)
    sys.exit(1 if st["warnings"] else 0)


main()
