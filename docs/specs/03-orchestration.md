# 03. Trừu tượng hoá orchestrator

> Mã nguồn: `src/mlops_framework/orchestration/`
> Kiểm chứng: `tests/unit/test_local_orchestrator.py`, `test_local_orchestrator_injection.py`, `test_airflow_orchestrator.py`

## 1. Mục đích

Framework quyết định *có nên* huấn luyện hay không; orchestrator *thực
thi*. Ranh giới này là lý do có thể đổi Airflow sang thứ khác mà không
đụng vào một dòng nào của tầng quản trị.

## 2. Hợp đồng (`Orchestrator` ABC)

```python
trigger_pipeline(pipeline_id, config=None) -> str   # execution_id
get_execution_status(execution_id)        -> ExecutionStatus
cancel_execution(execution_id)            -> ExecutionStatus
```

`ExecutionState`: `PENDING` · `RUNNING` · `SUCCESS` · `FAILED` ·
`CANCELLED` · `UNKNOWN`. Trạng thái riêng của từng hệ thống được chuẩn
hoá về tập này, nên tầng trên không bao giờ phải biết Airflow gọi nó là
gì.

## 3. Adapter

### `LocalDockerOrchestrator`

Chạy pipeline bằng một subprocess Python. Tên giữ chữ "Docker" cho tương
thích về sau với một `DockerOrchestrator` thật; hiện tại **không có
Docker nào cả**.

Hợp đồng gọi: `"package.module:function"`. Subprocess nhận config dạng
JSON qua stdin, ghi một dòng JSON ra stdout để báo cáo
`{"status": "SUCCESS"|"FAILED", …}`. Exit code 0 = SUCCESS.

**Điểm bảo mật quan trọng nhất của module này.** Chương trình con từng
được dựng bằng cách nội suy `pipeline_id` vào **mã nguồn**:

```python
f"from {module} import {fn} as _entry\n"     # ← đã bỏ
```

`pipeline_id` đến nguyên văn từ `POST /api/schedules`. Một giá trị chứa
xuống dòng mà vẫn giữ hai dòng xung quanh hợp lệ cú pháp sẽ thực thi bất
cứ thứ gì. Đã kiểm chứng bằng payload thật, nó ghi được file.

Hiện có **hai lớp phòng thủ độc lập**, mỗi lớp tự nó đủ để đóng lỗ hổng:

1. `_resolve_entry_point` từ chối mọi thứ không phải một đường import có
   dấu chấm cộng một identifier. Regex neo bằng `\A…\Z` chứ **không**
   phải `^…$` — trong Python `$` khớp cả trước newline cuối, nên
   `^\w+$` chấp nhận `"mod\n"`, đúng ký tự mà mẫu này sinh ra để loại.
2. `_BOOTSTRAP` là một chương trình **hằng**. Module và callable đi qua
   `argv` và được giải bằng `importlib.import_module` + `getattr`. Không
   có gì caller cung cấp bị phân tích như mã Python, kể cả nếu lớp (1)
   một ngày nào đó bị nới lỏng.

### `AirflowOrchestrator`

Nói chuyện với Airflow qua REST API (`httpx`). `execution_id` là chuỗi
ghép `"{dag_id}/{dag_run_id}"`; `dag_run_id` có hậu tố uuid vì Airflow
trả 409 cho id trùng và hai lần trigger trong cùng độ phân giải đồng hồ
sẽ va nhau.

Đọc log task **thẳng từ S3** khi `AIRFLOW_REMOTE_LOG_BASE` được đặt, thay
vì proxy qua webserver — webserver bị SIGKILL chính gunicorn worker của
nó dưới hạn mức 768 MiB khi được yêu cầu lấy log từ xa.

**Ảnh Airflow không cài `mlops_framework`.** Airflow 2.10.4 ghim
`SQLAlchemy==1.4.x`, xung khắc với `sqlalchemy>=2.0` của framework. DAG
vì thế nói chuyện với framework qua HTTP (`/api/internal/*`), không phải
in-process.

## 4. Quyết định thiết kế

**Vì sao "cancel" trên Airflow là xoá DAG run?** Airflow 2.x không có
REST endpoint huỷ sạch sẽ; xoá là cách chính thức được tài liệu hoá.

**Vì sao `pipeline_id` không được validate ở tầng API?** Nó *đã* được
validate — nhưng ở nơi hiểu ý nghĩa của nó. Với Airflow đó là một
`dag_id` (Airflow tự giới hạn ký tự), với local đó là đường import. Đặt
một regex chung ở tầng API sẽ sai cho ít nhất một trong hai.

## 5. Giới hạn đã biết

- `LocalDockerOrchestrator` là subprocess, không phải Docker.
- Huỷ trên Airflow xoá bản ghi DAG run.
- `RetrainingWorkflow` + `AirflowOrchestrator` cần DAG hợp tác qua cờ
  `owned_by_workflow`; một DAG tự viết không kiểm cờ này sẽ đăng ký
  ModelVersion hai lần. Xem [08-retraining-workflow.md](08-retraining-workflow.md).
