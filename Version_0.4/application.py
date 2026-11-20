"""Combined deployment. Each FastAPI service can also run on its own host."""

from contextlib import asynccontextmanager
from functools import lru_cache

from a2wsgi import ASGIMiddleware, WSGIMiddleware
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from main import app as browser_app
from system_service.api import app as system_api
from tester_service.api import app as tester_api


@asynccontextmanager
async def lifespan(_app):
    async with system_api.router.lifespan_context(system_api):
        async with tester_api.router.lifespan_context(tester_api):
            yield


browser = WSGIMiddleware(browser_app)
app = Starlette(lifespan=lifespan, routes=[
    # The browser tester has two existing URLs below the tester API prefix.
    Route("/tester/run", browser),
    Route("/tester/report/{run_id}", browser),
    Mount("/system", app=system_api),
    Mount("/tester", app=tester_api),
    Mount("/", app=browser),
])


@lru_cache(maxsize=1)
def _wsgi_adapter():
    return ASGIMiddleware(app)


def application(environ, start_response):
    """WSGI entry point for hosts that already load application:application."""
    return _wsgi_adapter()(environ, start_response)
