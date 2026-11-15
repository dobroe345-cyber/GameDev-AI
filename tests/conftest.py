import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

try:  # на машине с настоящим FastAPI тесты идут через него
    import fastapi  # noqa: F401
except ImportError:  # в окружении без fastapi подключаем тестовую замену на Starlette
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_shim"))
