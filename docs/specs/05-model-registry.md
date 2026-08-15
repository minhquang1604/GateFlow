# 05. Model registry & rollback

> Mã nguồn: `src/mlops_framework/model/` · Bảng: `models`, `model_versions`
> Kiểm chứng: `tests/unit/test_model_rollback.py`, `tests/api/test_rollback_api.py`, `tests/integration/test_model_lifecycle.py`

## 1. Mục đích

Trả lời "phiên bản nào đang chạy production, và nó đến từ đâu" — và, từ
nay, "đưa lại phiên bản đã chạy tốt".

## 2. Mô hình dữ liệu

### `models`
`id` · `name` (unique, indexed) · `description` · `task` (indexed)

### `model_versions`
| Cột | Ghi chú |
|---|---|
| `model_id` | FK, cascade |
| `dataset_version_id` | FK, **not null** — mọi model version đều huấn luyện trên dữ liệu nào đó |
| `training_run_id` | FK, null (có thể đăng ký ngoài luồng) |
| `version_number` | tăng dần trong phạm vi model |
| `state` | xem state machine |
| `mlflow_run_id`, `artifact_uri`, `metrics_json`, `notes` | |

## 3. State machine

```
TRAINING ──▶ CANDIDATE ──▶ APPROVED ──▶ PRODUCTION ──▶ ARCHIVED
   │              │            │                          │
   └──▶ REJECTED ◀┘            └──▶ REJECTED              │
                  CANDIDATE ──▶ PRODUCTION                │
                       APPROVED ◀──────────────────────────┘  (chỉ rollback)
```

`REJECTED` là trạng thái cuối. **`ARCHIVED` thì không** — đó là thay đổi
mà tính năng rollback mang lại.

### Ràng buộc "một PRODUCTION mỗi model"

Ràng buộc thật nằm ở **database**: partial unique index
`uq_model_versions_one_production_per_model` (migration `006`). Kiểm tra
ở tầng ứng dụng (`validate_transition`) chỉ nhìn được state machine của
chính dòng đó — nó không thấy một writer khác đang promote version khác
của cùng model trong transaction song song. `transition_state` bắt
`IntegrityError` và dịch thành `ConcurrentPromotionError`.

Hệ quả về **thứ tự ghi**: mọi đường promote/rollback đều archive bản
đương nhiệm **trước**, rồi mới promote bản mới. Làm ngược lại để lại một
cửa sổ — dù ngắn — với hai PRODUCTION cùng lúc, thứ mà index sẽ từ chối.

## 4. Rollback

### Vì sao cạnh là `ARCHIVED → APPROVED`, không phải `→ PRODUCTION`

"Phê duyệt lại một version đã nghỉ hưu" là mô tả trung thực của điều một
rollback quyết định, và nó **tái dùng** cạnh `APPROVED → PRODUCTION` sẵn
có thay vì thêm một lối tắt thứ hai vào PRODUCTION mà mã khác có thể vô
tình đi vào. `ModelManager.rollback_to()` đi cả hai bước cộng với việc
archive bản đương nhiệm — nó là thứ duy nhất nên đi trên cạnh này.

### Vì sao promotion policy **không** được hỏi

Policy trả lời "candidate này có đủ tốt để thay production không", phán
xét trên metrics. Rollback trả lời câu khác: "production đang hỏng, đưa
lại bản đã chạy được" — và bản được khôi phục đã qua policy đó một lần
rồi. Gate theo metrics sẽ chặn rollback **đúng trong trường hợp nó sinh
ra để phục vụ**: một bản đương nhiệm có metrics offline đẹp hơn bản bạn
cần lấy lại.

> `test_ignores_metrics` ghim điều này: bản đương nhiệm có f1 0.95 so với
> 0.90 của bản được khôi phục, và rollback vẫn đi qua.

Quyết định thuộc về người vận hành; framework **ghi lại thật to** thay vì
đoán già đoán non: một dòng `AuditLog` nêu tên actor và một
`GovernanceEvent` mức **CRITICAL** — vì rollback nghĩa là production đã
sai, không phải một thay đổi sổ sách yên ắng.

### Từ chối

| Tình huống | Kết quả |
|---|---|
| Version không tồn tại | `ModelVersionNotFoundError` → 404 |
| Đã là PRODUCTION | `RollbackError` → 409 |
| `CANDIDATE`/`REJECTED`/`TRAINING` | `RollbackError` → 409 — chưa từng là bản production tốt để quay về |
| Không có bản nào đang PRODUCTION | **Cho phép** — `previous_production_id = None` |

### Endpoint làm gì thêm ngoài việc hoán đổi

`POST /api/model-versions/{id}/rollback` (scope `write`) còn:

1. **AuditLog** — "ai đưa bản cũ về, lúc nào" là câu hỏi đầu tiên sau đó.
2. **GovernanceEvent CRITICAL** — hiện lên tab Alerts.
3. **Reload ServingBridge** — nếu không, endpoint chỉ sửa vài dòng dữ
   liệu và để model hỏng tiếp tục trả lời request, kết cục duy nhất khiến
   tính năng này *tệ hơn* là không có. Reload là best-effort và được
   **báo cáo** chứ không ném lỗi: database của framework là bản ghi
   quyết định, và một bridge đang chết không được để registry rollback
   nửa vời. `serving_reloaded` trong response cho caller biết cái nào đã
   xảy ra.

## 5. Giao diện

```python
model.rollback_to(3)     # theo version_number, không phải id
```

SDK đánh địa chỉ bằng `version_number` — con số người đọc nhìn thấy trên
console — thay vì database id mà mọi method SDK khác đều giấu đi. Rollback
là thao tác người ta gõ ra dưới áp lực.

`MLOpsModel.rollback_to` chỉ đổi registry của framework; nó **không**
publish reload — SDK cố ý không có quan điểm về nơi serving sống.

## 6. Giới hạn đã biết

- Rollback không kiểm tra artifact ở `artifact_uri` còn tồn tại hay không.
- Đồng bộ MLflow là best-effort; MLflow có thể lệch nếu không tra được
  version từ run id.
