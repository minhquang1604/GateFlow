# 04. Trừu tượng hoá experiment tracker

> Mã nguồn: `src/mlops_framework/tracking/`
> Kiểm chứng: `tests/unit/test_mlflow_tracker.py`, `tests/integration/test_mlflow_live.py`, `test_mlflow_registry_sync.py`

## 1. Mục đích

Ghi lại params/metrics/artifact của một lần huấn luyện, mà không buộc
framework phụ thuộc vào MLflow.

## 2. Hợp đồng (`ExperimentTracker` ABC)

```python
start_run(run_name=None, tags=None) -> str      # tracker run id
log_params(params) / log_metrics(metrics)
log_artifact(local_path)
end_run(status)
```

`RunStatus` của framework (3 trạng thái cuối) được ánh xạ sang MLflow:
`SUCCESS→FINISHED`, `FAILED→FAILED`, `CANCELLED→KILLED`. Giá trị lạ ánh
xạ về `FAILED` — thà bị gắn cờ để xem lại còn hơn trông như đã xong.

## 3. Adapter

### `MLflowTracker`

Import `mlflow` lười. Thiếu gói → `ExperimentTrackingError` với thông
điệp rõ ràng ngay lúc khởi tạo. Thứ tự ưu tiên tham số: kwargs tường
minh > `Settings` từ biến môi trường.

**Giới hạn đã biết, đã được pin bằng test:** `mlflow.start_run` giữ
active run trong trạng thái **toàn cục của process**, còn adapter này
theo dõi theo instance. Hai `MLflowTracker` trong cùng một process không
có run độc lập — cái thứ hai lồng vào cái thứ nhất. Huấn luyện song song
cần process riêng, hoặc viết lại trên `MlflowClient` (nhận `run_id`
tường minh mỗi lời gọi).
`tests/integration/test_mlflow_live.py::TestGlobalRunState` ghim hành vi
hiện tại với server thật.

### `InMemoryTracker`

Thay thế nguyên vẹn cho test. Không I/O.

## 4. `mlflow_registry` — đường ghi vào Model Registry

`api/mlflow_gateway` chỉ đọc; module này là nửa ghi. Nó đẩy quyết định
promotion của framework sang registry của MLflow để hai bên hiển thị
cùng một bức tranh.

| Hàm | Dùng ở đâu |
|---|---|
| `sync_candidate(name, run_id, artifact)` | ngay khi CANDIDATE tồn tại — không đợi được promote |
| `sync_production(name, version)` | stage=Production + alias `champion` |
| `version_for_run(name, run_id)` | **rollback**: tra ngược version từ run id |

**Hợp đồng thiết kế: không hàm nào ở đây được phép ném lỗi.** Dòng
Postgres của framework mới là quyết định quản trị của sự thật. MLflow
chết, chậm, hoặc bất đồng về stage transition là vấn đề *hiển thị bị
suy giảm*, không phải lý do để làm hỏng — hay tệ hơn, nửa vời — một
promotion đã xảy ra trong database.

`version_for_run` sinh ra vì rollback không có handle sẵn: các đường
promotion truyền cho `sync_production` chuỗi version mà `sync_candidate`
vừa trả về, còn rollback khôi phục một version đã đăng ký từ lần chạy
trước, có thể ở process khác — nên phải tra từ run id.

## 5. Quyết định thiết kế

**Vì sao đăng ký CANDIDATE ngay, không đợi promote?** Để registry của
MLflow phản ánh **mọi** lần huấn luyện framework đã ghi nhận, giống hệt
bảng của chính nó. Nếu chỉ đăng ký khi promote, hai bên sẽ lệch nhau và
`reconcile_model_registry` sẽ luôn báo bất đồng giả.

**Vì sao đặt cả stage lẫn alias?** MLflow 3 khuyến nghị alias, nhưng
server đang deploy (2.20.3) vẫn hiển thị cột Stage nổi bật. Đặt cả hai
khớp với bất kỳ nửa nào của UI mà người dùng đang nhìn.

## 6. Giới hạn đã biết

- Một active run mỗi process (xem §3).
- MLflow là tuỳ chọn: framework không bao giờ *cần* nó để import hay chạy.
