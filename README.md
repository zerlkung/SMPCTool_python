# SMPCTool Python — v3.0

เครื่องมือจัดการ asset ไฟล์ในเกม Marvel's Spider-Man Remastered / Spider-Man 2 บน PC

เขียนด้วย Python 3 ล้วน ไม่ต้องติดตั้ง library เพิ่มเติม

---

## คำสั่งทั้งหมด

```
positional arguments:
  {build-hashdb,info,list,extract,repack,patch,csv,hash,dag,loc-export,loc-import,loc-convert,dump-archive}
    build-hashdb   สร้าง hash DB จากไฟล์ dag          ← รันก่อนเป็นอันดับแรก!
    info           แสดงสรุป TOC (archives + จำนวน asset)
    list           แสดงรายการ asset
    extract        แตก asset จาก archive
    repack         Rebuild archive + TOC ใหม่ทั้งหมด
    patch          สร้าง patch.archive จากไฟล์ที่แก้ไข ← เร็วกว่า repack
    csv            Export รายการ asset ทั้งหมดเป็น CSV
    hash           คำนวณ CRC-64 hash จาก path string
    dag            ค้นหา / export ชื่อ asset จาก DAG
    loc-export     Export .localization → CSV (key, source, translation)
    loc-import     Import CSV ที่แปลแล้ว → .localization
    loc-convert    แปลง .localization ระหว่าง PC ↔ PS4 format
    dump-archive   Hexdump ข้อมูลดิบจาก archive (debug)

options:
  -h, --help     แสดง help
  --toc TOC      path ของไฟล์ toc
  --hashdb HASHDB  path ของ AssetHashes.txt หรือ hash DB
```

---

## เริ่มต้น

### 1. เตรียม Hash Database (ชื่อ asset)

ดาวน์โหลด `AssetHashes.txt` จาก [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src)
ไฟล์ขนาด 48 MB — ครอบคลุม ~238,000 จาก 771,000+ asset ทั้งหมด

หรือสร้างเองจากไฟล์เกม (ครอบคลุมน้อยกว่า):
```bash
python smpc_tool.py build-hashdb --dag dag --output hashdb.txt
```

### 2. ดูข้อมูล TOC

```bash
python smpc_tool.py info --toc toc --hashdb AssetHashes.txt
```

ตัวอย่างผลลัพธ์:
```
Archives : 49
  [  0]  g00s000                       32,939 assets
  [  1]  g00s001                       15,726 assets
  ...
  [ 46]  patch.archive                      0 assets
  [ 47]  patch.archive                      0 assets
  [ 48]  patch.archive                      3 assets

Total assets : 771,677
Named assets : 238,245 (30%)
Hex ID only  : 533,432
```

### 3. ค้นหา asset

```bash
# กรองตามชื่อ
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --filter localization

# กรองตาม archive
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --archive patch.archive
```

### 4. แตก asset

```bash
python smpc_tool.py extract \
  --toc toc \
  --archive-dir asset_archive \
  --archive patch.archive \
  --output out/ \
  --hashdb AssetHashes.txt
```

---

## Modding Workflow

### วิธีที่ 1: `patch` — แนะนำ

สร้าง `patch.archive` ใหม่จากเฉพาะไฟล์ที่แก้ไข ไม่ต้อง rebuild ทั้ง archive

```bash
python smpc_tool.py patch \
  --toc toc \
  --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace "localization/localization_all.localization:my_loc.localization" \
  --output-archive patch_mod.archive \
  --output-toc toc_mod
```

แทนที่หลายไฟล์พร้อมกัน:
```bash
python smpc_tool.py patch \
  --toc toc --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace "path/to/asset1.localization:file1.localization" \
  --replace "path/to/asset2.localization:file2.localization" \
  --output-archive patch_mod.archive \
  --output-toc toc_mod
```

หรือแทนที่จากโฟลเดอร์ที่แตกออกมา:
```bash
python smpc_tool.py patch \
  --toc toc --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace-dir out/ \
  --output-archive patch_mod.archive \
  --output-toc toc_mod
```

### วิธีที่ 2: `repack` — rebuild ทั้ง archive

```bash
python smpc_tool.py repack \
  --toc toc \
  --archive-dir asset_archive \
  --archive patch.archive \
  --output-archive patch_mod.archive \
  --output-toc toc_mod
```

### ติดตั้ง mod

1. Backup ไฟล์เดิมก่อน:
   ```
   asset_archive/toc → toc.bak
   asset_archive/patch.archive → patch.archive.bak
   ```
2. วาง `toc_mod` เป็น `toc` และ `patch_mod.archive` เป็น `patch.archive`

---

## ระบบแปลภาษา (Localization)

### Export เพื่อแปล

```bash
# แตกไฟล์ localization
python smpc_tool.py extract \
  --toc toc --archive-dir asset_archive \
  --archive patch.archive --output out/ \
  --hashdb AssetHashes.txt

# Export เป็น CSV (3 คอลัมน์)
python smpc_tool.py loc-export \
  --asset "out/localization/localization_all.localization" \
  --output strings.csv
```

CSV format (เข้ากันได้กับ [team-waldo/InsomniacArchive](https://github.com/team-waldo/InsomniacArchive)):

| key | source | translation |
|-----|--------|-------------|
| ABANDON_CONFIRM_BODY | Abandoning a mission will result in... | *(ใส่คำแปลที่นี่)* |
| ABANDON_CONFIRM_HEADER | ARE YOU SURE? | |

### Import คำแปล

```bash
python smpc_tool.py loc-import \
  --asset "out/localization/localization_all.localization" \
  --csv strings_th.csv \
  --output localization_th.localization
```

### Apply ลงเกม

```bash
python smpc_tool.py patch \
  --toc toc --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace "localization/localization_all.localization:localization_th.localization" \
  --output-archive patch_th.archive \
  --output-toc toc_th
```

### แปลง PC ↔ PS4

```bash
# PC (extracted) → PS4 format  (auto-detect)
python smpc_tool.py loc-convert \
  --asset localization_all.localization \
  --output localization_ps4.localization

# PS4 → PC format
python smpc_tool.py loc-convert \
  --asset localization_ps4.localization \
  --output localization_pc.localization \
  --mode ps42pc
```

---

## ตำแหน่ง Font ในเกม

UI font ของเกมถูกเก็บในไฟล์ **GFX (Scaleform Flash)** ใน `patch.archive`

| ข้อมูล | ค่า |
|--------|-----|
| Asset ID | `0xb1bc4746124fa7ed` |
| Format | GFX version 14 (Scaleform) |
| ชื่อ font ในไฟล์ | `Font_LatinAS3` |
| ขนาด | ~395 KB |
| ตำแหน่ง | `patch.archive` offset `0x00D171C6` |

**วิธีดู / แทนที่ font:**
```bash
# แตก font file ออกมา (ID ไม่มีชื่อ → จะได้ไฟล์ชื่อ 0xb1bc4746124fa7ed)
python smpc_tool.py extract \
  --toc toc --archive-dir asset_archive \
  --archive patch.archive --output out/

# แก้ไขด้วย Scaleform/GFX editor เพื่อเพิ่ม glyph ภาษาที่ต้องการ
# แล้ว patch กลับด้วย asset ID โดยตรง
python smpc_tool.py patch \
  --toc toc --hashdb AssetHashes.txt \
  --archive-dir asset_archive \
  --replace "0xb1bc4746124fa7ed:font_thai.gfx" \
  --output-archive patch_font.archive \
  --output-toc toc_font
```

> **หมายเหตุ:** ไฟล์ GFX เก็บ font แบบ embedded Flash/ActionScript — ต้องใช้ Scaleform GFX editor หรือ tool อื่นในการแก้ไข glyph

---

## รูปแบบไฟล์ PC (เทียบ PS4)

| Component | PS4 | PC |
|-----------|-----|----|
| TOC magic | `0xAF12AF77` | เหมือนกัน |
| TOC compression | Single zlib via `decompressobj` | เหมือนกัน |
| Archive entry stride | 24 bytes | **72 bytes** |
| Asset wrapper magic | `0xBA20AFB5` | **`0x122BB0AB`** |
| Asset header | magic(4) + DAT1 | magic(4) + size(4) + pad(28) + DAT1 |
| patch.archive flag | — | `flag=0x0002`, `unk04=0xCCCC`, `unk06=1` |
| DAG magic | `0x891F77AF` | เหมือนกัน |
| DAG string offset | byte 102 | **byte 88** |

### TOC Section IDs (จาก TocFile.cs)

| Hash | Section | เนื้อหา |
|------|---------|---------|
| `0x398abff0` | ArchiveFiles | archive descriptors (72 bytes/entry) |
| `0x506d7b8a` | NameHash | asset ID array (uint64[]) |
| `0x65bcf461` | FileChunkData | SizeEntries (chunkCount, totalSize, chunkIdx) |
| `0x6d921d7b` | KeyAssetHash | key asset IDs (uint64[]) |
| `0xdcd720b5` | ChunkInfo | OffsetEntries (archiveFileNo, offset) |
| `0xede8ada9` | Span | per-archive asset ranges |

### Localization Section IDs (จาก LocalizationFile.cs)

| Hash | Section | เนื้อหา |
|------|---------|---------|
| `0x4d73cebd` | KeyDataSection | key strings blob (ASCII) |
| `0xa4ea55b2` | KeyOffsetSection | int[] offset ต่อ key |
| `0x70a382b8` | TranslationDataSection | value strings blob (UTF-8) |
| `0xf80deeb4` | TranslationOffsetSection | int[] offset ต่อ value |
| `0xd540a903` | — | key count (uint32) |

---

## เครดิต

| ชื่อ | บทบาท |
|------|-------|
| [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src) | C# SMPCTool ต้นฉบับสำหรับ PC + AssetHashes.txt |
| [team-waldo/InsomniacArchive](https://github.com/team-waldo/InsomniacArchive) | Format spec, localization parser, patch logic |
| [zerlkung/SMPCTool-PS4_python](https://github.com/zerlkung/SMPCTool-PS4_python) | PS4 Python port ที่ใช้เป็นฐาน |
| [zerlkung/SMPCTool-PS4](https://github.com/zerlkung/SMPCTool-PS4) | C# PS4 fork |
