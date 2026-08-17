# 12. Truy vết nguồn gốc (lineage)

> Mã nguồn: `src/mlops_framework/lineage/manager.py`
> Kiểm chứng: `tests/unit/test_lineage.py`, `tests/api/test_lineage_api.py`

## 1. Mục đích

Trả lời được, từ **bất kỳ** mắt xích nào trong chuỗi, câu hỏi "cái này
đến từ đâu và nó dẫn tới đâu":

```
DatasetVersion → TrainingRun → ModelVersion → ServingInstance
```

cộng với `DatasetVersion → DatasetVersion` (`derived_from`) khi một
version được dựng bằng cách mở rộng version trước — trường hợp
retraining, V2 = V1 + dữ liệu production đã lệch.

Đây là thứ biến "model nào đang chạy" thành "model nào đang chạy, huấn
luyện trên dữ liệu nào, bởi pipeline nào, và tôi có thể tái lập nó".

## 2. Mô hình đồ thị

`LineageGraph` gồm `nodes[]` và `edges[]`.

`LineageNode.type` là một trong bốn: `DatasetVersion` · `TrainingRun` ·
`ModelVersion` · `ServingInstance`.

| Node | `attributes` đáng chú ý |
|---|---|
| `DatasetVersion` | `dataset_id`, `dataset_name`, `row_count`, `schema_hash` |
| `TrainingRun` | `status`, **`pipeline_id`**, **`mlflow_run_id`** |
| `ModelVersion` | `model_id`, `model_name`, `state` |
| `ServingInstance` | `serving_instance_id`, `is_active` |

`LineageEdge.type` là một trong bốn quan hệ, viết thường:

| Edge | Nối |
|---|---|
| `trained_with` | DatasetVersion → TrainingRun |
| `produced` | TrainingRun → ModelVersion |
| `served_by` | ModelVersion → ServingInstance |
| `derived_from` | DatasetVersion → DatasetVersion (cha → con) |

`TrainingRun` mang `pipeline_id` và `mlflow_run_id` trong attributes vì
đó chính là hai thứ người ta cần để đi từ đồ thị sang MLflow hoặc
Airflow mà xem chi tiết.

### Một node cho mỗi version, không phải hai

Trước đây `Dataset` và `Model` là hai node riêng, nối tới version của
chúng bằng cạnh `has_version` (`Dataset:1 --has_version--> DatasetVersion:1`).
Tách như vậy không thêm thông tin gì — tên là tĩnh, còn version mới là
thứ mang attributes và cạnh xuôi dòng khác nhau mỗi lần. Giờ tên được
gộp thẳng vào label của node version (`"{name} v{n}"`, ví dụ
`"credit-card-fraud v1"`), `has_version` không còn tồn tại — một thẻ,
không phải hai, và một loại cạnh ít hơn phải giải thích.

Cùng lý do đó xoá luôn cạnh dư thừa `trained_on`
(`DatasetVersion → ModelVersion` trực tiếp) mà `graph_for_model_version`
từng vẽ song song với đường nhân quả thật:
`DatasetVersion --trained_with--> TrainingRun --produced--> ModelVersion`.
Hai mũi tên cùng đổ vào một node từ hai nguồn chồng lấp đọc như nhiễu,
không phải hai sự kiện — bỏ đi, chỉ giữ đường thật: một model version
được huấn luyện *qua* một run, chưa bao giờ trực tiếp.

### `derived_from`

Nửa còn lại của lineage: nó trả lời *vì sao* một version tồn tại, chứ
không chỉ cái gì được dựng từ nó. Không có nó, một version dựng bằng
cách mở rộng version trước không phân biệt được với một version từ đâu
rơi xuống, và chuỗi phía sau một model đã retrain dừng lại ở dữ liệu nó
học, không đi tiếp tới dữ liệu đã gây ra dữ liệu đó. Cạnh này đến từ
`dataset_versions.parent_version_id` (migration `011`), nullable — phần
lớn version không có cha. Vòng đi lên có chặn chu trình: framework không
tạo được vòng, nhưng một hàng sửa tay không được phép treo console.

## 3. Bốn điểm vào — cùng một đồ thị "cả họ"

```python
LineageManager(session).graph_for_dataset(dataset_id)        # cả dataset
LineageManager(session).graph_for_dataset_version(id)
LineageManager(session).graph_for_training_run(id)
LineageManager(session).graph_for_model_version(id)
```

Cả bốn đều trả về **cùng một đồ thị** — mọi version của dataset đó,
song song, mỗi version với toàn bộ nhánh phía sau nó (mọi training run,
mọi model version, mọi serving instance) — chỉ khác nhau ở `root_id`:
điểm vào cụ thể được đánh dấu để UI highlight, còn `graph_for_dataset`
(không có một node khởi đầu tự nhiên) mặc định `root_id` là version
**mới nhất**, tức "cái hiện tại".

Đây là điểm khác biệt lớn nhất so với bản trước: một dataset có V1 đã
archived và V2 đang production hiện đủ cả hai nhánh trong cùng một đồ
thị, dù bạn bắt đầu từ node nào — không còn tình trạng vào từ V1 thì
không thấy V2, vào từ V2 thì không thấy nhánh serving cũ của V1. Ba
public method còn lại (rooted ở version/run/model cụ thể) chỉ là
`graph_for_dataset` với `root_id` ghim lại đúng điểm bạn bấm vào.

| HTTP |
|---|
| `GET /api/lineage/dataset/{id}` |
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

- Lineage đọc từ khoá ngoại; một `ModelVersion` không có
  `training_run_id` vẫn hiện (tìm qua `dataset_version_id` trực tiếp,
  không chỉ qua run), nhưng thiếu cạnh `produced`.
- `derived_from` chỉ ghi **một** cha. Một version gộp từ nhiều nguồn
  ngang hàng phải chọn một làm cha; phần còn lại nằm trong
  `metadata_json["derivation"]` chứ không thành cạnh.
- Một model có version huấn luyện trên **hai dataset khác nhau** (hiếm,
  framework không cấm) sẽ không thấy đủ trong một lần gọi
  `graph_for_dataset` — mỗi lệnh gọi chỉ đi theo một dataset. Không phải
  vấn đề với vòng lặp khép kín (một model, một dataset, nhiều version).
- Không có phiên bản hoá của chính đồ thị — nó luôn phản ánh trạng thái
  *hiện tại* của các bảng.
