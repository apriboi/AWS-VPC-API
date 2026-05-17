import boto3

ec2 = boto3.client("ec2")


def handler(event, context):
    vpc_id = event["vpcId"]
    subnet_spec = event["subnet"]
    job_id = event["jobId"]

    kwargs = {
        "VpcId": vpc_id,
        "CidrBlock": subnet_spec["cidrBlock"],
        "TagSpecifications": [
            {
                "ResourceType": "subnet",
                "Tags": [
                    {"Key": "Name", "Value": subnet_spec.get("name", "subnet")},
                    {"Key": "vpc-api:jobId", "Value": job_id},
                ],
            }
        ],
    }
    az = subnet_spec.get("availabilityZone")
    if az:
        kwargs["AvailabilityZone"] = az

    resp = ec2.create_subnet(**kwargs)
    subnet = resp["Subnet"]

    return {
        "subnetId": subnet["SubnetId"],
        "cidrBlock": subnet["CidrBlock"],
        "availabilityZone": subnet["AvailabilityZone"],
        "name": subnet_spec.get("name"),
    }
