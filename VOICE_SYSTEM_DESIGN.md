# ระบบเสียง Basic/Pro TTS — โครงสร้างโฟลเดอร์ + การ resolve เสียงตัวละคร (ร่าง)

**สถานะ (ตรวจซ้ำ 2026-08-17)**: resolver แบบทดสอบแยกได้อยู่ที่ `src/readji_tts/voice_resolution.py` และรองรับชื่อหมวดแบบมี `_male`/`_female` แล้ว แต่ยัง**ไม่ต่อเข้า `worker.py`**. ฝั่ง Novel Platform ทำ rename `display_label`/`block_kind`, UI label 6 ช่อง และ migration 046 (`voice_category`/`voice_index`/`voice_shared`) แล้ว; ส่วน narrator slot 0, รายชื่อหลายชื่อใน slot เดียว และการ render สลับเสียงจริง ยังไม่ทำ
**ขอบเขต**: วิธีที่ worker ควร resolve "ใครพูด" → "ไฟล์เสียงไหน" สำหรับทั้ง Basic และ Pro tier รวมถึงโครงสร้างข้อมูล 7-slot ที่ต้องแก้ที่ฝั่งเว็บ (Novel Platform) ก่อนโค้ด resolve ฝั่งนี้จะมีข้อมูลให้ใช้งานจริง
**วันที่ร่าง**: 2026-08-16
**บริบท**: คุยสรุปกับ user ในเซสชันเดียวกับที่ทำ `Tier1_DesignCore.md` (repo `Novel Platform`) ข้อ 4 (`voice_category`/`voice_index`/`voice_shared` — schema ส่วนนั้น**ทำเสร็จและ deploy แล้วจริง** ในฐาน `tts_work_character_labels` แต่เป็นแค่การ "จองลำดับไฟล์" เท่านั้น ยังไม่มีการ resolve เป็นไฟล์จริงหรือ fallback logic ใดๆ เลย — เอกสารนี้ขยายรายละเอียดต่อจากตรงนั้น)

---

## 1. ภาพรวมสั้นๆ — Basic ทำอะไร, Pro ทำอะไร

**BasicTTS** — ระบบอ่านบรรยายเฉยๆ ไม่มีตัวละครหลายเสียง
- มีไฟล์เสียงต้นแบบตายตัว 3 ไฟล์: `assets/voices/Basic/Basic_old_male.wav` (ชายแก่), `Basic_young_male.wav` (หนุ่มน้อย), `Basic_female.wav` (คุณผู้หญิง)
- โคลนเสียงจากไฟล์พวกนี้ตรงๆ อ่านทั้งตอนด้วยเสียงเดียวตลอด
- เปลี่ยนเสียง = เอาไฟล์ใหม่ทับ path เดิม (ชื่อไฟล์คงเดิม) เพิ่มไฟล์ที่ 4 ไม่มีผลอะไร เพราะฝั่งเว็บ hardcode ไว้แค่ 3 ชื่อ (`ชายแก่`/`หนุ่มน้อย`/`คุณผู้หญิง`) เปลี่ยนจำนวนต้องแก้ฝั่งเว็บด้วยเสมอ — **ไม่ใช่ขอบเขตของเอกสารนี้ Basic ทำงานได้ครบแล้วจริง ไม่ต้องแก้อะไรเพิ่ม**

**ProTTS** — ระบบยืดหยุ่นกว่า รองรับหลายเสียงต่อเรื่อง แบ่งเก็บเป็นหลายโฟลเดอร์ เพิ่มโฟลเดอร์ใหม่ได้เรื่อยๆ ตามใจแอดมิน (ไม่ต้องแก้โค้ดฝั่งเว็บทุกครั้งที่เพิ่มเสียงใหม่ — ต่างจาก Basic) นักเขียนสั่งงานด้วยการพิมพ์ `//ชื่อ` ในเนื้อเรื่อง — **นี่คือส่วนที่เอกสารนี้ออกแบบรายละเอียด**

---

## 2. สถานะปัจจุบัน (สิ่งที่ทำเสร็จแล้วจริง vs ยังไม่มีเลย)

### ทำเสร็จแล้ว (ฝั่งเว็บ Novel Platform, 2026-08-16)
- ตาราง `tts_work_character_labels` มีคอลัมน์ `voice_category` (text), `voice_index` (int), `voice_shared` (bool) แล้ว (migration `046_tts_voice_category.sql`)
- นักเขียนพิมพ์ free-text ในช่องตั้งค่าตัวละครได้ตาม grammar: `handsome` (auto-assign ลำดับถัดไป), `handsome_3` (ระบุลำดับเอง แบบจองเดี่ยว), `handsome_3!` (ระบุลำดับเอง แบบใช้ร่วมกันได้)
- ตอนบันทึก ระบบจะ auto-assign `voice_index` ให้อัตโนมัติถ้าไม่ระบุ (จำลำดับถัดไปต่อ `p_id`+`voice_category` ไว้ ไม่ชนกันแม้ auto-assign 2 ตัวละครพร้อมกันในการบันทึกครั้งเดียว) ตัวละครเดิมที่ยังไม่เปลี่ยนหมวด จะคง `voice_index` เดิมไว้เสมอ (ไม่สุ่มใหม่ทุกครั้งที่บันทึก)
- `//ชื่อตัวละคร` ในเนื้อเรื่อง parse เป็น `tts.speaker_slot` (อ้าง `slot_no` 1-6) อยู่แล้ว

### สถานะ implementation (2026-08-17)
- Worker ใช้ `voice_plan.py` อ่าน snapshot primitive บน job Pro แล้ว resolve reference WAV ต่อ block ตาม `tts.speaker_slot`; worker ไม่ query schema เว็บเอง
- API snapshot assignment slot 0–6 ลง `tts_jobs.voice_assignments` พร้อม hash; migration 050 เพิ่ม narrator slot และ child table alias ต่อ work, migration 051 เปิดรับ alias ไทย/อังกฤษ
- Pro มี safety interlock สองชั้น: `TTS_PRO_RENDER_ENABLED=false` เป็นค่า default ทั้ง API/worker. ระหว่างเติมไฟล์ในโฟลเดอร์ **ห้ามเปิดค่านี้**; job Pro จะไม่ถูก queue และ job ที่สร้างนอกระบบจะ fail ก่อนโหลดโมเดล

---

## 3. โครงสร้างใหม่ที่ต้องมี — 7 slot (narrator สงวน 1 + ตัวละคร 6 ช่องแบบ list)

**หลักการ**: ตั้งเสียงได้ 7 "ช่องเสียง" ต่อเรื่อง แต่ความหมายไม่เท่ากัน:

- **ช่อง 0 (สงวน, มีได้อันเดียวเท่านั้น) — `//narrator`**: ไม่ใช่ตัวละคร เป็นเสียงบรรยาย/narration กลางของเรื่องนั้น (บล็อกไหนไม่ได้ระบุตัวละครเลย ใช้เสียงนี้) ค่าเริ่มต้น = `Basic_female` แก้ไขได้ว่าจะชี้ไปเสียงไหนก็ได้ (ไฟล์ใน `Basic/` หรือหมวด Pro อื่นๆ) **ชื่อ `narrator` ต้องเป็นคำสงวน ห้ามนักเขียนตั้งเป็นชื่อตัวละครซ้ำ** (แบบเดียวกับที่ `//1`-`//20` สงวนไว้คำสั่งระบบอยู่แล้วฝั่งเว็บ)
- **ช่อง 1-6 (ตัวละครทั่วไป, แต่ละช่องผูกกับ "เสียง" ไม่ใช่ "ตัวละคร")**: แต่ละช่องเก็บ **list ของชื่อตัวละคร** ได้หลายชื่อ (เช่น ช่อง 1 มีทั้ง `จินนี่` และ `ลอวเรนส์`) ทุกชื่อในลิสต์เดียวกันใช้เสียงเดียวกัน (voice_category/voice_index/voice_shared ชุดเดียวกัน) — ประโยชน์: ไม่ต้องเปลืองช่องถ้าตัวละครหลายตัวใช้เสียงเดียวกันได้ (เช่น ตัวประกอบหลายตัวที่ไม่ต้องแยกเสียงจริงจัง)

**ผลต่อ schema `tts_work_character_labels` (ฝั่ง Novel Platform — ต้องแก้ที่นั่น ไม่ใช่ TTSCore)**:
ปัจจุบัน 1 แถว = 1 `shortcut` (unique ต่อ `p_id`) ผูกกับ `slot_no` ตรงๆ ต้องแยก "นิยาม slot/เสียง" ออกจาก "รายชื่อ shortcut ที่ชี้มาที่ slot" เช่น:

```sql
-- แนวทางที่เป็นไปได้ (ยังไม่ยืนยัน ดูข้อ 6 คำถามเปิด) — เพิ่มตาราง junction แยก
CREATE TABLE tts_work_character_shortcuts (
  id BIGSERIAL PRIMARY KEY,
  label_id BIGINT NOT NULL REFERENCES tts_work_character_labels(id) ON DELETE CASCADE,
  shortcut TEXT NOT NULL CHECK (shortcut ~ '^[A-Za-z][A-Za-z0-9_-]{0,30}$'),
  UNIQUE (label_id, shortcut)
);
-- ต้องมี unique constraint แยกอีกชั้นกัน shortcut ซ้ำกันข้าม label_id คนละอันในเรื่องเดียวกัน (ยังไม่ได้ร่าง SQL เป๊ะ)
```

หรือแนวทางง่ายกว่า: เก็บเป็น `TEXT[]`/`JSONB` array คอลัมน์เดียวใน `tts_work_character_labels` แทนตารางแยก (ตัดสินใจตอนลงมือทำจริงว่าแบบไหนเหมาะกว่า — junction table ดีกว่าถ้าต้องการ index/unique check ระดับ DB, array column ง่ายกว่าถ้าจำนวน shortcut ต่อ slot ไม่เยอะ)

**`slot_no` ของ narrator**: เสนอให้ใช้ `0` (แยกจาก 1-6 ชัดเจน ไม่ชนกับ `CHECK (slot_no BETWEEN 1 AND 6)` เดิม) ต้องขยาย constraint เป็น `BETWEEN 0 AND 6` และเพิ่ม `UNIQUE (p_id)` เฉพาะแถวที่ `slot_no = 0` (กันมีเกิน 1 narrator ต่อเรื่อง) — หรือแยกเป็นคอลัมน์/ตารางต่างหากสำหรับ narrator โดยเฉพาะเลยก็ได้ (ยังไม่ตัดสินใจ)

---

## 4. โครงสร้างโฟลเดอร์เสียงฝั่ง worker (TTSCore)

```
assets/voices/
  Basic/
    Basic_old_male.wav
    Basic_young_male.wav
    Basic_female.wav
  <category>/              # เช่น handsome, extra_female — 1 โฟลเดอร์ต่อ 1 หมวด
    <category>_1.wav
    <category>_2.wav
    ...
```

**Convention ที่ตัดสินใจและ implement แล้ว (2026-08-16)**: ใช้ `<category>/<category>_<N>.wav` เท่านั้น เช่น `handsome_male/handsome_male_1.wav` และ `extra_female/extra_female_1.wav` — module จะเรียงไฟล์ตามเลข `N` แล้วใช้ `voice_index` เลือกแบบ wrap-around ตามจำนวนไฟล์ที่มีจริง

**หลักการสำคัญที่ตกลงไว้แล้ว (จาก Tier1_DesignCore.md ข้อ 4)**: backend/editor **ไม่ต้องรู้จำนวนไฟล์จริงต่อหมวด** — เก็บแค่ `voice_index` เป็นเลขต่อเนื่องเพิ่มขึ้นเรื่อยๆ ฝั่ง worker เป็นคน wrap-around เองตอน resolve (เช่น หมวดมีจริงแค่ 3 ไฟล์ แต่จองไปถึงลำดับ 5 → worker วนกลับไปใช้ไฟล์ลำดับ 2)

---

## 5. Logic การ resolve เสียง (สิ่งที่ต้องเขียนใหม่ทั้งหมดใน TTSCore)

ต่อ 1 บล็อกเนื้อหาที่ต้องเรนเดอร์ (มาจาก `tts.speaker_slot` หรือไม่มีเลยแปลว่า narrator):

```
1. หา slot ที่บล็อกนี้ควรใช้:
   - มี speaker_slot → หา slot ตัวละครที่ shortcut ตรงกับที่พิมพ์ใน //ชื่อ
   - ไม่มี speaker_slot (narration เฉยๆ) → ใช้ slot narrator (slot 0)

2. อ่าน voice_category + voice_index ของ slot นั้น
   - ถ้า voice_category ว่าง/ไม่มี (ตัวละครยังไม่ได้ตั้งเสียง) → fallback ไปอะไร? (ดูคำถามเปิดข้อ 6.3)

3. หาโฟลเดอร์ assets/voices/<voice_category>/ บนเครื่องจริง
   a. โฟลเดอร์มีอยู่จริง + มีไฟล์ ≥ 1 → resolve ไฟล์จาก voice_index
      (wrap-around ถ้า voice_index เกินจำนวนไฟล์จริงที่มี —ดูข้อ 4)
   b. โฟลเดอร์ไม่มีอยู่จริง หรือพิมพ์ผิด (เช่น "preetty_female" ไม่ตรงกับโฟลเดอร์ไหนเป๊ะๆ)
      → ตรวจเพศจากชื่อหมวด (ต้องมี convention บอกเพศในชื่อ เช่น suffix _male/_female — ดูคำถามเปิด 6.1)
      → fallback ไปโฟลเดอร์ "extra_<เพศ>" (หมวดสำรองกลางที่ควรมีอยู่เสมอ เก็บเสียงตัวประกอบทั่วไป)
   c. แม้แต่ extra_<เพศ> ก็ไม่มี หรือเดาเพศจากชื่อไม่ได้เลย
      → ยกเลิก job นี้ (fail ทั้ง job ไม่ใช่แค่ข้ามบล็อกนี้ไป — ต้องยืนยันพฤติกรรมนี้กับ user อีกที ดูคำถามเปิด 6.4)

4. narrator slot (slot 0) โดยเฉพาะ: ถ้ายังไม่เคยตั้งค่าเลย (ตัวเริ่มต้นของเรื่องใหม่)
   → resolve เป็น assets/voices/Basic/Basic_female.wav ตรงๆ (ไม่ใช่หมวด Pro)
```

---

## 6. คำถามเปิด — ยังไม่ได้ตกลงกับ user ก่อนลงมือทำจริง

1. ~~**Naming convention ของหมวดที่บอกเพศได้**~~ **ตัดสินใจแล้ว (2026-08-16)**: หมวดชื่อใดก็ resolve ได้ถ้ามีโฟลเดอร์ตรงกัน; แต่หมวดที่ต้องการ fallback เมื่อชื่อผิด/โฟลเดอร์หาย ต้องลงท้าย `_male` หรือ `_female` (ไม่สนตัวพิมพ์เล็ก/ใหญ่) เช่น `handsome_male`/`little_female`. resolver จะ fallback ได้เฉพาะ `extra_male`/`extra_female` ที่เพศตรงกัน และจะไม่ fuzzy-match ไปหมวดอื่น
2. ~~**ชื่อไฟล์ในโฟลเดอร์หมวด Pro เป๊ะๆ**~~ **ตัดสินใจแล้ว (2026-08-16)**: `<category>/<category>_<N>.wav` โดย `N` เป็นจำนวนเต็มบวก
3. ~~**ตัวละครที่ยังไม่ได้ตั้ง voice_category**~~: API ปฏิเสธการ queue Pro ด้วย `TTS_PRO_VOICE_UNASSIGNED`; ห้ามเงียบ ๆ เปลี่ยนตัวละครเป็น narrator
4. ~~**Fallback แล้วยังไม่เจอ**~~: resolver raise `RuntimeError` และ worker fail ทั้ง job อย่างชัดเจน; ไม่สร้าง silent/mixed output
5. ~~**schema 7-slot**~~: ใช้ slot 0 narrator + slot 1–6 และ junction table `tts_work_character_shortcuts` พร้อม unique ต่อ work
6. ~~**Editor alias list**~~: ใช้ comma-separated aliases ต่อ slot; backend validate และส่ง snapshot ที่ไม่ผูก schema ให้ worker

---

## 7. ไฟล์ที่จะกระทบ (เมื่อลงมือทำจริง — list ไว้เผย ยังไม่ได้แก้สักไฟล์)

### ฝั่ง TTSCore (repo นี้)
- `src/readji_tts/worker.py` — ใช้ reference ต่อ block สำหรับ job `pro`, รวม timestamp/timing ตาม WAV ที่ใช้จริง
- `src/readji_tts/voice_plan.py` — validate snapshot และ resolve reference path แบบไม่รู้ schema เว็บ
- `src/readji_tts/voice_resolution.py` — folder lookup, Basic override, wrap-around, gender fallback
- `src/readji_tts/db.py` — claim JSONB `voice_assignments` ออกมากับ job

### ฝั่ง Novel Platform (คนละ repo — ต้องทำคู่กัน ไม่ใช่ TTSCore ทำฝ่ายเดียวได้จบ)
- **ทำแล้ว**: migration 046, types, editor validation/UI สำหรับ `voice_category` / `voice_index` / `voice_shared`; input ยอมรับ `handsome_male`, `handsome_male_3`, `handsome_male_3!`
- **ยังต้องทำ**: schema 7-slot ตามข้อ 3, narrator slot 0, รายชื่อหลายชื่อในหนึ่ง slot, และส่ง assignment ที่ resolve แล้วให้ worker ต่อบล็อก

---

## 8. แผนขั้นตอนที่แนะนำ (ยังไม่ได้ตกลง แค่เสนอลำดับ)

1. ตอบคำถามเปิดข้อ 6 ให้ครบก่อน (โดยเฉพาะ naming convention เพศ + schema 7-slot) — ตัดสินใจแล้วเปลี่ยนทีหลังจะแพงกว่านี้มาก
2. แก้ schema + UI ฝั่งเว็บให้รองรับ 7-slot ก่อน (ให้มีข้อมูลจริงให้ worker อ่านได้)
3. เตรียมไฟล์เสียงจริงอย่างน้อย 1 หมวด Pro + 1 หมวด `extra_<เพศ>` (fallback bucket) เพื่อทดสอบ end-to-end ได้จริง
4. ~~เขียน resolve logic ฝั่ง worker (ข้อ 5) แยกเป็น module ทดสอบเดี่ยวได้ก่อนต่อเข้า `worker.py` จริง~~ **ทำแล้วบางส่วน (2026-08-16)**: `voice_resolution.py` ครบ default narrator, category lookup, wrap-around, fallback `extra_<gender>`, และ failure path; ยังไม่เชื่อม render loop
5. ทดสอบ end-to-end: ตัวละคร 2 ตัวใน slot เดียวกัน, narrator ไม่ได้ตั้งค่า (ใช้ default), พิมพ์หมวดผิด (เช็ค fallback), หมวดที่ไม่มีจริงเลย (เช็คว่า fail ตามที่ตั้งใจ)
