# 16. SDK, HTTP API, console Gateflow

> Mã nguồn: `src/mlops_framework/sdk/`, `src/mlops_framework/api/`, `src/mlops_framework/ui/`
> Kiểm chứng: `tests/sdk/`, `tests/api/`, `tests/api/test_ui.py`, `test_app_factory.py`

## 1. Ba mặt tiền trên cùng một tầng lõi

Không mặt tiền nào chứa logic nghiệp vụ. Mỗi cái là một cách khác nhau để
tới cùng những manager và chính sách ấy.

```
Application code  ──▶  SDK (MLOpsProject)  ──┐
Airflow DAG       ──▶  HTTP /api/internal/* ─┼──▶  managers + policies  ──▶  DB
Trình duyệt       ──▶  Console → HTTP /api  ─┘
```

## 2. SDK — `MLOpsProject`

Điểm vào mà mã ứng dụng **nên** dùng. Nó không bao giờ để lộ manager,
model ORM, hay orchestrator.

```python
project = MLOpsProject.with_defaults("fraud-detection")
project.register_pipeline("xgboost-training", "my_pkg.pipelines:train_xgb")

dataset = project.create_dataset("credit-card-transactions")
version = dataset.create_version(storage_uri="s3://…", row_count=284_807)

run = project.train(dataset_version=version, pipeline="xgboost-training", wait=True)

model = project.get_model("fraud-xgboost")
model.production_version
model.rollback_to(3)

project.readiness(version)
project.lineage.for_model_version(mv_id)
project.report(mv_id, format="markdown")
```

Các dataclass trả về (`MLOpsDataset`, `MLOpsRun`, `MLOpsModelVersion`, …)
là ảnh chụp tách rời — chúng không phải object ORM còn gắn session.

**Ranh giới được kiểm bằng test AST tĩnh:** hai case study dưới
`case_studies/` chỉ được phép import `mlops_framework.sdk`, không được
chạm vào manager. Đó là bằng chứng SDK thật sự đủ dùng, chứ không phải
một lớp bọc mỏng phải chọc thủng liên tục.

## 3. HTTP API

**64 endpoint** dưới `/api`, cộng hai probe ở gốc (`/health`, `/ready`).
`test_app_factory.py` chốt cứng con số này — đó là chuông báo khi có
route bị thêm hoặc mất ngoài ý muốn.

| Nhóm | Vai trò |
|---|---|
| Bản ghi framework | mặt tiền mỏng trên manager — không logic mới |
| Lineage | JSON đồ thị |
| Proxy Airflow | trạng thái DAG/task trực tiếp |
| Điều khiển task Airflow | clear/retry — có gác |
| Proxy MLflow | dữ liệu run/experiment trực tiếp |
| Settings & policies | cấu hình hiệu lực + chính sách sửa được |
| Audit & alerts | hai dòng thời gian |
| API keys | scope `admin` |
| Internal | callback của DAG — **cả router** đều bị gác |
| Health | liveness + readiness |

### `/api/internal/*` — vì sao nó riêng biệt

Đây là bề mặt máy-nói-với-máy: DAG Airflow gọi ngược qua đây vì ảnh
Airflow không cài được framework (xung khắc SQLAlchemy 1.4 vs 2.0).

**Cả router** bị gác, kể cả GET: `/context` và
`/dataset-versions/{id}` phát ra `storage_uri`, không phải thứ để trả
cho người lạ.

Các endpoint tạo là **get-or-create** khi có khoá tự nhiên (tên dataset,
tên model) — script client chạy lại là chuyện bình thường, và 409 sẽ đẩy
logic retry vào mọi caller.

### Quy ước

- Danh sách: `limit`/`offset`, tổng chưa phân trang ở header `X-Total-Count`
- "Chưa có kết quả" là `null` + **200**, không phải 404 (readiness, drift)
- Thao tác xếp hàng trả **202** (`/drift/{id}/check`, `POST /training-runs`)
- Mặt tiền proxy là best-effort: MLflow/Airflow chết làm suy giảm một
  panel, không làm hỏng trang

## 4. Console Gateflow

Server-render, HTML + JS thuần, **không có bước build**.

Vỏ console — top nav, side nav, khung nội dung — được ghép ở
`ui/mount.py`; mỗi file dưới `templates/` là một **fragment**: phần bên
trong `<main>` cộng dòng `<script>` khởi động trang đó. Giữ vỏ ở một chỗ
là thứ khiến điều hướng nhất quán; trước đó mỗi trang mang một bản sao
header riêng và chúng lệch nhau.

| Trang | Route |
|---|---|
| Dashboard | `/`, `/dashboard` |
| Datasets | `/datasets`, `/datasets/{id}` |
| Runs | `/runs`, `/runs/{id}`, `/runs/compare` |
| MLflow run | `/mlflow-runs/{mlflow_run_id}` |
| Model registry | `/models`, `/models/{id}` |
| Scheduling | `/schedules` |
| Lineage | `/lineage` |
| Pipelines | `/pipelines`, `/pipelines/{dag_id}` |
| Activity | `/activity` |
| Settings | `/settings` |

`/experiments` và `/experiments/{id}` **redirect** về `/runs` — chúng
từng là trang riêng, nay gộp vào; redirect để bookmark cũ vẫn tới được
chỗ có ích.

### Cache busting

Không có bước build nghĩa là `app.css` / `app.js` / favicon luôn được
yêu cầu từ đúng một đường dẫn ở mọi lần deploy. `_asset_version()` băm
nội dung file lúc import và gắn vào `?v=` — URL tự đổi khi nội dung đổi.
Favicon đặc biệt cần điều này: trình duyệt cache nó bướng bỉnh hơn hẳn
stylesheet.

### `app.js` được nạp trong `<head>` **không** `defer`

Mỗi fragment kết thúc bằng một lời gọi `init*()` inline, và script inline
chạy trong lúc parse — trước mọi script `defer`. Nạp sớm là thứ giữ cho
lời gọi ấy hợp lệ. File chỉ khai báo hàm ở top level nên không có gì chạy
trước khi DOM nó cần tồn tại.

### Thao tác ghi từ console

Đi qua `apiWrite()`, thứ gắn credential và thử lại một lần trên 401/403.
Xem [14-security.md](14-security.md) §7.

| Nút | Trang |
|---|---|
| Train now | dataset version |
| Run check (drift) | dataset version (ẩn ở v1) |
| Roll back | model version ARCHIVED/APPROVED |
| Run now / Enable / Disable / Delete | schedules |
| Clear / Retry | task Airflow trên run detail |

## 5. Giới hạn đã biết

- Console không có login; GET không bị gác.
- `app.js` là một file ~3.660 dòng chứa `init*` của mọi trang, và được
  nạp trên mọi trang.
- Không có realtime nào ngoài SSE của run detail.
