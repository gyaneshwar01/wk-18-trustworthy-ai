"""Auditable, deliberately narrow guardrails for a public handbook assistant.

These rules are not a general safety classifier or a semantic entailment model.
All matched spans are internal; public diagnostics contain category names only.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import unicodedata
from typing import Any

MAX_INPUT = 4_000
MAX_OUTPUT = 32_000
UNKNOWN = "The approved handbook does not contain that information."
FALLBACK = "I could not produce a verified answer. Please contact the handbook administrator."


def normalize(text: str) -> str:
    """Normalize width and invisible format characters before all input guards."""
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(c) != "Cf")


PII_PATTERNS = {
    "email": r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "phone": r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?!\w)",
    "identification": r"\b(?:passport|national\s+id|citizenship\s+(?:id|number))\s*[:#-]?\s*[A-Z0-9][A-Z0-9-]{5,19}\b",
    "card": r"(?<!\d)(?:\d[ -]?){15}\d(?!\d)",
}
SECRET_PATTERNS = {
    "api_key": r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b",
    "aws_access_key": r"\bAKIA[A-Z0-9]{16}\b",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "assigned_secret": r"\b(?:api[_ -]?key|password|access[_ -]?token|secret)\s*[:=]\s*[^\s,;]{4,}",
}
INJECTION_PATTERNS = {
    "override": r"\b(?:ignore|disregard|forget)\s+(?:(?:all|the|your)\s+)?(?:previous|prior|above|system)\s+(?:instructions|rules|prompt)\b",
    "extract_prompt": r"\b(?:reveal|print|show|repeat|output)\s+(?:(?:the|your)\s+)?(?:system|developer|hidden)\s+(?:prompt|instructions|message|secrets)\b",
    "role_override": r"\b(?:you are now|act as)\s+(?:an?\s+)?(?:unrestricted|unfiltered|unconstrained)\b",
    "role_delimiter": r"(?:\[/?INST\]|<\|(?:system|assistant|im_start)\|>|\[system\])",
    "bypass": r"\b(?:bypass|disable)\s+(?:(?:all|the|your)\s+)?(?:safety|guardrails|restrictions|filters)\b",
}
TOPIC_PATTERNS = {
    "crisis": r"\b(?:kill myself|end my life|suicide methods|self[- ]harm instructions)\b",
    "harmful": r"\b(?:build a bomb|make a bomb|commit fraud|steal passwords|hack into|launder money)\b",
    "medical": r"\b(?:diagnose|diagnosis|dosage|prescribe|medical advice)\b",
    "legal": r"\b(?:legal advice|sue my|criminal liability)\b",
    "financial": r"\b(?:investment advice|which stock|invest my savings)\b",
}


def matching_rules(text: str, rules: dict[str, str]) -> list[str]:
    return [name for name, pattern in rules.items()
            if re.search(pattern, normalize(text), re.IGNORECASE)]


def sensitive_information(text: str) -> list[str]:
    return matching_rules(text, {**PII_PATTERNS, **SECRET_PATTERNS})


def redact(text: str) -> tuple[str, list[str]]:
    text = normalize(text)
    rules = {**PII_PATTERNS, **SECRET_PATTERNS}
    kinds = matching_rules(text, rules)
    # Merge overlapping intervals before replacement: sequential regex substitutions
    # can otherwise leave part of an email or credential exposed.
    spans = sorted((m.start(), m.end()) for pattern in rules.values()
                   for m in re.finditer(pattern, text, re.IGNORECASE))
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + "[REDACTED]" + text[end:]
    return text, kinds


@dataclass(frozen=True)
class InputDecision:
    action: str
    sanitized: str
    categories: tuple[str, ...]
    pii_types: tuple[str, ...]


def inspect_input(prompt: Any) -> InputDecision:
    if type(prompt) is not str or not prompt.strip() or len(prompt) > MAX_INPUT:
        return InputDecision("refuse", "", ("invalid_input",), ())
    prompt = normalize(prompt)
    sanitized, pii = redact(prompt)
    injections = matching_rules(prompt, INJECTION_PATTERNS)
    topics = matching_rules(prompt, TOPIC_PATTERNS)
    # Crisis handling has priority over a coincident injection so the caller receives
    # a supportive fixed response. No special category is forwarded to a provider.
    if "crisis" in topics:
        return InputDecision("support", "", tuple(topics + injections), tuple(pii))
    if injections or "harmful" in topics:
        return InputDecision("refuse", "", tuple(topics + injections), tuple(pii))
    if topics:
        return InputDecision("escalate", "", tuple(topics), tuple(pii))
    return InputDecision("redact" if pii else "allow", sanitized, (), tuple(pii))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def parse_json(raw: str) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")))


def schema_errors(value: Any) -> list[str]:
    if type(value) is not dict:
        return ["object_required"]
    if set(value) != {"status", "answer", "claims"}:
        return ["unexpected_or_missing_fields"]
    if type(value["status"]) is not str or value["status"] not in {"answer", "unknown"}:
        return ["invalid_status"]
    if type(value["answer"]) is not str or not 1 <= len(value["answer"]) <= 4_000:
        return ["invalid_answer"]
    if type(value["claims"]) is not list or len(value["claims"]) > 5:
        return ["invalid_claims"]
    for claim in value["claims"]:
        if type(claim) is not dict or set(claim) != {"text", "source_id"}:
            return ["invalid_claim_fields"]
        if type(claim["text"]) is not str or not 1 <= len(claim["text"]) <= 1_000:
            return ["invalid_claim_text"]
        if type(claim["source_id"]) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", claim["source_id"]):
            return ["invalid_source_id"]
    if value["status"] == "answer" and not value["claims"]:
        return ["answer_requires_claim"]
    if value["status"] == "unknown" and (value["claims"] or value["answer"] != UNKNOWN):
        return ["invalid_abstention"]
    return []


def grounding_errors(value: dict[str, Any], references: dict[str, str]) -> list[str]:
    """Check exact approved facts, never substring matches or confidence language.

    Call only after schema validation. This deliberately rejects valid paraphrases.
    It establishes provenance, not truth, completeness, or relevance to the query.
    """
    if value["status"] == "unknown":
        return []
    if any(c["source_id"] not in references for c in value["claims"]):
        return ["unknown_source"]
    if any(c["text"] != references[c["source_id"]] for c in value["claims"]):
        return ["unsupported_claim"]
    if value["answer"] != " ".join(c["text"] for c in value["claims"]):
        return ["answer_claim_mismatch"]
    if len({c["source_id"] for c in value["claims"]}) != len(value["claims"]):
        return ["duplicate_claim"]
    return []


@dataclass(frozen=True)
class OutputDecision:
    accepted: bool
    value: dict[str, Any] | None
    reasons: tuple[str, ...]


def inspect_output(raw: Any, references: dict[str, str]) -> OutputDecision:
    if type(raw) is not str or len(raw) > MAX_OUTPUT:
        return OutputDecision(False, None, ("invalid_output_size_or_type",))
    try:
        value = parse_json(raw)
    except (ValueError, RecursionError):
        return OutputDecision(False, None, ("invalid_json",))
    errors = schema_errors(value)
    if errors:
        return OutputDecision(False, None, tuple(errors))
    # Inspect every string field (including citation IDs), after JSON unescaping.
    # Unicode normalization here catches full-width and zero-width evasion.
    rendered = " ".join([value["answer"]] +
                        [s for c in value["claims"] for s in c.values()])
    if sensitive_information(rendered):
        return OutputDecision(False, None, ("sensitive_output",))
    errors = grounding_errors(value, references)
    return OutputDecision(not errors, value if not errors else None, tuple(errors))
