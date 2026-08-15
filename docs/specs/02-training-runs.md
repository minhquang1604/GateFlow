# 02. Vòng đời training run

> Mã nguồn: `src/mlops_framework/training/` · Bảng: `training_runs`
> Kiểm chứng: `tests/unit/test_lifecycle.py`, `test_tracker.py`, `tests/integration/test_training_lifecycle.py`

## 1. Mục đích

Một `TrainingRun` là bản ghi của framework về một lần huấn luyện: nó
chạy trên dataset version nào, ai kích hoạt, kết thúc ra sao, và trỏ tới
run tương ứng bên experiment tracker. Nó tồn tại **độc lập** với
orchestrator đã thực thi nó.

## 2. Mô hình dữ liệu

| Cột | Kiểu | Ghi chú |
|---|---|---|
| `id` | int, PK | |
| `dataset_version_id` | FK, **not null** | không có run nào không có dữ liệu |
| `pipeline_id` | str(255), null, indexed | ý nghĩa **khác nhau** theo orchestrator — xem §5 |
| `status` | enum | `PENDING`/`RUNNING`/`SUCCESS`/`FAILED`/`CANCELLED` |
| `trigger_type` | enum | `MANUAL`/`SCHEDULED`/`DRIFT`/`API` |
| `started_at`, `completed_at` | datetime, null | |
| `mlflow_run_id` | str(64), null, indexed | cầu nối sang tracker |
| `error_message` | text, null | |
| `metadata_json` | text, null | xem §3 |

## 3. Hành vi & quy tắc

### State machine

```
PENDING ──▶ RUNNING ──▶ SUCCESS
   │           ├──────▶ FAILED
   │           └──────▶ CANCELLED
   └──────────────────▶ CANCELLED
```

`SUCCESS`, `FAILED`, `CANCELLED` là trạng thái cuối. **Không có cạnh
`PENDING → FAILED`** — đây là ràng buộc đã cắn hai lần:

- `POST /internal/training-runs/{id}/finish`: một DAG nổ ngay task đầu
  vẫn *đã thực thi*, nên endpoint đẩy run qua `RUNNING` rồi mới `FAILED`,
  thay vì từ chối báo cáo và để run mắc kẹt ở `PENDING` vĩnh viễn.
- `POST /api/training-runs`: khi Airflow từ chối nhận run, handler cũng
  phải đi qua `RUNNING` trước khi đánh dấu `FAILED`.

### `metadata_json` — các khoá framework thực sự đọc

| Khoá | Ai ghi | Ai đọc |
|---|---|---|
| `training_entrypoint` | caller | DAG (`_resolve_entrypoint`) |
| `owned_by_workflow` | `RetrainingWorkflow.run()` | DAG — bỏ qua register/report của chính nó |
| `orchestrator_execution_id` | `TrainingService.start_run` | polling, UI |
| `orchestrator_result` | `/finish` | `RetrainingWorkflow._resolve_candidate_metrics` |
| `tracker_run_id`, `tracking_uri` | `/start` | DAG |
| `model_name`, `min_f1` | caller | task `register_and_promote` của DAG |

### `TrainingService` — chỗ ghép ba thành phần

`TrainingService(training_manager, orchestrator, tracker)` là nơi duy
nhất ba trừu tượng gặp nhau:

- `create_run()` — tạo dòng `PENDING`
- `start_run()` — mở run bên tracker → trigger orchestrator → chuyển
  `RUNNING`. Trả về execution id.
- `wait_for_completion()` — poll orchestrator, hợp nhất trạng thái, đóng
  run. Metrics báo cáo ngoài luồng (qua `/finish`) **sống sót** qua bước
  hợp nhất này.

## 4. Giao diện

```python
run = project.train(dataset_version=v, pipeline="xgboost-training",
                    parameters={"max_depth": 6}, wait=True)
run.status, run.metrics
```

| HTTP | Scope | Ghi chú |
|---|---|---|
| `GET /api/training-runs` | — | lọc `status`, `dataset_version_id`, `limit` |
| `GET /api/training-runs/{id}` | — | |
| `GET /api/training-runs/{id}/events` | — | SSE, chỉ phát khi trạng thái **đổi** |
| `POST /api/training-runs` | `write` | tạo **và** start, trả 202 |
| `POST /api/internal/training-runs` | `write` | chỉ tạo (đường của DAG) |
| `POST /internal/training-runs/{id}/start` | `write` | |
| `POST /internal/training-runs/{id}/finish` | `write` | callback của DAG |

### `POST /api/training-runs` — hai điều bắt buộc

1. **Tạo và start là một lời gọi.** Một run `PENDING` không bao giờ được
   start trông y hệt một run treo với mọi người đang nhìn `/runs`.
2. **Phải commit trước khi trigger.** DAG resolve run bằng cách gọi
   ngược `GET /internal/training-runs/{id}/context` qua HTTP — một kết
   nối khác, một transaction khác, không nhìn thấy được ghi chưa commit.
   Không commit thì DAG 404 ngay khoảnh khắc `resolve_context` hỏi.
   (`internal.py` không dính lỗi này vì create và start là hai request
   riêng, mỗi cái tự commit khi trả về.)

### SSE

Poll **bên trong server** (`_SSE_POLL_SECONDS = 2.0`), không phải trình
duyệt tự poll: mỗi tab tự đặt timer sẽ nhân số truy vấn lên. Stream có
trần `_SSE_MAX_SECONDS = 1800` để một tab bị quên không giữ kết nối mãi
khi orchestrator chết mà không gọi `/finish`.

## 5. Quyết định thiết kế

**`pipeline_id` mang hai nghĩa.** Với `LocalDockerOrchestrator` nó là
`"module:callable"`; với `AirflowOrchestrator` nó là `dag_id`. Vì thế
callable thật đi riêng trong `metadata["training_entrypoint"]`. Nhầm hai
thứ này từng khiến DAG `mlops_training_pipeline` import *chính nó* rồi
lỗi vì không tìm thấy `main`.

**Vì sao SSE chỉ phát khi đổi trạng thái?** Một run `RUNNING` trong 20
phút không nên tạo ra 600 sự kiện giống hệt nhau.

## 6. Giới hạn đã biết

- `wait_for_completion` là polling, không phải callback/event bus.
- `MLflowTracker` giữ active run trong biến toàn cục của process — hai
  run song song trong cùng một process sẽ chồng lên nhau. Xem
  [04-experiment-tracking.md](04-experiment-tracking.md).
