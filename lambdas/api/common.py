import json


ADMIN_GROUP = "vpc-admins"


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _owner_sub(event):
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return None


def _user_groups(event):
    """Return the caller's Cognito group memberships, robust to the formats
    API Gateway uses to serialize the `cognito:groups` claim."""
    try:
        raw = event["requestContext"]["authorizer"]["jwt"]["claims"].get(
            "cognito:groups"
        )
    except (KeyError, TypeError):
        return []

    if not raw:
        return []
    # API Gateway sometimes hands us the list as-is (REST API authorizer path).
    if isinstance(raw, list):
        return [str(g) for g in raw]

    raw = str(raw).strip()
    if not raw:
        return []

    # HTTP API serializes arrays as "[a b]" or "[a, b]" - brackets, no quotes.
    if raw.startswith("[") and raw.endswith("]"):
        # Try real JSON first in case it's the well-formed variant.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(g) for g in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        inner = raw[1:-1]
        return [g.strip() for g in inner.replace(",", " ").split() if g.strip()]

    # Plain comma-separated fallback.
    return [g.strip() for g in raw.split(",") if g.strip()]


def _primary_group(event):
    groups = _user_groups(event)
    return groups[0] if groups else None