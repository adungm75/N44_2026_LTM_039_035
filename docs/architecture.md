# Kiến trúc mạng CAN

## Định nghĩa mạng (Phase 1)

- Nguồn chân lý: `config/network/matrix.yaml` (node, bus, message, signal, T/D/J, E2E).
  `config/dbc/canav.dbc` được sinh bằng `make dbc` (cantools `as_dbc_string()`); test
  `test_committed_dbc_is_generated` bảo đảm file DBC trong repo đúng bằng output của generator.
- Đơn vị SI: `period`, `deadline`, `jitter` tính bằng giây; `period: null` = message sự kiện.
  `deadline` mặc định bằng `period`, `jitter` mặc định 0.
- Signal: byte order Intel, `start` = vị trí bit LSB. Danh sách signal đầy đủ xem `matrix.yaml`.

### E2E (`canav.e2e`)

| DLC | Signal | AliveCounter | CRC8 |
|---|---|---|---|
| 8 | bit 0–47 | nibble thấp byte 6 (bit 48–51) | byte 7 |
| 4 | bit 0–15 | nibble thấp byte 2 (bit 16–19) | byte 3 |

- CRC8 SAE J1850 (poly 0x1D, init 0xFF, xorout 0xFF) tính trên các byte 0..DLC-2 (gồm cả byte
  counter) **rồi đến** DataID = byte thấp của CAN ID. Vì DataID lấy từ ID thực tế, đổi ID scheme
  thì CRC cũng đổi theo.
- Counter 4 bit, 0..15, quay vòng. `check()` chấp nhận nếu bước nhảy `(c - c_prev) mod 16` nằm trong
  `1..max_delta` (mặc định 1): lặp lại hoặc mất frame đều bị báo `counter_err`.
- Nibble cao byte DLC-2 không dùng. Validator từ chối signal nằm ngoài vùng bit dữ liệu.
- `AliveCounter`, `CRC8` được netdef tự thêm vào DBC; tên này bị cấm cho signal của người dùng.

### Lựa chọn bổ sung

- `lane_heading_err`: s12, scale 0.001 rad (spec không cho scale).
- `ObjectList.cls`: 0 unknown, 1 car, 2 pedestrian, 3 cyclist, 4 static obstacle.
- `TrafficLight_Lane.tl_state`: 0 none/unknown, 1 red, 2 yellow, 3 green.
- `TargetPath`: tên điểm `p1_x`, `p1_y`, … (DBC không cho dấu chấm).
- `throttle`, `brake`: scale 1/255; `steer`: giới hạn vật lý −1..1.
- Diag (không E2E): `Diag_Request` (0x7DF, Gateway → tất cả) = `target u8, service u8, did u16`;
  `Diag_Response` (0x7E8, BodyHMI → Gateway) = `source u8, service u8, did u16, value u32`.

### ID scheme (`config/network/id_schemes.yaml`)

- `safety_first`: ID như trong `matrix.yaml`.
- `naive`: theo thứ tự node; node phát thứ k nhận base 0x100·(k+1), message đánh số theo thứ tự
  trong matrix → AEB_BrakeRequest = 0x600. ID chẩn đoán (0x7DF/0x7E8) giữ nguyên.

## Lý thuyết thời gian (`canav.timing`)

- `frame_bits(dlc, stuffing)`: `worst` (Davis), `none`; `frame_bits_actual(id, data)` dựng chuỗi bit
  SOF…CRC (CRC-15 0x4599) và đếm bit stuff thực tế. CAN FD (P2) chưa làm.
- `davis_wcrt`: phân tích Davis et al. 2007 với số hữu tỉ chính xác (`Fraction`). Message sự kiện
  (diag) chỉ tính vào blocking `B_m`, không có dòng WCRT riêng.
- `make theory` → `report/tables/theory_baseline.csv` (C, T, D, J, R theo giây).

## Mô hình bus SimPy (`canav.busmodel`, Phase 2)

- Thời gian mô phỏng = **int ns** (`env.now`); thời lượng bit làm tròn tới ns (chính xác với
  125k/250k/500k/1M).
- Phân xử: khi bus rảnh tại t, chờ một sự kiện ưu tiên thấp (`LATE = 2`, sau mọi sự kiện NORMAL
  cùng thời điểm) để mọi frame phát hành tại t vào hàng đợi trước; ID nhỏ nhất thắng, không
  chiếm quyền. `tx_queue: fifo` chỉ cho đầu hàng đợi của mỗi node tham gia.
- Overwrite (mặc định bật): instance mới cùng ID thay frame đang chờ → `overwritten`; frame đang
  truyền không bị thay. Frame lỗi mà đã có instance mới chờ cũng → `overwritten`.
- Lỗi: xác suất theo lần truyền (`error_prob`) hoặc BER (`1-(1-ber)^bits`); hủy tại bit ngẫu nhiên
  đều trong `1..bits-3`, cộng error frame cố định `error_frame_bits` (mặc định 31), truyền lại qua
  phân xử. TEC +8/−1; passive > 127 (chỉ ghi trạng thái, chưa mô hình hóa suspend transmission);
  bus-off > 255 → frame đang chờ của node → `busoff`; phục hồi sau 128×11 thời gian bit (tính theo
  thời gian, không đếm riêng bit lặn), tắt bằng `busoff_recovery: false`.
- Outcome `error` được dự trữ, hiện chưa dùng (frame lỗi luôn được truyền lại).
- Chuỗi nhân quả: node trong `sim.tasks` là task tiêu thụ; input = mọi message trên cùng bus có node
  trong `receivers`; output phát tại `release + exec_time` và mang `origin_ts` mới nhất (max) trong
  các input đã nhận (None nếu chưa nhận gì). Nguồn (cảm biến) có `origin_ts = t_release`.
- Offset: `zero` hoặc `random` (một pha ngẫu nhiên mỗi node trong `[0, chu kỳ dài nhất)`, lấy modulo
  từng chu kỳ). Payload = 0 + counter/CRC E2E (counter = instance mod 16).
- CAN FD (P2) chưa làm: `Bus(fd=True)` báo `NotImplementedError`.
- Output mỗi run: `can.log` (`candump -L`, chỉ frame `ok`, interface = tên bus), `frames.jsonl`
  (sidecar + `dlc`), `errors.jsonl` (mỗi lần truyền lỗi), `metrics.json`, `config.yaml`
  (cấu hình đã resolve + seed + git hash). `run_id = seed<N>-<hash cấu hình>`.

## Tầng online (Phase 3)

### Tiến trình và kênh
- 9 ECU (`canav.ecu.nodes`) + bridge (`canav.vehicle.bridge`) + `candump -L` = 11 process trên
  `vcan0` (`make demo-2d`). Node Gateway của matrix sẽ là gateway raw-socket ở Phase 5 (D5), nên
  chưa có ECU python-can cho nó.
- `--iface virtual`: mọi thứ chạy bằng thread trong 1 process (python-can virtual chỉ trong
  process); dùng cho test. Do GIL, độ trễ/jitter ở chế độ này **không đại diện**; số đo online lấy
  ở chế độ process trên vcan0.
- Side channel UDP 127.0.0.1: bridge gửi state tới ChassisSensors, GNSS, Perception, BodyHMI
  (cổng `port_base + 1 + chỉ số node`); actuator gửi ctrl tới `port_base + 100`. Mỗi actuator chỉ
  điền trường của mình (`steer` hoặc `throttle/brake`), bridge giữ giá trị mới nhất mỗi tick.
  `tl` trong state mang thêm `lane_offset`, `lane_heading_err` (dùng cho TrafficLight_Lane).
- Bridge ghi `route.json` trước; launcher chờ file này rồi mới khởi động ECU (Planning nạp route).

### ECU runtime (`canav.ecu.base`)
- Một vòng lặp/ECU: chu kỳ = `GenMsgCycleTime` nhỏ nhất của message ECU gửi (actuator: 10 ms
  watchdog); deadline tuyệt đối `start + k·T` theo `time.monotonic_ns`; nếu trễ quá 1 chu kỳ thì bỏ
  qua các deadline đã lỡ (đếm `overrun`), không trôi. Giữa các deadline: `bus.recv(timeout)`.
- Bộ lọc CAN theo các ID mà ECU nhận. Rx: kiểm CRC (sai → `crc_err`, bỏ frame), counter (sai →
  `counter_err`, vẫn dùng frame). Log JSONL `{t_ns, ecu, event, msg, counter, src?}`;
  `t_ns = CLOCK_MONOTONIC` (chung cho mọi process → join được). Thống kê cuối: `<ECU>.stats.json`.
- Actuator ghi `use` (msg `ThrottleBrake` / `Steer_Cmd`, `src` = counter lệnh đã áp dụng, có
  `AEB_BrakeRequest` khi AEB ghi đè); vào safe state ghi `use` với msg `safe_state`.
- `FaultConfig`: `--fault ECU:drop_prob=..,extra_delay_ms=..,silent_after_s=..` hoặc env
  `CANAV_FAULT`. Frame bị drop vẫn tiêu counter (bên nhận phát hiện mất).

### Logic ECU (tham số trong `config/online/ecu_params.yaml`)
- Hệ tọa độ ego: gốc tại trục sau, x tiến, y trái. `ObjectList.x` = khoảng cách từ cản trước tới
  mép gần của vật (perception ground truth); hành lang 50 m × ±3 m.
- Planning: pose = GNSS (100 ms) + dead reckoning bằng WheelSpeed/IMU; 3 điểm nhìn trước 5/10/15 m
  (hệ ego); vận tốc mục tiêu = min(cruise, giới hạn cong, dừng trước vật trong đường đi, chậm
  `v_pass` khi vật cạnh đường đi, dừng đèn đỏ/vàng, dừng ở đích). Profile dừng dùng vị trí dự đoán
  trước `lead_time` để bù độ trễ vòng tốc độ (không có thì xe vượt đèn đỏ ~1 m).
- Control: Pure Pursuit (look-ahead theo tốc độ) + PI tốc độ → gia tốc mong muốn, giới hạn giảm
  tốc `decel_max = 3 m/s²` (phanh mạnh hơn là việc của AEB).
- AEB: vật trong đường đi (|y| < 1.5 m) có TTC < 1.5 s → yêu cầu 6 m/s², giữ tới khi vật không
  còn tiến lại gần. ThrottleBrakeActuator ghi đè: throttle 0, brake ≥ decel/b_max.
- Actuator: > 3 chu kỳ không có lệnh hợp lệ → safe state (lái: giữ lệnh cuối; ga/phanh: phanh
  nhẹ 0.2).
- BodyHMI: VehicleStatus mode 1 (tự lái), gear 1 (D), odometer tích phân từ state. Diag (P1, Phase 5):
  `Diag_Request` có `target` = 8 (chỉ số BodyHMI trong danh sách node DBC) hoặc 0xFF, service 0x22
  → `Diag_Response` service 0x62, DID 0x0001 = odometer [0.1 m], 0x0002 = mode; service khác →
  0x7F với value = NRC 0x11, DID lạ → 0x7F với NRC 0x31.

### Kịch bản sim2d (`config/scenarios/sim2d_default.yaml`)
- Đường chữ L 231.4 m; xe đỗ ở s = 35 m, lệch −2.6 m (cạnh làn, **không chắn** đường đi — nếu chắn
  hẳn thì không về đích được vì Planning không đổi làn); người đi bộ xuất hiện khi cản trước còn
  cách điểm băng qua 12 m (≈ 1.5 s ở 8 m/s, như bước ra từ chỗ khuất; tùy chọn `start_time` theo
  spec); đèn đỏ tới t_sim = 30 s tại vạch s = 110 m.
- Va chạm: hộp bao của vật chiếu vào hệ ego (xấp xỉ AABB), đủ cho xe/người thẳng hàng với đường.

### Phân tích online (`canav.analysis.online`)
- Hop latency: mỗi `rx` ghép với `tx` gần nhất trước nó có cùng (msg, counter).
- e2e: từ `use` của actuator, lần ngược `src` tới thời điểm `tx` của message nguồn (không có `src`)
  và lấy nguồn mới nhất (max), giống quy tắc `origin_ts` của sim.
- Tải bus từ `can.log`: worst-case và stuffing thực tế (dựng chuỗi bit từng frame).

## Backend CARLA (Phase 4, `canav.vehicle.carla_bridge`)
- Cùng giao thức side channel và cùng 9 ECU như sim2d (ECU không sửa code); chỉ khác file tham số
  `config/online/ecu_params_carla.yaml` và kịch bản `config/scenarios/carla_town02.yaml`.
  `make demo-carla` = `launch_stack.py --backend carla --iface vcan0 --duration 300`.
- CARLA 0.9.16, Town02, synchronous mode, `fixed_delta_seconds = 0.05` (= tick bridge, chạy theo
  thời gian thực), Traffic Manager sync. Ego `vehicle.lincoln.mkz_2020` không bật autopilot: chỉ
  lái bằng lệnh từ actuator (CAN → UDP). Khi đóng: xóa mọi actor đã spawn, tắt TM sync, trả server
  về cài đặt ban đầu (async).
- Hệ tọa độ: CARLA là hệ tay trái (y sang phải, yaw theo chiều kim đồng hồ, độ; steer > 0 = rẽ
  phải). Mọi thứ ra khỏi backend đổi về hệ của dự án: `y → −y`, `yaw → −yaw` (rad), `steer → −steer`.
  Pose gửi đi = trục sau (từ vị trí actor lùi `rear_offset` đo từ vị trí bánh xe).
- Route: `GlobalRoutePlanner` (PythonAPI agents, cần `networkx`) từ spawn 1 tới spawn 46, 202.1 m,
  1 rẽ trái + 1 rẽ phải, 2 vạch dừng có đèn. Cặp spawn được chọn bằng cách quét các cặp spawn của
  Town02 (tuyến 200–300 m, 2–3 đèn, ít nhất 1 lần rẽ).
- Đèn: các stop waypoint của mọi `traffic.traffic_light` được chiếu lên route (|lệch ngang| ≤ 1.5 m,
  cùng hướng ±30°); `dist_stop` = s(vạch) − s(cản trước), trạng thái từ `get_state()` của đèn đó.
  Không dùng `vehicle.get_traffic_light_state()` vì hàm này chỉ báo khi xe đã vào vùng
  trigger sát giao lộ, quá muộn để dừng với `a_comf` và không cho khoảng cách tới vạch.
- Vật: mọi xe và người đi bộ (từ `world.get_actors()`) trong 60 m; tâm = tâm bounding box; id CARLA
  đổi sang id nhỏ 1..255 theo thứ tự xuất hiện (`ObjectList.obj_id` là u8). Va chạm từ cảm biến
  `sensor.other.collision` (in loại actor bị va vào `proc/bridge.log`).
- NPC: 10 xe autopilot ở spawn ngẫu nhiên (seed), cách ego ≥ 20 m; 8 người đi bộ có AI controller,
  `cross_factor` 0.1; spawn người đi bộ thử lại tối đa 5 lần vì điểm ngẫu nhiên hay bị chồng nhau.
- Hiệu chỉnh cho CARLA (đo bằng script thăm dò, Phase 4): chiều dài cơ sở 2.86 m, track 1.59 m,
  trục sau → cản trước 3.83 m (đo từ vị trí bánh xe/bounding box). Góc lái: `max_steer_angle` 70°,
  nhưng góc bánh thực tế ≈ 0.85 × 70° ở 2.5–4.5 m/s (trung bình bánh trái/phải; steering curve +
  Ackermann) → `max_steer = 1.04 rad`. Phanh 1.0 ≈ 5.2–5.6 m/s² (từ 4.3 m/s) → `b_max = 5.5`.
  Ga không tuyến tính (0.3 gần như không chạy, 1.0 ≈ 6 m/s² sau ~0.5 s) → để `a_max = 3` và để PI bù.
- Giao lộ Town02 rất gắt (độ cong route tới ~0.21 1/m): với điểm nhìn trước 5/10/15 m và
  `ld_min = 4` Pure Pursuit chọn điểm 10 m, cắt góc rồi văng ra 2.2 m, lệch ngang RMS
  0.79 m, 1 va chạm (lần thử 1). Đổi điểm nhìn trước 3/6/9 m, `ld_gain 0.6`, `ld_min 3` → lệch ngang RMS ≈ 0.25 m.
- Perception dùng hành lang thẳng theo hệ ego; trong khúc cua, người đi bộ ở phía trong cua có thể
  lọt vào hành lang và kích hoạt AEB (seed 1: TTC 1.47 s ở 3.9 m/s). Đây là hệ quả của perception
  đơn giản hóa (D8), chưa sửa.

## Gateway và monitor (Phase 5)
- Giao thức, phương pháp đo: `docs/gateway_protocol.md`.
- Trong `launch_stack.py` chế độ process, node Gateway = tiến trình `canav.gateway` (raw socket,
  không python-can, D5) với `config/online/gateway.yaml`; `--no-gateway` để bỏ. `--monitor` chạy
  thêm `canav.monitor` ghi vào `<out>/monitor`. Chế độ `virtual` không có gateway (raw socket cần
  interface thật).
- Số tiến trình: 9 ECU + bridge + gateway + candump = 12 (giới hạn ); thêm monitor là 13
  (tùy chọn, công cụ đo chứ không phải node của mạng).
- `EcuBase.tx_msgs` nay gồm cả message sự kiện (Diag_Response) để ECU gửi được; chu kỳ ECU vẫn lấy
  từ message định kỳ.
