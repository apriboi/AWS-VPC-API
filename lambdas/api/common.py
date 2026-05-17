# Helpers for API Lambda handlers.
import json

# Users in this Cognito group can create, modify, and delete VPCs
ADMIN_GROUP = "vpc-admins"


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _owner_sub(event: dict) -> str | None:
    #Extract Cognito 'sub' from the JWT claims injected by API Gateway
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return None


def _user_groups(event: dict) -> list[str]:
    #Return the caller's Cognito group memberships
    try:
        raw = event["requestContext"]["authorizer"]["jwt"]["claims"].get(
            "cognito:groups", ""
        )
        if not raw:
            return []
        raw = str(raw)
        if raw.startswith("["):
            return json.loads(raw)
        return [g.strip() for g in raw.split(",") if g.strip()]
    except (KeyError, TypeError, ValueError):
        return []


def _primary_group(event: dict) -> str | None:
    #Return the caller's first Cognito group, or None if they belong to none
    groups = _user_groups(event)
    return groups[0] if groups else None
