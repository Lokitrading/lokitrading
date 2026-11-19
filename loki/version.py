"""Version information for Loki Trading Platform."""

import os

__version__ = "2.85.0"
__version_info__ = tuple(int(i) for i in __version__.split("."))

# The ?v= tag on every /static link: the release plus the deployed commit (on Vercel), so a cached
# script or stylesheet can never outlive the deploy it belongs to.
ASSET_TAG = __version__ + ("-" + os.environ["VERCEL_GIT_COMMIT_SHA"][:7] if os.environ.get("VERCEL_GIT_COMMIT_SHA") else "")
