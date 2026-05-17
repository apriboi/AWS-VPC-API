#!/usr/bin/env python3

#entry point for the VPC-provisioning API

import os

import aws_cdk as cdk

from vpc_api.vpc_api_stack import VpcApiStack


app = cdk.App()

VpcApiStack(
    app,
    "VpcApiStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "eu-north-1"),
    ),
    description="Serverless API that provisions VPCs and subnets via Step Functions.",
)

app.synth()
