import json
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common import _owner_sub, _primary_group, _response

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])
OWNER_INDEX = os.environ.get("OWNER_INDEX_NAME", "byOwner")
TEAM_INDEX = os.environ.get("TEAM_INDEX_NAME", "byTeam")


def handler(event, context):
    owner = _owner_sub(event)
    if not owner:
        return _response(401, {"error": "Missing or invalid auth claims"})

    qs = event.get("queryStringParameters") or {}
    try:
        limit = max(1, min(int(qs.get("limit", 25)), 100))
    except ValueError:
        return _response(400, {"error": "'limit' must be an integer"})

    user_team = _primary_group(event)
    if user_team:
        # Team members see all VPCs belonging to their group.
        key_condition = Key("team").eq(user_team)
        index_name = TEAM_INDEX
    else:
        # No group — fall back to personal records only.
        key_condition = Key("ownerSub").eq(owner)
        index_name = OWNER_INDEX

    kwargs = {
        "IndexName": index_name,
        "KeyConditionExpression": key_condition,
        "ScanIndexForward": False,  # newest first (createdAt is the GSI sort key)
        "Limit": limit,
    }
    next_token = qs.get("nextToken")
    if next_token:
        try:
            kwargs["ExclusiveStartKey"] = json.loads(next_token)
        except (json.JSONDecodeError, TypeError):
            return _response(400, {"error": "Invalid nextToken"})

    try:
        resp = TABLE.query(**kwargs)
    except ClientError as e:
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    body = {"items": resp.get("Items", [])}
    if "LastEvaluatedKey" in resp:
        body["nextToken"] = json.dumps(resp["LastEvaluatedKey"], default=str)

    return _response(200, body)
