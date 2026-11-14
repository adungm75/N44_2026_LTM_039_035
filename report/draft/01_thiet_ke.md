# Chương 1. Thiết kế hệ thống

> Bản nháp. Chi tiết cài đặt: `docs/architecture.md`,
> `docs/gateway_protocol.md`.

## 1.1 Mục tiêu

Đồ án thiết kế một mạng CAN khoảng 10 nút cho một xe tự hành mô phỏng, chạy các ECU như các tiến
trình độc lập chỉ giao tiếp qua CAN, đo các chỉ số mạng khi thay đổi tham số, cấu trúc và nhu cầu
băng thông, và xây dựng một bộ phát hiện bất thường dựa trên ngưỡng/thống kê. Học máy chỉ được thảo
luận (chương 4).

## 1.2 Kiến trúc hai tầng, một định nghĩa mạng

- **Nguồn sự thật duy nhất**: `config/network/matrix.yaml` (10 nút, 1 bus 500 kbit/s, 20 message).
  File DBC `config/dbc/canav.dbc` được sinh bằng cantools, không sửa tay (D7).
- **Tầng online**: 9 ECU là 9 tiến trình Python trên SocketCAN `vcan0` (python-can), cùng một bridge
  nối với bộ mô phỏng xe (`sim2d` – mô hình xe đạp động học tự viết, hoặc CARLA 0.9.16), một gateway
  raw-socket và `candump`. Cảm biến thô không đi trên CAN: bridge gửi trạng thái qua UDP loopback
  tới các ECU cảm biến, actuator gửi lệnh về bridge qua UDP (D4).
- **Tầng offline**: mô hình bus SimPy với phân xử theo ID, thời gian bit, bit stuffing, lỗi khung,
  TEC/bus-off, gateway store-and-forward và chuỗi nhân quả `origin_ts` (D1, D2). Đây là công cụ chính
  cho các thí nghiệm quét tham số.
- **Một định dạng log** cho cả hai tầng: `candump -L` + sidecar JSONL (D6).

## 1.3 Ma trận message

| Nhóm | Message (ID, chu kỳ) | Gửi → nhận |
|---|---|---|
| An toàn | AEB_BrakeRequest (0x010, 10 ms) | AEB → ThrottleBrakeActuator |
| Điều khiển | Steer_Cmd (0x100), ThrottleBrake_Cmd (0x101), 10 ms | Control → actuator |
| Cảm biến xe | WheelSpeed (0x200), IMU (0x201), 10 ms; GNSS_Pose (0x210, 100 ms) | → Control, Planning |
| Nhận thức | ObjectList_0..7 (0x300–0x307), TrafficLight_Lane (0x310), 50 ms | Perception → AEB, Planning |
| Kế hoạch | TargetPath_0..1 (0x400–0x401), 50 ms | Planning → Control |
| Thân xe | VehicleStatus (0x500, 100 ms) | BodyHMI → Gateway |
| Chẩn đoán | Diag_Request (0x7DF), Diag_Response (0x7E8), sự kiện | Gateway ↔ BodyHMI |

- **Gán ID `safety_first`**: ID nhỏ (ưu tiên cao) cho chuỗi an toàn và điều khiển. Phương án đối
  chứng `naive` gán theo thứ tự node (AEB ở 0x600).
- **Bảo vệ E2E** trên mọi message định kỳ: CRC8 SAE J1850 (đa thức 0x1D) trên dữ liệu + DataID (byte
  thấp của ID) và AliveCounter 4 bit; bên nhận loại frame sai CRC và đánh dấu nhảy counter.

## 1.4 Logic ECU (tầng online)

- ChassisSensors/GNSS lấy mẫu trạng thái từ bridge; Perception lọc vật trong hành lang 50 m phía
  trước (ground truth, D8), luôn gửi 8 frame ObjectList.
- Planning: pose = GNSS + dead reckoning, 3 điểm nhìn trước, tốc độ mục tiêu giảm khi có vật trong
  đường đi, đèn đỏ, khúc cua, đích.
- Control: Pure Pursuit + PI tốc độ (giới hạn giảm tốc 3 m/s²). AEB: TTC < 1.5 s với vật trong đường
  đi → yêu cầu 6 m/s², actuator ghi đè lệnh ga/phanh.
- Actuator: kiểm E2E, quá 3 chu kỳ không có lệnh hợp lệ → trạng thái an toàn (phanh nhẹ).
- Bộ lập lịch theo deadline tuyệt đối trên `CLOCK_MONOTONIC` (không trôi); mọi sự kiện tx/rx/use được
  ghi JSONL với thời gian ns để đo độ trễ từng chặng và e2e.

## 1.5 Gateway CAN ↔ UDP và monitor

- Gateway dùng raw socket `AF_CAN` (không python-can, D5), một vòng `selectors`, timestamp kernel
  `SO_TIMESTAMPNS`, gom frame (N frame hoặc timeout) vào datagram UDP theo giao thức nhị phân "CAV1"
  (header 20 B + record 14 + len B, có `seq` để phát hiện mất gói).
- Monitor giải mã, tính mất gói, độ trễ gateway, tải bus theo cửa sổ, tần suất từng ID, ID lạ, lỗi
  E2E và gửi yêu cầu chẩn đoán (Diag_Request 0x7DF) xuống bus.

## 1.6 Hai backend xe

- `sim2d`: đường chữ L 231 m, một xe đỗ cạnh làn, một người đi bộ băng qua, một đèn giao thông.
- CARLA 0.9.16 (Town02, đồng bộ 20 Hz, 10 xe NPC, 8 người đi bộ, tuyến 202 m qua 2 đèn): cùng giao thức
  side channel, ECU không sửa code; chỉ hiệu chỉnh tham số xe (góc lái thực, phanh) trong file riêng.
