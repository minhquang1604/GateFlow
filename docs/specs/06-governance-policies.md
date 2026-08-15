# 06. Ba chính sách quản trị

> Mã nguồn: `readiness/engine.py`, `governance/eligibility.py`, `governance/promotion.py`
> Kiểm chứng: `tests/unit/test_readiness.py`, `test_eligibility.py`, `test_promotion.py`

## 1. Mục đích

Ba câu hỏi khác nhau, cố ý tách rời, vì gộp lại là cách người ta mất khả
năng trả lời "vì sao lần train này không chạy".

| Chính sách | Câu hỏi | Đối tượng |
|---|---|---|
| **Readiness** | Dữ liệu này có dùng được không? | `DatasetVersion` |
| **Eligibility** | *Bây giờ* có nên train không? | `DatasetVersion` + `Model` + drift |
| **Promotion** | Kết quả có đủ tốt để thay production không? | `ModelVersion` |

Cả ba trả về dataclass có cờ boolean **và** danh sách `reasons`. Không có
chính sách nào chỉ trả về `True`/`False`.

## 2. Readiness

`ReadinessEngine.evaluate(version, policy)` → `READY` hoặc `BLOCKED`, kèm
kết quả từng check. Kết quả được **lưu** vào `readiness_evaluations` —
quyết định gác training phải kiểm toán được sau này.

### `TrainingPolicy`

| Trường | Mặc định | Kiểm gì |
|---|---|---|
| `required_size` | `0` | `row_count` tối thiểu |
| `freshness_hours` | `None` | version cũ hơn ngưỡng → BLOCKED |
| `required_columns` | `[]` | cột bắt buộc phải có |
| `dtypes` | `{}` | kiểu dữ liệu mong đợi theo cột |
| `max_missing_ratio` | `None` | tỉ lệ thiếu tối đa |
| `expected_column_count` | `None` | số cột chính xác |
| `validation_rules` | `{}` | cờ tuỳ ý do caller khẳng định |

Mỗi check trả `PASSED` / `FAILED` / **`SKIPPED`**. `SKIPPED` quan trọng:
một policy không cấu hình `freshness_hours` thì check đó bị bỏ qua, chứ
không "pass" — báo cáo phân biệt được "đã kiểm và đạt" với "chưa hề kiểm".

## 3. Eligibility

`TrainingEligibilityPolicy.evaluate(context, config)`. Context gồm kết
quả readiness, kết quả drift (nếu có), model đích, và cờ `force`.

### `EligibilityConfig`

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `require_ready` | `True` | dữ liệu BLOCKED thì không train |
| `min_new_rows` | `None` | cần bao nhiêu dòng mới so với lần train trước |
| `require_drift_to_retrain` | `None` | chỉ train khi *có* drift |
| `block_when_drift_detected` | `None` | ngược lại — chặn khi có drift |
| `cooldown_hours` | `None` | thời gian nghỉ tối thiểu giữa hai lần train |
| `block_when_production_metrics_meet` | `None` | production đang đủ tốt → khỏi train |
| `require_production_below` | `None` | chỉ train khi production tụt dưới ngưỡng |
| `require_existing_production` | `None` | |
| `block_when_production_exists` | `None` | |

`force=True` bỏ qua các cổng eligibility. Đây là điều `run-now` và
scheduler dùng: **biểu thức cron chính là quyết định eligibility**.

Khi không có `reference_data`/`current_data`, drift là *chưa biết* và các
cổng liên quan tới drift thành no-op — chứ không mặc định là "không có
drift".

## 4. Promotion

`ModelPromotionPolicy.evaluate(context, config)` → `PromotionDecision`.
Context là `PromotionContext(candidate, production)`.

### `PromotionConfig`

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `min_metrics` | `{}` | ngưỡng sàn cho từng metric |
| `must_beat_production` | `True` | phải hơn bản đương nhiệm |
| `allow_cold_start` | `True` | cho promote khi chưa có production nào |
| `min_floors` | `{}` | sàn tuyệt đối, áp cả khi đã hơn production |

`min_floors` tách khỏi `min_metrics` vì chúng trả lời hai câu khác nhau:
"tốt hơn cái đang chạy" không có nghĩa là "đủ tốt để chạy".

## 5. Quyết định thiết kế

**Vì sao mặc định đọc từ `FrameworkSettings`?** `ReadinessEngine`,
`TrainingEligibilityPolicy`, `ModelPromotionPolicy` đều nhận `session` và
lấy config mặc định từ bảng `framework_settings`. Caller truyền config
tường minh thì cái đó thắng. Nhờ vậy chính sách sửa được từ console mà
không phải deploy lại — xem [15-configuration.md](15-configuration.md).

**Vì sao `PromotionContext` là dataclass thật?** `RetrainingWorkflow`
từng dựng `type("Ctx", (), {...})()` với đúng hai thuộc tính policy tình
cờ đọc tới. Hình dạng đó âm thầm lệch đi ngay khi `PromotionContext` mọc
thêm trường, trong khi `internal.py::promote_model` — caller còn lại của
cùng policy — vẫn luôn truyền dataclass thật.

## 6. Giới hạn đã biết

- Readiness tin vào `metadata_json` mà client cung cấp cho các check về
  cột/dtype/missing — nó không đọc file để tự kiểm.
- Eligibility không có khái niệm hàng đợi hay ưu tiên; nó chỉ trả lời
  có/không cho một cặp (dataset version, model).
