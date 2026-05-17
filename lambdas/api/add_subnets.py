#POST /vpcs/{id}/subnets — add new subnets to an existing VPC

import json
import os
import uuid
from datetime import datetime, timezone
from ipaddress import ip_network

import boto3
from botocore.exceptions import ClientError

from common import ADMIN_GROUP, _owner_sub, _primary_group, _response, _user_groups

ddb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")

TABLE = ddb.Table(os.environ["TABLE_NAME"])
ADD_SUBNETS_STATE_MACHINE_ARN = os.environ["ADD_SUBNETS_STATE_MACHINE_ARN"]
DEFAULT_AZ = os.environ.get("DEFAULT_AZ") or None


def _validate_subnets(new_subnets, vpc_cidr, existing_subnets):
    if not isinstance(new_subnets, list) or not new_subnets:
        return None, "Field 'subnets' must be a non-empty list"

    total = len(existing_subnets) + len(new_subnets)
    if total > 16:
        return None, (
            f"Adding {len(new_subnets)} subnet(s) would exceed the 16-subnet limit "
            f"(currently {len(existing_subnets)})"
        )

    vpc_net = ip_network(vpc_cidr, strict=False)
    # Checks for overlaps
    occupied = [ip_network(s["cidrBlock"], strict=False) for s in existing_subnets]

    normalized = []
    for i, s in enumerate(new_subnets):
        if not isinstance(s, dict):
            return None, f"subnets[{i}] must be an object"
        s_cidr = s.get("cidrBlock")
        if not s_cidr:
            return None, f"subnets[{i}].cidrBlock is required"
        try:
            sub_net = ip_network(s_cidr, strict=False)
        except ValueError:
            return None, f"subnets[{i}].cidrBlock invalid: {s_cidr}"
        if not sub_net.subnet_of(vpc_net):
            return None, f"subnets[{i}].cidrBlock {s_cidr} is not inside VPC CIDR {vpc_cidr}"
        for occupied_net in occupied:
            if sub_net.overlaps(occupied_net):
                return None, f"subnets[{i}].cidrBlock {s_cidr} overlaps with {occupied_net}"
        occupied.append(sub_net)
        normalized.append(
            {
                "cidrBlock": s_cidr,
                "availabilityZone": s.get("availabilityZone") or DEFAULT_AZ,
                "name": s.get("name") or f"subnet-{len(existing_subnets) + i}",
            }
        )

    return normalized, None


def handler(event, context):
    owner = _owner_sub(event)
    if not owner:
        return _response(401, {"error": "Missing or invalid auth claims"})

    if ADMIN_GROUP not in _user_groups(event):
        return _response(403, {"error": f"Only members of '{ADMIN_GROUP}' may modify VPCs"})

    vpc_id = (event.get("pathParameters") or {}).get("id")
    if not vpc_id:
        return _response(400, {"error": "Path parameter 'id' is required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Body is not valid JSON"})

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
        return _response(
            409,
            {"error": f"Can only add subnets to a SUCCEEDED VPC (current status: '{status}')"},
        )

    existing_subnets = item.get("subnets") or []
    new_subnets, err = _validate_subnets(
        body.get("subnets"), item["cidrBlock"], existing_subnets
    )
    if err:
        return _response(400, {"error": err})

    # Atomically transition SUCCEEDED → UPDATING; rejects concurrent requests that
    # already flipped the status, preventing double-execution of the workflow.
    try:
        TABLE.update_item(
            Key={"vpcId": vpc_id},
            UpdateExpression="SET #s = :updating, updatedAt = :u",
            ConditionExpression="#s = :succeeded",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":updating": "UPDATING",
                ":succeeded": "SUCCEEDED",
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(409, {"error": "VPC is already being modified"})
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    sfn_input = {
        "jobId": vpc_id,
        "awsVpcId": item["awsVpcId"],
        "subnets": new_subnets,
    }

    try:
        sfn.start_execution(
            stateMachineArn=ADD_SUBNETS_STATE_MACHINE_ARN,
            name=f"add-subnets-{uuid.uuid4()}",
            input=json.dumps(sfn_input),
        )
    except ClientError:
        # Restore SUCCEEDED so the VPC is not stuck in UPDATING
        TABLE.update_item(
            Key={"vpcId": vpc_id},
            UpdateExpression="SET #s = :s, updatedAt = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "SUCCEEDED",
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
        return _response(500, {"error": "Failed to start subnet addition workflow"})

    return _response(
        202,
        {"vpcId": vpc_id, "status": "UPDATING", "links": {"self": f"/vpcs/{vpc_id}"}},
    )
