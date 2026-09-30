# PingDiagnose – Giám sát kết nối IP (Windows Service)

Phần mềm chạy dạng **Windows Service**, định kỳ ping các địa chỉ IP, cảnh báo mất kết nối
qua trình duyệt, có dashboard và báo cáo thống kê. Viết bằng Python (Flask + SQLite),
đóng gói sẵn thành `PingDiagnose.exe`.

## Tính năng

| # | Chức năng | Chi tiết |
|---|-----------|----------|
| 1 | Khai báo IP giám sát | Thêm / sửa / xoá / tạm dừng, nhập hàng loạt (`IP, Tên, Mô tả` mỗi dòng), nút "Ping thử" |
| 2 | Ping định kỳ | Mặc định **3 phút** một lần, **2 gói ping** mỗi địa chỉ (ping song song, dùng ICMP API của Windows) |
| 3 | Cảnh báo | Không phản hồi **3 chu kỳ liên tiếp (9 phút)** → cảnh báo trên trình duyệt (thông báo hệ thống + popup trong trang + âm thanh + banner đỏ). Có thông báo khi phục hồi |
| 4 | Dashboard | Số địa chỉ hoạt động / lỗi / mất kết nối, tỷ lệ kết nối & tỷ lệ gói nhận 24h, biểu đồ theo giờ, tỷ lệ 24h/7 ngày từng IP |
| 5 | Báo cáo | Chọn IP + khoảng ngày, nhóm theo giờ/ngày: biểu đồ, bảng thống kê từng IP (tỷ lệ kết nối, gói nhận, RTT, số lần cảnh báo, thời gian mất kết nối ước tính), danh sách sự kiện, **xuất CSV (mở bằng Excel)**, in |
| 6 | Phân quyền | **Quản trị** (full quyền) và **Chỉ xem** |

Các thông số (chu kỳ, số gói, timeout, ngưỡng cảnh báo, số ngày lưu dữ liệu) chỉnh được trong trang **Cấu hình**.

## Cài đặt

1. Tải `PingDiagnose-win64.zip` (mục **Actions → Build Windows exe → Artifacts**, hoặc **Releases**), giải nén.
2. Chuột phải `install.bat` → **Run as administrator** (hoặc chạy `install.bat 9090` để dùng cổng khác, mặc định **8080**).
   Script sẽ:
   - chép chương trình vào `C:\Program Files\PingDiagnose`
   - tạo service **PingDiagnose** (tự khởi động cùng Windows, tự khởi động lại khi lỗi)
   - mở cổng trên Windows Firewall
   - khởi động service và mở trình duyệt
3. Truy cập `http://<IP-máy-chủ>:8080`, đăng nhập **admin / admin**. Hệ thống bắt buộc đổi mật khẩu ở lần đăng nhập đầu.
4. Vào **Tài khoản** để tạo thêm tài khoản quản trị hoặc chỉ xem.

**Gỡ cài đặt:** chạy `uninstall.bat` (quyền admin). Script dừng & xoá service, xoá rule firewall, xoá thư mục chương trình,
và hỏi có xoá dữ liệu hay không.

## Dữ liệu & log

| Thứ | Vị trí |
|-----|--------|
| Cơ sở dữ liệu (SQLite) | `C:\ProgramData\PingDiagnose\pingdiagnose.db` |
| Cấu hình cổng/HTTPS | `C:\ProgramData\PingDiagnose\config.json` |
| Log | `C:\ProgramData\PingDiagnose\logs\pingdiagnose.log` |

Sao lưu: dừng service rồi copy thư mục `C:\ProgramData\PingDiagnose`.

## Lệnh dòng lệnh

```bat
PingDiagnose.exe run                       :: chạy trực tiếp trong console (không cần service)
PingDiagnose.exe config --port 9090        :: đổi cổng (sau đó: sc stop PingDiagnose & sc start PingDiagnose)
PingDiagnose.exe config --https on         :: bật HTTPS (tự tạo chứng chỉ tự ký)
PingDiagnose.exe reset-admin               :: quên mật khẩu: đặt lại admin/admin
```

## Về thông báo trình duyệt

Trình duyệt chỉ cho phép **thông báo hệ thống** (popup góc màn hình, hiện cả khi đang ở tab khác) với trang
**HTTPS** hoặc **localhost**. Khi truy cập qua `http://IP-máy-chủ`, cảnh báo vẫn hiện **trong trang** (popup + âm thanh + banner đỏ
+ số lượng trên tiêu đề tab). Để có thông báo hệ thống từ máy khác, chọn một trong hai cách:

- Bật HTTPS: `PingDiagnose.exe config --https on`, khởi động lại service, truy cập `https://IP:8080`
  và chấp nhận chứng chỉ tự ký (hoặc khai báo `cert_file`/`key_file` của bạn trong `config.json`).
- Hoặc trên Chrome/Edge: mở `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, thêm `http://IP-máy-chủ:8080`.

Sau đó bấm **🔔 Bật thông báo** ở góc trái dưới giao diện. Cần để mở ít nhất một tab PingDiagnose để nhận cảnh báo.

## Cách tính

- **Tỷ lệ kết nối** = số lần kiểm tra có ít nhất 1 gói phản hồi / tổng số lần kiểm tra.
- **Tỷ lệ gói nhận** = tổng gói nhận / tổng gói gửi.
- **Trạng thái**: *Hoạt động* (có phản hồi), *Đang lỗi* (mất phản hồi nhưng chưa đủ ngưỡng), *Mất kết nối* (≥ ngưỡng chu kỳ liên tiếp – đã cảnh báo).
- **Thời gian mất kết nối (ước tính)** = số lần kiểm tra thất bại × chu kỳ.

## Build từ mã nguồn

Trên Windows có Python 3.10+:

```bat
build.bat
```

Kết quả: `dist\PingDiagnose\` và `dist\PingDiagnose-win64.zip`. Mỗi lần push lên GitHub, workflow
`.github/workflows/build.yml` tự build trên Windows, chạy test, chạy thử dạng service và đính kèm file zip.
Push tag `v*` (VD `v1.0.0`) để tạo Release.

Chạy thử trên máy dev (Windows/Linux): `pip install -r requirements.txt && python service.py run`.
