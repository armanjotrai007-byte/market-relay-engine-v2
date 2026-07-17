"""Deterministic, research-only shadow context policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import re
from typing import TYPE_CHECKING

from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.common.time import to_utc_iso
from market_relay_engine.context.decision_context import DecisionContext
from market_relay_engine.contracts.context import (
    ShadowContextAction,
    ShadowContextPolicyEvaluation,
)
from market_relay_engine.contracts.model import ModelSignal
from market_relay_engine.contracts.risk import RiskDecision

if TYPE_CHECKING:
    from market_relay_engine.context.research_projection import (
        ResearchEvidence,
        ResearchEvidenceSelection,
    )


_MATCH_KEY_RE = re.compile(
    r"(?:AI_EVENT_TYPE|DETERMINISTIC_EVENT_TYPE|FLAG_TYPE):[A-Z0-9_]+"
)


class ShadowEvaluationError(ValueError):
    """Raised when a shadow policy or evaluation input is incoherent."""


@dataclass(frozen=True, kw_only=True)
class ShadowContextRule:
    """One exact-match research rule in declared policy priority order."""

    rule_id: str
    match_keys: tuple[str, ...]
    action: ShadowContextAction
    reason_code: str
    proposed_size_factor: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _required_string(self.rule_id, "rule_id"))
        object.__setattr__(
            self,
            "reason_code",
            _required_string(self.reason_code, "reason_code"),
        )
        if not isinstance(self.action, ShadowContextAction):
            raise ShadowEvaluationError("action must be a ShadowContextAction")
        if self.action is ShadowContextAction.NO_CHANGE:
            raise ShadowEvaluationError(
                "NO_CHANGE is the no-match default and cannot be an active rule action"
            )
        keys = tuple(self.match_keys)
        if not keys:
            raise ShadowEvaluationError("match_keys must contain at least one key")
        if len(set(keys)) != len(keys):
            raise ShadowEvaluationError("match_keys must not contain duplicates")
        for key in keys:
            if not isinstance(key, str) or _MATCH_KEY_RE.fullmatch(key) is None:
                raise ShadowEvaluationError(
                    "match keys must use AI_EVENT_TYPE, "
                    "DETERMINISTIC_EVENT_TYPE, or FLAG_TYPE"
                )
        object.__setattr__(self, "match_keys", keys)
        if self.action is ShadowContextAction.REDUCE_SIZE:
            factor = self.proposed_size_factor
            if (
                isinstance(factor, bool)
                or not isinstance(factor, (int, float))
                or not 0.0 < float(factor) <= 1.0
            ):
                raise ShadowEvaluationError(
                    "REDUCE_SIZE requires proposed_size_factor greater than 0 and at most 1"
                )
            object.__setattr__(self, "proposed_size_factor", float(factor))
        elif self.proposed_size_factor is not None:
            raise ShadowEvaluationError(
                "proposed_size_factor is only valid for REDUCE_SIZE"
            )

    def to_config_payload(self) -> dict[str, object]:
        """Return the stable, JSON-safe rule configuration."""
        return {
            "rule_id": self.rule_id,
            "match_keys": list(self.match_keys),
            "action": self.action.value,
            "reason_code": self.reason_code,
            "proposed_size_factor": self.proposed_size_factor,
        }


@dataclass(frozen=True, kw_only=True)
class ShadowContextPolicy:
    """Small injected policy used only for hypothetical research results."""

    policy_version: str = "shadow_no_change_v1"
    rules: tuple[ShadowContextRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version",
            _required_string(self.policy_version, "policy_version"),
        )
        rules = tuple(self.rules)
        if not all(isinstance(rule, ShadowContextRule) for rule in rules):
            raise ShadowEvaluationError("rules must contain ShadowContextRule values")
        rule_ids = [rule.rule_id for rule in rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise ShadowEvaluationError("rule_id values must be unique within a policy")
        object.__setattr__(self, "rules", rules)

    @property
    def policy_config_hash(self) -> str:
        """Hash the ordered rule configuration independently of its version label."""
        return _sha256_payload(
            {"rules": [rule.to_config_payload() for rule in self.rules]}
        )


def evaluate_shadow_context(
    *,
    model_signal: ModelSignal,
    decision_context: DecisionContext,
    evidence_selection: "ResearchEvidenceSelection",
    policy: ShadowContextPolicy | None = None,
    risk_decision: RiskDecision | None = None,
) -> ShadowContextPolicyEvaluation:
    """Evaluate one policy without changing the real model or risk result."""
    if not isinstance(model_signal, ModelSignal):
        raise ShadowEvaluationError("model_signal must be a ModelSignal")
    if not isinstance(decision_context, DecisionContext):
        raise ShadowEvaluationError("decision_context must be a DecisionContext")
    from market_relay_engine.context.research_projection import (
        ResearchEvidenceSelection,
        build_shadow_context_fingerprint,
    )

    if not isinstance(evidence_selection, ResearchEvidenceSelection):
        raise ShadowEvaluationError(
            "evidence_selection must be a ResearchEvidenceSelection"
        )
    resolved_policy = ShadowContextPolicy() if policy is None else policy
    if not isinstance(resolved_policy, ShadowContextPolicy):
        raise ShadowEvaluationError("policy must be a ShadowContextPolicy")
    if decision_context.ticker != model_signal.ticker:
        raise ShadowEvaluationError("model signal and decision context ticker must match")
    if decision_context.evaluation_time != model_signal.signal_time:
        raise ShadowEvaluationError(
            "DecisionContext evaluation_time must equal ModelSignal signal_time"
        )
    if evidence_selection.decision_time != decision_context.evaluation_time:
        raise ShadowEvaluationError(
            "evidence selection time must equal DecisionContext evaluation_time"
        )
    if risk_decision is not None:
        if not isinstance(risk_decision, RiskDecision):
            raise ShadowEvaluationError("risk_decision must be a RiskDecision")
        if risk_decision.model_signal_id != model_signal.signal_id:
            raise ShadowEvaluationError(
                "risk_decision.model_signal_id must match model_signal.signal_id"
            )
        if risk_decision.ticker != model_signal.ticker:
            raise ShadowEvaluationError("risk decision and model signal ticker must match")

    winning_rule: ShadowContextRule | None = None
    winning_evidence: tuple["ResearchEvidence", ...] = ()
    for rule in resolved_policy.rules:
        matches = tuple(
            evidence
            for evidence in evidence_selection.selected_evidence
            if evidence.policy_match_key in rule.match_keys
        )
        if matches:
            winning_rule = rule
            winning_evidence = matches
            break

    if winning_rule is None:
        action = ShadowContextAction.NO_CHANGE
        proposed_size_factor = None
        reason_codes: list[str] = []
    else:
        action = winning_rule.action
        proposed_size_factor = winning_rule.proposed_size_factor
        reason_codes = [winning_rule.reason_code]

    matched_event_ids: list[str] = []
    matched_flag_ids: list[str] = []
    for evidence in winning_evidence:
        category = getattr(evidence.category, "value", evidence.category)
        if category == "FLAG":
            matched_flag_ids.append(evidence.evidence_id)
        elif category in {"AI_EVENT", "DETERMINISTIC_EVENT"}:
            matched_event_ids.append(evidence.evidence_id)
        else:
            raise ShadowEvaluationError("selected evidence has an unsupported category")

    shadow_context_fingerprint = build_shadow_context_fingerprint(
        decision_context=decision_context,
        evidence_selection=evidence_selection,
    )
    policy_config_hash = resolved_policy.policy_config_hash
    risk_decision_id = (
        None if risk_decision is None else risk_decision.risk_decision_id
    )
    identity_payload = {
        "model_signal_id": model_signal.signal_id,
        "risk_decision_id": risk_decision_id,
        "decision_evaluation_time": to_utc_iso(decision_context.evaluation_time),
        "shadow_context_fingerprint": shadow_context_fingerprint,
        "policy_version": resolved_policy.policy_version,
        "policy_config_hash": policy_config_hash,
    }
    shadow_evaluation_id = f"shadow_evaluation_{_sha256_payload(identity_payload)}"
    return ShadowContextPolicyEvaluation(
        shadow_evaluation_id=shadow_evaluation_id,
        model_signal_id=model_signal.signal_id,
        risk_decision_id=risk_decision_id,
        decision_evaluation_time=decision_context.evaluation_time,
        matched_context_event_ids=matched_event_ids,
        matched_context_flag_ids=matched_flag_ids,
        shadow_context_fingerprint=shadow_context_fingerprint,
        policy_version=resolved_policy.policy_version,
        policy_config_hash=policy_config_hash,
        hypothetical_action=action,
        proposed_size_factor=proposed_size_factor,
        reason_codes=reason_codes,
        trace_id=model_signal.trace_id,
    )


def _sha256_payload(payload: object) -> str:
    return sha256(to_json_string(payload).encode("utf-8")).hexdigest()


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowEvaluationError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "ShadowContextPolicy",
    "ShadowContextRule",
    "ShadowEvaluationError",
    "evaluate_shadow_context",
]
