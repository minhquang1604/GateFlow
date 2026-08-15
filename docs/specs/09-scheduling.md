# 09. Lập lịch theo cron

> Mã nguồn: `src/mlops_framework/scheduling/` · Bảng: `schedules`
> Kiểm chứng: `tests/unit/test_scheduler_failure_isolation.py`, `tests/integration/test_scheduling_runner.py`, `tests/api/test_schedules_api.py`

## 1. Mục đích

Retrain định kỳ mà không cần ai bấm nút, đi qua **đúng** chuỗi quản trị
mà một retrain do drift kích hoạt phải đi qua.

## 2. Mô hình dữ liệu (`schedules`)

| Cột | Ghi chú |
|---|---|
| `model_id`, `dataset_id` | FK |
| `pipeline_id` | `"module:callable"` — được giao cho `LocalDockerOrchestrator` |
| `cron_expression` | cron 5 trường, validate lúc tạo/sửa |
| `enabled` | bool |
| `parameters_json`, `min_f1`, `notes` | |
| `last_triggered_at` | mốc neo cho `is_due` |
| `last_training_run_id` | |

## 3. Ngữ nghĩa cron

`cron.is_due()` neo tại `last_triggered_at`, hoặc `created_at` nếu chưa
từng chạy. Một schedule tạo lúc 2:55 sáng cho cron "2 giờ sáng hằng ngày"
sẽ train **ngày mai** lúc 2 giờ, chứ không chạy ngay vì một khe sớm hơn
trong ngày đã trôi qua. Đây là ngữ nghĩa cron chuẩn — không bù các lần
đã lỡ trước khi job tồn tại, giống `catchup=False` của Airflow.

### `record_trigger` neo vào lúc **kết thúc**

`is_due` đo lần kế tiếp từ `last_triggered_at`. Ghi lại thời điểm *bắt
đầu* tick nghĩa là một lần chạy dài hơn chu kỳ cron của chính nó đã quá
hạn ngay khoảnh khắc nó trả về — một schedule mỗi phút mà training mất
90s sẽ chạy liên tục nối đuôi nhau vĩnh viễn. Ghi thời điểm kết thúc cho
đúng ngữ nghĩa "đợi lần xuất hiện kế tiếp" bất kể lần chạy tốn bao lâu.

Đây cũng là thứ từng làm
`test_scheduling_runner::test_does_not_double_fire_immediately` flaky: nó
hỏng bất cứ khi nào lần fire đầu vượt qua mốc phút, xác suất ≈
(thời lượng chạy / 60).

## 4. Điều gì xảy ra khi một schedule fire

`_fire()` chạy `RetrainingWorkflow` với **`force=True`** — biểu thức cron
*chính là* quyết định eligibility ở đây. Nó không đánh giá drift (không
có `reference_data`/`current_data`), nên `trigger_type` ra `SCHEDULED`
chứ không phải `DRIFT`.

Config: `FrameworkSettings` đã lưu là nền, `schedule.min_f1` và hai cờ
(`must_beat_production=False`, `allow_cold_start=True`) là override của
riêng call site này.

Dùng `LocalDockerOrchestrator`, không phải Airflow. Đây là lựa chọn có
chủ ý chứ không phải hạn chế: một retrain theo lịch không có người vận
hành ngồi nhìn, và kết quả subprocess của orchestrator cục bộ có ngay
đồng bộ.

## 5. Cô lập lỗi — bất biến quan trọng nhất của module

`run_due_schedules` từng gọi `_fire` **không** có try/except cho từng
schedule, và chỉ tới được `record_trigger` trên nhánh thành công. Một
schedule hỏng vì thế gây ra **hai** hậu quả:

1. Mọi schedule **đứng sau** nó trong danh sách không bao giờ chạy trong
   tick đó — exception cuốn trôi cả lượt.
2. Schedule hỏng giữ `last_triggered_at = None`, nên nó vẫn "due" và
   fire lại **mỗi 60 giây vĩnh viễn**, mỗi lần đẻ một dòng `TrainingRun`.

Vòng lặp ở `api/app.py` bắt lỗi ở tầng **tick**, giữ được vòng lặp sống
nhưng không thể cho các schedule khác lượt của chúng.

Nay `_record_failure()` hấp thụ lỗi theo từng schedule, làm ba việc
**theo đúng thứ tự**:

1. `session.rollback()` — những gì `_fire` đang làm dở không dùng được
   nữa, và hai thao tác ghi bên dưới cần một transaction lành.
2. Ghi `GovernanceEvent` mức **CRITICAL** (`SCHEDULE_FAILED`) để lỗi hiện
   lên tab Alerts thay vì chỉ nằm trong log container.
3. `record_trigger` đẩy `last_triggered_at` lên — nửa này mới là thứ chặn
   bão retry. Một schedule hỏng mọi lần sẽ hỏng theo **nhịp của chính
   nó** (mỗi giờ với cron hằng giờ), không phải mỗi
   `scheduler_poll_seconds`.

`ScheduleFireResult` có trường `error` tách khỏi `skipped_reason`: "chạy
rồi nổ" khác với "chọn không chạy".

### `run_schedule_now` thì ngược lại

Lỗi được **propagate**. Đường này có caller đang chờ response, nên lỗi
thuộc về response đó chứ không phải một dòng alert họ phải đi tìm. Và
một lần chạy tay thất bại **không** đẩy `last_triggered_at` — làm vậy sẽ
âm thầm ăn mất lượt tự động kế tiếp của schedule.

## 6. Vòng lặp nền

`api/app.py::_start_scheduler` — `asyncio.create_task`, không phải
thread: mỗi tick làm việc thật với database và orchestration, đáng được
huỷ sạch sẽ lúc shutdown hơn là để một daemon thread chạy giữa chừng
trên một kết nối database mà app đang đóng.

Tắt mặc định (`SCHEDULER_ENABLED=false`). Một vòng lặp nền có thể kích
hoạt training thật không có việc gì tự khởi động trong mọi test dựng
app — chỉ trong service đã deploy (docker-compose bật nó cho container
`app`).

## 7. Giao diện

| HTTP | Scope |
|---|---|
| `GET /api/schedules`, `GET /api/schedules/{id}` | — |
| `POST /api/schedules` | `write` |
| `PATCH /api/schedules/{id}` | `write` |
| `DELETE /api/schedules/{id}` | `write` |
| `POST /api/schedules/{id}/run-now` | `write` |

Tạo schedule **không** phải một thao tác chèn dòng vô hại:
`pipeline_id` của nó được giao cho `LocalDockerOrchestrator`, thứ sẽ
import và gọi nó bên trong container app. Xem
[03-orchestration.md](03-orchestration.md) §3.

`run-now` chạy **đồng bộ** — request block tới khi training xong. Pipeline
chậm làm endpoint chậm; đó là câu trả lời trung thực, không phải lý do để
giả vờ có một response async mà API này không có hạ tầng để poll.

## 8. Giới hạn đã biết

- Không có backoff hay auto-disable sau N lần hỏng liên tiếp.
- Một scheduler mỗi deployment; không có bầu cử leader nếu chạy nhiều
  bản app.
