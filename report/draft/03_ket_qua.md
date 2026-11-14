# Chương 3. Kết quả

> Bản nháp. Mọi số trong chương này lấy từ `docs/results.md` (sinh tự động từ `results/`),
> `report/tables/*.csv`. Hình: `report/figures/`.
> Khi chạy lại thí nghiệm, cập nhật chương này theo `docs/results.md`.

## 3.1 Baseline (E0)

- Lý thuyết: tải 17.58 % ở 500 kbit/s; 18/18 ID có WCRT ≤ D, độ dư nhỏ nhất 7.30 ms. SimPy (5 seed):
  max R/D = 0.246, R_max ≤ WCRT ở mọi ID; e2e chuỗi AEB 50.46 ms ≤ giới hạn 88.89 ms (Hình 2).
- Online trên vcan0 (sim2d): xe về đích, 0 va chạm, 0 lỗi CRC/counter; tải từ log candump 17.56 %
  (Hình 1), khớp `canbusload` (Phase 3, Phase 5: monitor lệch `canbusload` ≤ 0.07 %).
- CARLA Town02: xe về đích chỉ bằng lệnh qua CAN, 0 va chạm, sai lệch ngang RMS 0.24–0.27 m qua 3 lần
  chạy (Phase 4).

## 3.2 Biên an toàn của mạng

- **Bitrate (E1, Hình 1)**: tải tỉ lệ nghịch với bitrate (70.3 % ở 125 kbit/s). Breaking point giữa
  250 và 125 kbit/s: ở 125 kbit/s ObjectList_5..7 có WCRT 17.5–19.7 ms vượt D = 11.1 ms của chuỗi AEB.
- **Tải (E4, Hình 3)**: tải cao nhất còn đạt mọi deadline là 50 % (đạt ngưỡng kinh nghiệm 50 %, dưới
  mốc tham chiếu 70 %). Ở 70 % chỉ một message tổng hợp ưu tiên thấp có WCRT 10.02 ms > 10 ms; nếu chỉ
  xét message của ma trận thì mọi deadline đạt tới 110 %. Lớp ưu tiên cao gần như không bị ảnh hưởng
  (p99 0.47 → 0.72 ms), lớp thấp tăng tới 19.25 ms và từ 100 % bắt đầu bị ghi đè.
- **Chu kỳ và DLC (E2)**: chuỗi AEB chỉ trượt khi nhân chu kỳ ×2 (e2e ~99 ms > 88.9 ms, do tuổi dữ
  liệu ObjectList 100 ms), với cả DLC 4 và 8; DLC hầu như không ảnh hưởng.

## 3.3 Gán ID và cấu trúc (E3, E5)

- **E3 (Hình 4)**: với `naive` (AEB 0x600), WCRT của AEB_BrakeRequest tăng 5.43 → 10.02 ms khi tải
  30 → 90 %, trượt D = 10 ms ở 90 %; với `safety_first` giữ 0.46 ms. Mô phỏng (offset ngẫu nhiên) chỉ
  đạt 6.73 ms ở 90 %: trường hợp xấu nhất của Davis hiếm khi xảy ra nhưng là giới hạn phải thiết kế.
- **E5 (Hình 5)**: hai bus không làm chuỗi AEB trượt trong dải trễ gateway 0.1–1 ms. Đặt AEB ở bus
  chassis còn giảm e2e (50.20 ms so với 50.46 ms một bus); đặt ở bus ADAS thì e2e tăng theo trễ gateway
  (tới 51.65 ms).

## 3.4 Chịu lỗi (E7, Hình 6)

- Trễ thêm ở mọi node: breaking point giữa 10 và 20 ms (e2e 90.20 ms ở 20 ms).
- Lỗi khung: bus-off xuất hiện giữa xác suất lỗi 10 % và 12 %, đúng quanh ngưỡng trôi TEC 1/9; từ đó
  chuỗi AEB cũng trượt (e2e ~100 ms).
- Drop 1–10 %: chuỗi AEB không trượt theo e2e (xem giới hạn 4.3), nhưng mọi frame mất đều bị counter
  phát hiện (35 450/35 450 trong SimPy; online 440/440 chuỗi drop của Control). Khi Control im lặng,
  actuator vào trạng thái an toàn sau 30.4–32.8 ms (giới hạn watchdog 30 ms). Xe vẫn về đích không va
  chạm khi Control drop 5 %.

## 3.5 Gateway (Phase 5, Hình 9)

Không có ngưỡng; đánh đổi rõ: batch 1 cho p50 0.26 ms / p99 0.81 ms; batch 32 với timeout 5 ms cho p50
3.70 ms / p99 6.39 ms nhưng giảm số datagram khoảng 6 lần (5.8 frame/datagram). Không mất gói UDP.

## 3.6 Phát hiện bất thường (E8/E9, Hình 7, 8)

- k = 3: SimPy TPR 1.0 cho cả 4 loại, FPR 0.098 % (chỉ ở cửa sổ ngay sau anomaly, run sạch 0 %); online
  TPR 1.0 (flood, silent, spoof) và 0.990 (ID lạ), FPR 0 trên 1118 cửa sổ bình thường, độ trễ phát hiện
  52–61 ms.
- Spoofing bị phát hiện ở 100 % cửa sổ nhờ AliveCounter (luật R5) — giá trị của E2E không chỉ cho an
  toàn mà cả cho phát hiện tấn công đơn giản.
- Đặc trưng nhạy nhất: tải và ID lạ (flood), khoảng trống cùng ID (silent), lỗi counter (spoof), số ID
  lạ (unknown_id) — `report/tables/feature_sensitivity.csv`.

## 3.7 Tổng hợp tiêu chí

Bảng `report/tables/criteria_summary.csv` (in lại trong `docs/results.md`) cho kết luận từng tiêu chí;
không có tiêu chí bắt buộc nào trượt ở baseline. Các breaking point nêu trên phụ thuộc tốc độ thiết kế
60 km/h của budget AEB.
