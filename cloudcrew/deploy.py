from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import mimetypes
import sys
import time
from pathlib import Path

from .aws import AWS, AwsError
from .core import ROOT, Audit, Config, State, digest, now

CACHE_POLICY = "658327ea-f89d-4fab-a63d-7e88639e58f6"  # AWS managed CachingOptimized
SECURITY_POLICY = "67f7725c-6f97-4210-82d7-5512b31e9d03"  # AWS managed SecurityHeadersPolicy
CLOUDFRONT_ZONE = "Z2FDTNDATAQYW2"
STAGES = ("foundation", "certificate-dns", "delivery", "publish", "failure", "recovery")


def contains_config(actual, expected) -> bool:
    """AWS adds defaults to responses; compare every field that this project manages."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains_config(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains_config(a, e) for a, e in zip(actual, expected))
    return actual == expected


def manifest(root: Path = ROOT / "dist") -> dict:
    if not (root / "index.html").exists():
        raise ValueError("Build the website first: python -m cloudcrew build")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Website uploads cannot contain symlinks")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def origin_domain(config: Config) -> str:
    return f"{config['bucket']}.s3.{config['region']}.amazonaws.com"


def bucket_policy(config: Config, arn: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "CloudFrontReadOnly", "Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{config['bucket']}/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": arn}},
    }, {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{config['bucket']}", f"arn:aws:s3:::{config['bucket']}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}


def distribution_config(config: Config, state: dict) -> dict:
    return {
        "CallerReference": config.binding, "Comment": config["project_id"] + ":" + config.binding[:12],
        "Enabled": True, "DefaultRootObject": "index.html", "PriceClass": "PriceClass_100",
        "Aliases": {"Quantity": 1, "Items": [config["domain"]]},
        "Origins": {"Quantity": 1, "Items": [{"Id": "private-s3", "DomainName": origin_domain(config),
                    "OriginAccessControlId": state["oac_id"], "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
        "DefaultCacheBehavior": {"TargetOriginId": "private-s3", "ViewerProtocolPolicy": "redirect-to-https",
             "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
             "Compress": True, "CachePolicyId": CACHE_POLICY, "ResponseHeadersPolicyId": SECURITY_POLICY,
             "TrustedSigners": {"Enabled": False, "Quantity": 0},
             "TrustedKeyGroups": {"Enabled": False, "Quantity": 0}},
        "ViewerCertificate": {"ACMCertificateArn": state["certificate_arn"], "SSLSupportMethod": "sni-only",
                              "MinimumProtocolVersion": "TLSv1.2_2021"},
        "CustomErrorResponses": {"Quantity": 2, "Items": [
            {"ErrorCode": code, "ResponsePagePath": "/404.html", "ResponseCode": "404", "ErrorCachingMinTTL": 0}
            for code in (403, 404)]},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "HttpVersion": "http2and3", "IsIPV6Enabled": True,
    }


class Deployment:
    def __init__(self, config: Config, audit: Audit, state: State | None = None, aws: AWS | None = None):
        self.config, self.audit = config, audit
        self.state = state or State(config)
        self.aws = aws or AWS(config, audit)
        self.approval_denied = False

    @property
    def tags(self) -> list:
        return [{"Key": "Project", "Value": self.config["project_id"]},
                {"Key": "CloudCrewBinding", "Value": self.config.binding}]

    def assert_tags(self, tags: list) -> None:
        values = {tag["Key"]: tag["Value"] for tag in tags}
        if values.get("CloudCrewBinding") != self.config.binding:
            raise PermissionError("Refusing to modify a resource without this deployment's ownership tag")

    def owned_bucket(self) -> None:
        result = self.aws.call("s3api", "get-bucket-tagging", {"Bucket": self.config["bucket"]})
        self.assert_tags(result["TagSet"])

    def owned_distribution(self) -> dict:
        self.state.require("distribution_id", "distribution_arn")
        tags = self.aws.call("cloudfront", "list-tags-for-resource", {"Resource": self.state.data["distribution_arn"]})
        self.assert_tags(tags["Tags"].get("Items", []))
        return self.aws.call("cloudfront", "get-distribution", {"Id": self.state.data["distribution_id"]})["Distribution"]

    def plan(self, stage: str) -> dict:
        self.config.validate(cloud=True)
        if stage not in STAGES:
            raise ValueError("Unknown deployment stage")
        details: dict = {}
        if stage == "foundation":
            operations = ["ensure_private_bucket", "ensure_hosted_zone", "ensure_certificate", "ensure_oac"]
            details = {"bucket": self.config["bucket"], "region": self.config["region"],
                       "domain": self.config["domain"], "certificate_region": "us-east-1", "tags": self.tags,
                       "public_access_block": True, "encryption": "AES256", "ownership": "BucketOwnerEnforced"}
        elif stage == "certificate-dns":
            self.state.require("certificate_arn", "hosted_zone_id")
            cert = self.aws.call("acm", "describe-certificate", {"CertificateArn": self.state.data["certificate_arn"]}, region="us-east-1")["Certificate"]
            records = [option["ResourceRecord"] for option in cert.get("DomainValidationOptions", []) if "ResourceRecord" in option]
            if not records:
                raise ValueError("ACM has not returned DNS validation records yet; retry this stage shortly")
            details = {"records": records, "zone": self.state.data["hosted_zone_id"]}
            operations = ["write_certificate_dns"]
        elif stage == "delivery":
            self.state.require("certificate_arn", "hosted_zone_id", "oac_id", "bucket_ready")
            cert = self.aws.call("acm", "describe-certificate", {"CertificateArn": self.state.data["certificate_arn"]}, region="us-east-1")["Certificate"]
            if cert["Status"] != "ISSUED":
                raise ValueError(f"Certificate is {cert['Status']}. Delegate the domain to Route 53 and finish DNS validation first.")
            details = {"distribution": distribution_config(self.config, self.state.data),
                       "bucket_policy": bucket_policy(self.config, self.state.data.get("distribution_arn", "CREATED_DISTRIBUTION_ARN")),
                       "dns": "A and AAAA aliases to the created CloudFront distribution", "files": manifest()}
            operations = ["upload_website", "ensure_distribution", "secure_origin", "write_website_dns", "invalidate"]
        elif stage == "publish":
            self.state.require("distribution_id", "distribution_arn", "bucket_ready")
            details = {"files": manifest(), "invalidation_paths": ["/*"]}
            operations = ["upload_website", "invalidate"]
        elif stage == "failure":
            dist = self.owned_distribution()
            if dist["Status"] != "Deployed":
                raise ValueError("Wait for the current CloudFront deployment to finish")
            if self.state.data.get("failure_active"):
                raise ValueError("Recover the active failure before introducing another")
            if dist["DistributionConfig"].get("DefaultRootObject") != "index.html":
                raise ValueError("Failure exercise requires the normal index.html default root")
            if not self.state.data.get("last_verified_run"):
                raise ValueError("Run a passing live verification before introducing a failure")
            bad = copy.deepcopy(dist["DistributionConfig"])
            bad["DefaultRootObject"] = "controlled-missing-" + self.config.binding[:12] + ".html"
            # Verify the controlled missing key really is absent before selecting it.
            try:
                self.aws.call("s3api", "head-object", {"Bucket": self.config["bucket"], "Key": bad["DefaultRootObject"]})
            except AwsError as exc:
                if not any(code in exc.message for code in ("404", "Not Found", "NoSuchKey")):
                    raise
            else:
                raise ValueError("Controlled failure key already exists; refusing an ineffective experiment")
            details = {"before": dist["DistributionConfig"], "after": bad, "etag": dist["ETag"] if "ETag" in dist else self.aws.call("cloudfront", "get-distribution-config", {"Id": self.state.data["distribution_id"]})["ETag"],
                       "baseline_run": self.state.data["last_verified_run"], "effect": "Root URL returns a real 404; /index.html remains accessible"}
            operations = ["introduce_failure", "invalidate"]
        else:
            self.state.require("failure_backup", "failure_active", "failure_config_digest")
            dist = self.owned_distribution()
            if dist["Status"] != "Deployed":
                raise ValueError("Wait for CloudFront to deploy the failure before recovery")
            current_digest = digest(dist["DistributionConfig"])
            original_digest = digest(self.state.data["failure_backup"])
            if current_digest not in (self.state.data["failure_config_digest"], original_digest):
                raise ValueError("CloudFront changed outside the exercise; refusing to overwrite unrelated changes")
            current = self.aws.call("cloudfront", "get-distribution-config", {"Id": self.state.data["distribution_id"]})
            details = {"before": current["DistributionConfig"], "after": self.state.data["failure_backup"], "etag": current["ETag"]}
            if current_digest == original_digest:
                details["effect"] = "Original configuration is already present; reconcile an interrupted request and invalidate cache"
                operations = ["confirm_original_configuration", "invalidate"]
            else:
                operations = ["restore_configuration", "invalidate"]
        plan = {"schema_version": 1, "stage": stage, "binding": self.config.binding,
                "state_digest": digest(self.state.data), "operations": operations, "details": details}
        plan["sha256"] = digest(plan)
        self.audit.artifact(f"plan-{stage}.json", plan)
        return plan

    def apply(self, plan: dict, input_fn=input) -> dict:
        """Rebuild and compare the plan under a lock, then require a real terminal approval."""
        if self.approval_denied:
            raise PermissionError("Human approval was denied in this run; no further approval prompts will be issued")
        (ROOT / "runtime").mkdir(exist_ok=True)
        with (ROOT / "runtime" / "deployment.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Re-read state after acquiring the lock to detect another process's writes.
            self.state = State(self.config, self.state.path)
            expected = self.plan(plan["stage"])
            if expected != plan:
                raise ValueError("Plan is stale or edited. Generate and review a fresh plan.")
            if not sys.stdin.isatty():
                raise PermissionError("Human approval requires an interactive terminal; run apply yourself")
            print(json.dumps(plan, indent=2))
            phrase = "approve " + plan["sha256"][:12]
            print("This plan changes AWS resources and may incur charges.")
            if input_fn(f"Type '{phrase}' to apply this exact plan: ").strip() != phrase:
                self.approval_denied = True
                self.audit.emit("human", "approval_denied", {"plan": plan["sha256"]})
                raise PermissionError("Plan was not approved")
            self.audit.emit("human", "approval_granted", {"plan": plan["sha256"], "stage": plan["stage"], "method": "interactive_exact_digest"})
            self.aws.mutable = True
            try:
                for operation in plan["operations"]:
                    self.audit.emit("infrastructure", "operation_started", {"operation": operation})
                    getattr(self, "_" + operation)(plan["details"])
                    self.state.save()
                    self.audit.emit("infrastructure", "operation_completed", {"operation": operation})
            except Exception as exc:
                self.audit.emit("infrastructure", "operation_failed", {"error": str(exc), "operation": operation})
                raise
            finally:
                self.aws.mutable = False
            return {"status": "applied", "stage": plan["stage"], "state": self.state.data,
                    "next": "CloudFront, certificate, and DNS changes are asynchronous. Run status and verification."}

    def wait_until_ready(self, timeout_seconds: int = 900, interval_seconds: int = 15) -> dict:
        """Bounded read-only polling for CloudFront deployment and the last invalidation."""
        if not 1 <= timeout_seconds <= 1800 or not 1 <= interval_seconds <= 60:
            raise ValueError("Readiness timeout must be 1–1800 seconds and interval 1–60 seconds")
        self.state.require("distribution_id", "distribution_arn")
        deadline = time.monotonic() + timeout_seconds
        while True:
            dist = self.owned_distribution()
            invalidation_status = "not_requested"
            if self.state.data.get("last_invalidation"):
                invalidation_status = self.aws.call("cloudfront", "get-invalidation", {
                    "DistributionId": self.state.data["distribution_id"], "Id": self.state.data["last_invalidation"]})["Invalidation"]["Status"]
            result = {"distribution": dist["Status"], "invalidation": invalidation_status}
            self.audit.emit("infrastructure", "readiness_observation", result)
            if dist["Status"] == "Deployed" and invalidation_status in ("Completed", "not_requested"):
                print("CloudFront is deployed and cache invalidation is complete.", flush=True)
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CloudFront readiness timed out. Inspect status and rerun after propagation; no timeout is treated as success.")
            print(f"Waiting: CloudFront={dist['Status']}, invalidation={invalidation_status}; {int(remaining)}s remaining.", flush=True)
            time.sleep(min(interval_seconds, remaining))

    def _ensure_private_bucket(self, _: dict) -> None:
        bucket = self.config["bucket"]
        try:
            self.aws.call("s3api", "head-bucket", {"Bucket": bucket})
        except AwsError as exc:
            if not any(code in exc.message for code in ("404", "NoSuchBucket", "Not Found")):
                raise
            params = {"Bucket": bucket, "ObjectOwnership": "BucketOwnerEnforced"}
            if self.config["region"] != "us-east-1":
                params["CreateBucketConfiguration"] = {"LocationConstraint": self.config["region"]}
            self.aws.call("s3api", "create-bucket", params)
            self.state.data["bucket_created"] = True
            self.state.save()
        # The local creation receipt allows retrying a first-time tagging failure.
        try:
            self.owned_bucket()
        except AwsError as exc:
            if "NoSuchTagSet" not in exc.message or not self.state.data.get("bucket_created"):
                raise
        self.aws.call("s3api", "put-bucket-tagging", {"Bucket": bucket, "Tagging": {"TagSet": self.tags}})
        self.aws.call("s3api", "put-public-access-block", {"Bucket": bucket, "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}})
        self.aws.call("s3api", "put-bucket-ownership-controls", {"Bucket": bucket, "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}})
        self.aws.call("s3api", "put-bucket-encryption", {"Bucket": bucket, "ServerSideEncryptionConfiguration": {
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}})
        self.state.data["bucket_ready"] = True

    def _ensure_hosted_zone(self, _: dict) -> None:
        zone_id = self.state.data.get("hosted_zone_id") or self.config.values.get("hosted_zone_id")
        if zone_id:
            zone = self.aws.call("route53", "get-hosted-zone", {"Id": zone_id})
        else:
            zones = self.aws.call("route53", "list-hosted-zones-by-name", {"DNSName": self.config["domain"]})["HostedZones"]
            exact = [z for z in zones if z["Name"].rstrip(".") == self.config["domain"]]
            own = [z for z in exact if z.get("CallerReference") == self.config.binding]
            if exact and not own:
                raise ValueError("Domain already has a hosted zone. Explicitly set hosted_zone_id before first deployment to reuse it.")
            if own:
                zone = self.aws.call("route53", "get-hosted-zone", {"Id": own[0]["Id"]})
            else:
                zone = self.aws.call("route53", "create-hosted-zone", {"Name": self.config["domain"], "CallerReference": self.config.binding,
                    "HostedZoneConfig": {"Comment": self.config["project_id"], "PrivateZone": False}})
        if zone["HostedZone"]["Name"].rstrip(".") != self.config["domain"] or zone["HostedZone"].get("Config", {}).get("PrivateZone"):
            raise ValueError("Hosted zone must be public and exactly match the project domain")
        self.state.data.update(hosted_zone_id=zone["HostedZone"]["Id"], nameservers=zone["DelegationSet"]["NameServers"])
        self.state.save()

    def _ensure_certificate(self, _: dict) -> None:
        if self.state.data.get("certificate_arn"):
            self.aws.call("acm", "describe-certificate", {"CertificateArn": self.state.data["certificate_arn"]}, region="us-east-1")
            return
        certs = self.aws.call("acm", "list-certificates", {}, region="us-east-1").get("CertificateSummaryList", [])
        for cert in certs:
            if cert["DomainName"] == self.config["domain"]:
                tags = self.aws.call("acm", "list-tags-for-certificate", {"CertificateArn": cert["CertificateArn"]}, region="us-east-1")["Tags"]
                if {tag["Key"]: tag["Value"] for tag in tags}.get("CloudCrewBinding") == self.config.binding and cert["Status"] in ("ISSUED", "PENDING_VALIDATION"):
                    self.state.data["certificate_arn"] = cert["CertificateArn"]
                    return
        cert = self.aws.call("acm", "request-certificate", {"DomainName": self.config["domain"], "ValidationMethod": "DNS",
                    "IdempotencyToken": self.config.binding[:32], "KeyAlgorithm": "RSA_2048", "Tags": self.tags}, region="us-east-1")
        self.state.data["certificate_arn"] = cert["CertificateArn"]

    def _ensure_oac(self, _: dict) -> None:
        if self.state.data.get("oac_id"):
            self.aws.call("cloudfront", "get-origin-access-control", {"Id": self.state.data["oac_id"]})
            return
        name = self.config["project_id"] + "-" + self.config.binding[:12]
        items = self.aws.call("cloudfront", "list-origin-access-controls").get("OriginAccessControlList", {}).get("Items", [])
        existing = [item for item in items if item["Name"] == name and item.get("Description") == self.config.binding]
        if existing:
            self.state.data["oac_id"] = existing[0]["Id"]
        else:
            result = self.aws.call("cloudfront", "create-origin-access-control", {"OriginAccessControlConfig": {
                "Name": name, "Description": self.config.binding, "SigningProtocol": "sigv4", "SigningBehavior": "always", "OriginAccessControlOriginType": "s3"}})
            self.state.data["oac_id"] = result["OriginAccessControl"]["Id"]

    def _write_certificate_dns(self, details: dict) -> None:
        changes = []
        for record in details["records"]:
            if not record["Name"].rstrip(".").endswith("." + self.config["domain"]):
                raise ValueError("ACM validation record falls outside the project domain")
            changes.append({"Action": "UPSERT", "ResourceRecordSet": {"Name": record["Name"], "Type": record["Type"], "TTL": 300,
                                                                     "ResourceRecords": [{"Value": record["Value"]}]}})
        self.aws.call("route53", "change-resource-record-sets", {"HostedZoneId": details["zone"], "ChangeBatch": {"Changes": changes}})

    def _upload_website(self, details: dict) -> None:
        self.owned_bucket()
        for key, sha in details["files"].items():
            path = ROOT / "dist" / key
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError("Website changed after plan approval; generate a new plan")
            content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
            self.aws.call("s3api", "put-object", {"Bucket": self.config["bucket"], "Key": key,
                "ContentType": content_type, "CacheControl": "public,max-age=60", "ServerSideEncryption": "AES256",
                "Metadata": {"sha256": sha}}, body=str(path))
        self.state.data["published_manifest"] = details["files"]

    def _ensure_distribution(self, details: dict) -> None:
        if self.state.data.get("distribution_id"):
            dist = self.owned_distribution()
            if not contains_config(dist["DistributionConfig"], details["distribution"]):
                raise ValueError("Existing distribution differs from the approved architecture; inspect and diagnose its configuration before deployment")
            return
        items = self.aws.call("cloudfront", "list-distributions").get("DistributionList", {}).get("Items", [])
        expected_comment = details["distribution"]["Comment"]
        for item in items:
            if item.get("Comment") == expected_comment:
                self.assert_tags(self.aws.call("cloudfront", "list-tags-for-resource", {"Resource": item["ARN"]})["Tags"].get("Items", []))
                self.state.data.update(distribution_id=item["Id"], distribution_arn=item["ARN"], distribution_domain=item["DomainName"])
                if not contains_config(self.owned_distribution()["DistributionConfig"], details["distribution"]):
                    raise ValueError("Recovered distribution differs from the expected configuration; inspect before continuing")
                return
        result = self.aws.call("cloudfront", "create-distribution-with-tags", {"DistributionConfigWithTags": {
            "DistributionConfig": details["distribution"], "Tags": {"Items": self.tags}}})["Distribution"]
        self.state.data.update(distribution_id=result["Id"], distribution_arn=result["ARN"], distribution_domain=result["DomainName"])
        self.state.save()

    def _secure_origin(self, _: dict) -> None:
        self.owned_bucket()
        self.owned_distribution()
        self.aws.call("s3api", "put-bucket-policy", {"Bucket": self.config["bucket"], "Policy": json.dumps(bucket_policy(self.config, self.state.data["distribution_arn"]))})

    def _write_website_dns(self, _: dict) -> None:
        self.owned_distribution()
        self.aws.call("route53", "change-resource-record-sets", {"HostedZoneId": self.state.data["hosted_zone_id"], "ChangeBatch": {"Changes": [
            {"Action": "UPSERT", "ResourceRecordSet": {"Name": self.config["domain"], "Type": family,
             "AliasTarget": {"HostedZoneId": CLOUDFRONT_ZONE, "DNSName": self.state.data["distribution_domain"], "EvaluateTargetHealth": False}}}
            for family in ("A", "AAAA")]}})

    def _invalidate(self, _: dict) -> None:
        self.owned_distribution()
        result = self.aws.call("cloudfront", "create-invalidation", {"DistributionId": self.state.data["distribution_id"],
             "InvalidationBatch": {"CallerReference": self.audit.run_id, "Paths": {"Quantity": 1, "Items": ["/*"]}}})
        self.state.data["last_invalidation"] = result.get("Invalidation", {}).get("Id")

    def _introduce_failure(self, details: dict) -> None:
        self.owned_distribution()
        self.state.data.update(failure_backup=details["before"], failure_active=True, failure_started=now(),
                               failure_config_digest=digest(details["after"]), failure_baseline=details["baseline_run"])
        self.state.save()  # Persist recovery material before making any changes.
        self.aws.call("cloudfront", "update-distribution", {"Id": self.state.data["distribution_id"],
                      "IfMatch": details["etag"], "DistributionConfig": details["after"]})

    def _restore_configuration(self, details: dict) -> None:
        self.owned_distribution()
        self.aws.call("cloudfront", "update-distribution", {"Id": self.state.data["distribution_id"],
                      "IfMatch": details["etag"], "DistributionConfig": details["after"]})
        self.state.data.update(failure_active=False, failure_restored=now())

    def _confirm_original_configuration(self, details: dict) -> None:
        current = self.owned_distribution()["DistributionConfig"]
        if digest(current) != digest(details["after"]):
            raise ValueError("Configuration changed while reconciling an interrupted recovery")
        self.state.data.update(failure_active=False, failure_reconciled=now())

    def status(self) -> dict:
        result = {"state": self.state.data}
        if self.state.data.get("certificate_arn"):
            result["certificate"] = self.aws.call("acm", "describe-certificate", {"CertificateArn": self.state.data["certificate_arn"]}, region="us-east-1")["Certificate"]
        if self.state.data.get("distribution_id"):
            result["distribution"] = self.owned_distribution()
        return result
