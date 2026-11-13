# can-av

Mạng CAN ~10 nút cho xe tự hành mô phỏng (đồ án Lập trình mạng): ECU là các tiến trình chỉ giao
tiếp qua CAN (SocketCAN `vcan0`), xe mô phỏng bằng `sim2d` hoặc CARLA, mô hình bus SimPy cho các thí
nghiệm, gateway CAN↔UDP bằng raw socket và bộ phát hiện bất thường dựa trên ngưỡng/thống kê.

Đặc tả và các phase: ``. Tiến độ và quyết định: `PROGRESS.md`.

## Tái lập kết quả (3 lệnh)

```bash
make setup                    # uv sync (extras: carla client, pyarrow)
make test                     # toàn bộ test, không cần vcan/CARLA
make experiments figures      # SimPy E0-E5, E7, E8 + E9 + tiêu chí (~3 phút), rồi hình + docs/results.md
```

Kết quả: `results/` (không commit), `report/tables/*.csv`, `report/figures/*.png`, `docs/results.md`.

Phần online cần vcan (USER, cần root một lần sau mỗi lần khởi động máy):

```bash
sudo scripts/setup_vcan.sh    # tạo vcan0
make demo-2d                  # stack sim2d trên vcan0 (~50 s)
make experiments-online       # E0, E7, E8 online (~7 phút), rồi chạy lại: make figures
make gateway-bench            # độ trễ gateway theo batching (~3 phút)
make demo-carla               # cần CARLA 0.9.16 server đang chạy
```

Không có kết quả online thì bảng tiêu chí và các hình vẫn được sinh từ dữ liệu SimPy (các dòng/series
online bị bỏ qua); bảng gateway (`report/tables/gateway_latency.csv`) được commit sẵn.

## Tài liệu

| File | Nội dung |
|---|---|
| `docs/architecture.md` | Định nghĩa mạng, E2E, lý thuyết thời gian, mô hình SimPy, tầng online, CARLA |
| `docs/gateway_protocol.md` | Giao thức CAV1, batching, phương pháp đo độ trễ |
| `docs/experiments.md` | Thí nghiệm, tiêu chí, bộ phát hiện, giới hạn |
| `docs/results.md` | Hình + nhận xét + kết luận từng tiêu chí (sinh tự động) |
| `report/draft/` | Bản nháp báo cáo: thiết kế, phương pháp, kết quả, thảo luận |

## Lệnh Make

`setup test lint dbc theory sim-baseline budget experiments experiments-online figures demo-2d
demo-carla gateway-bench clean-results`
