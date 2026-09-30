# PingDiagnose – Giám sát kết nối IP (Windows Service)

Phần mềm chạy dạng **Windows Service**, định kỳ ping các địa chỉ IP, có dashboard và báo cáo thống kê qua giao diện web HTTPS.
Viết bằng Python (Flask + SQLite), đóng gói sẵn thành `PingDiagnose.exe`.

## Tính năng

| # | Chức năng | Chi tiết |
|---|-----------|----------|
| 1 | Khai báo IP giám sát | Thêm / sửa / xoá / tạm dừng, nhập hàng loạt (`IP, Tên, Mô tả` mỗi dòng), nút "Ping thử" |
| 2 | Ping định kỳ | Mặc định **3 phút** một lần, **2 gói ping** mỗi địa chỉ (song song, dùng ICMP API của Windows) |
| 3 | Theo dõi mất kết nối | Không phản hồi **3 chu kỳ liên tiếp (9 phút)** → trạng thái *Mất kết nối*, ghi vào lịch sử; ghi nhận khi phục hồi |
| 4 | Dashboard | Số địa chỉ hoạt động / lỗi / mất kết nối, tỷ lệ kết nối & gói nhận 24h, biểu đồ theo giờ, tỷ lệ 24h/7 ngày từng IP |
| 5 | Báo cáo | Chọn IP + khoảng ngày, nhóm theo giờ/ngày: biểu đồ, bảng thống kê, danh sách sự kiện, **xuất CSV (Excel)**, in |
| 6 | Phân quyền | **Quản trị** (full quyền) và **Chỉ xem** |

Chu kỳ, số gói, timeout, ngưỡng cảnh báo, số ngày lưu dữ liệu chỉnh trong trang **Cấu hình**.

## Cài đặt

1. Tải `PingDiagnose-win64.zip` (GitHub **Actions → Build Windows exe → Artifacts**, hoặc **Releases**), giải nén.
2. Chuột phải `install.bat` → **Run as administrator** (hoặc `install.bat 443` để dùng cổng khác, mặc định **8443**). Script sẽ:
   - chép chương trình vào `C:\Program Files\PingDiagnose`
   - tạo chứng chỉ HTTPS cho tên máy + mọi IP của máy chủ
   - tạo service **PingDiagnose** (tự chạy cùng Windows, tự khởi động lại khi lỗi), mở cổng firewall
3. Truy cập `https://<IP-máy-chủ>:8443`, đăng nhập **admin / admin** (bắt buộc đổi mật khẩu lần đầu).
4. Vào **Tài khoản** để tạo tài khoản quản trị hoặc chỉ xem.

**Gỡ cài đặt:** chạy `uninstall.bat` (quyền admin), có hỏi xoá dữ liệu hay không.

## HTTPS không bị cảnh báo chứng chỉ

Trình duyệt chỉ không cảnh báo khi chứng chỉ do một CA mà máy trạm tin cậy cấp. Chọn một trong các cách:

| Cách | Làm gì | Máy trạm |
|---|---|---|
| **Chứng chỉ của công ty** (wildcard, hoặc do CA nội bộ của AD cấp) | Trên máy chủ: `PingDiagnose.exe cert --import chungchi.pfx --password matkhau` rồi khởi động lại service. Truy cập bằng tên miền có trong chứng chỉ (VD `https://ping.congty.vn:8443`) | Không cần làm gì |
| **CA của PingDiagnose + Group Policy** | GPO → Computer Configuration → Windows Settings → Security Settings → Public Key Policies → Trusted Root Certification Authorities → Import `C:\ProgramData\PingDiagnose\ca.crt` | Tự nhận qua GPO |
| **CA của PingDiagnose, từng máy** | — | Ở trang đăng nhập bấm **Cài chứng chỉ cho máy này**, chạy file bat → **Yes**, đóng hết trình duyệt rồi mở lại |

Chứng chỉ tự cấp của PingDiagnose hợp lệ cho tên máy, `localhost` và mọi IP của máy chủ, tự cấp lại khi IP thay đổi.
Truy cập bằng tên miền nội bộ: `PingDiagnose.exe config --name ping.congty.local` rồi khởi động lại service.
Chứng chỉ riêng dạng PEM: `PingDiagnose.exe cert --import cert.pem --key key.pem`. Quay lại CA nội bộ: `PingDiagnose.exe cert --self`.

## Dữ liệu & log

| Thứ | Vị trí |
|-----|--------|
| Cơ sở dữ liệu | `C:\ProgramData\PingDiagnose\pingdiagnose.db` |
| Cấu hình cổng/HTTPS | `C:\ProgramData\PingDiagnose\config.json` |
| CA, chứng chỉ | `ca.crt`, `server.crt`, `custom.crt` cùng thư mục |
| Log | `C:\ProgramData\PingDiagnose\logs\pingdiagnose.log` |

Sao lưu: dừng service rồi copy thư mục `C:\ProgramData\PingDiagnose`.

## Lệnh dòng lệnh

```bat
PingDiagnose.exe run                          :: chạy trong console (không cần service)
PingDiagnose.exe config --port 443            :: đổi cổng
PingDiagnose.exe config --name ping.local     :: thêm tên miền vào chứng chỉ tự cấp
PingDiagnose.exe cert --import c.pfx --password x   :: dùng chứng chỉ của công ty
PingDiagnose.exe reset-admin                  :: quên mật khẩu: đặt lại admin/admin
```

Sau khi đổi cấu hình: `sc stop PingDiagnose & sc start PingDiagnose`.

## Cách tính

- **Tỷ lệ kết nối** = số lần kiểm tra có ít nhất 1 gói phản hồi / tổng số lần kiểm tra.
- **Tỷ lệ gói nhận** = tổng gói nhận / tổng gói gửi.
- **Trạng thái**: *Hoạt động* (có phản hồi), *Đang lỗi* (mất phản hồi, chưa đủ ngưỡng), *Mất kết nối* (≥ ngưỡng chu kỳ liên tiếp).
- **Thời gian mất kết nối (ước tính)** = số lần kiểm tra thất bại × chu kỳ.

## Build từ mã nguồn

Trên Windows có Python 3.10+: chạy `build.bat` → `dist\PingDiagnose-win64.zip`.
Mỗi lần push, GitHub Actions tự build, chạy test và chạy thử service; push tag `v*` để tạo Release.
