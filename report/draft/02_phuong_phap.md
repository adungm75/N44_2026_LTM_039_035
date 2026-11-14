# Chương 2. Phương pháp

> Bản nháp. Chi tiết: `docs/architecture.md`, `docs/experiments.md`.

## 2.1 Phân tích thời gian

- Số bit của frame cổ điển 11 bit với stuffing worst-case (Davis và cộng sự 2007):
  `bits = 34 + 8s + 13 + ⌊(34 + 8s − 1)/4⌋` (4 byte: 95 bit, 8 byte: 135 bit); tùy chọn đếm stuffing
  thực tế bằng cách dựng chuỗi bit và CRC-15.
- Tải lý thuyết U = Σ C/T. Thời gian đáp ứng xấu nhất theo Davis, Burns, Bril, Lukkien (2007): blocking
  của frame ưu tiên thấp dài nhất, busy period, xét mọi instance trong busy period; tính bằng phân số
  chính xác. Kiểm chứng bằng ví dụ trong bài báo (R_A/B/C = 2.0/3.0/3.5 ms).

## 2.2 Mô hình SimPy

- Phân xử khi bus rảnh: mọi frame đã xếp hàng tại thời điểm t cạnh tranh, ID nhỏ nhất thắng, không
  ngắt; các lần phát cùng thời điểm được xếp hàng trước khi phân xử (sự kiện ưu tiên thấp).
- Ghi đè instance chưa gửi (overwrite), lỗi khung theo xác suất mỗi lần truyền (khung lỗi 31 bit,
  truyền lại), TEC +8/−1, error-passive > 127, bus-off > 255, phục hồi sau 128 × 11 bit.
- Chuỗi nhân quả: task tiêu thụ (Control, AEB, Planning) có chu kỳ và thời gian thực thi; output
  mang `origin_ts` mới nhất của input → e2e = lúc gửi xong − origin. Thời gian thực thi là **giả định**.
- Kiểm chứng: với offset 0, max R mô phỏng ≤ WCRT Davis ở mọi ID; tải mô phỏng khớp lý thuyết.

## 2.3 Chỉ số

Tải bus (toàn run và cửa sổ 100 ms), thời gian đáp ứng R (mean/p99/max) so với WCRT, thời gian xếp
hàng, e2e, jitter, mất frame theo counter, tỉ lệ trễ deadline, khung lỗi, TEC lớn nhất, số lần bus-off;
online: độ trễ từng chặng (log tx/rx), e2e qua chuỗi `src`, chỉ số lái xe (va chạm, sai lệch ngang,
thời gian về đích, quãng phanh AEB).

## 2.4 Thí nghiệm (SimPy 60 s × 5 seed, trừ khi ghi khác)

| | Biến | Câu hỏi |
|---|---|---|
| E0 | baseline | Baseline có đạt mọi deadline không? (+ online sim2d, CARLA) |
| E1 | bitrate 125/250/500/1000 kbit/s | Bitrate thấp nhất còn đạt? |
| E2 | chu kỳ ×0.5/×1/×2, DLC 4/8 | Cấu hình nào làm chuỗi AEB trễ? |
| E3 | `safety_first`/`naive` × tải 30/60/90 % | Gán ID có quan trọng khi tải cao? |
| E4 | tải tổng hợp 30–110 % | Bus chịu được bao nhiêu tải? |
| E5 | 1 vs 2 bus qua gateway, trễ 0.1/0.5/1 ms, vị trí AEB | Hai bus có giữ được budget AEB? |
| E7 | drop 1/5/10 %, trễ thêm 1–20 ms, xác suất lỗi 0.1–20 % | Chịu được bao nhiêu lỗi? (+ online) |
| E8/E9 | flood, node im lặng, spoof, ID lạ | Bộ phát hiện có bắt được không? |

Tải tổng hợp: frame 8 byte, chu kỳ 10 ms, mỗi message một node, ID chia đều trên các ID trống.

## 2.5 Tiêu chí đánh giá

- **Deadline**: R ≤ D cho mọi ID, cả WCRT Davis và max R mô phỏng; D = T mặc định (giả định, với
  overwrite thì R > T nghĩa là mất instance).
- **Chuỗi AEB**: budget end-to-end `L_max(v) = TTC − v/(2a)` (TTC 1.5 s, a = 6 m/s² lệnh AEB), tinh thần
  FTTI của ISO 26262 và cách chia budget từng chặng của AUTOSAR. Chia 60 % cảm biến/quyết định, 20 %
  mạng, 20 % ECU phanh (giảng viên); phần mạng chia đều 2 chặng CAN; tốc độ thiết kế 60 km/h (chọn).
- **Tải**: tải cao nhất còn đạt mọi deadline so với ngưỡng kinh nghiệm 50 % (CiA) và tham chiếu 70 %
  (thực hành J1939/CANopen) — quy tắc kinh nghiệm, không phải chuẩn bắt buộc.
- **Breaking point**: báo dưới dạng khoảng giữa giá trị đạt cuối và trượt đầu; một seed trượt là trượt.
- **E2E**: baseline 0 lỗi; mọi lỗi tiêm vào phải bị phát hiện; actuator vào trạng thái an toàn sau
  giới hạn watchdog.
- **Gateway và bộ phát hiện**: không có ngưỡng đạt/trượt; báo cáo đánh đổi và TPR/FPR/độ trễ/ROC.

## 2.6 Bộ phát hiện bất thường

- Cửa sổ 100 ms; đặc trưng: tải, số frame mỗi ID, inter-arrival cùng ID, khoảng trống từ frame cuối,
  số ID lạ, lỗi counter/CRC, khung lỗi và queuing delay theo lớp ưu tiên (chỉ SimPy).
- Luật: R1 tải > μ + kσ, R2 số frame một ID lệch > kσ, R3 khoảng trống > 2.5·T, R4 có ID lạ, R5 có lỗi
  E2E; μ, σ học từ dữ liệu bình thường (sàn σ nửa frame vì dữ liệu SimPy tuần hoàn tuyệt đối).
- Đánh giá theo không gian thiết kế của Sharmin và cộng sự (Journal of Computer Security, 2024): IDS
  giám sát thụ động dựa trên luật; mô hình tấn công từng loại (kẻ spoof biết CRC nhưng không theo dõi
  counter); đánh giá bằng mô phỏng và vcan; lưu lượng tự sinh có nhãn; chỉ số TPR theo loại, FPR, độ
  trễ, ROC theo k ∈ {2, 3, 4, 5}. SimPy: học trên E0 seed 1–3, kiểm tra trên E0 seed 4–5 + E8; online:
  học trên một run bình thường, kiểm tra trên 4 run E8.
