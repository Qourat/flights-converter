import json

import pytest

from flights_converter import OperatorPolicy, Reducer
from flights_converter.privacy import normalise_opt_outs

from .conftest import body, consume


def test_all_privacy_sentinels_are_counts_only(airports, policy, day, privacy_cases):
    values = []
    for case in privacy_cases:
        values.append(
            body(
                day,
                identity=case["icao"],
                flags=case["dbFlags"],
                callsign=case["flight"],
                r=case["r"],
                ownOp=case["ownOp"],
            )
        )
    reducer = Reducer(airports, policy, opt_outs=["hex:f00012"])
    records, summary = consume(reducer, day, values)
    rendered = json.dumps(records)
    for case in privacy_cases:
        for field in ("icao", "r", "flight", "ownOp"):
            assert case[field] not in rendered
    assert not any(r["record_type"] == "eligible_segment" for r in records)
    pieces = [r for r in records if r["record_type"] == "airport_piece"]
    assert sum(p["deps"] for p in pieces) == len(values)
    assert sum(p["arrs"] for p in pieces) == len(values)
    assert summary["n_aircraft"] == len(values)
    assert summary["hour_aircraft"] == [len(values)] + [0] * 23
    reducer.close()
    assert not reducer._tracks


@pytest.mark.parametrize("flags", [None, -1, 16, 17, "1", True, 1.0, {}, []])
def test_unknown_flags_fail_closed(policy, flags):
    assert OperatorPolicy(policy).classify("f01234", "SYNTH", flags, {"TST123"}).page_class is None


@pytest.mark.parametrize("flags,expected", [(1, "military"), (2, None), (0, None), (3, "military")])
def test_military_is_bit_one(policy, flags, expected):
    assert (
        OperatorPolicy(policy).classify("f01234", None, flags, {"PRIVATE"}).page_class == expected
    )


def test_operator_pattern_has_to_match_entire_callsign(policy):
    classifier = OperatorPolicy(policy)
    assert classifier.classify("f01234", None, 0, {"TST123A"}).page_class == "operator"
    for callsign in ("TST", "TSTABC", "ZZZ123", "TST12345", "TST123XX"):
        assert classifier.classify("f01234", None, 0, {callsign}).page_class is None
    assert classifier.classify("f01234", None, 0, {"TST123", "PRIVATE"}).page_class is None


def test_registration_opt_out_overrides_military_and_operator(policy):
    deny = normalise_opt_outs(["reg:synth-ok"])
    assert (
        OperatorPolicy(policy).classify("f01234", "SYNTH-OK", 1, {"TST123"}, deny).page_class
        is None
    )


def test_missing_flags_are_not_assumed_zero(reducer, day):
    value = body(day)
    del value["dbFlags"]
    records, _ = consume(reducer, day, [value])
    assert not any(r["record_type"] == "eligible_segment" for r in records)


def test_free_text_and_receiver_are_discarded(reducer, day):
    value = body(day, ownOp="OWNER-SENTINEL", desc="DESC-SENTINEL")
    value["trace"][0].append("RECEIVER-SENTINEL")
    value["trace"][0][8]["untrusted"] = "EXTRA-SENTINEL"
    records, _ = consume(reducer, day, [value])
    assert "SENTINEL" not in json.dumps(records)


def test_public_classifier_normalises_deny_comparisons(policy):
    classifier = OperatorPolicy(policy)
    for identity, reg, deny in (
        ("E7DEAD", None, ["hex:e7dead"]),
        ("e7dead", "sent-denied", ["reg:SENT-DENIED"]),
        ("e7dead", None, ["reg:SENT-DENIED"]),
        ("invalid", None, []),
    ):
        assert (
            classifier.classify(identity, reg, 1, {"TST123"}, normalise_opt_outs(deny)).page_class
            is None
        )
