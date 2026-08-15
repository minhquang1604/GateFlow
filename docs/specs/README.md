# Đặc tả tính năng — MLOps Framework

Mỗi file trong thư mục này đặc tả **một** tính năng: nó giải quyết vấn đề
gì, mô hình dữ liệu ra sao, quy tắc nào bất biến, và — phần quan trọng
nhất — **vì sao** thiết kế lại như vậy chứ không phải cách khác.

Đây không phải tài liệu hướng dẫn sử dụng. `README.md` ở gốc repo làm
việc đó. Tài liệu này dành cho người sắp **sửa** framework: nó ghi lại
những ràng buộc mà một thay đổi vô ý sẽ phá vỡ.

## Mục lục

| # | Tính năng | Mã nguồn |
|---|---|---|
| [01](01-dataset-management.md) | Quản lý & phiên bản hoá dataset | `dataset/` |
| [02](02-training-runs.md) | Vòng đời training run | `training/` |
| [03](03-orchestration.md) | Trừu tượng hoá orchestrator | `orchestration/` |
| [04](04-experiment-tracking.md) | Trừu tượng hoá experiment tracker | `tracking/` |
| [05](05-model-registry.md) | Model registry & rollback | `model/` |
| [06](06-governance-policies.md) | Ba chính sách quản trị | `readiness/`, `governance/` |
| [07](07-drift-detection.md) | Phát hiện drift | `drift/` |
| [08](08-retraining-workflow.md) | Workflow retrain tự động | `workflow/` |
| [09](09-scheduling.md) | Lập lịch theo cron | `scheduling/` |
| [10](10-approval-gate.md) | Cổng phê duyệt của con người | `approval/` |
| [11](11-serving-and-events.md) | Sự kiện & serving bridge | `events/`, `serving/` |
| [12](12-lineage.md) | Truy vết nguồn gốc | `lineage/` |
| [13](13-observability.md) | Audit trail, alerts, health probe | `audit/`, `events/store.py` |
| [14](14-security.md) | Xác thực, phân quyền, write gate | `auth/`, `api/security.py` |
| [15](15-configuration.md) | Cấu hình tĩnh & chính sách lưu trong DB | `config/`, `framework_settings/` |
| [16](16-sdk-api-console.md) | SDK, HTTP API, console Gateflow | `sdk/`, `api/`, `ui/` |

## Quy ước chung trong toàn framework

Những quy ước dưới đây lặp lại ở nhiều tính năng; các file sau sẽ tham
chiếu về đây thay vì lặp lại.

### Manager chỉ `flush()`, không `commit()`

Mọi manager (`DatasetManager`, `ModelManager`, …) nhận một `Session` từ
bên ngoài và chỉ gọi `flush()`. Quyết định commit thuộc về caller —
`api/deps.py::get_db` cho đường HTTP, `DatabaseManager.get_session()` cho
đường in-process.

Lý do: một hành động nghiệp vụ thường gồm nhiều manager. Promote một
model = tạo `ModelVersion` + chuyển trạng thái + archive bản cũ. Đó là
**một** giao dịch nguyên tử, không phải ba. Nếu manager tự commit thì
không thể ghép chúng lại được nữa.

### Trạng thái đi qua state machine, không gán trực tiếp

`TrainingRun.status` và `ModelVersion.state` đều có bảng chuyển trạng
thái hợp lệ (`VALID_STATUS_TRANSITIONS`, `VALID_MODEL_STATE_TRANSITIONS`)
và một hàm `validate_transition` chặn ở tầng ứng dụng. Ràng buộc thật sự
nằm ở database khi có thể (xem `006_one_production_per_model`).

### Adapter được import lười (lazy import)

MLflow, Airflow, scipy, pandas đều **không** phải dependency bắt buộc để
`import mlops_framework` chạy được. Chúng được import bên trong hàm, và
adapter tương ứng ném lỗi cấp framework có thông điệp rõ ràng nếu thiếu.
Nhờ vậy test suite chạy hermetic, không cần dịch vụ ngoài.

### "Không bao giờ ném lỗi" phải kèm SAVEPOINT

`AuditManager.record()` và `GovernanceEventStore.record()` cam kết không
làm hỏng hành động mà chúng đang ghi lại. Bắt exception thôi là **chưa
đủ**: lỗi phát sinh trong `flush()` để lại session ở trạng thái đã
rollback, nên `commit()` kế tiếp của caller chết với
`PendingRollbackError`. Cả hai đều chạy trong `session.begin_nested()`.

Chi tiết: [13-observability.md](13-observability.md).

### Framework không tự đọc file dataset

Không có chỗ nào dưới `src/` mở một object S3 hay một file CSV chứa dữ
liệu huấn luyện. `DriftService` nhận sẵn giá trị đặc trưng từ caller;
orchestrator nhận `storage_uri` và tự lo. Đây là ranh giới có chủ ý —
xem [07-drift-detection.md](07-drift-detection.md) §5 để biết nó định
hình tính năng "chạy drift từ console" như thế nào.

### Mọi quyết định đều giải thích được

Readiness, eligibility, promotion, approval đều trả về một dataclass có
trường boolean **và** danh sách `reasons`. Không có quyết định nào chỉ
trả về `True`/`False`. Quyết định được lưu lại (`ReadinessEvaluation`,
`DriftEvaluation`, `AuditLog`) chứ không chỉ nằm trong log.
