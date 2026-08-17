# 17. Demo vòng lặp khép kín

> Mã nguồn: `demo/`
> Kiểm chứng: `tests/demo/`
> Hướng dẫn chạy: `demo/README.md`

## 1. Mục đích

Từng mảnh của vòng đời đã có spec riêng — dataset (01), training (02),
drift (07), retraining workflow (08), approval gate (10), lineage (12).
Cái chưa có là **một câu chuyện duy nhất** đi hết từ dữ liệu ban đầu tới
một model được huấn luyện lại và promote, để thấy các mảnh đó nối vào
nhau chứ không phải chín script rời rạc.

Demo cũ (`scripts/run_drift_recovery_demo.py`) đã làm được phần lớn,
nhưng có bốn chỗ hụt về mặt khoa học:

1. Không có **đối chứng âm** — nó tiêm drift ngay, nên "phát hiện được
   drift" không chứng minh bộ dò phân biệt được gì.
2. Dataset V2 là một file sinh lại ở phân phối mới, **thay thế** V1 chứ
   không mở rộng V1.
3. Quyết định của con người nằm *ngoài* workflow, nên không có dòng audit
   `RETRAIN_APPROVED` nào được ghi.
4. Không có liên kết lineage nào từ V2 về V1.

## 2. Câu chuyện

```
Dataset V1 → Model V1 → PRODUCTION
    → cửa sổ production nền      ⇒ KHÔNG drift   (đối chứng âm)
    → tiêm dịch chuyển có kiểm soát
    → cửa sổ production đã lệch  ⇒ CÓ drift
        → lưu drift event + báo Telegram
        → admin duyệt / từ chối
            → Dataset V2 = V1 + dữ liệu đã lệch
            → huấn luyện Model V2 qua DAG Airflow thật
            → thẩm định theo tiêu chí chấp nhận
            → lưu trữ V1, promote V2
    → tiếp tục giám sát
```

Bất biến an toàn — mọi nhánh hỏng đều phải giữ nguyên nó:

> Một lần retrain **thất bại, bị từ chối, hoặc chưa được duyệt** không
> bao giờ được thay thế model đang chạy production.

## 3. Bốn khái niệm không được lẫn

Spec này tách rõ bốn thứ mà demo cũ gộp làm một:

| Khái niệm | Ở đâu | Vì sao tách |
|---|---|---|
| Dataset huấn luyện | `credit-card-fraud`: V1, V2 | Quần thể model học từ đó |
| Dataset tham chiếu | V1 | Mốc để đo drift, không đổi theo cửa sổ |
| Dữ liệu production | `credit-card-fraud-production`: từng cửa sổ | **Quan sát về thế giới**, không phải một version của tập huấn luyện |
| Dữ liệu đã lệch | Cửa sổ có `drift_shift > 0` | Một cửa sổ production cụ thể, không phải một dataset riêng |

Gộp cột 1 và cột 3 là lý do câu hỏi "dữ liệu nào đã lệch?" không trả lời
được về sau.

Drift luôn so cửa sổ production với **tham chiếu huấn luyện** (V1), không
phải với cửa sổ trước đó. So hai cửa sổ liên tiếp đo *tốc độ thay đổi*
của lưu lượng; so với tham chiếu đo *giả định của model đang chạy còn
đúng không* — đó mới là câu hỏi nên kích hoạt retrain.

## 4. Thay đổi trong framework mà demo này đòi hỏi

Demo không tự hiện thực lại thứ gì. Bốn thay đổi dưới đây nằm trong
framework vì chúng là khiếm khuyết thật, không phải tiện nghi cho demo.

**`dataset_versions.parent_version_id`** (migration `011`). Lineage
trước đây chỉ đi *xuôi* từ một DatasetVersion. Một version được dựng bằng
cách mở rộng version trước — chính là trường hợp retrain — không phân
biệt được với một version từ đâu rơi xuống. `LineageManager` giờ đi
ngược chuỗi cha và phát cạnh `derived_from`.

**Đồ thị lineage gộp lại thành "cả họ"**, ngay sau khi demo này chạy
được lần đầu và cho thấy V1/V2 hiện ở hai đồ thị tách biệt tuỳ vào bấm
vào node nào — đúng cái §2's "một câu chuyện duy nhất" định tránh. Fix
thuộc về framework, không phải demo: `LineageManager` giờ luôn trả về
toàn bộ các version của một dataset trong cùng một đồ thị (V1 đã
archived và V2 đang production hiện song song), và `Dataset`/`Model` không
còn là node riêng — tên gộp vào label của version (`"{name} v{n}"`). Xem
spec 12 §2-3 cho chi tiết đầy đủ.

**`RecordedDecisionGate`** (`approval/base.py`). Xem spec 10 §6.

**`RetrainingWorkflow.run(run_metadata=...)`**. Pipeline phía Airflow đọc
`tracking_uri` và tham số huấn luyện từ metadata của TrainingRun qua
HTTP. Workflow trước đây không có đường nào truyền chúng, nên một lần
retrain qua DAG thật sẽ chạy không tham số và không log vào đâu cả.

**`TrainingService.start_run` ghi `tracker_run_id` vào metadata trước khi
trigger.** Cùng nguyên nhân: `LocalDockerOrchestrator` nhận config trực
tiếp nên không thấy vấn đề, còn DAG Airflow đọc lại metadata qua HTTP —
không thấy `tracker_run_id`, nó tự tạo một MLflow run khác, và
`TrainingRun.mlflow_run_id` trỏ vào một run rỗng trong khi metrics nằm
chỗ khác.

## 5. Hai quyết định thống kê

**Không giám sát `time`.** Nó là số giây kể từ giao dịch đầu — một bộ
đếm, không phải hiệp biến. Hai cửa sổ khác độ dài *nhất định* có phân
phối `time` khác nhau, nên KS test trên nó báo drift mọi lần chạy và
không nói gì về dữ liệu. Nó vẫn ở trong `feature_columns()` vì model có
thể học từ nó; "model đọc gì" và "ta giám sát dịch chuyển của gì" là hai
câu hỏi khác nhau — xem `monitored_feature_columns()`.

**Hiệu chỉnh Bonferroni** (`DriftConfig.correction`). Mỗi cửa sổ so 29
đặc trưng. Kiểm định từng cái ở α=0.05 rồi kết luận drift nếu *bất kỳ*
cái nào có ý nghĩa cho sai số toàn cục:

```
1 - 0.95²⁹ ≈ 0.77
```

Cửa sổ nền sẽ bị gắn cờ khoảng ba lần trên bốn, và đối chứng âm — thứ
làm cho lần phát hiện thật có giá trị — trở thành vô nghĩa. Đo được trên
đúng thí nghiệm này:

| Cửa sổ | Không hiệu chỉnh | Bonferroni |
|---|---|---|
| nền | **DRIFT** — `v3`, `v26` (dương tính giả) | KHÔNG drift |
| đã lệch | DRIFT — 9 đặc trưng, có `v7`, `v19` giả | DRIFT — đúng 7 cái đã tiêm |

Mặc định của framework vẫn là `"none"` để không đổi hành vi của caller
sẵn có; demo tự chọn `"bonferroni"`.

## 5b. Pha loãng: vì sao `require_drift_to_retrain=False`

`RetrainingWorkflow` có bước drift riêng, so **dataset ứng viên** với
version trước nó — V2 với V1. Bước đó báo **không** drift, trong cùng
lần chạy mà cảnh báo báo có drift rất mạnh. Cả hai đều đúng, vì chúng
hỏi hai câu khác nhau:

| Phép so | Mẫu | KS lớn nhất | Kết luận ở α/29 |
|---|---|---|---|
| cửa sổ production với V1 | 1.000 dòng đã lệch với 8.000 | 0.2490 | **DRIFT** |
| V2 với V1 | 9.000 dòng, trong đó 8.000 *chính là* V1 | 0.0277 | không drift |

V2 chứa V1. 1.000 dòng đã lệch bị pha loãng còn khoảng một phần chín
mẫu, đẩy thống kê xuống dưới ngưỡng tới hạn đã hiệu chỉnh (~0.0289).
Lưu lượng đến đã lệch; tập huấn luyện gần như không, vì nó hấp thụ dịch
chuyển vào một quần thể tham chiếu lớn hơn nhiều — đúng như mục tiêu của
"mở rộng V1 thay vì thay thế".

Ở một bản trước, cổng này để `True` và **có vẻ** hoạt động — nhưng chỉ vì
bước drift của workflow chạy **không hiệu chỉnh** ở α=0.05 trong khi cảnh
báo dùng α/29. Hai nghĩa khác nhau của "drift" trong cùng một lần chạy,
và cổng lặng lẽ đi qua nhờ nghĩa yếu hơn. Cho hai ngưỡng bằng nhau đã
phơi ra rằng cổng chưa bao giờ thật sự được thoả mãn theo tiêu chuẩn của
chính nó.

Biện minh cho lần retrain không phải là V2-với-V1, mà là (1) một drift
event đã lưu trên cửa sổ production, đã hiệu chỉnh, 7 đặc trưng,
p < 1e-26, và (2) một phê duyệt tường minh của con người — cả hai đều là
hàng có thể kiểm toán. Dẫn lại một phiên bản yếu hơn của cùng câu hỏi từ
dataset đã gộp không thêm an toàn nào, và đặt cổng lên nó khiến vòng lặp
phụ thuộc vào một sự tình cờ thống kê.

Hai tham số framework sinh ra từ phát hiện này —
`RetrainingWorkflow.run(drift_config=..., trigger_type=...)`. Cái thứ hai
cũng cần thiết: workflow suy ra `trigger_type` từ bước drift *của nó*,
nên nếu không nói rõ, một lần retrain do drift sẽ bị ghi là `SCHEDULED`
chỉ vì một phép so *khác* im lặng.

## 6. Dataset V2 mở rộng V1

```
dataset_v1.csv (8.000 dòng) + production_window_drifted.csv (1.000 dòng)
        ↓ nối, đúng thứ tự, header một lần
dataset_v2.csv (9.000 dòng)
```

Chỉ huấn luyện trên cửa sổ đã lệch sinh ra model quên mất quần thể nó
đang phục vụ đúng. Sinh lại V1 ở phân phối mới còn tệ hơn: nó viết lại
lịch sử để quần thể "trước khi lệch" chưa từng tồn tại, và lineage khi đó
khẳng định model đã học từ dữ liệu không ai quan sát.

Bước này từ chối đăng ký V2 nếu số dòng không đúng bằng V1 + cửa sổ, và
ghi lại công thức dựng (`derivation`) gồm cả hai version nguồn, số dòng,
content hash, và drift event đã biện minh cho việc gộp.

## 7. Thứ tự: duyệt trước, dựng V2 sau

`RetrainingWorkflow` hỏi cổng phê duyệt sau eligibility, ngay trước khi
train. Demo phải hỏi **sớm hơn** thế, vì dựng V2 là công việc không nên
làm khi admin có thể từ chối. Nên: hỏi một lần ở bước
`request_approval`, lưu quyết định, rồi truyền nó vào workflow bằng
`RecordedDecisionGate`. Con người bị hỏi đúng một lần; workflow vẫn ghi
dòng audit của nó; một lời từ chối ghi trước đó vẫn chặn được retrain
bên trong workflow.

Kết quả là hai dòng audit, cố ý: `RETRAIN_REQUEST_APPROVED` (lúc câu hỏi
được trả lời, bởi admin) và `RETRAIN_APPROVED` (workflow hành động theo
đó).

## 8. Bằng chứng thẩm định: ba cột, không phải hai

| | V1 (lưu) | V1 (chạy lại) | V2 |
|---|---|---|---|
| đo trên | quần thể lúc huấn luyện | cửa sổ đã lệch, **bây giờ** | cửa sổ đã lệch |

Chỉ báo cáo cột 1 và cột 3 là so hai con số đo trên hai quần thể khác
nhau. Cột giữa mới làm phép so có nghĩa — và cũng là lý do policy dùng
sàn tuyệt đối thay vì `must_beat_production`: khi quần thể đã dịch,
metric *đã lưu* của V1 không còn là mốc công bằng theo cả hai chiều.

Nếu không lấy được cột giữa, nó để trống chứ không điền. Thiếu số đo là
thiếu bằng chứng; bịa số đo là khẳng định sai, mà đây đúng là con số cả
lập luận retrain dựa vào.

## 9. Giới hạn đã biết

- Demo cần **toàn bộ** stack Docker; CI không chạy được đường hạnh phúc.
  Bù lại, `tests/demo/` kiểm chứng mọi *quyết định* trên database thật
  với `LocalDockerOrchestrator` thay cho Airflow.
- Chỉ một cửa sổ production mỗi loại. Không mô phỏng drift trôi dần theo
  nhiều cửa sổ liên tiếp.
- `concat_csv` giữ toàn bộ V1 mãi mãi; không có cơ chế cửa sổ trượt hay
  giảm trọng số dữ liệu cũ.
- Bonferroni là hiệu chỉnh duy nhất; chưa có Benjamini-Hochberg (FDR),
  vốn hợp lý hơn khi số đặc trưng lớn.
- Metric có thể lệch ở chữ số thập phân thứ tư giữa các lần chạy do lịch
  luồng của XGBoost; các *quyết định* thì cách ngưỡng đủ xa nên ổn định.
