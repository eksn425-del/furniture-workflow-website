"""Runtime ASGI entry point.

Import this module only for a real server run. Tests import ``app.main`` and
inject settings directly so repository credentials are never loaded.
"""

from app.main import create_runtime_app

app = create_runtime_app()
