from __future__ import annotations

import argparse
import random
from pathlib import Path

from reliability_lab.cache import SharedRedisCache
from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import LabConfig, load_config


def clear_measurement_cache(config: LabConfig) -> None:
    """Clear only the experiment namespace so warm Redis data cannot bias a run."""
    cache_config = config.cache
    if not cache_config.enabled or cache_config.backend != "redis":
        return
    cache = SharedRedisCache(
        cache_config.redis_url,
        cache_config.ttl_seconds,
        cache_config.similarity_threshold,
    )
    try:
        if cache.ping():
            cache.flush()
    finally:
        cache.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument("--comparison-out", default="reports/metrics_no_cache.json")
    args = parser.parse_args()
    config = load_config(args.config)
    queries = load_queries()

    clear_measurement_cache(config)
    random.seed(config.seed)
    metrics = run_simulation(config, queries)
    metrics.write_json(args.out)
    metrics.write_csv(Path(args.out).with_suffix(".csv"))
    print(f"wrote {args.out}")

    no_cache_config = config.model_copy(deep=True)
    no_cache_config.cache.enabled = False
    random.seed(config.seed)
    no_cache_metrics = run_simulation(no_cache_config, queries)
    no_cache_metrics.write_json(args.comparison_out)
    no_cache_metrics.write_csv(Path(args.comparison_out).with_suffix(".csv"))
    print(f"wrote {args.comparison_out}")


if __name__ == "__main__":
    main()
