# AWS VPC API

This project is a web service that lets your team create and manage VPCs in AWS.

## User types

**Admin users** (members of the `vpc-admins` group) can create, modify, and delete VPCs.

**Regular users** can look up and list VPCs.

---

## Repo layout

```
AWS-VPC-API/
├── app.py                          # CDK entry point — sets region and instantiates the stack
├── cdk.json                        # CDK toolkit configuration
├── requirements.txt                # Python dependencies
│
├── vpc_api/
│   └── vpc_api_stack.py            # All AWS infrastructure (Cognito, DynamoDB, API Gateway, IAM, Step Functions)
│
└── lambdas/
    ├── api/                        # HTTP-facing handlers (one file per route)
    │   ├── common.py               # Shared helpers: response formatter, identity/group checker
    │   ├── create_vpc.py           # POST /vpcs
    │   ├── get_vpc.py              # GET  /vpcs/{id}
    │   ├── list_vpcs.py            # GET  /vpcs
    │   ├── add_subnets.py          # POST /vpcs/{id}/subnets
    │   ├── delete_subnet.py        # DELETE /vpcs/{id}/subnets/{subnetId}
    │   └── delete_vpc.py           # DELETE /vpcs/{id}
    │
    └── workflow/                   # Step Functions tasks (run in the background, never called directly)
        ├── create_vpc_resource.py  # Creates the VPC in AWS and enables DNS
        ├── create_subnets.py       # Creates one subnet (runs up to 10 in parallel)
        ├── finalize.py             # Writes SUCCEEDED/FAILED result for VPC creation
        ├── finalize_add_subnets.py # Writes SUCCEEDED/FAILED result for subnet addition
        └── reconcile_vpcs.py       # Scheduled reconciler — syncs DB state with actual AWS state
```

---

## Project files

### `app.py`

The entry point for the deployment. This is the very first file that runs when a developer deploys the project to Amazon and puts it in the Stockholm (`eu-north-1`) data centre by default. You never need to touch this file unless you want to change which region the service runs in.

### `vpc_api/vpc_api_stack.py`

This file describes every piece of Amazon infrastructure the project needs.

What it tells Amazon to build:

- **Cognito** — handles user accounts and passwords
- **DynamoDB** — stores a record for every VPC ever created, including its current status and all the details
- **API Gateway** — the public URL requests are sent to
- **IAM** — strict limits on what each part of the system is allowed to do. For example, the "create VPC" function can only create resources it specifically tagged. It cannot accidentally touch anything else in your Amazon account.
- **Lambda functions and Step Functions**

---

## Lambda functions

### `lambdas/api/common.py`

A small utilities file used by all the web-facing functions. It contains three reusable pieces: a response formatter, an identity checker, and a group checker. Rather than copy these three pieces into every file, they live here.

### `lambdas/api/create_vpc.py`

Handles new VPC requests.

- **Triggered by:** `POST /vpcs`
- **Who can use it:** Admins only

When someone wants a new VPC, this function is called first. It:

1. Confirms the caller is logged in and is an admin
2. Checks the request makes sense — is the VPC address range valid? Do all the requested subnets fit inside the main range? Are there too many (the limit is 16)?
3. Creates a tracking record in the database with status `PENDING`
4. Immediately replies with a job ID — the caller doesn't have to wait for the VPC to actually be built

The caller uses that job ID to check back later.

### `lambdas/api/get_vpc.py`

Looks up one VPC record.

- **Triggered by:** `GET /vpcs/{id}`
- **Who can use it:** Any logged-in user (for records they or their team created)

Given a job ID, this function fetches the current record from the database and returns it. Callers track progress by polling the `status` field from `PENDING` to `SUCCEEDED` (or `FAILED`). It also enforces privacy: you can only see VPCs created by yourself or someone in the same group as you.

### `lambdas/api/list_vpcs.py`

Shows all your team's VPCs.

- **Triggered by:** `GET /vpcs`
- **Who can use it:** Any logged-in user

Returns a list of VPC records, newest first. Results are team-scoped:

- If you're in a group (like `vpc-admins`), you see all VPCs created by anyone in that group
- If you're not in any group, you only see your own VPCs

Returns up to 100 results at a time. If there are more, the response includes a cursor token you can pass in the next request to get the next page.

### `lambdas/api/delete_vpc.py`

Deletes an entire VPC.

- **Triggered by:** `DELETE /vpcs/{id}`
- **Who can use it:** Admins only (for records they or their team created)

Deletes the VPC and every subnet inside it from Amazon's infrastructure, then marks the database record as `DELETED` (the record itself is kept for audit purposes). Will refuse to delete a VPC that is currently being built or modified — you must wait for it to finish first.

### `lambdas/api/delete_subnet.py`

Removes one subnet from a VPC.

- **Triggered by:** `DELETE /vpcs/{id}/subnets/{subnetId}`
- **Who can use it:** Admins only

Removes a single subnet from an existing VPC. The VPC itself stays intact. Updates the database record to reflect that the subnet is gone.

### `lambdas/api/add_subnets.py`

Adds more subnets to an existing VPC.

- **Triggered by:** `POST /vpcs/{id}/subnets`
- **Who can use it:** Admins only

Lets you expand an existing VPC by adding more subnets after it was originally created. Checks that the new subnet fits inside the VPC's address range and doesn't overlap with any existing ones. Marks the VPC as `UPDATING` while the work happens in the background, then replies immediately with a `202` — poll `GET /vpcs/{id}` to track progress.

### `lambdas/workflow/create_vpc_resource.py`

Builds the VPC. This runs invisibly in the background — the caller never interacts with it directly. It:

1. Updates the database record to `IN_PROGRESS` so pollers can see work has started
2. Asks Amazon to create the actual VPC
3. Waits for Amazon to confirm it's ready (usually a few seconds)
4. Saves the real Amazon VPC ID to the database immediately — this is important because if something goes wrong in the next step, the ID is already recorded so the VPC can be found and cleaned up manually
5. Turns on DNS settings so anything running inside the VPC gets a readable hostname rather than just an IP address
6. Passes the VPC ID to the next function

### `lambdas/workflow/create_subnets.py`

Builds one subnet. This function is called once for every subnet that was requested — multiple copies can run at the same time (up to 10 in parallel) to speed things up. It creates one subnet inside the VPC in Amazon's infrastructure and returns: ID, address range, and which data centre zone it landed in.

### `lambdas/workflow/finalize.py`

Writes the final status of the VPC creation in the database.

- **Success:** saves the VPC ID and the complete list of subnets with their IDs, marks the record `SUCCEEDED`
- **Failure:** saves the error details, marks the record `FAILED`

### `lambdas/workflow/finalize_add_subnets.py`

Writes the final verdict for subnet addition, with slightly different behaviour from the above:

- **Success:** appends the new subnets to the existing list in the database, clears any previous error, marks the record `SUCCEEDED`
- **Failure:** marks the record back to `SUCCEEDED` (because the VPC itself is still intact and working, only the new subnets weren't added), but saves the error in a `lastError` field so the caller can see what went wrong

### `lambdas/workflow/reconcile_vpcs.py`

Keeps the database in sync with actual AWS state. Runs automatically every 5 minutes via an EventBridge schedule — it is never called directly.

On each run it scans for all `SUCCEEDED` records that have a real AWS VPC ID, then does a single bulk describe of all API-managed VPCs and subnets in AWS (identified by the `vpc-api:jobId` tag). For each database record it then:

- **VPC missing from AWS** — someone deleted it outside the API. Marks the record `DELETED` and stamps `reconciledAt`.
- **Subnet list differs** — subnets were added or removed outside the API. Overwrites the `subnets` list in the database with the real AWS state and stamps `reconciledAt`.
- **Everything matches** — stamps `reconciledAt` only, so you can see when the record was last verified.

---

## Deployment

You need an Amazon Web Services account and a computer with Python and Node.js installed.

**One-time setup:**

```bash
npm install -g aws-cdk
```

**Each deployment:**

```bash
# 1. Create a Python environment and install dependencies
python -m venv .venv
source .venv/bin/activate        # Mac/Linux
.venv\Scripts\activate           # Windows
pip install -r requirements.txt

# 2. First-time only: prepare your Amazon account for CDK
cdk bootstrap

# 3. Deploy everything
cdk deploy
```

When it finishes, Amazon will print a web address (the `ApiUrl` output) — that is the URL requests will be sent to.

**To tear down everything:**

```bash
cdk destroy
```

> **Note:** Any VPCs created through the API are not deleted automatically. You must delete them via `DELETE /vpcs/{id}` before running `cdk destroy`, otherwise they remain in your Amazon account.

---

## User setup — after deployment

**1. Create an account:**

```bash
aws cognito-idp sign-up \
  --client-id YOUR_CLIENT_ID \
  --username you@yourcompany.com \
  --password 'YourPassword1' \
  --user-attributes Name=email,Value=you@yourcompany.com
```

**2. Confirm the account:**

```bash
aws cognito-idp admin-confirm-sign-up \
  --user-pool-id YOUR_USER_POOL_ID \
  --username you@yourcompany.com
```

**3. Grant admin permissions** (skip this for read-only users):

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id YOUR_USER_POOL_ID \
  --username you@yourcompany.com \
  --group-name vpc-admins
```

**4. Sign in and get a token:**

```bash
aws cognito-idp initiate-auth \
  --client-id YOUR_CLIENT_ID \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=you@yourcompany.com,PASSWORD='YourPassword1'
```

Copy the `IdToken` from the response. Every API request uses it:

```bash
export TOKEN=eyJraWQiOi...   # paste your token here
export API_URL=https://abcd1234.execute-api.eu-north-1.amazonaws.com
```

> If you are added to `vpc-admins` after signing in, sign in again to get a fresh token — the group membership is baked into the token at sign-in time.

---

## Usage — common tasks

**Create a VPC:**

```bash
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
```

You get back a `jobId`. Save it — you need it for everything below.

**Check if the VPC is ready:**

```bash
curl "$API_URL/vpcs/$JOB_ID" -H "Authorization: Bearer $TOKEN"
```

Keep calling this every few seconds until `status` shows `SUCCEEDED`.

**List all VPCs your team has created:**

```bash
curl "$API_URL/vpcs" -H "Authorization: Bearer $TOKEN"
```

**Add more subnets to an existing VPC:**

```bash
curl -X POST "$API_URL/vpcs/$JOB_ID/subnets" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "subnets": [
      {"name": "section-c", "cidrBlock": "10.0.3.0/24"}
    ]
  }'
```

Poll `GET /vpcs/$JOB_ID` until status returns to `SUCCEEDED`.

**Delete one subnet:**

```bash
curl -X DELETE "$API_URL/vpcs/$JOB_ID/subnets/subnet-0abc1234" \
  -H "Authorization: Bearer $TOKEN"
```

**Delete an entire VPC:**

```bash
curl -X DELETE "$API_URL/vpcs/$JOB_ID" -H "Authorization: Bearer $TOKEN"
```
