from __future__ import annotations

import hashlib
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .core import Audit, Config, State, now
from .deploy import Deployment, origin_domain
from .site import PAGES


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def observed_default_document_failure(report: dict) -> bool:
    """The exercise is observed only when root fails and the actual index still works."""
    checks = {check["name"]: check for check in report.get("checks", [])}
    root = checks.get("page:/", {})
    explicit = checks.get("page:index.html", {})
    return (report.get("status") == "failed" and root.get("observation", {}).get("status_code") == 404
            and explicit.get("passed") is True and explicit.get("observation", {}).get("status_code") == 200)


def fetch(url: str) -> dict:
    """Anonymous request; no AWS signing, cookies, credentials, or redirects."""
    request = urllib.request.Request(url, headers={"User-Agent": "GBMCloud-Project5-Verification/2.0"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(2_000_000)
        headers = {k.lower(): v for k, v in response.headers.items()}
        return {"url": url, "status_code": response.code, "headers": headers,
                "body_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                "html_title_present": b"<title>" in body.lower(),
                "custom_404_present": b'data-error-page="404"' in body}


def tls_evidence(domain: str) -> dict:
    context = ssl.create_default_context()  # CERT_REQUIRED and hostname verification enabled.
    with socket.create_connection((domain, 443), timeout=15) as connection:
        with context.wrap_socket(connection, server_hostname=domain) as tls:
            certificate = tls.getpeercert()
            return {"domain": domain, "hostname_verified": context.check_hostname,
                    "chain_verified": context.verify_mode == ssl.CERT_REQUIRED,
                    "protocol": tls.version(), "cipher": tls.cipher(),
                    "subject_alt_names": certificate.get("subjectAltName"),
                    "not_after": certificate.get("notAfter"), "issuer": certificate.get("issuer")}


def verify_live(config: Config, audit: Audit, deployment: Deployment | None = None) -> dict:
    config.validate(cloud=True)
    deployment = deployment or Deployment(config, audit)
    deployment.state.require("distribution_id", "distribution_arn", "distribution_domain", "published_manifest")
    domain = config["domain"]
    checks = []

    def test(name, action, condition):
        try:
            observation = action()
            passed = bool(condition(observation))
            checks.append({"name": name, "passed": passed, "observation": observation})
        except Exception as exc:
            checks.append({"name": name, "passed": False, "error": str(exc)})
        audit.emit("verification", "verification_check", checks[-1])

    test("domain_dns", lambda: sorted({row[4][0] for row in socket.getaddrinfo(domain, 443)}), bool)
    test("certificate_and_tls", lambda: tls_evidence(domain), lambda result: result["hostname_verified"] and result["chain_verified"] and result["protocol"] in ("TLSv1.2", "TLSv1.3"))
    test("http_redirect", lambda: fetch(f"http://{domain}/"),
         lambda result: result["status_code"] in (301, 302, 307, 308)
         and urllib.parse.urlsplit(result["headers"].get("location", "")).scheme == "https"
         and urllib.parse.urlsplit(result["headers"].get("location", "")).netloc == domain)
    expected = deployment.state.data["published_manifest"]
    for page in ["/"] + list(PAGES.values()):
        key = "index.html" if page == "/" else page
        test("page:" + page, lambda page=page: fetch(f"https://{domain}/{page.lstrip('/')}"),
             lambda result, key=key: result["status_code"] == 200 and result["html_title_present"]
             and result["body_sha256"] == expected.get(key))
    # Assets are part of the deployed manifest, and must match the files actually uploaded.
    for key in expected:
        if key.startswith("assets/"):
            test("asset:" + key, lambda key=key: fetch(f"https://{domain}/{key}"),
                 lambda result, key=key: result["status_code"] == 200 and result["body_sha256"] == expected[key])
    test("cloudfront_delivery", lambda: fetch(f"https://{domain}/index.html"),
         lambda result: result["status_code"] == 200 and "cloudfront" in result["headers"].get("via", "").lower()
         and bool(result["headers"].get("x-amz-cf-id")) and result["body_sha256"] == expected.get("index.html"))
    test("custom_error_page", lambda: fetch(f"https://{domain}/missing-{uuid.uuid4().hex}.html"),
         lambda result: result["status_code"] == 404 and result["custom_404_present"])
    test("anonymous_s3_denied", lambda: fetch(f"https://{origin_domain(config)}/index.html"),
         lambda result: result["status_code"] == 403)
    test("s3_public_access_block", lambda: deployment.aws.call("s3api", "get-public-access-block", {"Bucket": config["bucket"]}),
         lambda result: all(result["PublicAccessBlockConfiguration"].get(key) is True for key in
                            ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")))
    test("s3_policy_not_public", lambda: deployment.aws.call("s3api", "get-bucket-policy-status", {"Bucket": config["bucket"]}),
         lambda result: result["PolicyStatus"]["IsPublic"] is False)
    test("bucket_policy_scoped_to_distribution", lambda: json.loads(deployment.aws.call("s3api", "get-bucket-policy", {"Bucket": config["bucket"]})["Policy"]),
         lambda result: any(s.get("Effect") == "Allow" and s.get("Principal") == {"Service": "cloudfront.amazonaws.com"}
                           and s.get("Action") == "s3:GetObject" and s.get("Condition", {}).get("StringEquals", {}).get("AWS:SourceArn") == deployment.state.data["distribution_arn"]
                           for s in result.get("Statement", [])))

    def distribution_secure(dist):
        cfg = dist["DistributionConfig"]
        origin = cfg["Origins"]["Items"][0]
        return (dist["Status"] == "Deployed" and cfg["Enabled"] and origin.get("OriginAccessControlId") == deployment.state.data.get("oac_id")
                and origin["DomainName"] == origin_domain(config) and cfg["DefaultRootObject"] == "index.html"
                and cfg["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
                and cfg["ViewerCertificate"].get("ACMCertificateArn") == deployment.state.data.get("certificate_arn")
                and cfg["ViewerCertificate"].get("MinimumProtocolVersion") == "TLSv1.2_2021"
                and config["domain"] in cfg.get("Aliases", {}).get("Items", []))

    test("distribution_configuration", deployment.owned_distribution, distribution_secure)
    test("oac_always_signs", lambda: deployment.aws.call("cloudfront", "get-origin-access-control", {"Id": deployment.state.data["oac_id"]}),
         lambda result: result["OriginAccessControl"]["OriginAccessControlConfig"]["SigningBehavior"] == "always")
    result = {"timestamp": now(), "status": "passed" if all(c["passed"] for c in checks) else "failed", "domain": domain,
              "checks": checks, "passed": sum(c["passed"] for c in checks), "total": len(checks), "evidence_type": "live_aws_and_anonymous_network_requests"}
    result["artifact"] = audit.artifact(f"verification-{audit.seq:04d}.json", result)
    audit.artifact("verification.json", result)  # Latest result, while preserving every observation round.
    if result["status"] == "passed":
        deployment.state.data["last_verified_run"] = audit.run_id
        deployment.state.save()
    return result
