"""Shared wording the server puts into pages: the revenue model's status and the
privacy and retention summaries on /transparency."""

from __future__ import annotations

REVENUE_STATUS = "proposed"
"""The 30% model is a proposal until the operator finalizes it legally.
Also mirrored as an env override so a deployment can promote it the day
the agreement is signed: LOKI_REVENUE_STATUS."""

PRIVACY_SUMMARY = (
    "Your email is handled by the account service. Journals and game "
    "progress stay in your browser until you explicitly save them to your "
    "cloud account; cloud copies are private to your account. Broker keys "
    "and cookies are excluded from cloud saves. Revenue books are private "
    "operator records."
)

RETENTION_SUMMARY = (
    "Browser records last until you clear them; cloud records last until "
    "you delete the account or ask for deletion in writing. Nothing is sold "
    "to anyone and nothing is posted publicly except the game leaderboard "
    "fields named in the terms."
)


def revenue_status() -> str:
    import os

    return (os.environ.get("LOKI_REVENUE_STATUS") or REVENUE_STATUS).strip().lower()
