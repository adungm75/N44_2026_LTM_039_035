# Giao thức gateway CAN ↔ UDP "CAV1" (Phase 5)

Mã nguồn: `canav.gateway.protocol` (mã hóa/giải mã, batching), `canav.gateway` (tiến trình
gateway), `canav.monitor` (bên nhận). Cấu hình: `config/online/gateway.yaml`.

## Kiến trúc

```
 vcan0 ──(AF_CAN / SOCK_RAW, CAN_RAW_FILTER)──► gateway ──UDP 127.0.0.1:48000 (uplink)──► monitor
   ▲                                               │ ▲
   └────────────── frame 0x7DF (Diag_Request) ◄────┘ └──UDP :48001 (downlink, DIAG_REQ)── monitor
```

- Gateway là một tiến trình, một vòng `selectors` theo dõi 2 socket: raw socket CAN (bind vào
  `vcan0`) và socket UDP downlink. Không dùng python-can (D5).
- Thời điểm nhận frame lấy từ kernel: `SO_TIMESTAMPNS` + `recvmsg` (CLOCK_REALTIME, ns). Nếu
  không có cmsg timestamp thì dùng `time.time_ns()` và bật cờ `FLAG_SW_TIMESTAMP`.
- `CAN_RAW_RECV_OWN_MSGS` bật: frame do chính gateway gửi (0x7DF) cũng được đưa lên uplink, có
  cờ `FLAG_OWN_TX` (kernel đánh dấu bằng `MSG_CONFIRM`), để monitor thấy toàn bộ bus.
- `SO_RXQ_OVFL`: kernel báo số frame bị drop ở hàng đợi socket; gửi lên trong STATS.
- Downlink chỉ chấp nhận DIAG_REQ có ID nằm trong `downlink_ids` (mặc định chỉ 0x7DF), ID
  chuẩn 11 bit, không RTR/ERR. Mọi thứ khác bị đếm vào `rejected` và bỏ đi.

## Định dạng (network byte order)

```
Header 20 B : magic "CAV1" 4s | version u8 | type u8 | count u16 | seq u32 | t_send_ns u64
              type 1 DATA, 2 DIAG_REQ, 3 HEARTBEAT, 4 STATS
DATA record : can_id u32 | t_rx_ns u64 | len u8 | flags u8 | data[len]      (14 + len B)
STATS body  : rx_frames u64 | rx_dropped u32 | datagrams u32 | diag_sent u32 | rejected u32  (24 B)
```

| Trường | Ý nghĩa |
|---|---|
| `version` | 1 |
| `count` | số record trong datagram (DATA, DIAG_REQ); 0 với HEARTBEAT, STATS |
| `seq` | đếm mọi datagram của một bên gửi, quay vòng ở 2³²; bên nhận tính mất gói từ khoảng trống |
| `t_send_ns` | CLOCK_REALTIME lúc gửi datagram |
| `can_id` | ID kèm cờ SocketCAN: EFF bit 31, RTR bit 30, ERR bit 29 |
| `t_rx_ns` | CLOCK_REALTIME lúc kernel nhận frame (DIAG_REQ: lúc monitor tạo yêu cầu) |
| `len` | 0–8 (chỉ CAN cổ điển) |
| `flags` | bit 0 `FLAG_OWN_TX` (frame do gateway gửi), bit 1 `FLAG_SW_TIMESTAMP` |

- DATA: gửi các frame nhận được từ bus. DIAG_REQ (downlink): mỗi record là một frame cần ghi ra bus.
- HEARTBEAT: chỉ có header, gửi khi đã `heartbeat_s` (1 s) không gửi gì.
- STATS: các bộ đếm của gateway, gửi mỗi `stats_s` (1 s) và một lần lúc dừng.
- Kích thước datagram ≤ 1400 B (dưới MTU Ethernet 1500 trừ IP/UDP header, không bị phân mảnh nếu
  sau này dùng mạng thật). 20 + 62 × 22 = 1384 B: tối đa 62 frame 8 byte/datagram.

### Ví dụ hex

DATA, `seq` = 5, 2 record (dữ liệu minh họa, sinh bằng `encode()`):
`0x010` 4 byte nhận từ bus, và `0x7DF` do chính gateway gửi (`flags` = 0x01). Tổng 60 B.

```
43 41 56 31 01 01 00 02 00 00 00 05 18 e7 b6 4b   "CAV1" v1 DATA count=2 seq=5 t_send…
7b 30 57 00 00 00 00 10 18 e7 b6 4b 7b 09 24 63   …t_send | can_id 0x010 | t_rx…
04 00 01 0e 17 45 00 00 07 df 18 e7 b6 4b 7b 0a   …t_rx len=4 flags=0 data(4) | can_id 0x7DF | t_rx…
31 60 08 01 08 22 01 00 00 00 00 00               …t_rx len=8 flags=OWN_TX data(8)
```

## Vì sao UDP và gom frame (batching)

- UDP trên loopback: không bắt tay, không giữ trạng thái kết nối, không có head-of-line blocking
  như TCP; mất gói (hiếm trên loopback) được phát hiện bằng `seq`. Dữ liệu là luồng giám sát, mất
  một datagram không cần gửi lại.
- Ở ~740 frame/s, mỗi frame một datagram nghĩa là ~740 syscall `sendto` + `recv` mỗi giây ở cả
  hai đầu. Gom frame giảm số datagram và syscall nhưng làm frame phải chờ trong bộ đệm.
- Quy tắc flush (`Batcher`): gửi khi đủ `batch_frames` frame, hoặc khi frame tiếp theo làm
  datagram vượt 1400 B, hoặc khi frame cũ nhất đã chờ `flush_ms`. Vòng `selectors` dùng hạn flush
  gần nhất làm timeout của `select`.

## Phương pháp đo độ trễ

- Độ trễ gateway của một frame = `t_monitor_rx − t_rx_ns`. `t_rx_ns` là timestamp kernel khi raw
  socket của gateway nhận frame; `t_monitor_rx` = `time.time_ns()` ngay sau `recv` của monitor.
  Cả hai cùng CLOCK_REALTIME trên cùng một máy, nên phép trừ hợp lệ. Độ trễ gồm: chờ trong batch,
  xử lý Python của gateway, UDP loopback, và lịch CPU tới khi monitor đọc.
- Mất gói UDP: tổng các khoảng trống `seq` (có xử lý quay vòng 2³²); datagram có `seq` cũ hơn
  được đếm là đảo thứ tự.
- Tải bus của monitor: cộng số bit của từng frame theo cửa sổ 1 s **theo `t_rx_ns`** (không theo
  lúc datagram tới), công thức worst-case Davis và stuffing thực tế giống `canav.timing`.
- Đo: `scripts/measure_gateway.py` (`make gateway-bench`) chạy stack sim2d trên vcan0 làm nguồn
  lưu lượng, rồi lần lượt batch 1/8/32 × timeout 1/5 ms, mỗi cấu hình 15 s, đồng thời chạy
  `canbusload` (worst-case và `-e`). Kết quả: `report/tables/gateway_latency.csv`,
  `report/tables/monitor_vs_canbusload.csv`, `results/gateway/<ts>/summary.json`.
