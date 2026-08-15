# 11. Sự kiện & serving bridge

> Mã nguồn: `src/mlops_framework/events/`, `src/mlops_framework/serving/`
> Bảng: `model_promotion_events`, `serving_instances`
> Kiểm chứng: `tests/unit/test_events.py`, `test_serving_bridge.py`

## 1. Mục đích

Khi framework quyết định một model lên production, thứ đang **thực sự
phục vụ request** phải đi theo. Nếu không, registry và thực tế lệch nhau
— và không ai biết cho tới lúc quá muộn.

## 2. Sự kiện

### `Event` và các lớp con

| `event_type` | Phát khi |
|---|---|
| `MODEL_PROMOTED` | một ModelVersion lên PRODUCTION |
| `MODEL_ROLLED_BACK` | người vận hành đưa bản đã nghỉ hưu trở lại |
| `TRAINING_FAILED` | TrainingRun kết thúc FAILED |
| `DRIFT_DETECTED` | DriftService kết luận có drift |
| `RUN_BLOCKED` | readiness / eligibility / approval chặn lại |
| `SCHEDULE_FAILED` | một schedule tới hạn nhưng nổ khi fire |

`SCHEDULE_FAILED` tách khỏi `TRAINING_FAILED` vì chúng khác nhau:
`TRAINING_FAILED` nghĩa là có một `TrainingRun` tồn tại và kết thúc
FAILED. `SCHEDULE_FAILED` phủ mọi thứ hỏng **xung quanh** lần chạy —
tracking server không tới được, dataset biến mất, một bước workflow ném
lỗi — kể cả trường hợp chưa hề có `TrainingRun` nào được tạo.

`MODEL_ROLLED_BACK` mang `actor` trong payload: đây là sự kiện quản trị
duy nhất luôn là quyết định của con người.

### `EventPublisher` ABC

| Adapter | Ghi chú |
|---|---|
| `HttpEventPublisher` | POST JSON tới một webhook; `publish()` trả `bool`, không ném lỗi |
| `InMemoryEventPublisher` | test |

## 3. `ServingBridge`

Một app FastAPI nhỏ (`serving/bridge.py`) chạy riêng ở cổng 8001.

| Endpoint | |
|---|---|
| `POST /internal/model/reload` | nhận `ModelPromotedEvent`, hoán đổi model đang hoạt động |
| `GET /internal/model/active/{name}` | |
| `GET /healthz` | liveness |

### Hoán đổi nguyên tử

`ServingModelRegistry` giữ `dict[model_name → record]` dưới một
`threading.RLock`. `set_active` là **một phép gán tham chiếu** trong
lock, nên không request nào từng nhìn thấy model nạp dở.

`get_active` trả **bản sao nông** để caller không sửa được registry.

### Bridge không thực thi mã model

Registry lưu metadata và một "payload" tuỳ caller cung cấp (object model
đã deserialize, một closure, hay một dict). Điều này giữ bridge trung lập
với framework ML: nó bọc được sklearn, pytorch, xgboost, hay không gì cả.

## 4. Ai publish reload

| Đường | Cơ chế |
|---|---|
| DAG Airflow | task `register_and_promote` tự POST tới bridge sau khi `/promote` trả về `promoted: true` |
| `RetrainingWorkflow` | `_publish_promotion()` qua `EventPublisher` đã cấu hình |
| `POST /api/model-versions/{id}/rollback` | dựng `HttpEventPublisher` từ `SERVING_BRIDGE_URL` |

### `model_promotion_events` — bản ghi bền của lần publish

`RetrainingWorkflow` ghi một dòng `ModelPromotionEvent` với
`status = PENDING`, gọi publisher, rồi cập nhật thành `PUBLISHED` hoặc
`FAILED` kèm `error_message`. Nghĩa là "đã cố gửi và bridge không nhận"
là một sự kiện **có thể truy vấn**, không chỉ một dòng log.

### Reload là best-effort và được **báo cáo**

Endpoint rollback trả `serving_reloaded: bool`. Database của framework là
bản ghi quyết định; một bridge đang chết không được để registry rollback
nửa vời. Nhưng người vận hành cần biết trường hợp nào đã xảy ra — nên nó
nằm trong response body chứ không bị nuốt.

## 5. `serving_instances`

Ghi lại bản nào đang hoạt động trên instance nào:
`serving_instance_id` · `model_id` · `model_version_id` · `is_active` ·
`reload_source`. Đây là mắt xích cuối của chuỗi lineage (xem
[12-lineage.md](12-lineage.md)).

## 6. Giới hạn đã biết

- `ServingModelRegistry` nằm trong bộ nhớ — restart bridge là mất trạng
  thái cho tới lần reload kế tiếp.
- Chỉ có publisher HTTP; Redis/Kafka là việc còn phải làm (ABC đã sẵn
  sàng cho việc đó).
- Bridge không tự nạp artifact từ `artifact_uri`; caller cung cấp payload.
