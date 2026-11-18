from canav.gateway import main
# Chạy hàm main() và truyền giá trị trả về (exit code: 0 nếu thành công, khác 0 nếu có lỗi)
# cho SystemExit để hệ điều hành hoặc script gọi nó nhận biết được trạng thái kết thúc
raise SystemExit(main())
