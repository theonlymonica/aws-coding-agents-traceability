"""Code signing for the Lambda functions that decide an identity.

This stack exists before the infrastructure stack. It has the signing profile of AWS Signer, the
bucket for the packages, and the code signing configuration that the functions use. With the
configuration, AWS Lambda accepts only a package that this signing profile signed.
"""
from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_signer as signer
from constructs import Construct


class SigningStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # AWS Signer reads a package from a versioned bucket and writes the signed package to a bucket.
        bucket = s3.Bucket(
            self, "PackageBucket", bucket_name=f"agentfarm-signing-{self.account}-{self.region}",
            versioned=True, block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,   # `make destroy` empties the bucket first
        )
        profile = signer.SigningProfile(self, "SigningProfile", platform=signer.Platform.AWS_LAMBDA_SHA384_ECDSA)
        configuration = lambda_.CodeSigningConfig(
            self, "CodeSigningConfig", signing_profiles=[profile],
            untrusted_artifact_on_deployment=lambda_.UntrustedArtifactOnDeployment.ENFORCE,
            description="agentfarm: only packages of the signing profile")
        for key, value in {
            "PackageBucketName": bucket.bucket_name,
            "SigningProfileName": profile.signing_profile_name,
            "CodeSigningConfigArn": configuration.code_signing_config_arn,
        }.items():
            CfnOutput(self, key, value=value)
