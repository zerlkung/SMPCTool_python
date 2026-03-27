# SMPCTool Python — v3.0

Python port ของ [SMPCTool](https://github.com/Phew/SMPCTool-src) สำหรับ Marvel's Spider-Man Remastered / Spider-Man 2 บน PC

Pure Python 3 ไม่ต้องติดตั้ง library เพิ่มเติม

---

## คำสั่งทั้งหมด

```
positional arguments:
  {build-hashdb,info,list,extract,repack,patch,csv,hash,dag,loc-export,loc-import,loc-convert,dump-archive}
    build-hashdb   สร้าง hash DB จากไฟล์ dag  ← รันก่อนเป็นอันดับแรก!
    info           แสดงสรุป TOC (archives + จำนวน asset)
    list           แสดงรายการ asset (กรองด้วย --filter, --archive)
    extract        แตก asset จาก archive
    repack         Rebuild archive + TOC ใหม่ทั้งหมด
    patch          สร้าง patch.archive จากไฟล์ที่แก้ไข (เร็วกว่า repack)
    csv            Export รายการ asset เป็น CSV
    hash           คำนวณ CRC-64 hash จาก path string
    dag            ค้นหา / export ชื่อ asset จาก DAG
    loc-export     Export .localization -> CSV (key, source, translation)
    loc-import     Import CSV ที่แปลแล้ว -> .localization
    loc-convert    แปลง .localization ระหว่าง PC และ PS4 format
    dump-archive   Hexdump ข้อมูลดิบจาก archive (debug)

options:
  --toc TOC
  --hashdb HASHDB
```

---

## เริ่มต้นใช้งาน

### 1. เตรียม Hash DB

ดาวน์โหลด `AssetHashes.txt` จาก [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src) (48 MB)
ครอบคลุม ~238,000 asset จาก 771,000+ ทั้งหมด

หรือสร้างเองจาก DAG:
```bash
python smpc_tool.py build-hashdb --dag dag --output hashdb.txt
```

### 2. ดูข้อมูล TOC

```bash
python smpc_tool.py info --toc toc --hashdb AssetHashes.txt
```

### 3. ค้นหา asset

```bash
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --filter localization
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --archive patch.archive
```

### 4. แตก asset

```bash
python smpc_tool.py extract \
  --toc toc --archive-dir asset_archive \
  --archive patch.archive --output out/ \
  --hashdb AssetHashes.txt
```

---

## Modding Workflow

### วิธีที่ 1: patch (แนะนำ — เร็วกว่า)

สร้าง `patch.archive` ใหม่จากไฟล์ที่แก้ไขเท่านั้น

```bash
python smpc_tool.py patch \
  --toc toc \
  --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace "localization/localization_all.localization:my_translation.localization" \
  --output-archive patch_new.archive \
  --output-toc toc_new
```

วาง `patch_new.archive` และ `toc_new` ลงในโฟลเดอร์ `asset_archive` (backup ไฟล์เดิมก่อน!)

### วิธีที่ 2: repack (rebuild ทั้ง archive)

```bash
python smpc_tool.py repack \
  --toc toc --archive-dir asset_archive \
  --archive patch.archive \
  --output-archive patch_new.archive --output-toc toc_new
```

---

## ระบบแปลภาษา (Localization)

### Export เพื่อแปล

```bash
# 1. แตกไฟล์ localization ออกมาก่อน
python smpc_tool.py extract \
  --toc toc --archive-dir asset_archive \
  --archive patch.archive --output out/ --hashdb AssetHashes.txt

# 2. Export เป็น CSV (3 คอลัมน์: key, source, translation)
python smpc_tool.py loc-export \
  --asset "out/localization/localization_all.localization" \
  --output strings.csv
```

CSV format เหมือนกับ [team-waldo/InsomniacArchive](https://github.com/team-waldo/InsomniacArchive):
| key | source | translation |
|-----|--------|-------------|
| ABANDON_CONFIRM_BODY | Abandoning a mission... | ยกเลิก... |
| ABANDON_CONFIRM_HEADER | ARE YOU SURE? | แน่ใจไหม? |

### Import คำแปล

```bash
# เติมคอลัมน์ 'translation' ใน CSV แล้ว import กลับ
python smpc_tool.py loc-import \
  --asset "out/localization/localization_all.localization" \
  --csv strings_th.csv \
  --output localization_th.localization

# Apply ลงในเกมด้วย patch
python smpc_tool.py patch \
  --toc toc --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace "localization/localization_all.localization:localization_th.localization" \
  --output-archive patch_th.archive --output-toc toc_th
```

### แปลงระหว่าง PC และ PS4

```bash
# PC extracted -> PS4 format (auto-detect)
python smpc_tool.py loc-convert \
  --asset localization_all.localization \
  --output localization_ps4.localization

# PS4 -> PC (ระบุ mode ชัดเจน)
python smpc_tool.py loc-convert \
  --asset localization_ps4.localization \
  --output localization_pc.localization \
  --mode ps42pc
```

---

## รูปแบบไฟล์ PC (เทียบกับ PS4)

| Component | PS4 | PC |
|-----------|-----|----|
| TOC magic | `0xAF12AF77` | เหมือนกัน |
| TOC compression | Single zlib via `decompressobj` | เหมือนกัน |
| Archive stride | 24 bytes | **72 bytes** |
| Asset wrapper | `0xBA20AFB5` + DAT1 | `0x122BB0AB` + size + pad(28) + DAT1 |
| patch.archive entry | — | flag=2, unk04=0xCCCC |
| DAG magic | `0x891F77AF` | เหมือนกัน |
| DAG strings | offset 102 | **offset 88** |

### Localization sections (DAT1)

| Section hash | ชื่อ | เนื้อหา |
|---|---|---|
| `0x4d73cebd` | KeyDataSection | key string blob (ASCII) |
| `0xa4ea55b2` | KeyOffsetSection | int[] offset ของแต่ละ key |
| `0x70a382b8` | TranslationDataSection | value string blob (UTF-8) |
| `0xf80deeb4` | TranslationOffsetSection | int[] offset ของแต่ละ value |
| `0xd540a903` | — | key count (4 bytes) |

---

## เครดิต

- **C# SMPCTool ต้นฉบับ (PC)**: [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src) — Phew
- **AssetHashes.txt**: [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src)
- **Format spec + Localization + Patch logic**: [team-waldo/InsomniacArchive](https://github.com/team-waldo/InsomniacArchive)
- **PS4 Python port ที่ใช้ต่อยอด**: [zerlkung/SMPCTool-PS4_python](https://github.com/zerlkung/SMPCTool-PS4_python) — zerlkung
- **C# PS4 original**: [zerlkung/SMPCTool-PS4](https://github.com/zerlkung/SMPCTool-PS4)
