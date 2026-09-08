"""Ad-account isolation. Every tool resolves and validates its account here.

The resolved account is the *only* source of the ``X-AP-Context`` header the
client sends; tool arguments never reach the header directly.
"""
from __future__ import annotations

from apple_ads_mcp.config import Settings


class AccountNotAllowed(PermissionError):
    pass


def resolve_account(settings: Settings, account_id: str | int | None) -> str:
    """Resolve an explicit or default account and enforce the allowlist."""
    resolved = str(account_id if account_id is not None else settings.default_account_id or "").strip()
    if not resolved:
        raise AccountNotAllowed(
            "no account_id given and no DEFAULT_ACCOUNT_ID configured; "
            f"allowed accounts: {sorted(settings.allowed_account_ids)}"
        )
    if resolved not in settings.allowed_account_ids:
        raise AccountNotAllowed(
            f"account {resolved!r} is not in ALLOWED_ACCOUNT_IDS; this "
            "deployment is restricted to explicitly allowlisted accounts"
        )
    return resolved


def filter_allowed(settings: Settings, acls: list[dict]) -> list[dict]:
    """Filter a ``/v1/acls`` discovery response down to allowlisted accounts."""
    out = []
    for entry in acls:
        account = entry.get("adAccount") or {}
        if str(account.get("id")) in settings.allowed_account_ids:
            out.append(entry)
    return out


# Role names Apple shows in the Ads UI. ``roles`` in the ACL response is an
# open list of strings, so unknown names are treated as *potentially* write
# capable (warn) rather than trusted.
READ_ONLY_ROLES = frozenset(
    {
        "API Account Read Only",
        "Limited Access API Read Only",
        "API Read Only",
        "Read Only",
    }
)


def write_capable_roles(roles: list[str]) -> list[str]:
    """Return the roles that are not recognized read-only roles."""
    return [r for r in roles if r not in READ_ONLY_ROLES]
