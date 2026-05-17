import json
import os
from datetime import datetime, timezone

import boto3

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["TABLE_NAME"])


def handler(event, context):
    job_id = event["jobId"]
    status = event["status"]
    now = datetime.now(timezone.utc).isoformat()

    if status == "SUCCEEDED":
        new_subnets = event.get("newSubnets") or []
        TABLE.update_item(
            Key={"vpcId": job_id},
            UpdateExpression=(
                "SET #s = :s, updatedAt = :u, "
                "subnets = list_append(if_not_exists(subnets, :empty), :ns) "
                "REMOVE lastError"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "SUCCEEDED",
                ":u": now,
                ":ns": new_subnets,
                ":empty": [],
            },
        )
    else:
        err = event.get("error") or {}
        if isinstance(err, dict):
            cause = err.get("Cause") or err.get("cause")
            if isinstance(cause, str):
                try:
                    parsed = json.loads(cause)
                    err = {**err, "Cause": parsed}
                except (json.JSONDecodeError, TypeError):
                    pass
        # Restore SUCCEEDED — the VPC is intact, only the subnet addition failed.
        TABLE.update_item(
            Key={"vpcId": job_id},
            UpdateExpression="SET #s = :s, updatedAt = :u, lastError = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "SUCCEEDED",
                ":u": now,
                ":e": json.dumps(err, default=str)[:4000],
            },
        )

    return {"jobId": job_id, "status": status}
