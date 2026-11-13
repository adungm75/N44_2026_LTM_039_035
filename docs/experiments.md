# Thí nghiệm (Phase 6)

Đặc tả: §5.10 (thí nghiệm), §5.12 (tiêu chí đánh giá). Mọi số liệu trong tài liệu
này lấy từ `report/tables/criteria_summary.csv` và `report/tables/criteria_variants.csv` (sinh
tự động); không có số nào viết tay.

## Tái lập

```
make setup                 # uv sync --extra carla --extra parquet
make experiments           # SimPy: budget -> E0..E5, E7 (60 s x 5 seed, cache) -> summary -> tiêu chí
make experiments-online    # USER: vcan0 đã bật (sudo scripts/setup_vcan.sh); ~3 phút
```

- `make budget` → `results/budget/aeb_budget.json` (budget end-to-end chuỗi AEB, §5.12.1).
- `make experiments` chạy mọi `config/experiments/E*.yaml` song song (`JOBS=4`), bỏ qua run đã có
  `metrics.json` (thêm `--force` khi gọi `python -m canav.experiments.run` để chạy lại). Thời gian
  đo trên máy dự án: 2 phút 51 giây cho 225 run.
- Kết quả: `results/<E>/<seed>-<hash>/{metrics.json, config.yaml[, can.log, errors.jsonl,
  frames.jsonl]}`, `results/summary.parquet` (một dòng mỗi run × message),
  `report/tables/criteria_summary.csv`, `report/tables/criteria_variants.csv`.
- Trace: E0 giữ `frames.jsonl` mọi seed (Phase 7 huấn luyện trên đó); các thí nghiệm khác chỉ giữ
  `can.log` + `errors.jsonl` của seed đầu (`traces: first`) để tiết kiệm đĩa.
- Online: `scripts/run_online_experiments.py` → `results/online_exp/{E0, E7_drop5_control,
  E7_silent_control}` (sim2d trên vcan0, có gateway); `--carla` thêm `E0_carla`.

## Định dạng cấu hình

`base:` (gộp sâu một YAML khác, ở đây luôn là E0), `network:` (matrix, ID scheme và các khóa biến
thể), `sim:` (`SimConfig`), `traces:`, `sweep:` (đường dẫn có dấu chấm → danh sách giá trị, tích
Descartes; phần tử danh sách theo chỉ số, ví dụ `sim.faults.0.drop_prob`). `run_id = seed<N>-<hash
cấu hình đã giải>`.

Khóa biến thể mạng (`canav.experiments.variants`, áp theo thứ tự):

| Khóa | Ý nghĩa |
|---|---|
| `period_factor` | nhân mọi chu kỳ; D = T mặc định cũng nhân theo, budget từng hop của chuỗi AEB thì không |
| `dlc` | đặt DLC mọi message định kỳ (chỉ ảnh hưởng độ dài frame; payload không mô phỏng) |
| `target_load` | thêm message tổng hợp tới tải lý thuyết (worst-case) mục tiêu |
| `deadlines` | D chuỗi AEB từ `config/experiments/aeb_budget.yaml` (cùng số với `aeb_budget.json`) |
| `topology` | 2 bus nối bằng gateway (E5) |

## Thí nghiệm

| | Biến | Ghi chú lựa chọn |
|---|---|---|
| E0 | baseline 500 kbit/s, `safety_first` | 5 seed, offset ngẫu nhiên theo node; thời gian thực thi Control/AEB 1 ms, Planning 5 ms là **giả định** (Phase 2) |
| E1 | bitrate 125/250/500/1000 kbit/s | |
| E2 | hệ số chu kỳ ×0.5/×1/×2 × DLC 4/8 | DLC áp cho mọi message định kỳ |
| E3 | `safety_first`/`naive` × tải 30/60/90 % | tải đạt bằng message tổng hợp của E4 |
| E4 | tải 30/50/70/80/90/100/110 % | xem "Tải tổng hợp" |
| E5 (P1) | độ trễ gateway 0.1/0.5/1 ms × vị trí AEB (chassis/ADAS) | Chassis: ChassisSensors, Control, 2 actuator, AEB; ADAS: Perception, Planning, BodyHMI, GNSS (chọn: GNSS chỉ cấp cho Planning) |
| E7_drop | drop 1/5/10 % tại mọi node | theo §5.10 |
| E7_delay | trễ thêm 1/2/5/10/20 ms tại mọi node | chọn: từ 1/10 tới 2 lần chu kỳ ngắn nhất (10 ms) |
| E7_error | xác suất lỗi mỗi lần truyền 0.001…0.2 tại mọi node | chọn quanh ngưỡng trôi TEC p = 1/9 (+8 mỗi lỗi, −1 mỗi lần thành công) |

### Tải tổng hợp (chọn)
- Frame 8 byte, chu kỳ 10 ms, mỗi message một node riêng (offset ngẫu nhiên độc lập); message cuối
  có chu kỳ dài hơn để tải lý thuyết đạt đúng mục tiêu (sai số < 1e-5).
- ID chia đều trên các ID 11 bit còn trống trong [0x011, 0x7FF], nên đan xen với lưu lượng thật ở
  mọi ID scheme (không phải trường hợp tốt nhất hay xấu nhất); không bao giờ cao hơn AEB 0x010.
- Message tổng hợp có D = T = 10 ms. Vì vậy câu hỏi "tải cao nhất mà mọi deadline đạt" của E4 phụ
  thuộc chính các message tổng hợp ưu tiên thấp; criteria có thêm một dòng chỉ xét message thật.

### Hai bus (E5)
- Message có receiver ở bus kia được gateway (`busmodel.Gateway`, store-and-forward) chép sang;
  bản sao giữ `origin_ts`, gửi bởi node Gateway trên bus đích.
- Lý thuyết: bản sao có jitter phát = độ trễ gateway + (R − C) của message nguồn; lặp tới điểm bất
  động vì hai bus chép sang nhau. Metrics: khóa `0x300@chassis` cho bản sao.

## Tiêu chí (§5.12, `canav.analysis.criteria`)

Mỗi biến thể được xét trên mọi seed (một seed trượt là biến thể trượt):
- **theory_ok**: mọi message định kỳ (cả tổng hợp, cả bản sao) có WCRT Davis và WCRT ≤ D.
- **sim_ok**: mọi message có R_max ≤ D và không instance nào bị ghi đè (ghi đè nghĩa là instance trước
  chưa gửi được trước lần phát kế tiếp, tức R > T ≥ D).
- **Chuỗi AEB**: ObjectList_k và AEB_BrakeRequest (cả bản sao) đạt hai điều trên, và e2e mô phỏng
  của chuỗi (AEB_BrakeRequest trên bus − origin cảm biến mới nhất) ≤ (0.6 + 0.2) × L_max ở tốc độ
  thiết kế (chọn: phần 20 % của ECU phanh không nằm trong e2e mô phỏng).
- **Budget** (`results/budget/aeb_budget.json`): L_max(v) = TTC − v/(2a), TTC và a = `decel_req`
  từ `ecu_params.yaml`; chia 60/20/20 % (advisor); phần mạng chia đều 2 hop (chọn); D mỗi hop chặn
  ở T (chọn). Tốc độ thiết kế 60 km/h (chọn: đầu trên dải quét của advisor).
- E4: tải cao nhất đạt mọi deadline so với 50 % (có verdict) và 70 % (chỉ tham chiếu), quyết định
  của USER.
- E2E (§5.12.4): sim — số instance mất nằm giữa hai frame nhận được (đếm chính xác theo số thứ tự)
  so với số khoảng trống counter; online — số chuỗi drop của Control so với `counter_err` ở
  actuator; Control im lặng → thời gian tới safe state > 3 chu kỳ.

## Giới hạn của phép đo

- **e2e theo origin mới nhất (§5.4 quy tắc 4)**: 8 frame ObjectList cùng một mốc phát, nên khi mất
  1 frame thì AEB vẫn có origin mới từ 7 frame còn lại. Vì vậy E7_drop không làm chuỗi AEB trượt
  trong dải quét, dù thông tin của vật trong frame bị mất đã cũ thêm một chu kỳ. Mất frame được đánh
  giá riêng ở tiêu chí 4.
- **Davis vs mô phỏng**: Davis tính tức thời tới hạn (mọi message phát cùng lúc); mô phỏng dùng
  offset ngẫu nhiên nên R_max mô phỏng thường thấp hơn nhiều (E3/E4 trượt ở lý thuyết trước).
- **Tốc độ thiết kế**: D chuỗi AEB (11.1 ms mỗi hop, giới hạn e2e) phụ thuộc mạnh vào tốc độ thiết
  kế; ở tốc độ thấp hơn budget lớn hơn và D = T.
- Thời gian thực thi task trong SimPy là giả định (E0), chưa thay bằng số đo online.
- §5.12.5 (tốc độ tránh va chạm tối đa, P1) chưa chạy: kịch bản sim2d hiện tại (người đi bộ xuất
  hiện ở 12 m, xe giảm tốc khi qua xe đỗ) giới hạn kết quả trước cả mạng (xem PROGRESS.md).
