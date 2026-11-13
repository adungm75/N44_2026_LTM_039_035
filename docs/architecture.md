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

### Lựa chọn bổ sung so với gợi ý §5.1

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
  (sidecar §5.4 + `dlc`), `errors.jsonl` (mỗi lần truyền lỗi), `metrics.json`, `config.yaml`
  (cấu hình đã resolve + seed + git hash). `run_id = seed<N>-<hash cấu hình>`.
