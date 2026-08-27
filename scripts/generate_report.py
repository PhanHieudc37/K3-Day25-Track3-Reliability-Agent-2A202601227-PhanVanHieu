# ruff: noqa: BLE001, ISC004

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.chaos import build_gateway
from reliability_lab.config import LabConfig, load_config


def load_json(path: str | Path) -> dict[str, Any]:
    raw: object = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"Expected a JSON object in {path}")
    return cast(dict[str, Any], raw)


def redis_evidence(config: LabConfig) -> tuple[str, str, str]:
    prefix = "rl:evidence:"
    first: SharedRedisCache | None = None
    second: SharedRedisCache | None = None
    try:
        first = SharedRedisCache(
            config.cache.redis_url,
            config.cache.ttl_seconds,
            config.cache.similarity_threshold,
            prefix,
        )
        second = SharedRedisCache(
            config.cache.redis_url,
            config.cache.ttl_seconds,
            config.cache.similarity_threshold,
            prefix,
        )
        first.flush()
        first.set("shared state proof", "visible from instance 2")
        shared_value, shared_score = second.get("shared state proof")
        first.set("account balance for user 123", "must never be stored")
        privacy_value, _ = second.get("account balance for user 123")
        keys = sorted(str(key) for key in first._redis.scan_iter(f"{prefix}*"))
        shared = f"instance_2.get = ({shared_value!r}, {shared_score:.1f})"
        privacy = f"privacy_get = {privacy_value!r}; total_evidence_keys = {len(keys)}"
        key_output = "\n".join(keys) if keys else "(no keys found)"
        return shared, privacy, key_output
    except Exception as exc:
        message = f"Redis evidence unavailable: {type(exc).__name__}: {exc}"
        return message, message, message
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()


def false_hit_evidence(config: LabConfig) -> tuple[float, str, str]:
    """Exercise the dated-query guardrail and return evidence for the report."""
    cached_query = "Summarize refund policy for 2024 deadline"
    requested_query = "Summarize refund policy for 2026 deadline"
    cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    cache.set(cached_query, "Old refund policy")
    result, score = cache.get(requested_query)
    reason = str(cache.false_hit_log[-1]["reason"]) if cache.false_hit_log else "not_logged"
    return score, repr(result), reason


def redis_degradation_evidence(config: LabConfig) -> str:
    """Prove that an unavailable Redis endpoint falls back to in-memory cache."""
    fallback_config = config.model_copy(deep=True)
    fallback_config.cache.redis_url = "redis://127.0.0.1:6390/0"
    gateway = build_gateway(fallback_config)
    return type(gateway.cache).__name__


def delta(without: float, with_cache: float) -> str:
    return f"{with_cache - without:+.4f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--comparison", default="reports/metrics_no_cache.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = load_json(args.metrics)
    comparison = load_json(args.comparison)
    config = load_config(args.config)
    cb = config.circuit_breaker
    cache = config.cache
    shared, privacy, redis_keys = redis_evidence(config)
    false_hit_score, false_hit_result, false_hit_reason = false_hit_evidence(config)
    degradation_backend = redis_degradation_evidence(config)

    recovery = metrics["recovery_time_ms"]
    checks = {
        "availability": metrics["availability"] >= 0.99,
        "p95": metrics["latency_p95_ms"] < 2500,
        "fallback": metrics["fallback_success_rate"] >= 0.95,
        "cache": metrics["cache_hit_rate"] >= 0.10,
        "recovery": recovery is not None and recovery < 5000,
    }
    check_labels = {
        "availability": "availability",
        "p95": "P95 latency",
        "fallback": "fallback success rate",
        "cache": "cache hit rate",
        "recovery": "recovery time",
    }
    unmet = [check_labels[name] for name, met in checks.items() if not met]
    slo_comment = (
        "Tất cả SLO đã đạt trong lần chạy này."
        if not unmet
        else "SLO chưa đạt: " + ", ".join(unmet) + ". Cần giảm lỗi backup hoặc điều chỉnh routing."
    )
    scenario_config_rows = [
        f"| scenario `{scenario.name}` | {scenario.provider_overrides} | {scenario.description} |"
        for scenario in config.scenarios
    ]
    cost_reduction = (
        (comparison["estimated_cost"] - metrics["estimated_cost"])
        / comparison["estimated_cost"]
        * 100
        if comparison["estimated_cost"]
        else 0.0
    )

    expectations = {
        "primary_timeout_100": "Primary lỗi 100%; breaker mở và traffic chuyển sang backup.",
        "primary_flaky_50": "Primary lỗi 50%; hệ thống vẫn phục vụ qua cache/provider dự phòng.",
        "all_healthy": "Hai provider lỗi 0%; không static fallback hoặc circuit mở.",
    }
    scenario_rows: list[str] = []
    details = metrics.get("scenario_details", {})
    for name, status in metrics.get("scenarios", {}).items():
        item = details.get(name, {})
        observed = (
            f"availability={item.get('availability')}, "
            f"fallback_success_rate={item.get('fallback_success_rate')}, "
            f"cache_hit_rate={item.get('cache_hit_rate')}, "
            f"circuit_open_count={item.get('circuit_open_count')}"
        )
        scenario_rows.append(
            f"| {name} | {expectations.get(name, 'Hệ thống còn khả dụng.')} "
            f"| {observed} | {status.upper()} |"
        )

    lines = [
        "# Day 25 Reliability Engineering — Báo cáo cuối",
        "",
        "> Số liệu được đọc tự động từ `reports/metrics.json` và "
        "`reports/metrics_no_cache.json`, không nhập tay.",
        "",
        "## 1. Kiến trúc",
        "",
        "```text",
        "User request",
        "     |",
        "     v",
        "ReliabilityGateway --> Semantic cache (memory/Redis) -- HIT --> response",
        "     | MISS",
        "     v",
        "CircuitBreaker(primary) --> Primary provider",
        "     | OPEN / provider error",
        "     v",
        "CircuitBreaker(backup)  --> Backup provider",
        "     | OPEN / provider error",
        "     v",
        "Static fallback (degraded response)",
        "```",
        "",
        "Cache đứng trước provider để giảm latency/chi phí. Mỗi provider có breaker riêng nên một "
        "provider hỏng không chặn provider dự phòng. Privacy guardrail loại truy vấn nhạy cảm trước "
        "khi đọc/ghi cache; kiểm tra số 4 chữ số ngăn false hit giữa các năm.",
        "",
        "## 2. Cấu hình và lý do",
        "",
        "| Tham số | Giá trị | Lý do |",
        "|---|---:|---|",
        f"| seed | {config.seed} | Cố định chuỗi chọn query, jitter và lỗi giả lập để hai lần chạy "
        "có thể tái lập cùng workload ngẫu nhiên. |",
        f"| primary fail/latency/cost | {config.providers[0].fail_rate} / "
        f"{config.providers[0].base_latency_ms} ms / {config.providers[0].cost_per_1k_tokens} | "
        "Provider nhanh, đắt hơn và có lỗi đủ thường xuyên để breaker hoạt động. |",
        f"| backup fail/latency/cost | {config.providers[1].fail_rate} / "
        f"{config.providers[1].base_latency_ms} ms / {config.providers[1].cost_per_1k_tokens} | "
        "Backup chậm hơn nhưng ổn định và rẻ hơn để giữ availability. |",
        f"| failure_threshold | {cb.failure_threshold} | Phản ứng sau 3 lỗi liên tiếp nhưng không trip "
        "vì một lỗi thoáng qua. |",
        f"| reset_timeout_seconds | {cb.reset_timeout_seconds} | Đủ để fail-fast và vẫn quan sát được "
        "tự hồi phục trong 100 request. |",
        f"| success_threshold | {cb.success_threshold} | Một probe thành công giúp phục hồi nhanh; "
        "production có thể tăng để giảm flapping. |",
        f"| cache enabled | {cache.enabled} | Bật trong phép đo chính để định lượng hit rate, cost saved "
        "và availability; phép đo đối chứng tự động tắt giá trị này. |",
        f"| cache backend | {cache.backend} | Redis chia sẻ state giữa các gateway instance; namespace "
        "đo được xóa trước mỗi chaos run để cache nóng không làm sai kết quả. |",
        f"| redis_url | `{cache.redis_url}` | Một endpoint dùng chung cho mọi gateway instance trong lab; "
        "production sẽ lấy từ secret/environment. |",
        f"| cache TTL | {cache.ttl_seconds} s | Giới hạn dữ liệu cũ trong 5 phút nhưng vẫn tạo hit trong "
        "load test. |",
        f"| similarity_threshold | {cache.similarity_threshold} | Ngưỡng cao ưu tiên correctness; "
        "number guard chặn false hit 2024/2026. |",
        f"| requests/scenario | {config.load_test.requests} | Đủ mẫu cho percentile; tổng đúng 3 × 100 "
        "request. |",
        *scenario_config_rows,
        "",
        "## 3. SLO",
        "",
        "| SLI | SLO | Thực tế | Đạt? |",
        "|---|---:|---:|---|",
        f"| Availability | >= 99% | {metrics['availability']:.2%} | "
        f"{'Có' if checks['availability'] else 'Không'} |",
        f"| P95 latency | < 2500 ms | {metrics['latency_p95_ms']} ms | "
        f"{'Có' if checks['p95'] else 'Không'} |",
        f"| Fallback success rate | >= 95% | {metrics['fallback_success_rate']:.2%} | "
        f"{'Có' if checks['fallback'] else 'Không'} |",
        f"| Cache hit rate | >= 10% | {metrics['cache_hit_rate']:.2%} | "
        f"{'Có' if checks['cache'] else 'Không'} |",
        f"| Recovery time | < 5000 ms | {recovery} ms | "
        f"{'Có' if checks['recovery'] else 'Không'} |",
        "",
        slo_comment,
        "",
        "## 4. Metrics (3 scenario × 100 request)",
        "",
        "| Metric | Giá trị |",
        "|---|---:|",
    ]
    for key in (
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
    ):
        lines.append(f"| {key} | {metrics[key]} |")

    lines += [
        "",
        "## 5. So sánh cache",
        "",
        f"Hai lần chạy dùng cùng config, 3 scenario × 100 request và seed {config.seed}; "
        "lần đối chứng chỉ tắt `cache.enabled`.",
        "",
        "| Metric | Không cache | Có cache | Delta (có - không) |",
        "|---|---:|---:|---:|",
        f"| latency_p50_ms | {comparison['latency_p50_ms']} | {metrics['latency_p50_ms']} | "
        f"{delta(comparison['latency_p50_ms'], metrics['latency_p50_ms'])} ms |",
        f"| latency_p95_ms | {comparison['latency_p95_ms']} | {metrics['latency_p95_ms']} | "
        f"{delta(comparison['latency_p95_ms'], metrics['latency_p95_ms'])} ms |",
        f"| estimated_cost | {comparison['estimated_cost']} | {metrics['estimated_cost']} | "
        f"{delta(comparison['estimated_cost'], metrics['estimated_cost'])} |",
        f"| cache_hit_rate | {comparison['cache_hit_rate']} | {metrics['cache_hit_rate']} | "
        f"{delta(comparison['cache_hit_rate'], metrics['cache_hit_rate'])} |",
        "",
        "Cache hit có latency/cost bằng 0 theo contract gateway nên giảm provider call. Percentile hiện "
        "chỉ tính provider call có latency dương; cost và hit rate thể hiện trực tiếp lợi ích cache.",
        f"Chi phí provider giảm {cost_reduction:.2f}% so với lần chạy không cache.",
        "",
        "### Bằng chứng false hit thật",
        "",
        "Hai câu `refund policy ... 2024 deadline` và `refund policy ... 2026 deadline` có điểm cosine "
        f"{false_hit_score:.4f}, cao hơn ngưỡng {cache.similarity_threshold}, nhưng cache trả về "
        f"`{false_hit_result}` và ghi reason `{false_hit_reason}`. Điều này chứng minh number guard từ "
        "chối một semantic match có bề mặt rất giống nhưng ngữ cảnh năm khác nhau.",
        "",
        "## 6. Redis shared state",
        "",
        "In-memory cache tách theo pod, gây duplicate provider call. `SharedRedisCache` dùng hash key "
        "ổn định, Redis Hash và EXPIRE nên mọi instance cùng namespace thấy chung state và TTL.",
        "",
        "```text",
        shared,
        privacy,
        "```",
        "",
        "Kết quả tương đương `redis-cli KEYS \"rl:evidence:*\"`:",
        "",
        "```text",
        redis_keys,
        "```",
        "",
        "### Redis graceful degradation (stretch goal)",
        "",
        f"Khi trỏ Redis tới endpoint không hoạt động, `build_gateway()` chọn backend "
        f"`{degradation_backend}`. Gateway vẫn phục vụ bằng cache cục bộ thay vì sập theo Redis.",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Kỳ vọng | Quan sát từ metrics | Kết quả |",
        "|---|---|---|---|",
        *scenario_rows,
        "",
        "## 8. Điểm yếu còn lại và cách sửa",
        "",
        "Breaker hiện lưu state trong từng process. Khi nhiều pod, pod A có thể OPEN nhưng pod B vẫn "
        "gọi provider hỏng, tạo retry storm phân tán. Trước production cần đưa breaker state và lease "
        "HALF_OPEN probe vào Redis (INCR/EXPIRE + atomic Lua hoặc distributed lock), thêm jitter và metric "
        "theo provider. Redis graceful fallback đã có, nhưng state của breaker vẫn chưa được chia sẻ.",
        "",
        "## 9. Bước tiếp theo",
        "",
        "1. Thêm load test concurrent để kiểm chứng chỉ một HALF_OPEN probe được phép chạy.",
        "2. Tính `estimated_cost_saved` theo model/provider và token thật thay vì hằng số mô phỏng.",
        "3. Cảnh báo theo error budget cho availability, P95/P99 và false-hit rate.",
    ]

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
