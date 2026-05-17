This project is a web service that lets your team create and manage VPCs in AWS

The two types of user
Admin users (members of the vpc-admins group) can create, modify, and delete vpcs.

Regular users can look up and list vpcs.

What the files in this project are:

app.py 

The entry point for the deployment.
This is the very first file that runs when a developer deploys the project to Amazon and puts it in the Stockholm (eu-north-1) data centre by default." You never need to touch this file unless you want to change which region the service runs in.

vpc_api/vpc_api_stack.py 

This file describes every piece of Amazon infrastructure the project needs.

What it tells Amazon to build:

Cognito: handles user accounts and passwords
DynamoDB: stores a record for every vpc ever created, including its current status and all the details
API Gateway: the public URL requests are sent to
IAM: strict limits on what each part of the system is allowed to do. For example, the "create vpc" function can only create resources it specifically tagged. It cannot accidentally touch anything else in your Amazon account!
Lambda functions and Step functions 
lambdas/api/common.py: A small utilities file used by all the web-facing functions. It contains three reusable pieces: a response formatter, an identity checker, a group checker
Rather than copy these three pieces into every file, they live here.

lambdas/api/create_vpc.py 

Handles new vpc requests
Triggered by: POST /vpcs
Who can use it: Admins only

When someone wants a new vpc, this function is called first.

Confirms the caller is logged in and is an admin
Checks the request makes sense — is the vpc address range valid? Do all the requested subnets fit inside the main range? Are there too many (the limit is 16)?
Creates a tracking record in the database with status PENDING
Immediately replies with a job ID — the caller doesn't have to wait for the vpc to actually be built
The caller uses that job ID to check back later.

lambdas/api/get_vpc.py

Looks up one vpc record

Triggered by: GET /vpcs/{id}
Who can use it: Any logged-in user (for records they or their team created)

Given a job ID, this function fetches the current record from the database and returns it. Callers track progresst by checking the status from PENDING to SUCCEEDED (or FAILED). It also enforces privacy: you can only see vpcs created by yourself or someone in the same group as you.

lambdas/api/list_vpcs.py

Shows all your team's vpcs

Triggered by: GET /vpcs
Who can use it: Any logged-in user

Returns a list of vpc records, newest first. Results are team-scoped:

If you're in a group (like vpc-admins), you see all vpcs created by anyone in that group
If you're not in any group, you only see your own vpcs
Returns up to 100 results at a time. If there are more, the response includes a cursor token you can pass in the next request to get the next page.

lambdas/api/delete_vpc.py 

Deletes an entire vpc

Triggered by: DELETE /vpcs/{id}
Who can use it: Admins only (for records they or their team created)

Deletes the vpc and every subnet inside it from Amazon's infrastructure, then marks the database record as DELETED (the record itself is kept for audit purposes). Will refuse to delete a vpc that is currently being built or modified — you must wait for it to finish first.

lambdas/api/delete_subnet.py 

Removes one subnet from a vpc

Triggered by: DELETE /vpcs/{id}/subnets/{subnetId}
Who can use it: Admins only

Removes a single subnet from an existing vpc. The vpc itself stays intact. Updates the database record to reflect that the subnet is gone.

lambdas/api/add_subnets.py 

Adds more subnets to an existing vpc

Triggered by: POST /vpcs/{id}/subnets
Who can use it: Admins only

Lets you expand an existing vpc by adding more snets after it was originally created. Checks that the new subnet fits inside the vpc's address range and doesnt overlap with any existing ones. Marks the vpc as UPDATING while the work happens in the background, then replies immediately with a 202 poll GET /vpcs/{id} to track progress.

lambdas/workflow/create_vpc_resource.py

Builds the vpc
This runs invisibly in the background. The caller never interacts with it directly. It:

Updates the database record to IN_PROGRESS so pollers can see work has started
Asks Amazon to create the actual vpc
Waits for Amazon to confirm it's ready (usually a few seconds)
Saves the real Amazon vpc ID to the database immediately — this is important because if something goes wrong in the next step, the ID is already recorded so the vpc can be found and cleaned up manually
Turns on DNS settings so anything running inside the vpc gets a readable hostname rather than just an IP address
Passes the vpc ID to the next function

lambdas/workflow/create_subnets.py 

Builds one subnet
This function is called once for every subnet that was requested — multiple copies can run at the same time (up to 10 in parallel) to speed things up. It creates one subnet inside the vpc in Amazon's infrastructure and returns: ID, address range, which data centre zone it landed in.

lambdas/workflow/finalize.py 

Writes the final status of the vpc creation in the db

Success: saves the vpc ID and the complete list of subnets with their IDs, marks the record SUCCEEDED
Failure: saves the error details, marks the record FAILED

lambdas/workflow/finalize_add_subnets.py 

Writes the final verdict for subnet addition, slightly different behaviour from the above:

Success: appends the new subnet to the existing list in the database, clears any previous error, marks the record SUCCEEDED
Failure: marks the record back to SUCCEEDED (because the vpc itself is still intact and working, only the new subnets weren't added), but saves the error in a lastError field so the caller can see what went wrong

Deployment 

You need an Amazon Web Services account and a computer with Python and Node.js installed.

One-time setup:

npm install -g aws-cdk
Each deployment:


# 1. Create a Python environment and install dependencies
python -m venv .venv
source .venv/bin/activate        # Mac/Linux

.venv\Scripts\activate         # Windows

pip install -r requirements.txt

# 2. First-time only: prepare your Amazon account for CDK
cdk bootstrap

# 3. Deploy everything
cdk deploy

When it finishes, Amazon will print a web address (the ApiUrl output) — that is the URL requests will be sent to

# To tear down everything:

cdk destroy

Note: any vpcs created through the API are not deleted automatically. You must delete them via DELETE /vpcs/{id} before running cdk destroy, otherwise they remain in your Amazon account.

# User setup — after deployment
Create an account

aws cognito-idp sign-up \
  --client-id YOUR_CLIENT_ID \
  --username you@yourcompany.com \
  --password 'YourPassword1' \
  --user-attributes Name=email,Value=you@yourcompany.com
Confirm the account

aws cognito-idp admin-confirm-sign-up \
  --user-pool-id YOUR_USER_POOL_ID \
  --username you@yourcompany.com
Grant admin permissions (skip this for read-only users)

aws cognito-idp admin-add-user-to-group \
  --user-pool-id YOUR_USER_POOL_ID \
  --username you@yourcompany.com \
  --group-name vpc-admins
Sign in and get a token

aws cognito-idp initiate-auth \
  --client-id YOUR_CLIENT_ID \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=you@yourcompany.com,PASSWORD='YourPassword1'
Copy the IdToken from the response. Every API request uses it:


export TOKEN=eyJraWQiOi...   # paste your token here
export API_URL=https://abcd1234.execute-api.eu-north-1.amazonaws.com
If you are added to vpc-admins after signing in, sign in again to get a fresh token — the group membership is baked into the token at sign-in time.

Usage — common tasks
Create a vpc

curl -X POST "$API_URL/vpcs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-vpc",
    "cidrBlock": "10.0.0.0/16",
    "subnets": [
      {"name": "section-a", "cidrBlock": "10.0.1.0/24"},
      {"name": "section-b", "cidrBlock": "10.0.2.0/24"}
    ]
  }'

You get back a jobId. Save it you need it for everything below.

Check if the vpc is ready

curl "$API_URL/vpcs/$JOB_ID" -H "Authorization: Bearer $TOKEN"
Keep calling this every few seconds until "status" shows "SUCCEEDED".

List all vpc your team has created

curl "$API_URL/vpcs" -H "Authorization: Bearer $TOKEN"
Add more subnets to an existing vpc

curl -X POST "$API_URL/vpcs/$JOB_ID/subnets" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "subnets": [
      {"name": "section-c", "cidrBlock": "10.0.3.0/24"}
    ]
  }'
Poll GET /vpcs/$JOB_ID until status returns to SUCCEEDED.

Delete one subnet

curl -X DELETE "$API_URL/vpcs/$JOB_ID/subnets/subnet-0abc1234" \
  -H "Authorization: Bearer $TOKEN"

Delete an entire vpc

curl -X DELETE "$API_URL/vpcs/$JOB_ID" -H "Authorization: Bearer $TOKEN"
