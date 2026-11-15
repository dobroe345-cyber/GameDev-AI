"""Минимальная замена FastAPI ТОЛЬКО ДЛЯ ТЕСТОВ в окружении, где пакет fastapi не установлен.

Построена на Starlette: роутеры, зависимости, привязка параметров (путь, query, тело pydantic),
JSON-ответы и формат ошибок {"detail": ...}. Если настоящий fastapi установлен, этот пакет не используется.
"""
import inspect
import typing

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

__all__ = ["FastAPI", "APIRouter", "Depends", "HTTPException"]


class Depends:
    def __init__(self, dependency):
        self.dependency = dependency


class APIRouter:
    def __init__(self, prefix="", dependencies=None):
        self.prefix, self.dependencies, self.routes = prefix, list(dependencies or []), []

    def _add(self, method, path):
        def deco(fn):
            self.routes.append((method, self.prefix + path, fn))
            return fn
        return deco

    def get(self, path): return self._add("GET", path)
    def post(self, path): return self._add("POST", path)
    def put(self, path): return self._add("PUT", path)
    def patch(self, path): return self._add("PATCH", path)
    def delete(self, path): return self._add("DELETE", path)


def _coerce(raw, ann):
    origin = typing.get_origin(ann)
    if origin is typing.Union:
        ann = next(a for a in typing.get_args(ann) if a is not type(None))
    if ann is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    if ann in (int, float):
        return ann(raw)
    return raw


def _endpoint(fn, deps):
    sig, hints = inspect.signature(fn), typing.get_type_hints(fn)

    async def endpoint(request: Request):
        for d in deps:
            await run_in_threadpool(d.dependency, request)
        kwargs = {}
        for name, p in sig.parameters.items():
            ann = hints.get(name, str)
            if ann is Request:
                kwargs[name] = request
            elif name in request.path_params:
                kwargs[name] = request.path_params[name]
            elif isinstance(ann, type) and issubclass(ann, BaseModel):
                try:
                    kwargs[name] = ann.model_validate(await request.json())
                except (ValidationError, ValueError) as e:
                    return JSONResponse({"detail": str(e)}, status_code=422)
            elif ann is dict:
                kwargs[name] = await request.json()
            else:
                raw = request.query_params.get(name)
                if raw is None:
                    if p.default is inspect.Parameter.empty:
                        return JSONResponse({"detail": f"нужен параметр {name}"}, status_code=422)
                    kwargs[name] = p.default
                else:
                    try:
                        kwargs[name] = _coerce(raw, ann)
                    except ValueError:
                        return JSONResponse({"detail": f"неверный параметр {name}"}, status_code=422)
        if inspect.iscoroutinefunction(fn):
            result = await fn(**kwargs)
        else:
            result = await run_in_threadpool(fn, **kwargs)
        return result if isinstance(result, Response) else JSONResponse(result)

    return endpoint


class FastAPI(Starlette):
    def __init__(self, title="", version="", lifespan=None):
        super().__init__(lifespan=lifespan)
        self.add_exception_handler(
            HTTPException, lambda request, exc: JSONResponse({"detail": exc.detail}, status_code=exc.status_code))

    def include_router(self, router):
        for method, path, fn in router.routes:
            self.router.routes.append(Route(path, _endpoint(fn, router.dependencies), methods=[method]))

    def exception_handler(self, exc_class):
        def deco(fn):
            self.add_exception_handler(exc_class, fn)
            return fn
        return deco
