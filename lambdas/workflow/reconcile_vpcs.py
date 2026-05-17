import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

ec2 = boto3.client("ec2")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])

_RECONCILABLE = {"SUCCEEDED"}


def _paginate(method, result_key, **kwargs):
    items = []
    while True:
        resp = method(**kwargs)
        items.extend(resp.get(result_key, []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return items


def handler(event, context):
    # Scan for terminal-state VPCs that have a real AWS resource to check against
    db_items = []
    scan_kwargs = {
        "FilterExpression": (
            Attr("status").is_in(list(_RECONCILABLE)) & Attr("awsVpcId").exists()
        )
    }
    while True:
        resp = TABLE.scan(**scan_kwargs)
        db_items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    if not db_items:
        return {"reconciled": 0, "total": 0}

    # Single bulk pass — describe all API-managed VPCs and subnets by tag
    ec2_vpcs = _paginate(
        ec2.describe_vpcs,
        "Vpcs",
        Filters=[{"Name": "tag-key", "Values": ["vpc-api:jobId"]}],
    )
    ec2_subnets = _paginate(
        ec2.describe_subnets,
        "Subnets",
        Filters=[{"Name": "tag-key", "Values": ["vpc-api:jobId"]}],
    )

    ec2_vpc_ids = {v["VpcId"] for v in ec2_vpcs}
    subnets_by_vpc: dict = {}
    for s in ec2_subnets:
        subnets_by_vpc.setdefault(s["VpcId"], []).append(s)

    now = datetime.now(timezone.utc).isoformat()
    reconciled = 0

    for item in db_items:
        aws_vpc_id = item["awsVpcId"]

        if aws_vpc_id not in ec2_vpc_ids:
            # VPC was deleted outside the API
            TABLE.update_item(
                Key={"vpcId": item["vpcId"]},
                UpdateExpression="SET #s = :s, updatedAt = :u, reconciledAt = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "DELETED", ":u": now, ":r": now},
            )
            reconciled += 1
            continue

        # VPC exists — reconcile subnet list
        actual_subnets = [
            {
                "subnetId": s["SubnetId"],
                "cidrBlock": s["CidrBlock"],
                "availabilityZone": s["AvailabilityZone"],
                "name": next(
                    (t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), ""
                ),
            }
            for s in subnets_by_vpc.get(aws_vpc_id, [])
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
            reconciled += 1
        else:
            TABLE.update_item(
                Key={"vpcId": item["vpcId"]},
                UpdateExpression="SET reconciledAt = :r",
                ExpressionAttributeValues={":r": now},
            )

    return {"reconciled": reconciled, "total": len(db_items)}
