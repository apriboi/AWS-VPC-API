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
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def _validate(payload: dict) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object"

    name = payload.get("name")
    cidr = payload.get("cidrBlock")
    subnets = payload.get("subnets")

    if not name or not isinstance(name, str):
        return None, "Field 'name' is required and must be a string"
    if not cidr or not isinstance(cidr, str):
        return None, "Field 'cidrBlock' is required (e.g. '10.0.0.0/16')"
    try:
        vpc_net = ip_network(cidr, strict=False)
    except ValueError:
        return None, f"'cidrBlock' is not a valid CIDR: {cidr}"

    if not isinstance(subnets, list) or not subnets:
        return None, "Field 'subnets' must be a non-empty list"
    if len(subnets) > 16:
        return None, "At most 16 subnets per request"

    normalized_subnets = []
    for i, s in enumerate(subnets):
        if not isinstance(s, dict):
            return None, f"subnets[{i}] must be an object"
        s_cidr = s.get("cidrBlock")
        s_az = s.get("availabilityZone")
        if not s_cidr:
            return None, f"subnets[{i}].cidrBlock is required"
        try:
            sub_net = ip_network(s_cidr, strict=False)
        except ValueError:
            return None, f"subnets[{i}].cidrBlock invalid: {s_cidr}"
        if not sub_net.subnet_of(vpc_net):
            return None, f"subnets[{i}].cidrBlock {s_cidr} not inside VPC CIDR {cidr}"
        normalized_subnets.append(
            {
                "cidrBlock": s_cidr,
                "availabilityZone": s_az or "eu-north-1a",
                "name": s.get("name") or f"subnet-{i}",
            }
        )

    return (
        {"name": name, "cidrBlock": cidr, "subnets": normalized_subnets},
        None,
    )


def handler(event, context):
    owner = _owner_sub(event)
    if not owner:
        return _response(401, {"error": "Missing or invalid auth claims"})

    if ADMIN_GROUP not in _user_groups(event):
        return _response(403, {"error": f"Only members of '{ADMIN_GROUP}' may create VPCs"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Body is not valid JSON"})

    payload, err = _validate(body)
    if err:
        return _response(400, {"error": err})

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    team = _primary_group(event)

    item = {
        "vpcId": job_id,  # repurposed as job id until provisioning completes
        "ownerSub": owner,
        "team": team,
        "status": "PENDING",
        "name": payload["name"],
        "cidrBlock": payload["cidrBlock"],
        "requestedSubnets": payload["subnets"],
        "createdAt": now,
        "updatedAt": now,
    }

    try:
        TABLE.put_item(Item=item)
    except ClientError as e:
        return _response(500, {"error": f"DynamoDB error: {e.response['Error']['Code']}"})

    sfn_input = {
        "jobId": job_id,
        "ownerSub": owner,
        "name": payload["name"],
        "cidrBlock": payload["cidrBlock"],
        "subnets": payload["subnets"],
    }

    try:
        sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=f"vpc-{job_id}",
            input=json.dumps(sfn_input),
        )
    except ClientError as e:
        TABLE.update_item(
            Key={"vpcId": job_id},
            UpdateExpression="SET #s = :s, updatedAt = :u, errorMessage = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "FAILED",
                ":u": datetime.now(timezone.utc).isoformat(),
                ":e": str(e),
            },
        )
        return _response(500, {"error": "Failed to start provisioning workflow"})

    return _response(
        202,
        {
            "jobId": job_id,
            "status": "PENDING",
            "links": {"self": f"/vpcs/{job_id}"},
        },
    )
