"""Sign the packages of the Lambda functions with AWS Signer.

For each package: write its settings to `config.json` (infra/config.py), make a zip file, upload it
to the package bucket, start a signing job, and wait for the signed package. The result is in
.build/signed.json, which app.py reads. A package that did not change since the last signature is
not signed again.

The packages get their signature after the signing stack and before the stack AgentFarmRuntime,
which creates the functions.

Usage: python -m infra.sign <package> [<package> ...]
"""
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3

from infra import config
from infra.names import SIGNING_STACK

ROOT = Path(__file__).resolve().parent.parent
REGION = os.environ.get("AWS_REGION", "eu-central-1")
RESULT = ROOT / ".build" / "signed.json"
# The broker and the interceptor decide an identity; the session starter decides the agent, the use case and the
# session ID.
PACKAGES = {"broker": ".build/broker", "interceptor": ".build/interceptor", "starter": ".build/starter"}


def zip_of(directory: Path) -> bytes:
    """A zip file with the same bytes for the same content."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            info = zipfile.ZipInfo(str(path.relative_to(directory)), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def main() -> None:
    selected = sys.argv[1:]
    unknown = [name for name in selected if name not in PACKAGES]
    if not selected or unknown:
        raise SystemExit(f"usage: python -m infra.sign <package> [...]; packages: {', '.join(PACKAGES)}")
    stack = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=SIGNING_STACK)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    bucket, profile = outputs["PackageBucketName"], outputs["SigningProfileName"]
    s3 = boto3.client("s3", region_name=REGION)
    signer = boto3.client("signer", region_name=REGION)
    previous = json.loads(RESULT.read_text()) if RESULT.exists() else {}
    same_profile = previous.get("profile") == profile
    # A package that this run does not sign keeps its last signature, if the signing profile is the same.
    packages = dict(previous.get("packages", {})) if same_profile else {}
    for name in selected:
        directory = ROOT / PACKAGES[name]
        config.write(name, directory, REGION)
        data = zip_of(directory)
        digest = hashlib.sha256(data).hexdigest()
        known = previous.get("packages", {}).get(name, {})
        if known.get("sha256") == digest and same_profile:
            print(f"{name}: no change, signed package {known['key']}")
            continue
        source_key = f"source/{name}-{digest[:16]}.zip"
        version = s3.put_object(Bucket=bucket, Key=source_key, Body=data)["VersionId"]
        job_id = signer.start_signing_job(
            source={"s3": {"bucketName": bucket, "key": source_key, "version": version}},
            destination={"s3": {"bucketName": bucket, "prefix": f"signed/{name}-"}},
            profileName=profile)["jobId"]
        while (job := signer.describe_signing_job(jobId=job_id))["status"] == "InProgress":
            time.sleep(2)
        if job["status"] != "Succeeded":
            raise SystemExit(f"signing job {job_id} of {name}: {job['status']} {job.get('statusReason')}")
        packages[name] = {"key": job["signedObject"]["s3"]["key"], "sha256": digest, "signing_job": job_id}
        print(f"{name}: signed by job {job_id}, signed package {packages[name]['key']}")
    RESULT.parent.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps({
        "bucket": bucket, "profile": profile, "code_signing_config_arn": outputs["CodeSigningConfigArn"],
        "packages": packages}, indent=2))


if __name__ == "__main__":
    main()
