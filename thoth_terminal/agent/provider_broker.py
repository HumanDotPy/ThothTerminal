from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import llm_available, provider_request_profile
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.models import AgentRequestManifest, AgentRequestOutcome


BROKER_VERSION = "provider-broker-v1"


def _text(value: object) -> str:
    return str(value or "").strip()


def _csv(value: object) -> list[str]:
    return [part.strip().lower() for part in str(value or "").split(",") if part.strip()]


def deepseek_peak_pricing_active(now_utc: datetime) -> bool:
    """Return whether the configured DeepSeek price-avoidance window is active."""

    hour = now_utc.astimezone(timezone.utc).hour
    return (1 <= hour < 4) or (6 <= hour < 10)


def provider_temporally_blocked(provider: str, now_utc: datetime) -> bool:
    if _text(getattr(settings, "cloud_research_schedule_strategy", "provider_aware")).lower() != "provider_aware":
        return False
    provider_v = _text(provider).lower()
    off_peak = set(_csv(getattr(settings, "cloud_research_off_peak_providers", "deepseek")))
    return provider_v == "deepseek" and provider_v in off_peak and deepseek_peak_pricing_active(now_utc)


def heartbeat_local_times_for_provider(provider: str) -> str:
    strategy = _text(getattr(settings, "cloud_research_schedule_strategy", "provider_aware")).lower()
    distributed = set(_csv(getattr(settings, "cloud_research_distributed_providers", "meta")))
    if strategy == "provider_aware" and _text(provider).lower() in distributed:
        return _text(
            getattr(
                settings,
                "cloud_research_distributed_heartbeat_local_times",
                "08:00,20:00",
            )
        )
    return _text(getattr(settings, "cloud_research_heartbeat_local_times", "08:00,20:00"))


def _provider_model(provider: str, requested_model: str | None = None) -> str:
    if _text(requested_model):
        return _text(requested_model)
    provider_v = _text(provider).lower()
    setting_name = {
        "meta": "meta_model",
        "deepseek": "deepseek_model",
        "nous": "nous_model",
        "openrouter": "openrouter_model",
        "openai": "openai_model",
        "anthropic": "anthropic_model",
        "local": "agent_llm_model",
    }.get(provider_v)
    return _text(getattr(settings, setting_name, "")) if setting_name else ""


def _cache_policy(provider: str) -> dict[str, Any]:
    provider_v = _text(provider).lower()
    profile = provider_request_profile(provider_v, prompt_cache_key="thoth:stable-prefix")
    if provider_v == "meta":
        mechanism = "explicit_key_and_retention" if profile.get("prompt_cache_retention") else "automatic_prefix"
    elif provider_v == "deepseek":
        mechanism = "automatic_prefix"
    elif provider_v == "openai":
        mechanism = "explicit_key"
    else:
        mechanism = "provider_default_or_unknown"
    return {
        "mechanism": mechanism,
        "endpoint_kind": profile.get("endpoint_kind"),
        "retention": profile.get("prompt_cache_retention"),
    }


def _cost_class(provider: str, model: str) -> str:
    provider_v = _text(provider).lower()
    model_v = _text(model).lower()
    if provider_v == "meta" and "contributor" in model_v:
        return "contributor_rate_limited"
    if provider_v == "openrouter" and (model_v == "openrouter/free" or model_v.endswith(":free")):
        return "free"
    if provider_v == "deepseek":
        return "metered_cache_discounted"
    return "unknown_or_provider_managed"


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    sample_size: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_outcome_at: str | None = None

    @property
    def degraded(self) -> bool:
        threshold = max(1, int(getattr(settings, "provider_broker_failure_threshold", 2) or 2))
        return self.consecutive_failures >= threshold

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "sample_size": self.sample_size,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_outcome_at": self.last_outcome_at,
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class ProviderCandidate:
    provider: str
    model: str
    available: bool
    temporally_blocked: bool
    health: ProviderHealth
    cache_policy: dict[str, Any]
    cost_class: str
    score: float
    accepted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "available": self.available,
            "temporally_blocked": self.temporally_blocked,
            "health": self.health.as_dict(),
            "cache_policy": dict(self.cache_policy),
            "cost_class": self.cost_class,
            "score": self.score,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ProviderBrokerDecision:
    task_kind: str
    requested_provider: str
    requested_model: str
    selected_provider: str | None
    selected_model: str | None
    strategy: str
    explicit: bool
    scheduled: bool
    reason: str
    candidates: tuple[ProviderCandidate, ...]
    decided_at: str

    @property
    def selected(self) -> bool:
        return bool(self.selected_provider and self.selected_model)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": BROKER_VERSION,
            "task_kind": self.task_kind,
            "requested_provider": self.requested_provider,
            "requested_model": self.requested_model,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "strategy": self.strategy,
            "explicit": self.explicit,
            "scheduled": self.scheduled,
            "reason": self.reason,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "decided_at": self.decided_at,
        }


def provider_health_snapshot(
    session: Session | None,
    *,
    providers: list[str],
    now: datetime | None = None,
) -> dict[str, ProviderHealth]:
    provider_values = sorted({_text(provider).lower() for provider in providers if _text(provider)})
    empty = {provider: ProviderHealth(provider=provider) for provider in provider_values}
    if session is None or not provider_values:
        return empty
    now_v = now or utcnow()
    lookback_hours = max(1, int(getattr(settings, "provider_broker_health_lookback_hours", 24) or 24))
    sample_limit = max(1, int(getattr(settings, "provider_broker_health_sample_size", 12) or 12))
    rows = (
        session.query(AgentRequestManifest.provider, AgentRequestOutcome.status, AgentRequestOutcome.created_at)
        .join(AgentRequestOutcome, AgentRequestOutcome.manifest_id == AgentRequestManifest.id)
        .filter(
            AgentRequestManifest.provider.in_(provider_values),
            AgentRequestOutcome.created_at >= now_v - timedelta(hours=lookback_hours),
        )
        .order_by(AgentRequestOutcome.created_at.desc(), AgentRequestOutcome.id.desc())
        .limit(sample_limit * len(provider_values))
        .all()
    )
    grouped: dict[str, list[tuple[str, datetime]]] = {provider: [] for provider in provider_values}
    for provider, status, created_at in rows:
        provider_v = _text(provider).lower()
        if provider_v in grouped and len(grouped[provider_v]) < sample_limit:
            grouped[provider_v].append((_text(status).lower(), created_at))
    result: dict[str, ProviderHealth] = {}
    for provider, observations in grouped.items():
        successes = sum(1 for status, _ in observations if status == "success")
        failures = len(observations) - successes
        streak = 0
        for status, _ in observations:
            if status == "success":
                break
            streak += 1
        result[provider] = ProviderHealth(
            provider=provider,
            sample_size=len(observations),
            successes=successes,
            failures=failures,
            consecutive_failures=streak,
            last_outcome_at=observations[0][1].isoformat() if observations else None,
        )
    return result


def select_provider(
    session: Session | None,
    *,
    task_kind: str,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    explicit: bool = False,
    scheduled: bool = False,
    now: datetime | None = None,
) -> ProviderBrokerDecision:
    """Resolve a provider without obscuring why or what was selected.

    Explicit operator selections never fall back. Scheduled configured routes may
    fall back only after health/availability failure; a provider-aware time block
    deliberately defers the run so a price-window policy cannot silently alter
    the requested model.
    """

    now_v = now or datetime.now(timezone.utc)
    if now_v.tzinfo is None:
        now_v = now_v.replace(tzinfo=timezone.utc)
    provider_v = _text(requested_provider or getattr(settings, "cloud_research_provider", "meta")).lower() or "meta"
    model_v = _provider_model(provider_v, requested_model)
    strategy = _text(getattr(settings, "provider_broker_strategy", "configured_first")).lower() or "configured_first"
    enabled = bool(getattr(settings, "provider_broker_enabled", True))
    fallback_order = _csv(getattr(settings, "provider_broker_fallback_order", "meta,deepseek,nous,openrouter"))
    providers = [provider_v]
    if enabled and not explicit:
        providers.extend(provider for provider in fallback_order if provider not in providers)
    health = provider_health_snapshot(session, providers=providers, now=now_v.replace(tzinfo=None))
    candidates: list[ProviderCandidate] = []
    for index, provider in enumerate(providers):
        model = model_v if index == 0 else _provider_model(provider)
        available = bool(model) and llm_available(provider=provider, model=model)
        blocked = bool(scheduled and provider_temporally_blocked(provider, now_v))
        provider_health = health.get(provider, ProviderHealth(provider=provider))
        reasons: list[str] = []
        if not model:
            reasons.append("no configured model")
        if not available:
            reasons.append("provider credentials/model unavailable")
        if blocked:
            reasons.append("provider-aware schedule defers this pricing window")
        if provider_health.degraded:
            reasons.append("recent terminal outcome failure streak exceeded threshold")
        accepted = available and not blocked and not provider_health.degraded
        cache_policy = _cache_policy(provider)
        cost_class = _cost_class(provider, model)
        cost_bonus = {
            "contributor_rate_limited": 20.0,
            "free": 15.0,
            "metered_cache_discounted": 10.0,
            "unknown_or_provider_managed": 0.0,
        }.get(cost_class, 0.0)
        cache_bonus = {
            "explicit_key_and_retention": 10.0,
            "automatic_prefix": 5.0,
            "explicit_key": 4.0,
            "provider_default_or_unknown": 0.0,
        }.get(str(cache_policy.get("mechanism") or ""), 0.0)
        health_bonus = (
            (provider_health.successes / provider_health.sample_size) * 5.0
            if provider_health.sample_size
            else 2.5
        )
        score = (1000.0 if index == 0 else 500.0 - (index * 3.0)) + cost_bonus + cache_bonus + health_bonus
        candidates.append(
            ProviderCandidate(
                provider=provider,
                model=model,
                available=available,
                temporally_blocked=blocked,
                health=provider_health,
                cache_policy=cache_policy,
                cost_class=cost_class,
                score=round(score, 4),
                accepted=accepted,
                reasons=tuple(reasons),
            )
        )
        if index == 0 and blocked:
            break
        if accepted and index == 0:
            break
        if explicit or not enabled or strategy == "pinned":
            break
    accepted_candidates = [candidate for candidate in candidates if candidate.accepted]
    selected = max(accepted_candidates, key=lambda candidate: candidate.score, default=None)
    if selected is not None:
        reason = "configured provider accepted" if selected.provider == provider_v else "configured provider unhealthy or unavailable; fallback accepted"
    elif candidates and candidates[0].temporally_blocked:
        reason = "deferred by configured provider schedule policy"
    else:
        reason = "no acceptable configured provider candidate"
    return ProviderBrokerDecision(
        task_kind=_text(task_kind) or "cloud_chat",
        requested_provider=provider_v,
        requested_model=model_v,
        selected_provider=selected.provider if selected else None,
        selected_model=selected.model if selected else None,
        strategy=strategy,
        explicit=bool(explicit),
        scheduled=bool(scheduled),
        reason=reason,
        candidates=tuple(candidates),
        decided_at=now_v.astimezone(timezone.utc).isoformat(),
    )
