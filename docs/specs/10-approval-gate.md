# 10. Cổng phê duyệt của con người

> Mã nguồn: `src/mlops_framework/approval/`
> Kiểm chứng: `tests/unit/test_approval_gate.py`, `test_telegram_approval.py`

## 1. Mục đích

Mọi quyết định **của máy** trong framework đều đã có kết quả giải thích
được — readiness, drift, eligibility, promotion. Quyết định **của con
người** thì chưa. Nó chỉ tồn tại trong `scripts/_telegram_approval.py`,
đấu dây bằng tay vào đúng một demo, nên một lần retrain mà chính sách cho
phép không thể bị bắt phải đợi một con người ở bất kỳ chỗ nào khác.

## 2. Hợp đồng

```python
class ApprovalGate(ABC):
    def request_approval(self, request: ApprovalRequest, *,
                         timeout: float = 3600.0) -> ApprovalDecision: ...
```

| Dataclass | Trường |
|---|---|
| `ApprovalRequest` | `summary` (văn xuôi cho người đọc) · `action` · `context` (dữ kiện có cấu trúc) |
| `ApprovalDecision` | `approved` · `reason` · `responder` |

Cùng hình dạng với `DriftDetector` và `EventPublisher`: framework phụ
thuộc vào ABC, adapter **không** phụ thuộc ngược vào framework.

## 3. Từ chối là mặc định

`request_approval` **trả về** một quyết định, không bao giờ ném lỗi để
diễn đạt "không". Timeout, kênh không tới được, reply hỏng — tất cả trả
`approved=False`.

Một cổng không hỏi được ai thì **chưa** được nói đồng ý. Coi việc không
liên lạc được là sự đồng thuận sẽ khiến cổng *tệ hơn* là không có: nó sẽ
mở toang đúng vào lúc có gì đó đã hỏng.

`TelegramApprovalGate.request_approval` vì thế tự bắt lỗi truyền tải của
chính nó thay vì ném xuyên qua workflow.

## 4. Adapter sẵn có

| Gate | Dùng khi |
|---|---|
| `TelegramApprovalGate` | thật — gửi nút Approve/Deny tới một chat admin |
| `AutoApproveGate` | test; hoặc muốn có bản ghi kiểm toán của một cổng mà không cần người |
| `DenyAllGate` | test — làm cho "cổng đã được hỏi và nói không" kiểm chứng được |

`AutoApproveGate` được đặt tên theo đúng việc nó làm. Một cổng âm thầm
phê duyệt sẽ không phân biệt được với việc *không có cổng* khi review
code; cái này thì lộ ngay ở call site.

### Telegram

- `httpx` import lười; Telegram **không bao giờ** là dependency cứng
- Bỏ qua click từ chat khác chat admin đã cấu hình (có test riêng)
- Sửa lại chính message prompt để hiển thị kết quả (✅ / ⛔ / ⏱)

## 5. Tích hợp vào workflow

```python
RetrainingWorkflow(session, training_service=service,
                   approval_gate=DenyAllGate())
```

- **Tuỳ chọn theo thiết kế.** Workflow không có cổng hành xử y hệt xưa
  nay: bước bị **bỏ qua**, không phải tự động phê duyệt, và không xuất
  hiện trong step trace.
- Bị từ chối → `blocked_reason="approval_denied"`, **không** có
  `TrainingRun` nào được tạo.
- Bị từ chối được ghi giống một lần bị chính sách chặn — `RUN_BLOCKED`
  event và `blocked_reason` — vì với mọi thứ phía sau nó là **cùng một
  sự thật**: lần retrain này đã không xảy ra, và đây là lý do.
- Được duyệt → `AuditLog` với `actor = decision.responder`. Đây là sự
  kiện quản trị duy nhất luôn là một *con người* chứ không phải kết quả
  của một chính sách.

## 6. Quyết định thiết kế

**Vì sao script cũ trở thành shim?** `scripts/_telegram_approval.py` giờ
chỉ re-export bản của framework. Giữ hai bản sao là cách chúng lệch nhau.

**Vì sao `context` tách khỏi `summary`?** Kênh nào cũng cần văn xuôi cho
người đọc; nhưng dữ kiện có cấu trúc (tên model, điểm drift) là thứ một
kênh khác có thể render theo cách khác, và là thứ audit trail ghi lại
cạnh câu trả lời.

## 7. Giới hạn đã biết

- Chỉ có adapter Telegram; Slack/webhook chưa có.
- Cổng chạy **đồng bộ** trong workflow — nó block cho tới khi có câu trả
  lời hoặc hết timeout.
- Không có khái niệm nhiều người duyệt hay đủ số phiếu.
