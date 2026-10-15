"""Positive classification, with exclusions taking precedence over eligibility."""

import re
from dataclasses import dataclass

from .errors import DataRejected


@dataclass(frozen=True)
class Classification:
    page_class: str | None
    rule: str | None


class OperatorPolicy:
    """Caller-reviewed, versioned full-callsign patterns; no prefix inference."""

    def __init__(self, config: dict):
        if not isinstance(config, dict) or not isinstance(config.get("version"), str):
            raise DataRejected("operator_config")
        self.version = config["version"]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.version):
            raise DataRejected("operator_config")
        rules = config.get("rules")
        if not isinstance(rules, list):
            raise DataRejected("operator_config")
        self.rules = []
        try:
            for rule in rules:
                code, pattern = rule["code"], rule["pattern"]
                if not re.fullmatch(r"[A-Z]{3}", code) or len(pattern) > 128:
                    raise DataRejected("operator_config")
                self.rules.append((code, re.compile(pattern, re.ASCII)))
        except (KeyError, TypeError, re.error):
            raise DataRejected("operator_config") from None

    def operator(self, callsign: str) -> str | None:
        matches = {code for code, pattern in self.rules if pattern.fullmatch(callsign)}
        return next(iter(matches)) if len(matches) == 1 else None

    def classify(
        self,
        icao: str,
        reg: str | None,
        flags: object,
        callsigns: set[str],
        opt_outs: frozenset[str] = frozenset(),
    ) -> Classification:
        denied = (
            not isinstance(icao, str)
            or not re.fullmatch(r"[0-9a-fA-F]{6}", icao)
            or f"hex:{icao.lower()}" in opt_outs
            or (reg is not None and (not isinstance(reg, str) or f"reg:{reg.upper()}" in opt_outs))
            or (reg is None and any(key.startswith("reg:") for key in opt_outs))
        )
        # Missing, malformed, or unknown bits are unavailable privacy information.
        if denied or type(flags) is not int or flags < 0 or flags & ~15 or flags & 12:
            return Classification(None, None)
        if flags & 1:
            return Classification("military", "dbFlags&1")
        if not callsigns or any(
            not isinstance(c, str) or not re.fullmatch(r"[A-Z0-9-]{1,16}", c) for c in callsigns
        ):
            return Classification(None, None)
        operators = {self.operator(c) for c in callsigns}
        if callsigns and None not in operators and len(operators) == 1:
            return Classification("operator", f"{self.version}:{next(iter(operators))}")
        return Classification(None, None)


def normalise_opt_outs(keys) -> frozenset[str]:
    result = set()
    for key in keys:
        if not isinstance(key, str):
            raise DataRejected("opt_out_config")
        if re.fullmatch(r"hex:[0-9a-fA-F]{6}", key):
            result.add(key.lower())
        elif re.fullmatch(r"reg:[A-Za-z0-9-]{1,16}", key):
            result.add(key.upper().replace("REG:", "reg:", 1))
        else:
            raise DataRejected("opt_out_config")
    return frozenset(result)
