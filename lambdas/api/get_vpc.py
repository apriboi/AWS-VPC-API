import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from common import _owner_sub, _primary_group, _response

ec2 = boto3.client("ec2")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])

# Workflows are in-flight for these statuses — don't interfere with EC2 state
_SKIP_RECONCILE = {"PENDING", "IN_PROGRESS", "UPDATING", "DELETED"}


def _reconcile(item: dict) -> dict:
    """Describe actual EC2 state and update DynamoDB if drift is detected."""
    if item.get("status") in _SKIP_RECONCILE or not item.get("awsVpcId"):
        return item

    aws_vpc_id = item["awsVpcId"]
    now = datetime.now(timezone.utc).isoformat()

    try:
        vpcs = ec2.describe_vpcs(VpcIds=[aws_vpc_id]).get("Vpcs", [])
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidVpcID.NotFound":
            return item  # EC2 unavailable — return last-known DB state
        vpcs = []

    if not vpcs:
        TABLE.update_item(
            Key={"vpcId": item["vpcId"]},
            UpdateExpression="SET #s = :s, updatedAt = :u, reconciledAt = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "DELETED", ":u": now, ":r": now},
        )
        return {**item, "status": "DELETED", "updatedAt": now, "reconciledAt": now}

    # VPC exists — reconcile subnet list
    try:
        actual_raw = ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [aws_vpc_id]}]
        ).get("Subnets", [])
    except ClientError:
        return item

    actual_subnets = [
        {
            "subnetId": s["SubnetId"],
            "cidrBlock": s["CidrBlock"],
            "availabilityZone": s["AvailabilityZone"],
            "name": next(
                (t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), ""
            ),
        }
        for s in actual_raw
    ]

    db_subnet_ids = {
        s["subnetId"] for s in (item.get("subnets") or []) if s.get("subnetId")
    }
    actual_subnet_ids = {s["subnetId"] for s in actual_subnets}

    if db_subnet_ids != actual_subnet_ids:
        TABLE.update_item(
            Key={"vpcId": item["vpcId"]},
            UpdateExpression="SET subnets = :sb, updatedAt = :u, reconciledAt = :r",
            ExpressionAttributeValues={":sb": actual_subnets, ":u": now, ":r": now},
        )
        return {**item, "subnets": actual_subnets, "updatedAt": now, "reconciledAt": now}

    # No drift — stamp reconciledAt only
    TABLE.update_item(
        Key={"vpcId": item["vpcId"]},
        UpdateExpression="SET reconciledAt = :r",
        ExpressionAttributeValues={":r": now},
    )
    return {**item, "reconciledAt": now}


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

    user_team = _primary_group(event)
    can_access = item.get("ownerSub") == owner or (
        user_team and item.get("team") == user_team
    )
    if not can_access:
        return _response(404, {"error": "VPC record not found"})

    return _response(200, _reconcile(item))
