"""Delete all resources of the system in AWS, then check what remains in the account.

Order: the stacks of the sessions and of the live environments, the stack AgentFarmRuntime (the trail stops
first, and its bucket is emptied), the signing stack with the bucket of the Lambda packages, and the assets
that `cdk deploy` uploaded for the stack AgentFarmRuntime. The command changes nothing on GitHub.
"""
import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

from infra.names import GITHUB_APP_KEY_ALIAS_PREFIX, RUNTIME_STACK, SIGNING_STACK, STACK_PREFIX

ROOT = Path(__file__).resolve().parent.parent
REGION = os.environ.get("AWS_REGION", "eu-central-1")
PREFIX = "agentfarm"


def client(service: str, region: str = REGION):
    return boto3.client(service, region_name=region)


def stack_names(prefix: str) -> list[str]:
    pages = client("cloudformation").get_paginator("describe_stacks").paginate()
    return [s["StackName"] for page in pages for s in page["Stacks"] if s["StackName"].startswith(prefix)]


def stack_exists(name: str) -> bool:
    try:
        client("cloudformation").describe_stacks(StackName=name)
        return True
    except ClientError:
        return False


def empty(bucket: str) -> None:
    s3, count = client("s3"), 0
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=item["Key"])
                count += 1
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchBucket":
            raise
        print(f"s3://{bucket} does not exist")
        return
    print(f"deleted {count} objects of s3://{bucket}")


def delete_stacks() -> None:
    """The stacks of the sessions and of the live environments (agentfarm-<session ID>, agentfarm-live-<use case>),
    then the stack AgentFarmRuntime."""
    cloudformation = client("cloudformation")
    waiter = cloudformation.get_waiter("stack_delete_complete")
    session_stacks = stack_names(STACK_PREFIX)
    for name in session_stacks:
        print(f"delete stack {name}")
        cloudformation.delete_stack(StackName=name)
    for name in session_stacks:
        waiter.wait(StackName=name)

    if not stack_exists(RUNTIME_STACK):
        print(f"{RUNTIME_STACK} does not exist")
        return
    outputs = {o["OutputKey"]: o["OutputValue"] for o in
               cloudformation.describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0].get("Outputs", [])}
    if outputs.get("TrailArn"):   # no more log files, then the bucket of the trail can be empty
        print(f"stop the trail {outputs['TrailArn']}")
        try:
            client("cloudtrail").stop_logging(Name=outputs["TrailArn"])
        except ClientError as error:
            if error.response["Error"]["Code"] != "TrailNotFoundException":
                raise
    # CloudTrail can deliver a log file after the trail stopped: empty the bucket again and delete again.
    for attempt in range(1, 4):
        if outputs.get("TrailBucketName"):
            empty(outputs["TrailBucketName"])
        print(f"delete the stack {RUNTIME_STACK} (attempt {attempt})")
        cloudformation.delete_stack(StackName=RUNTIME_STACK)
        try:
            waiter.wait(StackName=RUNTIME_STACK, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
            break
        except WaiterError:
            if attempt == 3:
                raise
            print("the deletion failed: the bucket of the trail is not empty")


def delete_signing_stack() -> None:
    """The stack with the signing profile and the bucket of the Lambda packages. The bucket is versioned."""
    if not stack_exists(SIGNING_STACK):
        print(f"{SIGNING_STACK} does not exist")
        return
    cloudformation, s3 = client("cloudformation"), client("s3")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in
               cloudformation.describe_stacks(StackName=SIGNING_STACK)["Stacks"][0].get("Outputs", [])}
    bucket = outputs.get("PackageBucketName")
    if bucket:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                s3.delete_object(Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])
        print(f"emptied s3://{bucket}")
    print(f"delete signing stack {SIGNING_STACK}")
    cloudformation.delete_stack(StackName=SIGNING_STACK)
    cloudformation.get_waiter("stack_delete_complete").wait(StackName=SIGNING_STACK)
    (ROOT / ".build" / "signed.json").unlink(missing_ok=True)


def delete_cdk_assets() -> list[str]:
    """Delete the assets of the last `cdk deploy` of the stack AgentFarmRuntime from the CDK bootstrap resources."""
    manifest = ROOT / "cdk.out" / f"{RUNTIME_STACK}.assets.json"
    if not manifest.exists():
        return ["The assets of the stack in the CDK bootstrap bucket and repository (cdk.out is missing)"]
    account = client("sts").get_caller_identity()["Account"]

    def resolve(text: str) -> str:
        return text.replace("${AWS::AccountId}", account).replace("${AWS::Region}", REGION)

    assets = json.loads(manifest.read_text())
    for asset in assets.get("files", {}).values():
        for destination in asset["destinations"].values():
            bucket, key = resolve(destination["bucketName"]), destination["objectKey"]
            print(f"delete asset s3://{bucket}/{key}")
            client("s3").delete_object(Bucket=bucket, Key=key)
    for asset in assets.get("dockerImages", {}).values():
        for destination in asset["destinations"].values():
            repository, tag = resolve(destination["repositoryName"]), destination["imageTag"]
            print(f"delete image {repository}:{tag}")
            client("ecr").batch_delete_image(repositoryName=repository, imageIds=[{"imageTag": tag}])
    return []


def remaining() -> list[str]:
    """Search the account for resources of the system."""
    found = [f"CloudFormation stack {name}" for name in stack_names(STACK_PREFIX)]
    found += [f"CloudFormation stack {name}" for name in (SIGNING_STACK, RUNTIME_STACK) if stack_exists(name)]
    found += [f"Secrets Manager secret {s['Name']}" + (" (deletion scheduled)" if s.get("DeletedDate") else "")
              for page in client("secretsmanager").get_paginator("list_secrets").paginate(IncludePlannedDeletion=True)
              for s in page["SecretList"] if s["Name"].startswith(PREFIX)]
    iam = client("iam")
    found += [f"IAM role {r['RoleName']}" for page in iam.get_paginator("list_roles").paginate()
              for r in page["Roles"] if r["RoleName"].startswith(PREFIX)]
    found += [f"IAM managed policy {p['PolicyName']}" for page in
              iam.get_paginator("list_policies").paginate(Scope="Local")
              for p in page["Policies"] if p["PolicyName"].startswith(PREFIX)]
    logs = client("logs")
    for prefix in (f"/aws/lambda/{PREFIX}", f"/{PREFIX}", f"/aws/bedrock-agentcore/runtimes/{PREFIX}",
                   f"/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{PREFIX}"):
        found += [f"CloudWatch log group {g['logGroupName']}" for page in
                  logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
                  for g in page["logGroups"]]
    found += [f"S3 bucket {b['Name']}" for b in client("s3").list_buckets()["Buckets"]
              if b["Name"].startswith(PREFIX)]
    found += [f"Lambda function {f['FunctionName']}" for page in
              client("lambda").get_paginator("list_functions").paginate()
              for f in page["Functions"] if f["FunctionName"].startswith(PREFIX)]
    found += [f"DynamoDB table {name}" for name in client("dynamodb").list_tables()["TableNames"]
              if name.startswith(PREFIX)]
    agentcore = client("bedrock-agentcore-control")
    found += [f"AgentCore gateway {g['name']} ({g['gatewayId']})" for g in agentcore.list_gateways()["items"]
              if g["name"].startswith(PREFIX)]
    found += [f"AgentCore agent runtime {r['agentRuntimeName']} ({r['agentRuntimeId']})" for r in
              agentcore.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"].startswith(PREFIX)]
    found += [f"AgentCore code interpreter {c['name']} ({c['codeInterpreterId']})" for c in
              agentcore.list_code_interpreters()["codeInterpreterSummaries"] if c.get("name", "").startswith(PREFIX)]
    found += [f"SSM parameter {p['Name']}" for page in
              client("ssm").get_paginator("describe_parameters").paginate(
                  ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [f"/{PREFIX}"]}])
              for p in page["Parameters"]]
    found += [f"CloudTrail trail {t['Name']}" for t in client("cloudtrail").describe_trails()["trailList"]
              if t["Name"].startswith(PREFIX)]
    return found


def kept_keys() -> list[str]:
    """The KMS keys of the GitHub Apps: the stack keeps them on purpose, because a key has the private key of an
    App. A person schedules their deletion."""
    kms = client("kms")
    return [f"KMS key {a['TargetKeyId']} ({a['AliasName']}): the stack keeps it, because it has the private key "
            f"of a GitHub App. Schedule its deletion with `aws kms schedule-key-deletion` when the App is not used"
            for page in kms.get_paginator("list_aliases").paginate() for a in page["Aliases"]
            if a["AliasName"].startswith(GITHUB_APP_KEY_ALIAS_PREFIX) and a.get("TargetKeyId")]


def main() -> None:
    delete_stacks()
    delete_signing_stack()
    not_deleted = delete_cdk_assets()
    print("\nCheck of the account after the deletion:")
    found = remaining()
    for item in found:
        print(f"  REMAINS: {item}")
    if not found:
        print("  No stack, role, policy, log group, bucket, function, table, gateway, agent runtime, code")
        print("  interpreter, SSM parameter, trail or secret of the system remains.")
    print("\nNot deleted by this command:")
    for item in not_deleted + kept_keys() + [
        "The repositories, the GitHub Apps, the workflows and the label on GitHub",
        "Assets of earlier `cdk deploy` runs in the CDK bootstrap bucket and repository (CDKToolkit stack)",
        "The CloudTrail event history (AWS keeps it for 90 days)",
        "The signing jobs and the cancelled signing profile in AWS Signer (AWS keeps them)",
        "The account setting IAM outbound identity federation (it stays enabled)",
        "The IAM OIDC identity provider of GitHub Actions (a resource of the account, not of the system)",
    ]:
        print(f"  {item}")


if __name__ == "__main__":
    main()
