import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from common import ADMIN_GROUP, _owner_sub, _primary_group, _response, _user_groups

ec2 = boto3.client("ec2")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])


def handler(event, context):
    owner = _owner_sub(event)
    if not owner:
        return _response(401, {"error": "Missing or invalid auth claims"})

    if ADMIN_GROUP not in _user_groups(event):
        return _response(403, {"error": f"Only members of '{ADMIN_GROUP}' may delete subnets"})

    path = event.get("pathParameters") or {}
    vpc_id = path.get("id")
    subnet_id = path.get("subnetId")

    if not vpc_id or not subnet_id:
        return _response(400, {"error": "Path parameters 'id' and 'subnetId' are required"})

    try:
        resp = TABLE.get_item(Key={"vpcId": vpc_id})
    except ClientError as e:
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    item = resp.get("Item")
    if not item:
        return _response(404, {"error": "VPC record not found"})

    user_team = _primary_group(event)
    can_access = item.get("ownerSub") == owner or (
        user_team and item.get("team") == user_team
    )
    if not can_access:
        return _response(404, {"error": "VPC record not found"})

    status = item.get("status")
    if status != "SUCCEEDED":
        return _response(409, {"error": f"Cannot modify a VPC with status '{status}'"})

    subnets = item.get("subnets") or []
    if not any(s.get("subnetId") == subnet_id for s in subnets):
        return _response(404, {"error": "Subnet not found in this VPC"})

    try:
        ec2.delete_subnet(SubnetId=subnet_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code != "InvalidSubnetID.NotFound":
            return _response(500, {"error": f"Failed to delete subnet {subnet_id}: {code}"})

    updated_subnets = [s for s in subnets if s.get("subnetId") != subnet_id]

    try:
        TABLE.update_item(
            Key={"vpcId": vpc_id},
            UpdateExpression="SET subnets = :sb, updatedAt = :u",
            ExpressionAttributeValues={
                ":sb": updated_subnets,
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    return _response(200, {"vpcId": vpc_id, "subnetId": subnet_id, "deleted": True})
