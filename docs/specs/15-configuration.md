# 15. Cấu hình tĩnh & chính sách lưu trong DB

> Mã nguồn: `src/mlops_framework/config/settings.py`, `src/mlops_framework/framework_settings/`
> Bảng: `framework_settings` (migration `009`)
> Kiểm chứng: `tests/unit/test_settings.py`, `test_framework_settings.py`, `test_settings_wiring.py`

## 1. Hai tầng cấu hình, cố ý tách rời

| | `Settings` (env) | `FrameworkSetting` (DB) |
|---|---|---|
| Chứa gì | **nơi mọi thứ nằm** | **quy tắc nghiệp vụ** |
| Ví dụ | `DATABASE_URL`, `MLFLOW_TRACKING_URI`, `CONSOLE_WRITE_TOKEN` | ngưỡng promotion, chính sách readiness |
| Đổi bằng cách | sửa env + deploy lại | sửa trên console, có hiệu lực ngay |
| Ai đổi | người vận hành hạ tầng | người sở hữu model |

Ranh giới: nếu đổi giá trị đó cần restart process thì nó thuộc `Settings`;
nếu nó là một quyết định quản trị thì thuộc `framework_settings`.

## 2. `Settings` — nạp từ biến môi trường

`pydantic-settings`, đọc `.env`, `extra="ignore"`, cache bằng
`@lru_cache`.

| Nhóm | Biến |
|---|---|
| Database | `DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT`, `DATABASE_ECHO` |
| MLflow | `MLFLOW_TRACKING_URI`, `MLFLOW_EXPERIMENT_NAME`, `MLFLOW_ARTIFACT_ROOT`, `MLFLOW_S3_ENDPOINT_URL` |
| Airflow | `AIRFLOW_BASE_URL`, `AIRFLOW_USERNAME`, `AIRFLOW_PASSWORD`, `AIRFLOW_REMOTE_LOG_BASE` |
| Bảo mật | `CONSOLE_WRITE_TOKEN` |
| Serving | `SERVING_BRIDGE_URL` |
| Scheduler | `SCHEDULER_ENABLED` (mặc định `false`), `SCHEDULER_POLL_SECONDS` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`, `TELEGRAM_APPROVAL_TIMEOUT_SECONDS` |
| Ứng dụng | `APP_NAME`, `APP_VERSION`, `DEBUG` |

`@lru_cache` nghĩa là test đổi env **phải** gọi `get_settings.cache_clear()`
— cả trước lẫn sau, vì cache còn sót sẽ rò giá trị sang module test chạy
kế tiếp.

## 3. `framework_settings` — một bảng đa hình

`key` (unique) → `value_json`. **Một** bảng chứ không phải một bảng cho
mỗi policy: chúng đều là dataclass được serialize, không cái nào có quan
hệ hay được truy vấn theo trường con, nên bốn bảng gần như giống hệt nhau
chỉ tạo ra bốn migration cho cùng một hình dạng.

| `key` | Dataclass |
|---|---|
| `promotion` | `PromotionConfig` |
| `eligibility` | `EligibilityConfig` |
| `training_policy` | `TrainingPolicy` |
| `drift` | `DriftConfig` |

### `FrameworkSettingsManager`

```python
get_promotion_config()  get_eligibility_config()
get_training_policy()   get_drift_config()
```

**Bảng rỗng ⇒ trả về đúng dataclass mặc định trần.** Đây là ràng buộc
tương thích ngược: một deployment chưa bao giờ chạm vào Settings hành xử
y hệt như trước khi bảng này tồn tại.

### Kiểu ghép chồng (layering) ở các call site

Mẫu lặp lại ở `scheduling/runner.py::_fire` và
`api/routers/internal.py::promote_model`:

```python
promotion_config = dataclasses.replace(
    FrameworkSettingsManager(db).get_promotion_config(),   # nền: đã lưu
    min_metrics={"f1": request.min_f1},                    # override tại chỗ
    must_beat_production=False,
    allow_cold_start=True,
)
```

Nền là chính sách đã lưu; override là quyết định riêng của call site đó.
Với `framework_settings` rỗng, biểu thức này bằng **đúng**
`PromotionConfig(...)` mà mã cũ dựng trực tiếp.

Readiness ghép **nông** (shallow), không sâu: các trường dạng dict trong
`request.policy` (ví dụ `dtypes`) thay thế nguyên cụm của nền chứ không
merge theo từng khoá.

## 4. Giao diện

| HTTP | Scope |
|---|---|
| `GET /api/settings` | — | cấu hình hiệu lực + ping khả năng kết nối |
| `GET /api/settings/policies` | — |
| `GET /api/settings/policies/{key}` | — |
| `PUT /api/settings/policies/{key}` | `write` |
| `POST /api/settings/policies/{key}/reset` | `write` |

### Che secret

`GET /api/settings` che mật khẩu Airflow và phần mật khẩu trong
`DATABASE_URL` bằng `••••••••` — giữ nguyên scheme/user/host/database, vì
đó mới là thứ trả lời "tôi đang trỏ vào database nào". Độ dài cũng không
bị lộ.

Endpoint này để trả lời "tôi đang trỏ vào đâu", không phải để đọc
credential. Vì **không có tầng auth cho GET**, che là biện pháp bảo vệ
duy nhất một giá trị ở đây có — nên không thứ gì thật sự bí mật được
phép đọc qua đây, kể cả đã che.

## 5. Giới hạn đã biết

- Sửa policy không có phiên bản hoá — không có lịch sử "ai đổi ngưỡng từ
  0.7 xuống 0.6". (Có `AuditLog` cho hành động, không cho giá trị cũ.)
- `@lru_cache` trên `get_settings` nghĩa là đổi env lúc runtime không có
  hiệu lực cho tới khi cache được xoá hoặc process restart.
