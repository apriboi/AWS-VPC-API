import os
import time
from datetime import datetime, timezone

import boto3

ec2 = boto3.client("ec2")
ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])


def _wait_for_vpc(vpc_id: str, timeout_s: int = 45) -> None:
    """Block until the VPC reaches the 'available' state."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        state = resp["Vpcs"][0]["State"]
        if state == "available":
            return
        time.sleep(2)
    raise RuntimeError(f"VPC {vpc_id} never reached 'available' state")


def handler(event, context):
    job_id = event["jobId"]
    name = event["name"]
    cidr_block = event["cidrBlock"]
    owner_sub = event["ownerSub"]

    # Move the record to IN_PROGRESS.
    TABLE.update_item(
        Key={"vpcId": job_id},
        UpdateExpression="SET #s = :s, updatedAt = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "IN_PROGRESS",
            ":u": datetime.now(timezone.utc).isoformat(),
        },
    )

    create_resp = ec2.create_vpc(
        CidrBlock=cidr_block,
        TagSpecifications=[
            {
                "ResourceType": "vpc",
                "Tags": [
                    {"Key": "Name", "Value": name},
                    {"Key": "vpc-api:jobId", "Value": job_id},
                    {"Key": "vpc-api:ownerSub", "Value": owner_sub},
                ],
            }
        ],
    )
    vpc_id = create_resp["Vpc"]["VpcId"]
    _wait_for_vpc(vpc_id)

    # Write the AWS VPC ID to DynamoDB first so callers can find the real resource even if the DNS configuration or final update below fails.
    TABLE.update_item(
        Key={"vpcId": job_id},
        UpdateExpression="SET awsVpcId = :v, updatedAt = :u",
        ExpressionAttributeValues={
            ":v": vpc_id,
            ":u": datetime.now(timezone.utc).isoformat(),
        },
    )

    # Enable DNS hostnames so downstream resources behave normally.
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})

    return {"vpcId": vpc_id, "cidrBlock": cidr_block}
