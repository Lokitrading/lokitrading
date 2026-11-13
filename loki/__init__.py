"""The Loki trading platform package.

``loki.api`` holds the web application (``loki.api.server`` builds the FastAPI app and
mounts every feature router). ``loki.version`` holds the release number. Importing this
package is cheap: the web application is only loaded when something asks for it.
"""
from __future__ import annotations

import logging

from loki.version import __version__, __version_info__

__all__ = ["__version__", "__version_info__"]

logging.getLogger(__name__).addHandler(logging.NullHandler())
