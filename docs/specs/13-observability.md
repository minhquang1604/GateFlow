# 13. Audit trail, alerts, health probe

> Mã nguồn: `audit/manager.py`, `events/store.py`, `api/routers/health.py`
> Bảng: `audit_logs`, `governance_events`
> Kiểm chứng: `tests/unit/test_audit.py`, `test_governance_event_store.py`, `tests/api/test_health_api.py`

## 1. Hai dòng thời gian, hai câu hỏi khác nhau

| | `audit_logs` | `governance_events` |
|---|---|---|
| Trả lời | **Ai** đã làm gì | Framework **tự phát hiện** ra gì |
| Ví dụ | `MODEL_PROMOTED` bởi `alice` | drift phát hiện, training hỏng, retrain bị chặn |
| Console | tab Activity → Audit trail | tab Activity → Alerts |
| API | `GET /api/audit` | `GET /api/alerts` |

Gộp chung sẽ mất đi phân biệt quan trọng nhất: một người đã quyết định
điều gì đó, so với một điều kiện đã tự xảy ra.

## 2. `AuditLog`

`actor` · `action` (indexed) · `entity_type` · `entity_id` (indexed) ·
`metadata_json`

### Các `action` được ghi

| Nhóm | `action` |
|---|---|
| Model | `MODEL_PROMOTED` · `MODEL_REJECTED` · `MODEL_ROLLED_BACK` |
| Schedule | `SCHEDULE_CREATED` · `SCHEDULE_UPDATED` · `SCHEDULE_DELETED` · `SCHEDULE_RUN_NOW` |
| Training | `TRAINING_RUN_STARTED` |
| Drift | `DRIFT_CHECK_TRIGGERED` |
| Phê duyệt | `RETRAIN_APPROVED` · `RETRAIN_DENIED` |
| Chính sách | `SETTINGS_UPDATED` · `SETTINGS_RESET` |
| Credential | `API_KEY_CREATED` · `API_KEY_REVOKED` |

### `actor` từ đâu ra

Với API key: **từ dòng key**, đã xác minh. Với shared secret: từ header
`X-Actor` **chưa xác minh**, giá trị mặc định `system`. Xem
[14-security.md](14-security.md) — đây chính là lý do RBAC tồn tại.

## 3. `GovernanceEvent`

`event_type` (indexed) · `severity` (`INFO`/`WARNING`/`CRITICAL`) ·
`entity_type` · `entity_id` (indexed) · `message` · `payload_json`

Store **cố ý độc lập** với `EventPublisher`: việc lưu ở đây **luôn** xảy
ra, dù có cấu hình webhook hay không. Đó là toàn bộ lý do
`governance_events` tồn tại — tab Alerts cần có thứ để hiển thị ngay cả
khi chưa ai dựng webhook. Caller nào muốn fan-out webhook thì tự gọi
`EventPublisher.publish()` bên cạnh; store này không làm hộ.

## 4. Hợp đồng "không bao giờ ném lỗi" — và vì sao nó cần SAVEPOINT

Cả `AuditManager.record()` và `GovernanceEventStore.record()` đều hứa
rằng một lần ghi hỏng **không** kéo theo hành động mà chúng đang ghi lại.
Model đã lên production là điều **đã xảy ra**; thiếu một dòng audit là
bản ghi bị suy giảm, không phải lý do để huỷ hành động.

**Bắt exception thôi là chưa đủ.** Lỗi phát sinh trong `flush()` — vi
phạm ràng buộc, giá trị cột sai — để lại *session* ở trạng thái đã
rollback, nên câu lệnh kế tiếp của caller (thường là `commit()` trong
`api/deps.py::get_db`) chết với `PendingRollbackError`. Promotion đang
được ghi lại vì thế **mất luôn**, đúng kết cục mà hợp đồng sinh ra để
ngăn.

**`session.rollback()` trong handler cũng không phải lời giải.** Nó sẽ
vứt bỏ công việc chưa commit của caller và để request trả 2xx trên một
database chưa từng thấy promotion nào — đánh đổi một lỗi ồn ào lấy mất
dữ liệu âm thầm, tệ hơn hẳn.

Nên lệnh insert chạy trong `session.begin_nested()`. Khi hỏng,
SQLAlchemy chỉ rollback về savepoint: dòng ghi bị bỏ, transaction của
caller nguyên vẹn và vẫn dùng được, hành động đi tiếp với đúng một bản
ghi thiếu — đúng mức suy giảm mà hợp đồng mô tả.

> `TestFailureIsolation` trong cả hai file test ghim điều này. Chúng
> **đỏ** trên phiên bản trước SAVEPOINT.

Lỗi xảy ra **trước** flush (ví dụ `json.dumps` không serialize được) vốn
đã vô hại và vẫn nằm ngoài savepoint — nó không cần gì để gỡ.

## 5. Health probe

Hai endpoint ở **gốc**, không dưới `/api`: chúng trả lời câu hỏi về
*process*, không phải về nghiệp vụ, và thứ đi hỏi chúng — container
runtime, load balancer, `healthcheck` của compose — mong đợi một đường
dẫn ngắn cố định.

| | Trả lời | Chạm vào |
|---|---|---|
| `GET /health` | **Liveness** — process có sống và phục vụ không? | không gì cả |
| `GET /ready` | **Readiness** — có làm được việc ngay bây giờ không? | ping database |

Hai cái vì chúng trả lời hai câu khác nhau, và một caller gộp chúng lại
sẽ ra quyết định sai: một dependency chết **không được** làm hỏng
liveness, nếu không orchestrator sẽ restart một app khoẻ mạnh vì sự cố
của người khác và biến một lần đọc bị suy giảm thành một sự cố mới.

MLflow và Airflow **cố ý không** được kiểm: console suy giảm một panel
khi chúng không tới được chứ không hỏng, nên chúng không phải điều kiện
tiên quyết để phục vụ traffic. `/api/settings` đã báo cáo khả năng kết
nối trực tiếp của chúng cho người vận hành nào cần.

`/ready` mở session riêng ngắn hạn thay vì phụ thuộc `get_db`: probe
phải báo cáo database hỏng bằng một body 503 nó kiểm soát, còn `get_db`
sẽ ném lỗi ngay lúc vào và biến nó thành 500 của framework.

Cả hai **không bao giờ** bị write gate chặn — probe không mang được
credential.

### Nơi chúng được dùng

- `docker-compose.yml`: healthcheck của `app` gọi `/ready` (không phải
  `/health`), vì app chạy `alembic upgrade head` trước uvicorn — "container
  đã lên" và "app trả lời được truy vấn" cách nhau vài phút lúc boot đầu.
- Terraform prod: health check của ECS trước đây gọi `/` — trang console
  render từ file trên đĩa, không chạm database, nên nó trả 200 xuyên suốt
  một sự cố RDS. Nay dùng `/ready`.

## 6. Giới hạn đã biết

- Không có metrics Prometheus.
- `GET /api/audit` và `/api/alerts` chỉ có `limit`, chưa phân trang đầy đủ.
