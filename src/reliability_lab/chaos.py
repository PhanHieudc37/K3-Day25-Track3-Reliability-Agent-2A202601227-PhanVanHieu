from __future__ import annotations

import json
import random
import time
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            redis_cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
            if redis_cache.ping():
                cache = redis_cache
            else:
                redis_cache.close()
                cache = ResponseCache(
                    config.cache.ttl_seconds,
                    config.cache.similarity_threshold,
                )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time calculation:
    1. For each breaker in gateway.breakers.values():
       - Walk breaker.transition_log entries
       - Track when circuit goes to "open" (save ts)
       - Track when circuit goes to "closed" (compute delta from open ts)
       - Recovery time = (close_ts - open_ts) * 1000 (convert to ms)
    2. Return average of all recovery times, or None if no recovery occurred.

    Each transition_log entry is a dict with keys: "from", "to", "reason", "ts"
    where "ts" is time.time() (epoch seconds).
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            if transition["to"] == "open":
                opened_at = float(transition["ts"])
            elif transition["to"] == "closed" and opened_at is not None:
                recovery_times.append((float(transition["ts"]) - opened_at) * 1000)
                opened_at = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario.

    Scenario runner behavior:
    1. Build gateway with build_gateway(config, scenario.provider_overrides or None)
    2. Create empty RunMetrics()
    3. Loop config.load_test.requests times:
       a. Pick random query from queries
       b. Call gateway.complete(prompt)
       c. Update metrics:
          - total_requests += 1
          - estimated_cost += result.estimated_cost
          - If cache_hit: cache_hits += 1, estimated_cost_saved += 0.001
          - If route == "fallback": fallback_successes += 1, successful_requests += 1
          - If route == "static_fallback": static_fallbacks += 1, failed_requests += 1
          - Else: successful_requests += 1
          - If result.latency_ms > 0: append to latencies_ms
    4. Count circuit_open_count from breaker transition logs (entries where to == "open")
    5. Set recovery_time_ms via calculate_recovery_time_ms(gateway)
    6. Return metrics
    """
    gateway = build_gateway(config, scenario.provider_overrides or None)
    if isinstance(gateway.cache, SharedRedisCache):
        # Each scenario is an independent measurement. Reusing warm keys would
        # hide provider failures and can prevent recovery time from being observed.
        gateway.cache.flush()
    metrics = RunMetrics()

    primary = gateway.providers[0] if gateway.providers else None
    configured_primary_fail_rate = primary.fail_rate if primary is not None else 0.0

    for request_index in range(config.load_test.requests):
        if scenario.name == "primary_flaky_50" and primary is not None:
            if request_index < config.circuit_breaker.failure_threshold:
                # Deterministic fault burst: bypass cache and force the breaker OPEN.
                primary.fail_rate = 1.0
                prompt = f"account balance chaos probe for user {1000 + request_index}"
            elif request_index == config.circuit_breaker.failure_threshold:
                # A healthy probe after the reset window proves actual recovery.
                time.sleep(config.circuit_breaker.reset_timeout_seconds + 0.01)
                primary.fail_rate = 0.0
                prompt = "account balance recovery probe for user 2000"
            else:
                primary.fail_rate = configured_primary_fail_rate
                prompt = random.choice(queries)
        else:
            prompt = random.choice(queries)
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1

        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    The CLI performs a second cache-disabled run for direct comparison.
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        if scenario.name == "primary_timeout_100":
            passed = (
                result.availability >= 0.99
                and result.fallback_success_rate >= 0.95
                and result.circuit_open_count > 0
            )
        elif scenario.name == "primary_flaky_50":
            passed = (
                result.availability >= 0.95
                and result.fallback_successes > 0
                and result.circuit_open_count > 0
            )
        elif scenario.name == "all_healthy":
            passed = result.failed_requests == 0 and result.circuit_open_count == 0
        else:
            passed = result.availability >= 0.9
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_details[scenario.name] = result.to_report_dict()

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
