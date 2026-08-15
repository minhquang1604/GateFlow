# 08. Workflow retrain tự động

> Mã nguồn: `src/mlops_framework/workflow/retraining.py`
> Kiểm chứng: `tests/unit/test_retraining_governance_events.py`, `test_approval_gate.py`, `tests/integration/test_governance_end_to_end.py`

## 1. Mục đích

Một lời gọi duy nhất xâu toàn bộ chuỗi quản trị lại với nhau, để "có nên
retrain không, và kết quả có được lên production không" là **một** quyết
định framework sở hữu từ đầu tới cuối — chứ không phải mấy đoạn script
rải rác mỗi nơi làm một kiểu.

## 2. Chuỗi bước

```
1. Readiness        ─ BLOCKED ──▶ dừng, blocked_reason="readiness_blocked"
2. Drift (tuỳ chọn) ─ chỉ chạy khi có cả reference_data và current_data
3. Eligibility      ─ không đạt ─▶ dừng, blocked_reason="not_eligible"
3b. Approval (tuỳ chọn) ─ bị từ chối ─▶ dừng, blocked_reason="approval_denied"
4. Training         ─ thất bại ──▶ dừng, blocked_reason="training_failed"
5. Đăng ký ModelVersion (CANDIDATE)
6. Promotion policy ─ từ chối ──▶ dừng, blocked_reason="model_rejected"
7. Publish sự kiện MODEL_PROMOTED
```

Mỗi bước sinh một `StepResult(name, passed, detail, data)`.
`RetrainingOutcome` mang toàn bộ dấu vết ấy — đó là thứ khiến workflow
kiểm toán được thay vì chỉ trả về true/false.

### Vị trí của cổng phê duyệt

**Sau** eligibility: không có ích gì khi hỏi một con người về lần retrain
mà các chính sách đã loại. **Trước** training: hỏi trước khi tiêu bất kỳ
compute nào chính là toàn bộ giá trị của cổng. Xem
[10-approval-gate.md](10-approval-gate.md).

## 3. Chi tiết quan trọng

### `trigger_type` được suy ra

`DRIFT` nếu bước drift đã chạy **và** phát hiện drift; ngược lại
`SCHEDULED`.

### `owned_by_workflow`

`run()` đặt cờ này vào metadata của run. Nó nói với DAG Airflow rằng
việc quản trị của run này — đóng run, đăng ký ModelVersion, promote —
do workflow sở hữu trọn vẹn, không phải các task `register_and_promote`
/ `report_status` của DAG.

Thiếu cờ, **cả hai bên** sẽ đua nhau `complete_run()` cùng một
`TrainingRun` và tạo + promote **hai** ModelVersion riêng biệt cho cùng
một lần huấn luyện, đánh giá chúng bằng hai policy khác nhau. Vô hại (và
không được đọc) với `LocalDockerOrchestrator`, vốn không có task nào như
vậy.

### Commit trước khi chờ

```python
self._service.start_run(run.id)
self._session.commit()          # ← bắt buộc
self._service.wait_for_completion(run.id, timeout=training_timeout)
```

`AirflowOrchestrator` vừa giao run cho một process bên ngoài, process ấy
resolve run bằng `GET /internal/training-runs/{id}/context` qua HTTP —
kết nối khác, transaction khác, không nhìn thấy ghi chưa commit.
`LocalDockerOrchestrator` không truy vấn database nên commit này vô hại
với nó; nhưng thiếu nó, một DAG Airflow thật sẽ 404 ngay khoảnh khắc
`resolve_context` hỏi.

### Tìm metrics của candidate

Thứ tự ưu tiên trong `_resolve_candidate_metrics`:

1. Hook `evaluate_model` do caller truyền
2. `metadata["metrics"]`
3. `metadata["orchestrator_result"]["metrics"]` — nơi `/finish` cất báo
   cáo của pipeline phía Airflow
4. Metadata trạng thái execution, truy vấn lại từ orchestrator
5. `{}`

### Thứ tự khi promote

Archive bản production cũ **trước**, promote bản mới **sau**. Thứ tự
ngược lại để lại cửa sổ — dù ngắn — với hai PRODUCTION cùng lúc, thứ mà
partial unique index sẽ từ chối. Xem [05-model-registry.md](05-model-registry.md).

## 4. Giao diện

```python
workflow = RetrainingWorkflow(
    session,
    training_service=service,
    drift_service=DriftService(session, ScipyDriftDetector()),   # tuỳ chọn
    approval_gate=TelegramApprovalGate.from_settings(settings),  # tuỳ chọn
    event_publisher=HttpEventPublisher(url),                     # tuỳ chọn
    actor="schedule:12",
)
outcome = workflow.run(
    dataset_version=v, model=m,
    reference_data=..., current_data=...,     # bật bước drift
    pipeline_id="mlops_training_pipeline",
    training_entrypoint="pkg.pipelines:train",  # chỉ cần với Airflow
    training_timeout=600.0,
    force=False,
)
```

`training_timeout` mặc định 60s — hào phóng cho subprocess của
`LocalDockerOrchestrator`, nhưng **quá ngắn** cho một DAG Airflow nhiều
task thật (các script demo dùng 600s cho cùng DAG đó).

## 5. Giới hạn đã biết

- Chạy đồng bộ; `wait_for_completion` là polling.
- Với Airflow, cần DAG hợp tác qua `owned_by_workflow` (xem §3).
- Chỉ xử lý một cặp (dataset version, model) mỗi lời gọi.
