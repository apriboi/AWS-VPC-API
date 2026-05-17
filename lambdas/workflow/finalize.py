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

    update = "SET #s = :s, updatedAt = :u"
    names = {"#s": "status"}
    values = {":s": status, ":u": now}

    if status == "SUCCEEDED":
        vpc = event.get("vpc") or {}
        subnets = event.get("subnets") or []
        update += ", awsVpcId = :v, subnets = :sb"
        values[":v"] = vpc["vpcId"]
        values[":sb"] = subnets
    else:
        err = event.get("error") or {}
        update += ", errorMessage = :e"
        # Step Functions error payloads are sometimes JSON-encoded strings.
        if isinstance(err, dict):
            cause = err.get("Cause") or err.get("cause")
            if isinstance(cause, str):
                try:
                    parsed = json.loads(cause)
                    err = {**err, "Cause": parsed}
                except (ValueError, TypeError):
                    pass
        values[":e"] = json.dumps(err, default=str)[:4000]

    TABLE.update_item(
        Key={"vpcId": job_id},
        UpdateExpression=update,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )

    return {"jobId": job_id, "status": status}
