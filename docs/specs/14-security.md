# 14. Xác thực, phân quyền, write gate

> Mã nguồn: `src/mlops_framework/auth/`, `src/mlops_framework/api/security.py`
> Bảng: `api_keys` (migration `010`)
> Kiểm chứng: `tests/unit/test_api_keys.py`, `test_security.py`, `tests/api/test_write_auth.py`, `test_api_keys_api.py`

## 1. Vấn đề, theo hai giai đoạn

**Giai đoạn 1 — ghi ẩn danh.** Toàn bộ `/api/internal/*` và nửa ghi của
`/api/schedules` từng không có bất kỳ xác thực nào. Ai chạm được cổng
8000 đều có thể promote một model lên PRODUCTION, trigger DAG thật, hoặc
đưa cho `LocalDockerOrchestrator` một `pipeline_id` để import và gọi —
kết hợp với lỗi nội suy mã nguồn ở module đó, đây là **thực thi mã từ xa
không cần xác thực**.

**Giai đoạn 2 — không phân biệt được ai với ai.** Shared secret đóng được
lỗ hổng trên, nhưng không làm được việc kế tiếp: `X-Actor` — thứ audit
trail ghi lại — do chính caller khai. "Ai đã promote model này" chỉ đáng
tin đúng bằng mức bạn tin người đang trả lời.

## 2. Hai loại credential

### API key có scope (đường chính)

`Authorization: Bearer mlops_ak_…` → một `Principal` có tên và tập scope.
Tên đó là thứ vào `AuditLog.actor`, dẫn xuất từ một credential caller
**phải sở hữu mới có** — nên dòng audit là *bằng chứng*, không phải lời
khai.

> Test ghim hệ quả: một request mang key của `alice` kèm
> `X-Actor: definitely-bob` được ghi là **alice**.

### Shared secret (đường chuyển tiếp)

`X-Console-Token` / `CONSOLE_WRITE_TOKEN`, cấp scope `write`. Được giữ
lại vì đó là thứ **mọi deployment hiện tại** đang cấu hình — DAG Airflow
nằm trong số đó, ở cả docker-compose lẫn Terraform — và gỡ nó trong cùng
thay đổi giới thiệu API key sẽ làm hỏng tất cả cùng lúc.

Request đi đường này được đánh dấu `via_shared_secret=True` và `actor`
vẫn lấy từ `X-Actor` **chưa xác minh**: không tốt hơn trước, nhưng cũng
không tệ hơn, và **phân biệt được**.

Nó cố ý **không** cấp `admin`: một secret dùng chung mà mint được key
riêng từng người sẽ trao cho bất kỳ ai giữ nó khả năng chế tạo danh tính.

## 3. Scope

`read` < `write` < `admin`, mỗi cái hàm ý các cái trước.

| Scope | Cấp gì |
|---|---|
| `read` | mọi GET console render từ đó |
| `write` | mọi thứ đổi trạng thái — promote, rollback, start run, schedules, policies |
| `admin` | quản lý chính các key |

Ba giá trị, cố ý không nhiều hơn. Một hệ role có bảng kế thừa sẽ là nghi
lễ vòng quanh ba giá trị; nếu một ngày cần cái thứ tư,
`auth/manager.py` là file sẽ lớn lên.

**GET không bị gác.** Console không có login; gác GET sẽ khiến nó không
dùng được nếu chưa giải quyết xong quản lý session.

## 4. `api_keys`

| Cột | Ghi chú |
|---|---|
| `name` | unique — principal mà key này đóng vai |
| `key_hash` | **sha256**, unique, indexed |
| `key_prefix` | `"mlops_ak_A1b2c3"` — nhận diện được mà không xác thực được |
| `scopes_json` | mảng JSON |
| `last_used_at`, `revoked_at` | |

### Chỉ lưu hash

Plaintext trả về **một lần duy nhất**, lúc tạo, và không lấy lại được.
Một bản dump database vì thế không cho ra credential dùng được, và "tôi
làm mất key" là một lần xoay vòng chứ không phải một lần tra cứu.

**sha256 trần, không phải KDF chậm — có chủ ý.** Mật khẩu cần KDF chậm vì
nó entropy thấp và do người chọn. Đây là 256 bit từ
`secrets.token_urlsafe`: không có từ điển nào để chạy thử, và làm lookup
chậm chỉ đánh thuế mọi request đã xác thực. Điều quan trọng là dạng lưu
trữ **một chiều**, và tra cứu là một phép so khớp chính xác có index.

### Thu hồi là dấu thời gian, không phải xoá

Một key **đã hành động** phải còn giải được, chừng nào các dòng audit
nêu tên nó còn tồn tại. `revoke` idempotent và giữ dấu thời gian đầu.

### Prefix

Tồn tại để một chuỗi bị rò rỉ **tự nhận diện** được là credential của
framework này — máy quét secret bắt theo đúng nó. Bản thân prefix không
xác thực được (có test).

## 5. Ba mã lỗi, ba nghĩa khác nhau

| Mã | Khi nào | Vì sao |
|---|---|---|
| **401** | không trình gì hợp lệ | key sai và key lạ trả **cùng** câu trả lời — phân biệt chúng là xác nhận rằng một chuỗi nào đó là key thật |
| **403** | biết caller nhưng thiếu scope | "đăng nhập đi" và "bạn không được phép" là hai cách sửa khác nhau |
| **503** | không có key nào **và** không có shared secret | deployment không xác thực được ai cả; nói thẳng vẫn hơn 401 mà không credential nào thoả |

Thay đổi này chuyển "secret sai" từ 403 sang **401**. Hai test cũ khẳng
định 403 đã được cập nhật kèm lý do.

### Key hỏng **không** bị âm thầm hạ cấp

Trình một key nghĩa là **có ý dùng** nó. Âm thầm thành công với danh tính
khác (`system`) sẽ đặt sai tên vào audit trail — đúng cái duy nhất mà cả
tính năng này sinh ra để ngăn. Nên một Bearer không giải được là 401,
ngay cả khi shared secret cũng đang được gửi kèm và hợp lệ.

## 6. Bootstrap

`/api/api-keys` cần `admin`, nên key **đầu tiên** không thể qua đó.

```bash
python -m mlops_framework.auth.cli create alice --scopes admin
python -m mlops_framework.auth.cli list
python -m mlops_framework.auth.cli revoke alice
```

CLI nói chuyện thẳng với database — cùng ranh giới tin cậy với việc chạy
migration, và có mặt ở đúng những nơi ấy — thay vì cần một credential để
lấy một credential.

## 7. Console

`writeToken()` nhắc nhập credential, cất trong `sessionStorage` (theo
tab). `writeAuthHeaders()` đánh hơi prefix `mlops_ak_` để chọn giữa
`Authorization: Bearer` và `X-Console-Token` — đó chính là công dụng của
prefix. 401/403 → xoá cache và nhắc lại một lần; **503 thì không** —
server chưa cấu hình gì cả, hỏi lại không sửa được và người dùng cần đọc
thông báo đó.

## 8. Giới hạn đã biết

- Không có login trên trình duyệt, không có session.
- Không có hạn dùng (expiry) cho key — chỉ có thu hồi thủ công.
- Chưa có OIDC/SSO.
- Shared secret vẫn còn: đóng nó lại đòi hỏi cấp key cho DAG và các
  script trước, rồi mới `unset CONSOLE_WRITE_TOKEN`.
