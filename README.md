# PingDiagnose – Giám sát kết nối IP (Windows Service)

Phần mềm chạy dạng **Windows Service**, định kỳ ping các địa chỉ IP, cảnh báo mất kết nối
qua trình duyệt, có dashboard và báo cáo thống kê. Viết bằng Python (Flask + SQLite),
đóng gói sẵn thành `PingDiagnose.exe`.

## Tính năng

| # | Chức năng | Chi tiết |
|---|-----------|----------|
| 1 | Khai báo IP giám sát | Thêm / sửa / xoá / tạm dừng, nhập hàng loạt (`IP, Tên, Mô tả` mỗi dòng), nút "Ping thử" |
| 2 | Ping định kỳ | Mặc định **3 phút** một lần, **2 gói ping** mỗi địa chỉ (ping song song, dùng ICMP API của Windows) |
| 3 | Cảnh báo | Không phản hồi **3 chu kỳ liên tiếp (9 phút)** → thông báo ở góc màn hình Windows + âm thanh + banner đỏ. **Không cần Internet** khi giữ tab mở; có Internet thì nhận cả khi đã đóng tab (Web Push). Có thông báo khi phục hồi |
| 4 | Dashboard | Số địa chỉ hoạt động / lỗi / mất kết nối, tỷ lệ kết nối & tỷ lệ gói nhận 24h, biểu đồ theo giờ, tỷ lệ 24h/7 ngày từng IP |
| 5 | Báo cáo | Chọn IP + khoảng ngày, nhóm theo giờ/ngày: biểu đồ, bảng thống kê từng IP (tỷ lệ kết nối, gói nhận, RTT, số lần cảnh báo, thời gian mất kết nối ước tính), danh sách sự kiện, **xuất CSV (mở bằng Excel)**, in |
| 6 | Phân quyền | **Quản trị** (full quyền) và **Chỉ xem** |

Các thông số (chu kỳ, số gói, timeout, ngưỡng cảnh báo, số ngày lưu dữ liệu) chỉnh được trong trang **Cấu hình**.

## Cài đặt

1. Tải `PingDiagnose-win64.zip` (mục **Actions → Build Windows exe → Artifacts**, hoặc **Releases**), giải nén.
2. Chuột phải `install.bat` → **Run as administrator** (hoặc `install.bat 9443` để dùng cổng khác, mặc định **8443**, HTTPS).
   Script sẽ:
   - chép chương trình vào `C:\Program Files\PingDiagnose`
   - tạo service **PingDiagnose** (tự khởi động cùng Windows, tự khởi động lại khi lỗi)
   - tạo CA nội bộ + chứng chỉ HTTPS cho tên máy và mọi IP của máy chủ, cài CA lên máy chủ
   - mở cổng trên Windows Firewall
   - khởi động service và mở trình duyệt
3. Truy cập `https://<IP-máy-chủ>:8443`, đăng nhập **admin / admin**. Hệ thống bắt buộc đổi mật khẩu ở lần đăng nhập đầu.
4. Vào **Tài khoản** để tạo thêm tài khoản quản trị hoặc chỉ xem.

**Gỡ cài đặt:** chạy `uninstall.bat` (quyền admin). Script dừng & xoá service, xoá rule firewall, xoá thư mục chương trình,
và hỏi có xoá dữ liệu hay không.

## Dữ liệu & log

| Thứ | Vị trí |
|-----|--------|
| Cơ sở dữ liệu (SQLite) | `C:\ProgramData\PingDiagnose\pingdiagnose.db` |
| Cấu hình cổng/HTTPS/proxy | `C:\ProgramData\PingDiagnose\config.json` |
| CA, chứng chỉ HTTPS, khoá VAPID | `ca.crt`, `server.crt`, `vapid.pem` trong cùng thư mục |
| Log | `C:\ProgramData\PingDiagnose\logs\pingdiagnose.log` |

Sao lưu: dừng service rồi copy thư mục `C:\ProgramData\PingDiagnose`.

## Lệnh dòng lệnh

```bat
PingDiagnose.exe run                       :: chạy trực tiếp trong console (không cần service)
PingDiagnose.exe config --port 9443        :: đổi cổng (sau đó: sc stop PingDiagnose & sc start PingDiagnose)
PingDiagnose.exe config --name ping.local  :: thêm tên miền vào chứng chỉ HTTPS
PingDiagnose.exe config --proxy http://proxy:3128  :: proxy để máy chủ gửi push
PingDiagnose.exe cert                      :: cấp lại chứng chỉ, in đường dẫn ca.crt
PingDiagnose.exe reset-admin               :: quên mật khẩu: đặt lại admin/admin
```

## Thông báo trình duyệt

Có hai kênh, chạy song song, cùng một cảnh báo không bị hiện hai lần:

| Kênh | Cần Internet | Khi nào nhận |
|---|---|---|
| **Mạng nội bộ** (SSE) | Không | Khi có ít nhất một tab PingDiagnose đang mở (thu nhỏ/ở tab khác vẫn nhận) |
| **Web Push** (giống WorkPing) | Có, cả máy chủ và máy trạm | Cả khi đã đóng tab, chỉ cần trình duyệt đang chạy |

Kênh mạng nội bộ: trình duyệt giữ một kết nối tới máy chủ, máy chủ đẩy cảnh báo ngay khi phát hiện, trang hiện thông báo Windows
qua service worker. Trình duyệt chỉ cho phép thông báo này trên **HTTPS được tin cậy** (PingDiagnose tự tạo CA nội bộ, cài 1 lần,
không cần Internet), hoặc trên `http://` nếu máy trạm được cấu hình chính sách cho phép.

**Trên mỗi máy trạm (làm 1 lần):**
1. Mở `https://IP-máy-chủ:8443` (bỏ qua cảnh báo lần đầu), bấm **Cài chứng chỉ cho máy này** ở trang đăng nhập →
   chạy file `PingDiagnose-cai-chung-chi.bat` → **Yes**.
2. Vào **Cấu hình** → tải `PingDiagnose-may-tram.reg` → mở (quyền admin) → **Yes**. File này cấu hình Edge/Chrome không cho tab
   PingDiagnose "ngủ" (Sleeping tabs / Memory Saver) để không lỡ cảnh báo. Nếu tải khi đang mở bằng `http://` thì file kèm luôn
   chính sách cho phép thông báo trên địa chỉ http đó (khi đó không cần bước 1).
3. Đóng hết cửa sổ trình duyệt, mở lại → đăng nhập → **Bật thông báo** → **Cho phép** → **Thử thông báo trên máy này**.
4. Windows: Settings → System → Notifications bật cho Edge/Chrome, tắt Do not disturb.

Có thể triển khai hàng loạt qua Group Policy: import `C:\ProgramData\PingDiagnose\ca.crt` vào Trusted Root Certification Authorities,
và đặt policy `SleepingTabsBlockedForUrls` (Edge) / `TabDiscardExceptions` (Chrome) cho địa chỉ PingDiagnose.

**Web Push (tuỳ chọn, khi có Internet):** bấm **Bật thêm thông báo qua Internet** trong Cấu hình.
**Máy chủ phải ra được Internet** (cổng 443) tới `fcm.googleapis.com`, `*.notify.windows.com`, `updates.push.services.mozilla.com`.
Qua proxy: `PingDiagnose.exe config --proxy http://proxy:port` rồi khởi động lại service. Trang **Cấu hình** (quản trị) có nút kiểm tra
kết nối và danh sách thiết bị kèm lỗi gửi gần nhất.

Dùng chứng chỉ riêng của công ty: điền `cert_file`, `key_file` trong `config.json`. Truy cập bằng tên miền nội bộ:
`PingDiagnose.exe config --name ping.congty.local` để thêm tên vào chứng chỉ.

Lỗi thường gặp:
| Hiện tượng | Cách xử lý |
|---|---|
| "Trình duyệt chặn vì chứng chỉ HTTPS chưa được tin cậy" | Cài chứng chỉ (bước 1), đóng hết trình duyệt rồi mở lại |
| "Không kết nối được dịch vụ thông báo của hãng" | Mạng máy trạm chặn Google/Microsoft push; dùng Edge hoặc mở mtalk.google.com:5228, *.notify.windows.com:443 |
| Gửi thử báo "Không kết nối được ...:443" | Máy chủ không ra Internet → mở firewall hoặc cấu hình `--proxy` |
| Bật rồi nhưng không thấy popup | Windows Settings → System → Notifications: bật cho Chrome/Edge, tắt Do not disturb |
| Không hiện nút Bật thông báo, báo "ẩn danh" | Dùng cửa sổ thường, không dùng Incognito/InPrivate |

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
