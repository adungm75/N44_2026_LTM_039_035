# Chương 4. Thảo luận

> Bản nháp.

## 4.1 Ý nghĩa thiết kế

- **Gán ưu tiên quan trọng hơn bitrate dư**: ở cùng tải, chỉ đổi ID của AEB_BrakeRequest từ 0x010 sang
  0x600 đã làm chuỗi an toàn trượt deadline ở 90 % tải (E3), trong khi `safety_first` gần như miễn nhiễm
  với tải. Message an toàn nên nhận ID nhỏ nhất, độc lập với node gửi.
- **Ngưỡng tải 50 % là hợp lý cho ma trận này**: tải cao nhất còn đạt mọi deadline đúng bằng 50 % (E4);
  vượt đó, message ưu tiên thấp với chu kỳ ngắn là thứ trượt đầu tiên. Quy tắc kinh nghiệm không thay
  được phân tích WCRT vì kết quả phụ thuộc chu kỳ và ưu tiên của từng message.
- **Budget end-to-end mới là ràng buộc thật**: với chuỗi AEB, tuổi dữ liệu do lấy mẫu (chu kỳ 50 ms của
  ObjectList) chiếm phần lớn budget; độ trễ trên CAN chỉ vài trăm µs. Gấp đôi chu kỳ (E2) hoặc thêm trễ
  phần mềm 20 ms (E7) làm trượt budget trong khi mạng vẫn rảnh.
- **E2E vừa là an toàn vừa là phát hiện**: counter 4 bit phát hiện 100 % frame mất (khi chuỗi < 16) và
  100 % cửa sổ bị spoof bởi kẻ tấn công không theo dõi counter; watchdog bao phần còn lại.
- **Gateway**: gom frame đổi độ trễ lấy số datagram; với lưu lượng ~740 frame/s, timeout quyết định độ
  trễ chứ không phải kích thước batch.

## 4.2 Học máy cho phát hiện bất thường (chỉ thảo luận)

- **Isolation Forest**: cô lập điểm bất thường bằng các cây chia ngẫu nhiên; phù hợp với vector đặc
  trưng cửa sổ (tải, số frame mỗi ID, inter-arrival); không cần nhãn, nhưng cần chọn tỉ lệ nhiễm
  (contamination) và khó giải thích hơn luật.
- **One-class SVM**: học biên của dữ liệu bình thường trong không gian đặc trưng; nhạy với chuẩn hoá và
  tham số kernel (ν, γ), chi phí huấn luyện tăng nhanh theo số cửa sổ.
- **Mô hình chuỗi thời gian** (LSTM/GRU dự đoán frame hoặc giá trị tín hiệu kế tiếp, autoencoder tái tạo
  cửa sổ): bắt được bất thường theo ngữ cảnh mà luật cửa sổ bỏ qua (ví dụ giá trị tín hiệu giả nhưng
  đúng tần suất và counter); đổi lại cần nhiều dữ liệu, khó kiểm chứng và khó chạy trên ECU.
- Với 4 loại tấn công của E8, luật đơn giản đã đạt TPR ≥ 0.99 và FPR ≈ 0, nên ML chỉ đáng giá cho các
  tấn công tinh vi hơn (masquerade giữ đúng chu kỳ và counter), vốn nằm ngoài mô hình tấn công ở đây.

## 4.3 Giới hạn

- **vcan không phải bus thật**: không có phân xử bit, không có lỗi khung, băng thông không giới hạn —
  flood trên vcan không làm các ECU khác mất frame như trong SimPy; timing online phản ánh lịch của hệ
  điều hành và Python, không phải bộ điều khiển CAN.
- **Perception đơn giản hoá (D8)**: ground truth của bộ mô phỏng, hành lang thẳng phía trước; trong khúc
  cua, người đi bộ ở phía trong cua có thể kích hoạt AEB (đã thấy ở CARLA).
- **e2e theo origin mới nhất**: mất 1 trong 8 frame ObjectList không làm tăng e2e đo được, nên E7 drop
  không thể làm chuỗi AEB trượt theo định nghĩa này; tuổi dữ liệu của từng vật cần một định nghĩa khác.
- **Thời gian thực thi task trong SimPy là giả định** (Control/AEB 1 ms, Planning 5 ms), chưa thay bằng
  số đo online.
- **Breaking point phụ thuộc lựa chọn**: tốc độ thiết kế 60 km/h, cách chia budget, mức tải tổng hợp và
  dải quét lỗi đều là lựa chọn có ghi lý do; kết luận E1, E2 sẽ khác nếu D = T.
- **Thống kê**: SimPy rất tuần hoàn nên kết quả bộ phát hiện là cận trên lạc quan; online chỉ có một run
  huấn luyện và một run cho mỗi loại anomaly, không có khoảng tin cậy.
- **CARLA**: chạy không camera (server crash khi bật camera); bridge CARLA được ngoại lệ ngân sách RAM.
- **Chưa làm**: thí nghiệm tốc độ tránh va chạm tối đa (P1), CAN FD (E6, P2), Isolation Forest
  (P2), Wireshark dissector (P2).

## 4.4 Hướng phát triển

Đo thời gian thực thi task online và đưa vào SimPy; định nghĩa e2e theo tuổi dữ liệu từng vật; kịch
bản riêng cho thí nghiệm tốc độ tránh va chạm; thử trên phần cứng CAN thật (ví dụ USB-CAN) để có phân
xử bit và lỗi khung thật; mô hình kẻ tấn công theo dõi counter và bộ phát hiện dựa trên giá trị tín hiệu.
