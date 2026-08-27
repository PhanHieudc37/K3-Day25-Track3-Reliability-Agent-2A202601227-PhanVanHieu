# Day 25 Reliability Engineering — Báo cáo cuối

> Số liệu được đọc tự động từ `reports/metrics.json` và `reports/metrics_no_cache.json`, không nhập tay.

## 1. Kiến trúc

```text
User request
     |
     v
ReliabilityGateway --> Semantic cache (memory/Redis) -- HIT --> response
     | MISS
     v
CircuitBreaker(primary) --> Primary provider
     | OPEN / provider error
     v
CircuitBreaker(backup)  --> Backup provider
     | OPEN / provider error
     v
Static fallback (degraded response)
```

Cache đứng trước provider để giảm latency/chi phí. Mỗi provider có breaker riêng nên một provider hỏng không chặn provider dự phòng. Privacy guardrail loại truy vấn nhạy cảm trước khi đọc/ghi cache; kiểm tra số 4 chữ số ngăn false hit giữa các năm.

## 2. Cấu hình và lý do

| Tham số | Giá trị | Lý do |
|---|---:|---|
| seed | 25 | Cố định chuỗi chọn query, jitter và lỗi giả lập để hai lần chạy có thể tái lập cùng workload ngẫu nhiên. |
| primary fail/latency/cost | 0.25 / 180 ms / 0.01 | Provider nhanh, đắt hơn và có lỗi đủ thường xuyên để breaker hoạt động. |
| backup fail/latency/cost | 0.05 / 260 ms / 0.006 | Backup chậm hơn nhưng ổn định và rẻ hơn để giữ availability. |
| failure_threshold | 3 | Phản ứng sau 3 lỗi liên tiếp nhưng không trip vì một lỗi thoáng qua. |
| reset_timeout_seconds | 2.0 | Đủ để fail-fast và vẫn quan sát được tự hồi phục trong 100 request. |
| success_threshold | 1 | Một probe thành công giúp phục hồi nhanh; production có thể tăng để giảm flapping. |
| cache enabled | True | Bật trong phép đo chính để định lượng hit rate, cost saved và availability; phép đo đối chứng tự động tắt giá trị này. |
| cache backend | redis | Redis chia sẻ state giữa các gateway instance; namespace đo được xóa trước mỗi chaos run để cache nóng không làm sai kết quả. |
| redis_url | `redis://localhost:6379/0` | Một endpoint dùng chung cho mọi gateway instance trong lab; production sẽ lấy từ secret/environment. |
| cache TTL | 300 s | Giới hạn dữ liệu cũ trong 5 phút nhưng vẫn tạo hit trong load test. |
| similarity_threshold | 0.92 | Ngưỡng cao ưu tiên correctness; number guard chặn false hit 2024/2026. |
| requests/scenario | 100 | Đủ mẫu cho percentile; tổng đúng 3 × 100 request. |
| scenario `primary_timeout_100` | {'primary': 1.0, 'backup': 0.0} | Primary provider fails 100% â€” all traffic should fallback |
| scenario `primary_flaky_50` | {'primary': 0.5} | Forced fault burst and recovery probe, then primary fails 50% |
| scenario `all_healthy` | {'primary': 0.0, 'backup': 0.0} | Baseline â€” both providers healthy |

## 3. SLO

| SLI | SLO | Thực tế | Đạt? |
|---|---:|---:|---|
| Availability | >= 99% | 99.67% | Có |
| P95 latency | < 2500 ms | 322.62 ms | Có |
| Fallback success rate | >= 95% | 98.39% | Có |
| Cache hit rate | >= 10% | 62.00% | Có |
| Recovery time | < 5000 ms | 2522.9886770248413 ms | Có |

Tất cả SLO đã đạt trong lần chạy này.

## 4. Metrics (3 scenario × 100 request)

| Metric | Giá trị |
|---|---:|
| total_requests | 300 |
| availability | 0.9967 |
| error_rate | 0.0033 |
| latency_p50_ms | 266.91 |
| latency_p95_ms | 322.62 |
| latency_p99_ms | 331.02 |
| fallback_success_rate | 0.9839 |
| cache_hit_rate | 0.62 |
| circuit_open_count | 9 |
| recovery_time_ms | 2522.9886770248413 |
| estimated_cost | 0.05407 |
| estimated_cost_saved | 0.186 |

## 5. So sánh cache

Hai lần chạy dùng cùng config, 3 scenario × 100 request và seed 25; lần đối chứng chỉ tắt `cache.enabled`.

| Metric | Không cache | Có cache | Delta (có - không) |
|---|---:|---:|---:|
| latency_p50_ms | 264.87 | 266.91 | +2.0400 ms |
| latency_p95_ms | 323.05 | 322.62 | -0.4300 ms |
| estimated_cost | 0.141782 | 0.05407 | -0.0877 |
| cache_hit_rate | 0.0 | 0.62 | +0.6200 |

Cache hit có latency/cost bằng 0 theo contract gateway nên giảm provider call. Percentile hiện chỉ tính provider call có latency dương; cost và hit rate thể hiện trực tiếp lợi ích cache.
Chi phí provider giảm 61.86% so với lần chạy không cache.

### Bằng chứng false hit thật

Hai câu `refund policy ... 2024 deadline` và `refund policy ... 2026 deadline` có điểm cosine 0.9375, cao hơn ngưỡng 0.92, nhưng cache trả về `None` và ghi reason `date_or_number_mismatch`. Điều này chứng minh number guard từ chối một semantic match có bề mặt rất giống nhưng ngữ cảnh năm khác nhau.

## 6. Redis shared state

In-memory cache tách theo pod, gây duplicate provider call. `SharedRedisCache` dùng hash key ổn định, Redis Hash và EXPIRE nên mọi instance cùng namespace thấy chung state và TTL.

```text
instance_2.get = ('visible from instance 2', 1.0)
privacy_get = None; total_evidence_keys = 1
```

Kết quả tương đương `redis-cli KEYS "rl:evidence:*"`:

```text
rl:evidence:c2924cb05c03
```

### Redis graceful degradation (stretch goal)

Khi trỏ Redis tới endpoint không hoạt động, `build_gateway()` chọn backend `ResponseCache`. Gateway vẫn phục vụ bằng cache cục bộ thay vì sập theo Redis.

## 7. Chaos scenarios

| Scenario | Kỳ vọng | Quan sát từ metrics | Kết quả |
|---|---|---|---|
| primary_timeout_100 | Primary lỗi 100%; breaker mở và traffic chuyển sang backup. | availability=1.0, fallback_success_rate=1.0, cache_hit_rate=0.67, circuit_open_count=5 | PASS |
| primary_flaky_50 | Primary lỗi 50%; hệ thống vẫn phục vụ qua cache/provider dự phòng. | availability=0.99, fallback_success_rate=0.9655, cache_hit_rate=0.58, circuit_open_count=4 | PASS |
| all_healthy | Hai provider lỗi 0%; không static fallback hoặc circuit mở. | availability=1.0, fallback_success_rate=0.0, cache_hit_rate=0.61, circuit_open_count=0 | PASS |

## 8. Điểm yếu còn lại và cách sửa

Breaker hiện lưu state trong từng process. Khi nhiều pod, pod A có thể OPEN nhưng pod B vẫn gọi provider hỏng, tạo retry storm phân tán. Trước production cần đưa breaker state và lease HALF_OPEN probe vào Redis (INCR/EXPIRE + atomic Lua hoặc distributed lock), thêm jitter và metric theo provider. Redis graceful fallback đã có, nhưng state của breaker vẫn chưa được chia sẻ.

## 9. Bước tiếp theo

1. Thêm load test concurrent để kiểm chứng chỉ một HALF_OPEN probe được phép chạy.
2. Tính `estimated_cost_saved` theo model/provider và token thật thay vì hằng số mô phỏng.
3. Cảnh báo theo error budget cho availability, P95/P99 và false-hit rate.
