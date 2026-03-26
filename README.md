# SMPCTool Python

Python port ของ [SMPCTool](https://github.com/Phew/SMPCTool-src) สำหรับ Marvel's Spider-Man Remastered / Spider-Man 2 บน PC

Pure Python 3 ไม่ต้องติดตั้ง library เพิ่มเติม รองรับทุก platform ที่รัน Python 3 ได้

---

## คำสั่งที่รองรับ

```
positional arguments:
  {build-hashdb,info,list,extract,repack,repack-dir,csv,hash,dag,loc-export,loc-import,dump-archive}
    build-hashdb   สร้าง hash DB จากไฟล์ dag  ← รันก่อนเป็นอันดับแรก!
    info           แสดงสรุป TOC (archive ทั้งหมด + จำนวน asset)
    list           แสดงรายการ asset
    extract        แตก asset จาก archive
    repack         Rebuild archive + TOC ใหม่ (สำหรับ modding)
    repack-dir     Repack จากโฟลเดอร์ที่แตกออกมา (รองรับ lang suffix)
    csv            Export รายการ asset เป็น CSV
    hash           คำนวณ CRC-64 hash จาก path string
    dag            ค้นหาชื่อ asset ใน DAG หรือ export รายการทั้งหมด
    loc-export     Export .localization asset เป็น CSV สำหรับแปล
    loc-import     นำ CSV ที่แปลแล้วกลับเป็น .localization asset
    dump-archive   Hexdump ข้อมูลดิบจาก archive (สำหรับ debug)

options:
  --toc TOC
  --hashdb HASHDB
```

---

## เริ่มต้นใช้งาน

### 1. เตรียม hash DB (ชื่อ asset)

**วิธีที่ 1 (แนะนำ):** ดาวน์โหลด `AssetHashes.txt` จาก [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src) (ไฟล์ 48 MB)
ครอบคลุม ~212,921 asset จาก 771,677 ทั้งหมด (30%)

**วิธีที่ 2:** สร้างจาก DAG เอง (ได้ชื่อประมาณ 415,100 รายการ แต่อาจ match กับ TOC ได้น้อยกว่า)
```bash
python smpc_tool.py build-hashdb --dag dag --output hashdb.txt
```

### 2. ดู TOC summary
```bash
python smpc_tool.py info --toc toc --hashdb AssetHashes.txt
```

### 3. แสดงรายการ asset
```bash
# ดูทุก asset ใน archive
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --archive g00s000

# กรองตามชื่อ
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --filter .texture

# กรองตาม archive + ชื่อ
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --archive g00s000 --filter spider_man
```

### 4. แตก asset
```bash
# แตกทุก asset จาก archive
python smpc_tool.py extract \
  --toc toc \
  --archive-dir asset_archive \
  --archive g00s000 \
  --output out/ \
  --hashdb AssetHashes.txt

# แตกเฉพาะ asset ที่มีชื่อ (ข้าม hex ID)
python smpc_tool.py extract ... --skip-hex
```

### 5. Repack (สำหรับ modding)
```bash
# Rebuild archive จากไฟล์ต้นฉบับ
python smpc_tool.py repack \
  --toc toc \
  --archive-dir asset_archive \
  --archive patch.archive \
  --output-archive patch_new.archive \
  --output-toc toc_new \
  --hashdb AssetHashes.txt

# Repack จากโฟลเดอร์ที่แก้ไขแล้ว
# (แทนที่ด้วยไฟล์ใหม่ ส่วนที่ไม่มีในโฟลเดอร์จะใช้ไฟล์ต้นฉบับ)
python smpc_tool.py repack-dir \
  --toc toc \
  --archive-dir asset_archive \
  --src-dir out/ \
  --archive patch.archive \
  --output-archive patch_new.archive \
  --output-toc toc_new \
  --hashdb AssetHashes.txt
```

---

## ระบบแปลภาษา (Localization)

ไฟล์ `.localization` เก็บ string ทั้งหมดที่แสดงใน UI เกม (ชื่อ mission, คำอธิบาย accessibility, dialog ฯลฯ)

### Export เป็น CSV สำหรับแปล
```bash
# 1. แตก localization asset ออกมาก่อน
python smpc_tool.py extract \
  --toc toc --archive-dir asset_archive \
  --archive patch.archive --output out/ \
  --hashdb AssetHashes.txt

# 2. Export เป็น CSV
python smpc_tool.py loc-export \
  --asset "out/localization/localization_all.localization" \
  --output strings_en.csv
```

CSV จะมี 2 คอลัมน์:
| key | value |
|-----|-------|
| ABANDON_CONFIRM_BODY | Abandoning a mission will result in... |
| ABANDON_CONFIRM_HEADER | ARE YOU SURE? |
| ... | ... |

### Import CSV ที่แปลแล้ว
```bash
python smpc_tool.py loc-import \
  --asset "out/localization/localization_all.localization" \
  --csv strings_th.csv \
  --output localization_all_th.localization
```

### นำไฟล์แปลกลับเข้าเกม
```bash
# สร้าง archive ใหม่ด้วยไฟล์แปลที่แก้ไข
python smpc_tool.py repack-dir \
  --toc toc \
  --archive-dir asset_archive \
  --src-dir out/ \
  --archive patch.archive \
  --output-archive patch_translated.archive \
  --output-toc toc_translated \
  --hashdb AssetHashes.txt

# วาง patch_translated.archive และ toc_translated ลงในโฟลเดอร์ asset_archive
# (backup ไฟล์เดิมก่อน!)
```

---

## รูปแบบ Hash DB

รองรับ 2 format อัตโนมัติ:

| Format | ตัวอย่าง | ที่มา |
|--------|----------|-------|
| C# SMPCTool | `path\to\asset.model,9223384287010557067` | `AssetHashes.txt` จาก Phew/SMPCTool-src |
| Tab-separated | `0x800035f1ebdcbcec	path/to/asset.model` | สร้างด้วย `build-hashdb` |

---

## รายละเอียด Format ไฟล์ PC

| Component | PS4 | PC |
|-----------|-----|-----|
| TOC magic | `0xAF12AF77` | เหมือนกัน |
| TOC compression | Single zlib via `decompressobj` | เหมือนกัน |
| Archive entry stride | 24 bytes | **72 bytes** |
| Archive magic | `0xBA20AFB5` | **`0x122BB0AB`** |
| Archive format | Raw binary | 36-byte PC wrapper + raw DAT1 |
| Archive compression | Raw | ไม่ compress (patch); custom LZ (g-archives) |
| DAG magic | `0x891F77AF` | เหมือนกัน |
| DAG string offset | 102 | **88** |
| Asset ID scheme | CRC-64 hash | Mixed: CRC-64 (30%) + structured 0xe0 IDs (70%) |

### โครงสร้าง .localization asset (DAT1)
| Section hash | เนื้อหา |
|---|---|
| `0x4d73cebd` | String keys (ASCII, null-terminated) |
| `0x70a382b8` | Translated values (UTF-8, null-terminated) |
| `0xf80deeb4` | Value offset table (1 uint32 per key) |
| `0xd540a903` | Key count (4 bytes) |

---

## เครดิต

- **C# SMPCTool ต้นฉบับ (PC)**: [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src) — Phew
- **AssetHashes.txt**: [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src)
- **PS4 Python port ที่ใช้ต่อยอด**: [zerlkung/SMPCTool-PS4_python](https://github.com/zerlkung/SMPCTool-PS4_python) — zerlkung
- **C# PS4 fork ที่เป็นแรงบันดาลใจ**: [zerlkung/SMPCTool-PS4](https://github.com/zerlkung/SMPCTool-PS4)
