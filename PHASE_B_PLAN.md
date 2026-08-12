# Phase B — TTSCore คุยผ่าน API แทนต่อ DB ตรง (ยังไม่ได้ทำ)

**สถานะ**: ยังไม่ได้ลงมือทำเลยสักบรรทัด — เลื่อนออกจากรอบ deploy test (คุยกันไว้ว่าภายในพฤหัสนี้) เพราะประเมินแล้วเป็นงานใหญ่เกินจะทำทันในเวลาที่เหลือ ตอนนี้ TTSCore ยังใช้ Phase A (ต่อ Postgres ตรงผ่าน `psycopg`) เหมือนเดิมทุกอย่าง ใช้งานได้ปกติตราบใดที่ worker กับ DB ยังอยู่เครือข่ายเดียวกัน — **ไม่ต้องแตะอะไรตอนนี้จนกว่าจะกลับมาอ่านไฟล์นี้อีกที**

**บันทึกไว้**: 2026-08-12 ระหว่างคุยเตรียม deploy test (ต่อจากตอนสร้าง GUI ให้ worker เสร็จ — ดู `README.md`/`scripts/setup-gui-launcher.ps1`)

---

## ทำไมต้องทำ

แผน deploy ที่คุยกันไว้ตอนนี้:
- `apps/web` + `apps/api` (รวม admin) → deploy ขึ้น server ที่คนภายนอกเข้าถึงได้ (public-facing)
- Database → **ยังไม่ตัดสินใจ** ว่าจะย้ายออกจากบ้านไปด้วยหรือเปล่า
- **TTSCore + Local LLM (speaker attribution, ยังไม่ได้ทำ — ดู `../Novel Platform/Tier1_DesignCore.md` ข้อ 8) → อยู่ที่เครื่อง local เสมอ (ตอนนี้ RTX 4060, แผนจะย้ายไป RTX 3090 ทั้งคู่ในที่สุด) และต้อง "รับงานจากเซิฟเวอร์" แทนที่จะต่อ DB ตรง**

เหตุผล: ไม่ว่า DB จริงๆ จะอยู่ที่ไหนในที่สุด (บ้านหรือ cloud) เครื่องที่รัน TTSCore/LLM จะอยู่คนละที่กับ `apps/api` เสมอ (ต่างเครือข่าย ข้าม internet) — ให้ worker ต่อ Postgres ตรงข้ามเครือข่ายแบบนี้ไม่ปลอดภัย (ต้อง expose DB port ออก public หรือพึ่ง VPN/tunnel ตลอดเวลา) และผูก credential เต็มสิทธิ์ไว้กับเครื่องที่อยู่นอกการควบคุมของ server หลัก

นี่คือ "Phase B" ที่ร่างแนวคิดไว้คร่าวๆ แล้วใน `../Novel Platform/TTS_CORE_DESIGN.md` (หัวข้อ "แกนที่ 2: โครงสร้างเครือข่าย") — ไฟล์นี้ขยายรายละเอียดเพิ่มจากตอนคุยจริงจังรอบนี้

---

## ของเดิม (Phase A) ที่ต้องแทนที่

ทั้งหมดอยู่ใน `src/readji_tts/db.py`'s `JobRepository` — คุย Postgres ตรงผ่าน `psycopg`, ใช้ transaction + row-locking ของ Postgres เองเป็นตัวรับประกัน correctness:

| Method | หน้าที่ | จุดที่ยากตอนย้ายเป็น API |
|---|---|---|
| `register_worker()` / `touch_worker()` | heartbeat | ตรงไปตรงมา |
| `claim_next()` | หยิบงานถัดไปแบบ atomic (`FOR UPDATE SKIP LOCKED`) + join `work_ep`/`works` เอาเนื้อหา+ชื่อเรื่อง | **ยากที่สุด** — ต้องมั่นใจว่า worker หลายตัว (ถ้ามีวันหนึ่ง) จะไม่มีทางได้งานเดียวกันพร้อมกัน ต้องให้ apps/api ทำ atomic claim ในทรานแซกชันเดียวเหมือนเดิม ไม่ใช่แค่ SELECT แล้ว UPDATE แยกกัน (จะเกิด race ได้) |
| `prepare_block_manifest()` | reset manifest ต่อ attempt | ตรงไปตรงมา |
| `mark_block_started()` / `complete_block()` / `mark_block_failed()` | อัปเดตสถานะราย block | เรียกถี่ (ทุก block) — ต้องคิดเรื่อง network latency สะสม |
| `update_progress()` | อัปเดต progress + **ต่ออายุ lease** | เรียกถี่มาก (ทุก chunk ภายใน block ด้วย ไม่ใช่แค่ทุก block) — เป็นจุดที่กระทบ throughput มากที่สุดถ้าแต่ละครั้งมี HTTP round-trip แทรก ต้องวัด latency จริงก่อนสรุปว่าเรนเดอร์ช้าลงแค่ไหน |
| `complete()` | บันทึกผลตอน job เสร็จ **พร้อมเช็ค content-hash** ว่า episode ไม่ถูกแก้ระหว่างเรนเดอร์ (ถ้าแก้แล้ว cancel งานทิ้งแทนทับข้อมูลใหม่) | ต้อง preserve safety check นี้ให้เป๊ะ — เป็น critical logic กันบั๊กข้อมูลเสีย ไม่ใช่แค่เขียนข้อมูลเข้า DB เฉยๆ |
| `fail()` | retry with backoff (15/30/60/120/240/300s) หรือ fail ถาวรถ้าหมด attempt | ต้อง preserve backoff schedule เดิมเป๊ะ |
| `record_cleanup_failure()` | log เมื่อ cleanup R2 orphan ไม่สำเร็จ | ตรงไปตรงมา |

---

## Scope งานที่ต้องทำ (ยังไม่ได้เริ่มสักจุด)

### ฝั่ง `apps/api` (Novel Platform repo)
สร้าง internal endpoint ชุดใหม่ (auth แยกจาก user JWT ปกติ — ใช้ worker API key) ที่ทำหน้าที่แทนทุก method ในตารางข้างบน — ร่างคร่าวๆ ไว้แล้วบางส่วนใน `TTS_CORE_DESIGN.md`:
- `GET /internal/tts-jobs/pending` (claim, ต้อง atomic)
- `PATCH /internal/tts-jobs/:id` (progress/complete/fail — รวมกันหรือแยก endpoint ย่อยตามที่สะดวกตอนออกแบบจริง)
- endpoint สำหรับ heartbeat, block manifest, block status — **ยังไม่เคยร่างไว้เลย** (ตอนร่าง Phase A/B เดิมยังไม่รู้ว่าจะมี method ละเอียดขนาดนี้)

### ฝั่ง TTSCore (repo นี้)
`src/readji_tts/db.py`'s `JobRepository` ทั้งไฟล์ ต้องเขียนใหม่เป็น HTTP client (เช่น `httpx`/`requests` — ต้องเพิ่มเป็น dependency ใหม่ใน `pyproject.toml`) เรียก endpoint ชุดใหม่ข้างต้นแทน `psycopg` — `worker.py`/`schemas.py` ที่เรียกใช้ `JobRepository` ไม่ควรต้องแก้เลย ถ้าออกแบบ interface ของ `JobRepository` ให้ signature เหมือนเดิมทุกอย่าง เปลี่ยนแค่ implementation ข้างใน (worker.py เพิ่งแก้เรื่อง instance-lock/events/GUI ไปหมาดๆ — อย่าลืมว่า hook พวกนั้น (`on_activity`/`on_progress`) ต้องยังทำงานถูกต้องกับ `JobRepository` เวอร์ชันใหม่ด้วย)

### Config ใหม่ที่ต้องเพิ่ม
`TTSCore/.env` ต้องมี URL ของ apps/api + API key สำหรับ worker แทนที่ (หรือคู่กับ) `DATABASE_URL` ระหว่างช่วงเปลี่ยนผ่าน

---

## แผนอนาคต (ไกลกว่านั้น)

TTSCore + Local LLM (speaker attribution ตาม `Tier1_DesignCore.md` Phase 2 ของ Novel Platform) จะย้ายไปรันที่เครื่องเดียวกัน (RTX 3090) — ทั้งคู่จะ "รับงานจากเซิฟเวอร์" ผ่าน API เดียวกันนี้ ไม่ใช่คนละ pattern กัน

---

## ก่อนจะลงมือทำจริง (เช็คลิสต์)

1. เช็ค timeline จริงว่าเหลือกี่วัน (ตอนคุยรอบนี้ยังไม่ได้ตอบ)
2. เช็คว่า deploy target (VPS/server) พร้อมหรือยัง
3. ตัดสินใจให้ชัดว่า DB จะอยู่ที่ไหนในที่สุด — ไม่กระทบว่าต้องทำ Phase B ไหม (ต้องทำอยู่ดี) แต่กระทบรายละเอียด network/firewall setup
4. วัด latency จริงของ HTTP round-trip ระหว่างเครื่อง TTSCore ↔ apps/api ก่อนตัดสินใจ design การเรียก `update_progress()`/`mark_block_*` (เรียกถี่มาก) — อาจต้อง batch หรือลดความถี่การรายงานถ้า latency สูงเกินไป
5. ถ้า deadline กระชั้นเกินจะทำเต็มรูปแบบ พิจารณาทางลัดชั่วคราวที่ยังปลอดภัย (เช่น SSH tunnel ให้ worker ยัง connect `localhost:5432` เหมือนเดิมได้โดยไม่ expose DB port ออก public) ระหว่างรอเวลาทำ Phase B แบบเต็ม — **อย่า expose Postgres port ออก internet ตรงๆ เด็ดขาดไม่ว่าจะรีบแค่ไหน**
