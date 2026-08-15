# 12. Truy vết nguồn gốc (lineage)

> Mã nguồn: `src/mlops_framework/lineage/manager.py`
> Kiểm chứng: `tests/unit/test_lineage.py`, `tests/api/test_lineage_api.py`

## 1. Mục đích

Trả lời được, từ **bất kỳ** mắt xích nào trong chuỗi, câu hỏi "cái này
đến từ đâu và nó dẫn tới đâu":

```
Dataset → DatasetVersion → TrainingRun → ModelVersion → ServingInstance
```

Đây là thứ biến "model nào đang chạy" thành "model nào đang chạy, huấn
luyện trên dữ liệu nào, bởi pipeline nào, và tôi có thể tái lập nó".

## 2. Mô hình đồ thị

`LineageGraph` gồm `nodes[]` và `edges[]`.

`LineageNode.type` là một trong: `Dataset` · `DatasetVersion` ·
`TrainingRun` · `Model` · `ModelVersion` · `ServingInstance`.

| Node | `attributes` đáng chú ý |
|---|---|
| `Dataset` | `name` |
| `DatasetVersion` | `version_number`, `storage_uri`, `row_count`, `schema_hash` |
| `TrainingRun` | `status`, **`pipeline_id`**, **`mlflow_run_id`** |
| `ModelVersion` | `version_number`, `state`, `metrics` |
| `ServingInstance` | `serving_instance_id`, `is_active` |

`LineageEdge.type` là một trong năm quan hệ, viết thường:

| Edge | Nối |
|---|---|
| `has_version` | Dataset → DatasetVersion, Model → ModelVersion |
| `trained_on` | DatasetVersion → TrainingRun |
| `trained_with` | TrainingRun → DatasetVersion (chiều ngược) |
| `produced` | TrainingRun → ModelVersion |
| `served_by` | ModelVersion → ServingInstance |

`TrainingRun` mang `pipeline_id` và `mlflow_run_id` trong attributes vì
đó chính là hai thứ người ta cần để đi từ đồ thị sang MLflow hoặc
Airflow mà xem chi tiết.

## 3. Ba điểm vào

```python
LineageManager(session).graph_for_dataset_version(id)   # xuôi
LineageManager(session).graph_for_training_run(id)      # cả hai chiều
LineageManager(session).graph_for_model_version(id)     # ngược
```

Đồ thị được đi **cả hai chiều** từ điểm vào, nên
`graph_for_model_version` trả về cả dataset đã sinh ra nó lẫn serving
instance đang chạy nó.

| HTTP |
|---|
| `GET /api/lineage/dataset-version/{id}` |
| `GET /api/lineage/training-run/{id}` |
| `GET /api/lineage/model-version/{id}` |

## 4. Báo cáo tái lập

`sdk/report.py::build_report(session, model_version_id, format=…)` dựng
một tài liệu **tự chứa** (markdown hoặc HTML) từ chính đồ thị này: dataset
version + checksum + schema hash, pipeline, tham số, metrics, artifact
URI, và các quyết định quản trị đã dẫn tới promotion.

Cùng một hàm phục vụ cả `MLOpsProject.report()` và
`GET /api/model-versions/{id}/report` — nút "Download report" trên
console hoạt động mà không cần process Python nào chạy SDK.
`Content-Disposition: attachment` khiến trình duyệt lưu file thay vì điều
hướng tới nó.

## 5. Giới hạn đã biết

- Lineage đọc từ khoá ngoại; một `ModelVersion` đăng ký ngoài luồng với
  `training_run_id = NULL` sẽ có đồ thị đứt đoạn.
- Không có phiên bản hoá của chính đồ thị — nó luôn phản ánh trạng thái
  *hiện tại* của các bảng.
