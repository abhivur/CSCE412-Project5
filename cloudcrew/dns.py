"""Read-only DNS diagnostics. No resolver settings, hosts files or AWS records are changed."""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .core import Audit, Config, State, now


def dig_query(name: str, record_type: str, server: str | None = None, extra: tuple = ()) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", name) or name.startswith("-"):
        raise ValueError("Invalid diagnostic DNS name")
    if record_type not in ("A", "NS", "DS"):
        raise ValueError("Unsupported diagnostic record type")
    if server and (not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", server) or server.startswith("-")):
        raise ValueError("Invalid diagnostic nameserver")
    if extra not in ((), ("+norecurse",), ("+cdflag",)):
        raise ValueError("Unsupported DNS diagnostic flags")
    executable = shutil.which("dig")
    if not executable:
        return {"name": name, "type": record_type, "server": server or "system", "error": "dig is not installed"}
    command = [executable, "+time=2", "+tries=1", "+noall", "+comments", "+answer", "+authority"]
    if server:
        command.append("@" + server)
    command.extend([name, record_type, *extra])
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
        status = re.search(r"status:\s*(\w+)", process.stdout)
        records = []
        for line in process.stdout.splitlines():
            if not line or line.startswith(";"):
                continue
            fields = line.split()
            if len(fields) >= 5 and fields[2] == "IN":
                records.append({"name": fields[0], "ttl": int(fields[1]), "type": fields[3], "value": " ".join(fields[4:])})
        return {"name": name, "type": record_type, "server": server or "system", "flags": list(extra),
                "status": status.group(1) if status else "NO_RESPONSE", "records": records,
                "exit_code": process.returncode, "error": process.stderr.strip() or None, "raw": process.stdout[:7000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": name, "type": record_type, "server": server or "system", "error": str(exc)}


def classify(observations: dict, expected_ns: list[str]) -> dict:
    def values(key, kind):
        return [r["value"].rstrip(".").lower() for r in observations.get(key, {}).get("records", []) if r["type"] == kind]

    public_answers = values("cloudflare_a", "A") + values("google_a", "A")
    local_answers = observations.get("system_lookup", {}).get("addresses", [])
    parent_ns = values("parent_delegation", "NS")
    expected = {name.rstrip(".").lower() for name in expected_ns}
    if local_answers and public_answers:
        return {"classification": "dns_resolves_now", "finding": "System and public recursive lookups now return addresses.",
                "next": "Run live verification again; DNS success alone does not prove HTTPS or website correctness."}
    if public_answers and not local_answers:
        return {"classification": "local_resolver_failure", "finding": "Public resolvers return addresses, while Python's system DNS lookup fails.",
                "next": "Inspect the Mac's configured DNS, cached negative answers, VPN or DNS filtering. Keep the AWS records unchanged while testing the local resolver."}
    if parent_ns and expected and set(parent_ns) != expected:
        return {"classification": "parent_delegation_mismatch", "finding": "The parent zone delegates the domain to different nameservers than this Route 53 zone.",
                "expected_nameservers": sorted(expected), "observed_nameservers": sorted(parent_ns),
                "next": "Compare Namecheap's saved Custom DNS nameservers with the expected set and verify registrar delegation propagation."}
    if observations.get("google_a", {}).get("status") == "SERVFAIL" and values("google_checking_disabled", "A"):
        return {"classification": "possible_dnssec_failure", "finding": "Google's validating lookup fails, but disabling DNSSEC checking returns addresses.",
                "next": "Inspect the parent DS record and Route 53 signing configuration before proposing any DNSSEC change."}
    if values("authoritative_a", "A") and parent_ns and set(parent_ns) == expected:
        return {"classification": "recursive_cache_or_propagation", "finding": "The parent delegates to the expected nameservers and the authoritative server returns addresses; recursive lookups have not succeeded.",
                "next": "Inspect recorded recursive statuses and negative-answer SOA TTLs; retry resolution after caches expire. Do not recreate working infrastructure."}
    return {"classification": "insufficient_dns_evidence", "finding": "The available observations do not isolate the DNS failure.",
            "next": "Inspect query errors, parent delegation, domain registration status, and recursive/authoritative responses. No configuration change has been made."}


def diagnose_dns(config: Config, audit: Audit, state: State | None = None) -> dict:
    config.validate(cloud=True)
    state = state or State(config)
    domain = config["domain"]
    observations = {}
    try:
        observations["system_lookup"] = {"addresses": sorted({row[4][0] for row in socket.getaddrinfo(domain, 443)})}
    except OSError as exc:
        observations["system_lookup"] = {"addresses": [], "error": str(exc)}
    requests = {
        "system_a": (domain, "A", None),
        "cloudflare_a": (domain, "A", "1.1.1.1"),
        "google_a": (domain, "A", "8.8.8.8"),
        "cloudflare_ns": (domain, "NS", "1.1.1.1"),
        "parent_nameservers": (domain.rsplit(".", 1)[1], "NS", "1.1.1.1"),
    }
    nameservers = state.data.get("nameservers", [])
    if nameservers:
        requests["authoritative_a"] = (domain, "A", nameservers[0])
    if state.data.get("distribution_domain"):
        requests["cloudfront_system_a"] = (state.data["distribution_domain"], "A", None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {key: pool.submit(dig_query, *args) for key, args in requests.items()}
        for key, future in futures.items():
            observations[key] = future.result()
    parents = [r["value"] for r in observations["parent_nameservers"].get("records", []) if r["type"] == "NS"]
    if parents:
        observations["parent_delegation"] = dig_query(domain, "NS", parents[0], ("+norecurse",))
        observations["parent_ds"] = dig_query(domain, "DS", parents[0], ("+norecurse",))
    if observations["google_a"].get("status") == "SERVFAIL":
        observations["google_checking_disabled"] = dig_query(domain, "A", "8.8.8.8", ("+cdflag",))
    result = {"timestamp": now(), "domain": domain, "status": "diagnostic_complete", "read_only": True,
              "diagnosis": classify(observations, nameservers), "observations": observations,
              "scope": "Measured DNS diagnostic observations; does not bypass system DNS or establish a passing website baseline."}
    result["artifact"] = audit.artifact("dns-diagnostic.json", result)
    audit.emit("diagnosis", "dns_diagnostic", result)
    return result
