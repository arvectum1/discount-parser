from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from src.shared.config import get_settings
from src.web.processes import process_manager
from src.web.setup import is_setup_complete


def _open_browser(url: str, *, delay: float = 1.0) -> None:
    if delay > 0:
        time.sleep(delay)
    webbrowser.open(url)


def _panel_is_running(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.35):
            return True
    except OSError:
        return False


def _autostart_packaged_services() -> None:
    if not getattr(sys, 'frozen', False) or not is_setup_complete():
        return
    for name in ('bot', 'scheduler'):
        try:
            process_manager.start(name)
        except Exception:
            # The dashboard still opens and lets the user retry manually.
            pass


def _uvicorn_logging_kwargs() -> dict[str, object]:
    """Return logging overrides safe for console-less frozen GUI builds.

    PyInstaller windowed applications on Windows can set ``sys.stdout`` and
    ``sys.stderr`` to ``None``. Uvicorn's default logging formatter probes
    ``sys.stdout.isatty()`` while Config is being constructed, so the web panel
    crashes before it can bind a port. In that environment we leave logging to
    the application and prevent Uvicorn from installing console handlers.

    Source/dev and console-backed frozen runs keep Uvicorn's normal logging.
    """
    if getattr(sys, 'frozen', False) and (sys.stdout is None or sys.stderr is None):
        return {'log_config': None}
    return {}


def run_web_panel() -> None:
    settings = get_settings()
    from src.shared.logging import configure_logging
    configure_logging(level=settings.log_level, log_format=settings.log_format, component="web", enable_file=True)
    url = f'http://127.0.0.1:{settings.web_port}'

    # A repeated click on the desktop shortcut should focus/open the existing
    # local panel instead of starting a second web server and service set.
    if _panel_is_running(settings.web_port):
        _open_browser(url, delay=0)
        return

    _autostart_packaged_services()
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    from src.web.application import app

    uvicorn.run(
        app,
        host='127.0.0.1',
        port=settings.web_port,
        reload=False,
        log_level=settings.log_level.lower(),
        **_uvicorn_logging_kwargs(),
    )
