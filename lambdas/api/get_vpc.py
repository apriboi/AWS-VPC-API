import os

import boto3
from botocore.exceptions import ClientError

from common import _owner_sub, _primary_group, _response

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])


def handler(event, context):
    owner = _owner_sub(event)
    if not owner:
        return _response(401, {"error": "Missing or invalid auth claims"})

    vpc_id = (event.get("pathParameters") or {}).get("id")
    if not vpc_id:
        return _response(400, {"error": "Path parameter 'id' is required"})

    try:
        resp = TABLE.get_item(Key={"vpcId": vpc_id})
    except ClientError as e:
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    item = resp.get("Item")
    if not item:
        return _response(404, {"error": "VPC record not found"})

    # Allow access if the caller owns the record or belongs to the same team.
    user_team = _primary_group(event)
    can_access = item.get("ownerSub") == owner or (
        user_team and item.get("team") == user_team
    )
    if not can_access:
        # Don't leak existence — return 404, not 403.
        return _response(404, {"error": "VPC record not found"})

    return _response(200, item)
