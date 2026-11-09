import unittest
from unittest.mock import patch

from cloudcrew.dns import classify, dig_query


def response(kind, values, status="NOERROR"):
    return {"status": status, "records": [{"type": kind, "value": value} for value in values]}


class DnsTests(unittest.TestCase):
    def test_public_success_and_system_failure_is_local_resolver_evidence(self):
        observations = {"system_lookup": {"addresses": []}, "cloudflare_a": response("A", ["192.0.2.1"])}
        self.assertEqual(classify(observations, [])["classification"], "local_resolver_failure")

    def test_parent_mismatch_identifies_delegation_problem(self):
        observations = {"parent_delegation": response("NS", ["wrong.example."])}
        self.assertEqual(classify(observations, ["expected.example"])["classification"], "parent_delegation_mismatch")

    def test_authority_success_does_not_count_as_public_dns_success(self):
        observations = {"authoritative_a": response("A", ["192.0.2.1"]),
                        "parent_delegation": response("NS", ["expected.example."]),
                        "google_a": response("A", [], "NXDOMAIN")}
        self.assertEqual(classify(observations, ["expected.example"])["classification"], "recursive_cache_or_propagation")

    def test_validating_failure_with_checking_disabled_success_flags_dnssec(self):
        observations = {"google_a": response("A", [], "SERVFAIL"), "google_checking_disabled": response("A", ["192.0.2.1"])}
        self.assertEqual(classify(observations, [])["classification"], "possible_dnssec_failure")

    def test_unavailable_queries_produce_uncertainty_not_a_fabricated_cause(self):
        self.assertEqual(classify({}, [])["classification"], "insufficient_dns_evidence")

    def test_query_arguments_reject_command_options_or_arbitrary_flags(self):
        for args in [("-command", "A"), ("example.test", "TXT"), ("example.test", "A", "-bad"),
                     ("example.test", "A", None, ("+unapproved",))]:
            with self.assertRaises(ValueError):
                dig_query(*args)


if __name__ == "__main__":
    unittest.main()
