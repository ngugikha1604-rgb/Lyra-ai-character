# Tổng quan logic hội thoại AIRI (không gồm API / request mạng)

Tài liệu này mô tả **cách app xử lý một cuộc trò chuyện**: input của người dùng, cách ghép thành nội dung gửi cho mô hình (system + lịch sử + context), cách xử lý luồng trả lời (stream, công cụ, hook), và **cái gì được coi là “nhớ”** giữa các lần mở app.

Phạm vi **cố tình loại trừ**: chi tiết HTTP, URL, header, hay cách gọi từng provider — chỉ nói đến chỗ “sau khi đã có luồng sự kiện từ mô hình”.

---

## 1. Các khối logic chính (vai trò từng phần)

Ứng dụng không gom hết vào một chỗ; nó chia thành vài lớp, mỗi lớp một trách nhiệm rõ:

| Khối | Vai trò ngắn gọn |
|------|------------------|
| **Orchestrator chat** | Điều phối **một lượt gửi tin**: xếp hàng, chèn context, ghép message, bắt đầu stream, thu output, ghi lại lịch sử, gọi hook. |
| **Phiên (session) chat** | Giữ **danh sách tin nhắn** theo từng phiên, chọn phiên đang active, **lưu / đọc** từ bộ nhớ máy (IndexedDB), có cơ chế “thế hệ” để hủy gửi khi reset. |
| **Context runtime** | Giữ **thông tin bổ sung theo thời gian thực** (giờ giấc, trạng thái game, tin từ plugin…) dưới dạng các “bucket” theo nguồn; có thể thay hoặc nối thêm tùy chiến lược cập nhật. |
| **Luồng hiển thị (stream UI)** | Tin trợ lý **đang được gõ dở** trên giao diện (trước khi khóa vào lịch sử). |
| **Hook vòng đời** | Các điểm neo cho phép **can thiệp** trước/sau compose, trước/sau gửi, theo từng token, khi kết stream, khi xong một lượt… |
| **Lớp stream LLM** | Nhận **chuỗi sự kiện** (đoạn chữ, gọi công cụ, kết thúc, lỗi) và gọi callback orchestrator để xử lý. |
| **AIRI Card** | **Không** là nơi người dùng gõ chữ; nó là **persona + cấu hình mặc định** (và đồng bộ một phần cài đặt module) khi bạn đổi “thẻ nhân vật”. |

---

## 2. AIRI Card: nó làm gì, **không** làm gì, và “nhập” liên quan thế nào

### 2.1 AIRI Card **không** xử lý trực tiếp ô nhập chat

- Ô chat / mic / plugin gửi vào hệ thống là **chuỗi văn bản (và đôi khi ảnh đính kèm)** — luồng đó đi vào **orchestrator chat**, không đi qua “parser” riêng của thẻ.
- **AIRI Card** cung cấp phần **định hình tính cách / hướng dẫn hệ thống** mà mô hình sẽ thấy ở đầu phiên, thông qua tin nhắn vai trò `system` trong lịch sử phiên.

### 2.2 System prompt hiển thị cho mô hình lấy từ đâu trên thẻ

Một thẻ (card) chứa nhiều trường mô tả nhân vật. Phần dùng làm “hướng dẫn hệ thống” thực tế được **ghép** theo thứ tự ưu tiên nội dung:

1. `systemPrompt` — nếu có, đây là phần chủ đạo do tác giả thẻ viết cho LLM.
2. `description` — mô tả thêm (thường là bối cảnh / ngoại hình / setting).
3. `personality` — tính cách, giọng điệu.

Ba phần được **nối bằng xuống dòng** thành một khối văn bản duy nhất. Đó là nội dung mà session sẽ đưa vào **tin `system` đầu tiên** khi tạo phiên mới (cộng thêm các quy tắc kỹ thuật nhỏ về code block / công thức toán — xem mục 4).

### 2.3 Thẻ “mặc định” khi chưa có thẻ tùy chỉnh

Lần đầu khởi tạo, nếu chưa có thẻ nào, hệ thống tạo một thẻ mặc định. **Toàn bộ nội dung bản prompt hệ thống phiên bản 2** (template i18n + hậu tố) được ghi vào trường **`description`** của thẻ (trường `systemPrompt` trên thẻ có thể để trống). Khi ghép `systemPrompt` dùng cho mô hình (mục 2.2), ba phần `systemPrompt` + `description` + `personality` vẫn được nối lại — nên persona mặc định vẫn đầy đủ dù nằm ở `description`.

### 2.4 Phần mở rộng `extensions.airi` trên thẻ — không phải “input chat”

Mỗi thẻ có thể mang thêm cấu hình AIRI riêng, ví dụ:

- **Module consciousness (chat):** snapshot `provider` + `model` mà thẻ muốn dùng khi thẻ đó active.
- **Module speech (TTS):** snapshot provider / model / voice.
- **Model hiển thị (Live2D/VRM):** id model preset hoặc model người dùng đã nhập.

**Cơ chế đồng bộ một chiều khi đổi thẻ:** khi người dùng chọn thẻ khác làm active, một watcher sẽ **ghi đè** các lựa chọn consciousness + speech + (nếu có) model sân khấu trong cài đặt global bằng giá trị đọc từ `extensions.airi` của thẻ đó (thiếu thì lấy giá trị đang có trên máy làm mặc định khi tạo thẻ mới).

Ý nghĩa thực tế:

- Đổi thẻ = **đổi persona (system)** + thường kèm **đổi cặp provider/model chat và TTS** theo thẻ.
- **Không** đổi nội dung ô nhập hiện tại; cũng **không** tự dịch hoặc sửa câu người dùng đang gõ.

### 2.5 So sánh nhanh: “nhập” ở đâu xử lý?

| Nguồn | Xử lý ở đâu (logic) |
|--------|---------------------|
| Người gõ trong ô chat | Orchestrator: hàng đợi → compose → stream → lưu phiên. |
| Ảnh đính kèm | Orchestrator: ghép thành phần nội dung đa phần (text + `image_url`) trên cùng một tin user. |
| Tin từ plugin / bridge (văn bản + cập nhật context) | Bridge đăng ký consumer; khi có sự kiện text, gọi cùng API `ingest` như chat — **prefix** có thể được thêm vào đầu chuỗi nếu override yêu cầu; context updates được đẩy vào store context **trước** khi compose. |
| Persona / mô tả nhân vật | AIRI Card → system message đầu phiên (qua session), không phải từng ký tự nhập. |

---

## 3. Phiên chat (session): cấu trúc tin nhắn và “thế hệ”

### 3.1 Một phiên chứa gì

Mỗi phiên là một danh sách tin nhắn theo thời gian. Các vai trò điển hình:

- **`system`:** hướng dẫn hệ thống + quy tắc kỹ thuật (code/math) — thường **ổn định ở đầu** phiên.
- **`user`:** người dùng (hoặc nội dung tương đương do plugin đưa vào).
- **`assistant`:** trợ lý; có thể có cấu trúc phụ (`slices`, kết quả công cụ, phân loại speech/reasoning) để UI và TTS dùng.
- **`tool`:** kết quả thực thi công cụ (khi luồng có tool calling).

### 3.2 Tạo phiên mới

Khi cần phiên mới cho một nhân vật (character):

1. Sinh id phiên mới.
2. Gắn meta: user, character, thời điểm tạo/cập nhật.
3. Khởi tạo danh sách tin bắt đầu bằng **một tin `system`**:
   - phần quy tắc render code / LaTeX (cố định),
   - phần nội dung persona lấy từ **system prompt đã ghép của thẻ đang active** (mục 2.2).
4. Ghi phiên và chỉ mục xuống bộ nhớ cục bộ (IndexedDB).

### 3.3 “Thế hệ” (generation) — vì sao cần

Mỗi phiên có một số đếm **thế hệ**. Mỗi lần người dùng **xóa / reset** nội dung phiên hoặc thao tác tương đương, thế hệ **tăng**.

Mọi tin nhắn đang xếp hàng gửi đều ghi nhận thế hệ lúc enqueue. Khi đến lượt xử lý:

- Nếu thế hệ hiện tại của phiên **khác** thế hệ lúc enqueue → tin đó **bị hủy** (không gửi nữa, promise reject với lỗi có nghĩa).

Mục đích: tránh race khi người dùng reset chat trong lúc vẫn còn tin cũ trong hàng đợi — không để một phản hồi cũ “chèn” vào phiên mới.

### 3.4 Lưu trữ lâu dài trên máy

- **Chỉ mục theo user:** map từng character → các phiên + phiên đang active.
- **Nội dung từng phiên:** meta + mảng tin nhắn.

Cả hai nằm trong IndexedDB (namespace cục bộ trên máy). Hệ quả:

- **Tắt mở app** thường **không** làm mất lịch sử.
- Chỉ mất khi người dùng xóa dữ liệu app, đổi profile, hoặc chức năng reset/xóa phiên.

---

## 4. Một lượt gửi tin: từ input đến bản “đã compose” gửi mô hình

Dưới đây là trình tự **theo thời gian** (cùng một lượt).

### 4.1 Xếp hàng (queue)

Mỗi lần gửi tạo một job trong hàng đợi tuần tự:

- Tránh hai lượt chạy song song cùng sửa một phiên.
- Cho phép hủy an toàn nhờ thế hệ phiên (mục 3.3).

### 4.2 Chuẩn bị context runtime **trước** khi ghép prompt

Ngay trước bước ghép, orchestrator **đẩy** vào store context:

- **Thời gian hiện tại** (datetime) — luôn cập nhật mỗi lượt.
- **Bối cảnh Minecraft** (nếu module liên quan đang cung cấp) — có thể không có.

Các nguồn khác (plugin, bridge, vision…) cũng đổ vào cùng store context theo thời gian; orchestrator không cần biết chi tiết từng plugin, nó chỉ lấy **ảnh chụp (snapshot)** toàn bộ bucket khi compose.

### 4.3 Khung ngữ cảnh cho hook (`ChatStreamEventContext`)

Trước khi có danh sách tin cuối cùng, hệ thống tạo một struct ngữ cảnh luồng gồm:

- Tin user “logic” (nội dung gốc người gõ / plugin gửi),
- **Snapshot toàn bộ context** lúc đó,
- `composedMessage` ban đầu rỗng (sẽ điền sau),
- Loại input (text, voice metadata…) nếu có.

Struct này đi xuyên suốt các hook để module bên ngoài có thể quan sát hoặc bổ sung hành vi (không bắt buộc cho luồng tối thiểu).

### 4.4 Hook: trước khi ghép (`before message composed`)

Cho phép các phần mở rộng can thiệp **trước** khi hệ thống ghép chuỗi cuối cùng (ví dụ: chèn ghi chú, chuẩn hóa văn bản — nếu có module đăng ký).

### 4.5 Xây nội dung tin user “vật lý”

- Nếu chỉ có chữ: nội dung user là **chuỗi** đó.
- Nếu có ảnh: nội dung user là **danh sách phần**: đoạn text + một hoặc nhiều phần ảnh (data URL).

Sau bước này, tin user được **append vào lịch sử phiên** (để UI và lần sau đều thấy).

### 4.6 Chuẩn bị mảng tin gửi mô hình (projection)

Từ lịch sử phiên, hệ thống tạo một **bản sao “đã làm sạch”** cho mô hình:

- Bỏ các trường chỉ phục vụ UI / lưu trữ cục bộ (id nội bộ, timestamp, lớp context UI…).
- Với tin assistant trong quá khứ: có thể **bỏ** các trường `slices`, `tool_results`, `categorization` — chỉ giữ phần tương thích chuẩn message API.

Mục đích: mô hình nhận **chuỗi hội thoại chuẩn**, không bị nhiễu metadata renderer.

### 4.7 Chèn context runtime vào **cùng** tin user cuối (không tách tin)

Hệ thống:

1. Lấy snapshot context.
2. Biến snapshot thành **một đoạn văn bản XML-like** (các `<module name="…">…</module>` bọc trong `<context>…</context>`).
3. **Nối thêm** đoạn đó vào **cuối** tin user **vừa thêm** (tin cuối của mảng gửi đi).

Vì sao không tạo thêm một tin `user` riêng chỉ chứa context?

- Một số nhà cung cấp **cấm hai tin `user` liên tiếp** → tách riêng dễ gây lỗi 400.
- Ghép vào cuối tin user giữ **tiền tố lịch sử** phía trước ổn định hơn cho các kỹ thuật cache prefix trên phía mô hình / proxy.

### 4.8 Hook: sau khi ghép (`after message composed`) và trước khi bắt đầu stream (`before send`)

- **Sau compose:** cho phép module đọc toàn bộ mảng tin sắp gửi (đã có context dính vào user cuối).
- **Trước send:** mốc cuối để can thiệp trước khi bắt đầu nhận stream (ví dụ logging, policy).

Sau đó mới bắt đầu nhận luồng sự kiện từ lớp stream LLM.

---

## 5. Xử lý luồng trả lời (output): từ sự kiện stream đến tin assistant trong phiên

### 5.1 Vòng lặp sự kiện stream

Trong một lượt, orchestrator lắng nghe các loại sự kiện, ví dụ:

- **Đoạn chữ (`text-delta`):** cộng dồn vào bản full text nội bộ, đồng thời đưa qua parser.
- **Gọi công cụ / kết quả công cụ / lỗi công cụ:** đưa vào hàng đợi riêng để cập nhật `slices` và `tool_results` trên UI theo thứ tự.
- **`finish`:** đánh dấu xong một pha; có thể có nhiều pha nếu còn vòng tool.
- **`lỗi`:** ném lỗi để lượt thất bại rõ ràng.

Có cờ **`waitForTools`**: nếu stream báo kết thúc nhưng thực chất còn bước công cụ tiếp theo, hệ thống **chờ** đến khi thật sự kết thúc lượt có trả lời trợ lý — tránh UI “tắt stream” quá sớm khi tool vẫn chạy.

### 5.2 Parser marker + phân loại (speech vs reasoning)

Luồng chữ không hiển thị thô mà đi qua:

- **Parser marker:** tách literal chữ và các token đặc biệt (special) nếu mô hình dùng ký hiệu riêng.
- **Bộ phân loại theo stream:** với từng đoạn literal, có thể tách phần **đọc được cho TTS** vs phần suy luận / meta — để hook TTS chỉ nhận phần “nói”.

Đồng thời, UI nhận cập nhật từng nhịp nhỏ (tin assistant đang dựng).

### 5.3 Kết thúc lượt

Khi parser kết thúc:

1. Nếu assistant có ít nhất một `slice` (thường là đã có nội dung hoặc công cụ), **append** bản assistant đã dựng vào **lịch sử phiên**.
2. Gọi các hook: kết stream, kết phản hồi assistant, hoàn thành lượt (để module phía sau như pipeline output / analytics).

Sau đó UI có thể **xóa** tin assistant “đang stream” tạm (tùy phiên có đang foreground hay không).

---

## 6. “Memory” trong AIRI nghĩa là gì (thực tế triển khai)

### 6.1 Bộ nhớ phiên (persistent) — đây là “nhớ chính”

Đây là **toàn bộ lịch sử chat đã lưu** theo user + character + phiên.

- **Khởi động lại app:** thường vẫn còn.
- **Mô hình “nhớ” trước đó nói gì:** vì lần sau **cùng một phiên** các tin cũ được đưa lại vào mảng compose (sau bước làm sạch).

Đây **không** phải trí nhớ ngữ nghĩa kiểu “trích xuất sự kiện quan trọng vào graph” — chỉ là **lưu lại đúng những gì đã chat**.

### 6.2 Bộ nhớ context runtime (ngắn hạn / theo bucket)

Store context giữ các cập nhật theo **nguồn** (datetime, minecraft, plugin…), mỗi nguồn có thể:

- **Thay toàn bộ** bucket (`ReplaceSelf`), hoặc
- **Nối thêm** (`AppendSelf`).

Có giới hạn lịch sử context (ví dụ ~400 mục gần nhất) để không phình vô hạn.

**Cách nó ảnh hưởng câu trả lời:** tại mỗi lượt, phần context hiện có được **serialize** và dính vào **cuối** tin user (mục 4.7). Nếu context không đổi giữa hai lượt, phần dính **giống nhau**; nếu đổi (ví dụ giờ mới), phần cuối user **khác** → mô hình “thấy” thế giới cập nhật.

### 6.3 Có “long-term memory thông minh” riêng không?

Trong luồng orchestrator mặc định **không** có bước kiểu:

- tóm tắt định kỳ,
- embedding + retrieval,
- graph tri thức,

được mô tả như một pipeline bắt buộc cho mọi chat.

Nếu cần hành vi “nhớ ý chính suốt tháng” độc lập với độ dài lịch sử, hiện tại phải dựa vào:

- **lịch sử dài** (giới hạn bởi context window mô hình / chính sách cắt bớt nếu có ở tầng khác), hoặc
- **module / hook tùy chỉnh** (nếu team bạn thêm) can thiệp vào các mốc hook ở mục 4.8 và 5.3.

---

## 7. Đa cửa sổ và bridge (ý niệm, không đi sâu triển khai)

- Có kênh **broadcast** để các cửa sổ cùng app chia sẻ cảm giác “đang stream” (phục vụ devtools / đa cửa).
- Có **khóa trình duyệt** (`Web Locks`) quanh một số loại input từ server để **tránh xử lý trùng** khi người dùng mở nhiều tab.
- Cửa sổ **nhận** sự kiện phản chiếu stream được thiết kế để **không ghi đè** lịch sử IndexedDB sai (tránh assistant không có user tương ứng).

Chi tiết triển khai thuộc lớp bridge; về mặt **logic chat cốt lõi**, bạn chỉ cần nhớ: **một cửa sổ leader thực hiện persist**, các cửa khác có thể chỉ phản chiếu UI.

---

## 8. Sơ đồ luồng một lượt (tóm tắt một trang)

```
[Input] → [Queue + kiểm tra thế hệ phiên]
       → [Bơm context: datetime (+ minecraft nếu có)]
       → [Hook: trước khi ghép]
       → [Tạo tin user trong phiên]
       → [Projection: làm sạch lịch sử cho mô hình]
       → [Dính context XML vào CUỐI tin user vừa thêm]
       → [Hook: sau ghép + trước stream]
       → [Stream: text-delta → parser/categorizer → UI]
       → [Stream: tool events → slices/tool_results]
       → [Kết stream → lưu assistant vào phiên]
       → [Hook: sau stream / hoàn tất lượt]
       → [Ghi IndexedDB]
```

---

## 9. Glossary nhanh

| Thuật ngữ | Ý nghĩa trong AIRI |
|-----------|-------------------|
| **Orchestrator** | Điều phối một lượt chat end-to-end. |
| **Session** | Một cuộc hội thoại có id, có danh sách tin, được persist. |
| **Generation** | Số phiên bản “nội dung phiên”; tăng khi reset để hủy gửi cũ. |
| **Context snapshot** | Ảnh chụp toàn bộ bucket context tại một thời điểm. |
| **Projection** | Bản lịch sử đã strip metadata gửi cho mô hình. |
| **AIRI Card** | Persona + snapshot cấu hình module; đổi thẻ đổi system + thường đổi provider/model. |

---

*Tài liệu này mô tả hành vi theo mã nguồn hiện tại của monorepo; nếu sau này thêm pipeline memory hay cắt bớt context tự động, nên cập nhật lại mục 4.6–6.*
