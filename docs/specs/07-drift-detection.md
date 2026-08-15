# 07. Phát hiện drift

> Mã nguồn: `src/mlops_framework/drift/` · Bảng: `drift_evaluations`
> DAG: `infrastructure/airflow/dags/mlops_drift_check.py`
> Kiểm chứng: `tests/unit/test_drift.py`, `tests/api/test_drift_api.py`

## 1. Mục đích

Phát hiện phân phối dữ liệu đã dịch chuyển so với dữ liệu model được
huấn luyện trên đó, và **lưu lại** phán quyết ấy như một sự kiện quản trị
kiểm toán được.

## 2. Hợp đồng (`DriftDetector` ABC)

```python
detect(reference_data, current_data, config) -> DriftResult
```

`DriftResult` chuẩn hoá: `drift_detected` · `score` · `method` ·
`threshold` · `feature_results[]` · `method_summary`.

### `ScipyDriftDetector` (bản tham chiếu)

- Đặc trưng số → **Kolmogorov–Smirnov**
- Đặc trưng phân loại → **chi-square**
- `threshold` là ngưỡng p-value mỗi đặc trưng (mặc định `0.05`)
- Thiếu scipy → tự lùi về detector dựa trên thống kê phân phối
  (dịch chuyển mean/std), để framework vẫn chạy được trong môi trường tối
  giản

### `DriftConfig`
`threshold=0.05` · `min_samples=30` · `methods=["ks", "chi2"]`

## 3. `DriftService`

Bọc detector và **lưu** mỗi lần đánh giá vào `drift_evaluations`:
`reference_dataset_version_id`, `current_dataset_version_id`, `method`,
`outcome` (`DRIFT_DETECTED`/`NO_DRIFT`), `score`, `threshold`,
`details_json`, `notes`.

Khi không truyền `config`, service lấy `DriftConfig` từ
`FrameworkSettingsManager` — "đọc mặc định đã lưu của framework" thuộc về
tầng điều phối của framework, không nên nướng vào một implementation cụ
thể, vì `DriftDetector` vốn được thiết kế để thay thế được.

## 4. Đọc kết quả

`GET /api/drift/{version_id}` trả về đánh giá **mới nhất** liên quan tới
version đó — ở **cả hai phía** của phép so sánh (reference hoặc current).

"Chưa đánh giá" là `null` với HTTP **200**, không phải 404 — giống quy
ước của `GET /readiness/{version_id}`. Console render một panel rỗng chứ
không coi đó là lỗi. 404 dành riêng cho dataset version thật sự không
tồn tại.

## 5. Chạy drift theo yêu cầu — và vì sao nó đi qua Airflow

Framework **không đọc file dataset**. `DriftService` nhận sẵn giá trị đặc
trưng từ caller, và không có chỗ nào dưới `src/` mở object S3 hay CSV
chứa dữ liệu huấn luyện. Đó là ranh giới được giữ nhất quán.

Hệ quả: nút "Run check" trên console **không thể** tự tính drift, và
endpoint API cũng vậy. Cấp cho container app quyền S3 cộng một CSV 144 MB
nằm trong request handler với hạn mức 256 MiB chính là hình dạng của sự
cố đã từng giết gunicorn worker của Airflow.

Nên việc được đưa **tới nơi dữ liệu đã sẵn ở đó**:

```
console          app                        airflow
-------          ---                        -------
Run check   ──▶  POST /api/drift/{id}/check
                 trigger DAG            ──▶  đọc 2 CSV từ S3
                                             lấy mẫu, giao đặc trưng chung
                 POST /api/internal/drift ◀──
                 detector + ngưỡng + phán quyết
                 lưu DriftEvaluation
panel cập nhật khi DAG xong
```

### DAG chỉ làm I/O, không quyết định gì

DAG gửi **giá trị đặc trưng**, không gửi phán quyết. Framework chọn
detector, áp ngưỡng đã cấu hình, kết luận, và ghi dòng
`DriftEvaluation`. Một DAG tự tính phán quyết thì có thể khẳng định bất
cứ điều gì, và dòng dữ liệu ấy trở thành *lời khai của client* chứ không
phải kết luận của framework. Đây đúng cách tách như
`resolve_context`/`readiness` trong `mlops_training_pipeline.py`.

### Lấy mẫu là chuyện truyền tải, không phải thống kê

Mặc định 5000 dòng/đặc trưng. Kiểm định KS đi tới kết luận trên vài nghìn
điểm; gửi 284.807 giá trị mỗi đặc trưng qua HTTP chỉ tới cùng câu trả lời
chậm hơn. Seed lấy từ `dag_run.run_id` nên retry cùng một run so sánh
đúng những dòng cũ.

### DAG loại bỏ cột nào

Nhãn (`class`, `label`, `target`, `y`) và cột sổ sách (`time`,
`timestamp`, `id`). Một cột `time` tăng đơn điệu **drift theo định
nghĩa** trên mọi batch mới, sẽ khiến mọi lần kiểm đều dương tính.

Chỉ **giao** của các cột số hai bên được so sánh: một đặc trưng chỉ có ở
một phía thì không có phân phối tham chiếu.

### Endpoint

| | |
|---|---|
| `POST /api/drift/{id}/check` | scope `write`, trả **202** |
| Body | `reference_version_id` (mặc định: version liền trước), `sample_size` (100–100 000) |
| 422 | version đầu tiên của dataset — không có gì để so sánh |
| 422 | so sánh version với chính nó |
| 503 | chưa cấu hình `AIRFLOW_BASE_URL` |

202 chứ không phải 200: nó trả về khi DAG run được **xếp hàng**. Phán
quyết xuất hiện ở `GET /api/drift/{id}` khi DAG xong, y như kết quả một
training run.

Nút trên console bị **ẩn** ở version 1 — endpoint đúng đắn trả 422 ở đó,
và một nút luôn luôn lỗi còn tệ hơn không có nút.

## 6. Giới hạn đã biết

- `POST /api/internal/drift` chỉ nhận đặc trưng **số** trong một mapping;
  drift phân loại qua đường DAG là việc còn phải làm.
- Lấy mẫu không phân tầng — với dữ liệu rất mất cân bằng, mẫu 5000 dòng
  có thể chứa rất ít lớp thiểu số.
