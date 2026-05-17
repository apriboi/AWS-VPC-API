import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from common import ADMIN_GROUP, _owner_sub, _primary_group, _response, _user_groups

ec2 = boto3.client("ec2")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])

_IMMUTABLE_STATUSES = {"PENDING", "IN_PROGRESS", "UPDATING"}


def handler(event, context):
    owner = _owner_sub(event)
    if not owner:
        return _response(401, {"error": "Missing or invalid auth claims"})

    if ADMIN_GROUP not in _user_groups(event):
        return _response(403, {"error": f"Only members of '{ADMIN_GROUP}' may delete VPCs"})

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

    status = item.get("status")
    if status == "DELETED":
        return _response(409, {"error": "VPC is already deleted"})
    if status in _IMMUTABLE_STATUSES:
        return _response(409, {"error": f"Cannot delete a VPC with status '{status}'"})

    # Delete subnets before the VPC (EC2 requires this order).
    for subnet in item.get("subnets") or []:
        subnet_id = subnet.get("subnetId")
        if not subnet_id:
            continue
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "InvalidSubnetID.NotFound":
                return _response(500, {"error": f"Failed to delete subnet {subnet_id}: {code}"})

    aws_vpc_id = item.get("awsVpcId")
    if aws_vpc_id:
        try:
            ec2.delete_vpc(VpcId=aws_vpc_id)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "InvalidVpcID.NotFound":
                return _response(500, {"error": f"Failed to delete VPC {aws_vpc_id}: {code}"})

    try:
        TABLE.update_item(
            Key={"vpcId": vpc_id},
            UpdateExpression="SET #s = :s, updatedAt = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "DELETED",
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    return _response(200, {"vpcId": vpc_id, "status": "DELETED"})
