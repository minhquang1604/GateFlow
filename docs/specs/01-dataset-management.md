# 01. Quản lý & phiên bản hoá dataset

> Mã nguồn: `src/mlops_framework/dataset/` · Bảng: `datasets`, `dataset_versions`
> Kiểm chứng: `tests/unit/test_checksum.py`, `test_schema_hash.py`, `test_dataset_content_verification.py`

## 1. Mục đích

Trả lời được câu "model này được huấn luyện trên **chính xác** dữ liệu
nào" sau khi mọi thứ đã chạy xong. Một `Dataset` là một khái niệm logic
(*"giao dịch thẻ tín dụng"*); một `DatasetVersion` là một ảnh chụp bất
biến của khái niệm đó tại một thời điểm.

## 2. Mô hình dữ liệu

### `datasets`
| Cột | Kiểu | Ghi chú |
|---|---|---|
| `id` | int, PK | |
| `name` | str(255), **unique** | khoá tự nhiên; API dùng để get-or-create |
| `description` | text, null | |

### `dataset_versions`
| Cột | Kiểu | Ghi chú |
|---|---|---|
| `id` | int, PK | |
| `dataset_id` | FK → `datasets`, cascade | |
| `version_number` | int | tăng dần trong phạm vi một dataset, bắt đầu từ 1 |
| `storage_uri` | str(512) | `s3://…`, đường dẫn cục bộ, … — framework không diễn giải |
| `checksum` | str(64) | SHA-256 |
| `schema_hash` | str(64) | SHA-256 |
| `row_count` | bigint | |
| `metadata_json` | text, null | tuỳ ý; `content_sha256` là khoá được framework hiểu |
| `is_immutable` | bool | luôn `True` khi tạo |

## 3. Hành vi & quy tắc

### Bất biến (immutability)

`is_immutable` được đặt `True` ngay khi tạo và `DatasetManager` **không
phơi ra bất kỳ phương thức xoá hay sửa nào**. Mọi đường ghi đều đi qua
`_ensure_mutable()`, hàm này ném `ImmutableDatasetVersionError` cho mọi
version. Nói cách khác, tính bất biến ở đây là *sự vắng mặt của thao
tác*, không phải một thao tác bị canh gác.

> `tests/integration/test_complete_flow.py::test_version_cannot_be_deleted`
> kiểm cả hai vế: manager không có method nào chứa `delete`/`remove`, và
> guard thực sự từ chối.

### Đánh số version

`_get_next_version_number()` lấy `max(version_number) + 1` trong phạm vi
`dataset_id`. Không dùng sequence toàn cục — số version là thuộc tính của
dataset, và người đọc mong đợi mỗi dataset bắt đầu từ v1.

### Hai loại hash, hai mục đích khác nhau

| | `checksum` | `schema_hash` |
|---|---|---|
| Băm cái gì | `storage_uri` + metadata | danh sách `(tên cột, dtype)` đã chuẩn hoá |
| Trả lời | "đây có phải cùng một bản ghi version không" | "cấu trúc dữ liệu có đổi không" |
| Đổi khi | URI hoặc metadata đổi | thêm/xoá/đổi kiểu cột |

`schema_hash` là thứ readiness engine so sánh giữa các version để phát
hiện schema drift, và là thứ báo cáo tái lập (reproducibility report)
ghi lại.

### `content_sha256` — băm nội dung thật

`checksum` **không** băm nội dung file (framework không đọc file). Vì
thế client tính SHA-256 của file và đặt vào
`metadata["content_sha256"]`. Hai hệ quả:

1. `POST /api/internal/datasets/{id}/versions` sẽ **trả về version cũ**
   nếu `content_sha256` trùng, thay vì tạo version mới. Nếu không, một
   script chạy lại trên dữ liệu không đổi sẽ đẻ ra version mới mỗi lần
   và phá vỡ lời hứa "một version ghim một tập dữ liệu".
2. Worker từ xa (DAG Airflow) nhận `dataset_content_sha256` qua
   `/internal/training-runs/{id}/context` và có thể tự xác nhận nó đọc
   đúng những byte đã được đăng ký.

## 4. Giao diện

```python
dataset = project.create_dataset("credit-card-fraud", description="…")
version = dataset.create_version(
    storage_uri="s3://bucket/v1.parquet",
    row_count=284_807,
    metadata={"columns": [{"name": "v1", "dtype": "float64"}, …],
              "content_sha256": "…"},
)
dataset.versions          # list[MLOpsDatasetVersion]
dataset.latest_version    # version_number lớn nhất
```

| HTTP | Ghi chú |
|---|---|
| `GET /api/datasets` | có phân trang `limit`/`offset`, tổng ở header `X-Total-Count` |
| `GET /api/datasets/{id}` | |
| `GET /api/datasets/{id}/versions` | |
| `GET /api/dataset-versions/{id}` | |
| `POST /api/internal/datasets` | get-or-create theo tên, cần scope `write` |
| `POST /api/internal/datasets/{id}/versions` | dedupe theo `content_sha256` |
| `GET /api/internal/dataset-versions/{id}` | trả `storage_uri` cho DAG; bị gác vì URI không phải thứ phát công khai |

## 5. Quyết định thiết kế

**Vì sao get-or-create chứ không 409?** Script client chạy lại là
chuyện bình thường. Trả 409 sẽ đẩy logic retry vào mọi caller.

**Vì sao `GET /api/datasets` phải phân trang?** Trước đây nó trả về *mọi*
dataset rồi chạy thêm 2 truy vấn mỗi dòng để điền `version_count` và
`latest_version` — 2N+1 truy vấn, đo được **101 SELECT cho 50 dataset**.
Nay là 3 truy vấn gộp, phẳng ở 4 SELECT bất kể số dòng.
`tests/api/test_list_paging.py` đếm bằng SQLAlchemy event listener thật,
ở hai mức số dòng, vì "đã bỏ N+1" đúng là loại khẳng định dễ âm thầm sai
trở lại.

## 6. Giới hạn đã biết

- `checksum` không phải băm nội dung. Muốn ghim nội dung phải chủ động
  gửi `content_sha256`.
- Framework không đọc, không xác thực, không di chuyển dữ liệu ở
  `storage_uri`. URI hỏng chỉ lộ ra khi orchestrator cố đọc nó.
