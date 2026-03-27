#!/usr/bin/env python3
"""
SMPCTool Python — PC Version v3.0
Marvel's Spider-Man Remastered / Spider-Man 2 PC asset tool.
Pure Python 3, zero dependencies.

อ้างอิงจาก:
  - Phew/SMPCTool-src  (C# original PC tool + AssetHashes.txt)
  - team-waldo/InsomniacArchive  (format spec + localization + patch logic)
  - Binary analysis ของไฟล์เกมจริง (toc, dag, patch.archive)

ข้อมูล format PC ที่ยืนยันแล้ว:
  TOC        : magic 0xAF12AF77, single zlib via decompressobj, DAT1 header offset 16
  Archive    : AssetId(4)+rawsize(4)+pad(0x1C)+DAT1  หรือ  raw binary ไม่มี header
  Archive    : stride 72 bytes/entry, flag=2 = patch archive
  Localization: KeyOffsetSection(0xa4ea55b2) → KeyDataSection(0x4d73cebd)
                TranslationOffsetSection(0xf80deeb4) → TranslationDataSection(0x70a382b8)
  Patch      : สร้าง patch.archive ใหม่ + update TOC offsets (ไม่ต้อง repack ทั้ง archive)
"""

import argparse
import csv as csv_mod
import os
import struct
import sys
import zlib
from collections import defaultdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TOC_MAGIC       = 0xAF12AF77
DAT1_MAGIC      = 0x44415431   # b'DAT1'
PC_ASSET_MAGIC  = 0x122BB0AB   # PC archive wrapper (LE)
PS4_ASSET_MAGIC = 0xBA20AFB5   # PS4 asset wrapper (LE)
PC_HEADER_SIZE  = 0x24         # 36 bytes: AssetId(4)+rawsize(4)+pad(28)
ARCH_STRIDE     = 72           # bytes per ArchiveFileEntry
DAG_MAGIC       = 0x891F77AF
DAG_STR_OFFSET  = 88

# TOC section IDs (from TocFile.cs / binary analysis)
SEC_ARCH_FILES  = 0x398abff0   # ArchiveFileEntry[]
SEC_NAME_HASH   = 0x506d7b8a   # ulong[] AssetIDs
SEC_FILE_CHUNK  = 0x65bcf461   # FileChunkDataEntry[] = SizeEntries
SEC_KEY_ASSET   = 0x6d921d7b   # ulong[] KeyAssets
SEC_CHUNK_INFO  = 0xdcd720b5   # ChunkInfoEntry[] = OffsetEntries
SEC_SPAN        = 0xede8ada9   # SpanEntry[] = ArchiveTOC

# Localization section IDs (from LocalizationFile.cs)
LOC_KEY_DATA    = 0x4d73cebd   # key string blob
LOC_KEY_OFF     = 0xa4ea55b2   # int[] offsets into key blob
LOC_VAL_DATA    = 0x70a382b8   # value string blob (UTF-8)
LOC_VAL_OFF     = 0xf80deeb4   # int[] offsets into value blob
LOC_COUNT       = 0xd540a903   # int  key count

LOC_SIGNATURE   = "Localization Built File"

# New patch archive entry fields (from ArchiveDirectory.SaveArchives())
PATCH_FLAG  = 0x0002
PATCH_UNK04 = 0xCCCC
PATCH_UNK06 = 0x0001

# ---------------------------------------------------------------------------
# CRC-64 (Insomniac) — Crc64.HashPath from InsomniacArchive/Hash/Crc64.cs
# ---------------------------------------------------------------------------
def _build_crc64():
    POLY = 0xAD93D23594C935A9
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ POLY if c & 1 else c >> 1
        t.append(c & 0xFFFFFFFFFFFFFFFF)
    return t

_CRC64 = _build_crc64()

def compute_hash(path):
    norm, prev = [], False
    for ch in path:
        c = ord(ch)
        if 0x41 <= c <= 0x5A: c += 0x20
        if c == 0x5C: c = 0x2F
        if c == 0x2F:
            if not prev: norm.append(c)
            prev = True
        else:
            norm.append(c); prev = False
    crc = 0xC96C5795D7870F42
    for b in norm:
        crc = _CRC64[(crc ^ b) & 0xFF] ^ (crc >> 8)
        crc &= 0xFFFFFFFFFFFFFFFF
    return ((crc >> 2) | 0x8000000000000000) & 0xFFFFFFFFFFFFFFFF

# ---------------------------------------------------------------------------
# TOC compression
# ---------------------------------------------------------------------------
def _toc_decompress(raw):
    if struct.unpack_from('>I', raw, 0)[0] != TOC_MAGIC:
        raise ValueError(f"Bad TOC magic: {struct.unpack_from('>I',raw,0)[0]:#010x}")
    expected = struct.unpack_from('<I', raw, 4)[0]
    dec = zlib.decompressobj().decompress(raw[8:])
    return dec[:expected] if len(dec) != expected else dec

def _toc_compress(dec):
    return struct.pack('>I', TOC_MAGIC) + struct.pack('<I', len(dec)) + zlib.compress(dec, 9)

# ---------------------------------------------------------------------------
# TOC parser — using section IDs from TocFile.cs
# ---------------------------------------------------------------------------
class AssetEntry:
    __slots__ = ('asset_id','filename','file_size','archive_index','archive_offset',
                 '_ai_toc','_ao_toc','_sz_toc','_toc_idx')
    def __init__(self):
        self.asset_id = self.file_size = self.archive_index = 0
        self.archive_offset = self._ai_toc = self._ao_toc = self._sz_toc = self._toc_idx = 0
        self.filename = ''
    def __repr__(self):
        return f'<Asset {self.asset_id:#018x} {self.filename!r} arch={self.archive_index} off={self.archive_offset:#x} sz={self.file_size}>'


class TOC:
    def __init__(self):
        self.archives = []    # list[str]
        self.assets   = []    # list[AssetEntry]
        self.dec_data = b''
        # Raw section data for in-place patching
        self._secs = {}       # hash -> (offset, size) in dec_data

    @classmethod
    def load(cls, path, hashdb=None):
        with open(path, 'rb') as f: raw = f.read()
        toc = cls()
        toc.dec_data = _toc_decompress(raw)
        toc._build(hashdb or {})
        return toc

    def _build(self, hashdb):
        dec = self.dec_data
        if struct.unpack_from('<I', dec, 0)[0] != DAT1_MAGIC:
            raise ValueError("Bad DAT1 magic in TOC")
        nsec = struct.unpack_from('<I', dec, 12)[0]

        self._secs = {}
        for i in range(nsec):
            base = 16 + i*12
            h = struct.unpack_from('<I', dec, base)[0]
            o = struct.unpack_from('<I', dec, base+4)[0]
            s = struct.unpack_from('<I', dec, base+8)[0]
            self._secs[h] = (o, s)

        arch_off, arch_sz  = self._secs[SEC_ARCH_FILES]
        asid_off, _        = self._secs[SEC_NAME_HASH]
        size_off, size_sz  = self._secs[SEC_FILE_CHUNK]
        oe_off,   _        = self._secs[SEC_CHUNK_INFO]

        # Archives
        n_arch = arch_sz // ARCH_STRIDE
        self.archives = []
        for i in range(n_arch):
            e = dec[arch_off + i*ARCH_STRIDE : arch_off + (i+1)*ARCH_STRIDE]
            self.archives.append(e[8:].split(b'\x00')[0].decode('ascii','replace'))

        # Assets
        n_assets = size_sz // 12
        self.assets = []
        for i in range(n_assets):
            aid = struct.unpack_from('<Q', dec, asid_off + i*8)[0]
            sp  = size_off + i*12
            chunk_count, total_size, chunk_idx = struct.unpack_from('<III', dec, sp)
            op  = oe_off + chunk_idx*8
            arch_idx = struct.unpack_from('<I', dec, op)[0]
            arch_off_ = struct.unpack_from('<I', dec, op+4)[0]

            a = AssetEntry()
            a.asset_id       = aid
            a.filename       = hashdb.get(aid, f'{aid:#018x}')
            a.file_size      = total_size
            a.archive_index  = arch_idx
            a.archive_offset = arch_off_
            a._toc_idx       = i
            a._ai_toc        = op        # ChunkInfoEntry.archiveFileNo
            a._ao_toc        = op + 4    # ChunkInfoEntry.offset
            a._sz_toc        = sp + 4    # FileChunkDataEntry.totalSize
            self.assets.append(a)

    def by_archive(self, name):
        idxs = {i for i, n in enumerate(self.archives) if n == name}
        return [a for a in self.assets if a.archive_index in idxs]

    def find_by_filename(self, filename):
        """Find all assets matching a filename (supports partial match)."""
        fl = filename.lower().replace('\\','/')
        return [a for a in self.assets if fl in a.filename.lower().replace('\\','/')]

    def find_by_id(self, asset_id):
        return [a for a in self.assets if a.asset_id == asset_id]

    def patch_asset(self, asset, new_arch_idx, new_offset, new_size):
        """Update TOC in-place for one asset."""
        buf = bytearray(self.dec_data)
        struct.pack_into('<I', buf, asset._ai_toc, new_arch_idx)
        struct.pack_into('<I', buf, asset._ao_toc, new_offset)
        struct.pack_into('<I', buf, asset._sz_toc, new_size)
        self.dec_data = bytes(buf)

    def add_archive_entry(self, name):
        """
        Append a new ArchiveFileEntry to the ArchiveFiles section.
        Returns the new archive index.
        New entry: flag=2, unk04=0xCCCC, unk06=1, name=name
        (matches ArchiveDirectory.SaveArchives() in InsomniacArchive)
        """
        arch_off, arch_sz = self._secs[SEC_ARCH_FILES]
        new_idx = arch_sz // ARCH_STRIDE  # current count = new index

        # Build new 72-byte entry
        entry = bytearray(ARCH_STRIDE)
        struct.pack_into('<H', entry, 0, PATCH_FLAG)     # flag = 2
        entry[2] = 0; entry[3] = 0
        struct.pack_into('<H', entry, 4, PATCH_UNK04)    # unk04 = 0xCCCC
        struct.pack_into('<H', entry, 6, PATCH_UNK06)    # unk06 = 1
        nb = name.encode('ascii')[:63]
        entry[8:8+len(nb)] = nb
        entry[8+len(nb)] = 0

        # Insert new entry bytes into dec_data right after current arch section
        insert_pos = arch_off + arch_sz
        buf = bytearray(self.dec_data)
        buf[insert_pos:insert_pos] = bytes(entry)

        # All offsets after insert_pos need to shift by ARCH_STRIDE
        shift = ARCH_STRIDE
        # Update section header for SEC_ARCH_FILES (size += ARCH_STRIDE)
        nsec = struct.unpack_from('<I', buf, 12)[0]
        for i in range(nsec):
            base = 16 + i*12
            h = struct.unpack_from('<I', buf, base)[0]
            o = struct.unpack_from('<I', buf, base+4)[0]
            s = struct.unpack_from('<I', buf, base+8)[0]
            if h == SEC_ARCH_FILES:
                struct.pack_into('<I', buf, base+8, s + shift)
            elif o > arch_off:  # section starts after insertion point → shift offset
                struct.pack_into('<I', buf, base+4, o + shift)

        # Update total size in DAT1 header (byte 8)
        old_size = struct.unpack_from('<I', buf, 8)[0]
        struct.pack_into('<I', buf, 8, old_size + shift)

        self.dec_data = bytes(buf)

        # Rebuild _secs cache
        nsec2 = struct.unpack_from('<I', self.dec_data, 12)[0]
        self._secs = {}
        for i in range(nsec2):
            base = 16 + i*12
            h = struct.unpack_from('<I', self.dec_data, base)[0]
            o = struct.unpack_from('<I', self.dec_data, base+4)[0]
            s = struct.unpack_from('<I', self.dec_data, base+8)[0]
            self._secs[h] = (o, s)

        # Rebuild asset offset cache (they may have shifted)
        arch_off2 = self._secs[SEC_ARCH_FILES][0]
        asid_off2 = self._secs[SEC_NAME_HASH][0]
        size_off2 = self._secs[SEC_FILE_CHUNK][0]
        oe_off2   = self._secs[SEC_CHUNK_INFO][0]
        for a in self.assets:
            sp = size_off2 + a._toc_idx*12
            _, _, chunk_idx = struct.unpack_from('<III', self.dec_data, sp)
            op = oe_off2 + chunk_idx*8
            a._ai_toc = op
            a._ao_toc = op + 4
            a._sz_toc = sp + 4

        self.archives.append(name)
        return new_idx

    def save(self, path):
        with open(path, 'wb') as f: f.write(_toc_compress(self.dec_data))


# ---------------------------------------------------------------------------
# Archive reader
# ---------------------------------------------------------------------------
class ArchiveReader:
    def __init__(self, archive_dir):
        self.archive_dir = archive_dir
        self._fh = {}

    def _open(self, name):
        if name not in self._fh:
            self._fh[name] = open(os.path.join(self.archive_dir, name), 'rb')
        return self._fh[name]

    def close_all(self):
        for f in self._fh.values():
            try: f.close()
            except: pass
        self._fh.clear()

    def read_asset(self, asset, archives):
        """Return full on-disk bytes (wrapper + payload)."""
        fh = self._open(archives[asset.archive_index])
        fh.seek(asset.archive_offset)
        data = fh.read(asset.file_size)
        if len(data) != asset.file_size:
            raise IOError(f"Short read: {len(data)}/{asset.file_size}")
        return data

    def read_asset_payload(self, asset, archives):
        """Return DAT1 payload only (strips PC wrapper if present)."""
        data = self.read_asset(asset, archives)
        if len(data) >= 4 and struct.unpack_from('<I', data, 0)[0] == PC_ASSET_MAGIC:
            return data[PC_HEADER_SIZE:]
        return data


# ---------------------------------------------------------------------------
# DAG parser
# ---------------------------------------------------------------------------
def load_dag(path):
    with open(path, 'rb') as f: raw = f.read()
    if struct.unpack_from('<I', raw, 0)[0] != DAG_MAGIC:
        raise ValueError(f"Bad DAG magic: {struct.unpack_from('<I',raw,0)[0]:#010x}")
    dec = zlib.decompressobj().decompress(raw[12:])
    pos = DAG_STR_OFFSET
    try: pos = dec.index(b'\x00', pos) + 1
    except ValueError: pass
    db = {}
    while pos < len(dec) - 3:
        if dec[pos:pos+4] == b'\x00\x00\x00\x00': break
        if dec[pos] == 0: pos += 1; continue
        if not (0x20 <= dec[pos] <= 0x7E): break
        try: end = dec.index(b'\x00', pos)
        except ValueError: break
        name = dec[pos:end].decode('ascii','replace')
        db[compute_hash(name)] = name
        pos = end + 1
    return db


# ---------------------------------------------------------------------------
# Hash DB
# ---------------------------------------------------------------------------
def load_hashdb(path):
    """Supports tab-separated (our format) and CSV (Phew/SMPCTool-src AssetHashes.txt)."""
    db = {}
    if not path or not os.path.isfile(path): return db
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n\r')
            if not line: continue
            if '\t' in line:
                hp, name = line.split('\t', 1)
                try: db[int(hp.strip(), 16)] = name
                except ValueError: pass
            elif ',' in line:
                parts = line.rsplit(',', 1)
                if len(parts) == 2:
                    try: db[int(parts[1])] = parts[0]
                    except ValueError: pass
    return db


# ---------------------------------------------------------------------------
# Localization — using section IDs from LocalizationFile.cs
# ---------------------------------------------------------------------------

def _parse_dat1_sections(data):
    """
    Parse DAT1 block. Returns (sections_dict, string_literals, data_start).
    sections_dict: {hash: (offset, size)}
    string_literals: list of embedded string literals after section headers
    """
    if struct.unpack_from('<I', data, 0)[0] != DAT1_MAGIC:
        raise ValueError("Not a DAT1 block")
    nsec = struct.unpack_from('<I', data, 12)[0]
    secs = {}
    for i in range(nsec):
        base = 16 + i*12
        h = struct.unpack_from('<I', data, base)[0]
        o = struct.unpack_from('<I', data, base+4)[0]
        s = struct.unpack_from('<I', data, base+8)[0]
        secs[h] = (o, s)

    # String literals: null-terminated strings after section headers, until empty
    pos = 16 + nsec*12
    literals = []
    while pos < len(data):
        try: end = data.index(b'\x00', pos)
        except ValueError: break
        s = data[pos:end].decode('utf-8','replace')
        pos = end + 1
        if not s: break
        literals.append(s)

    return secs, literals


def _get_dat1_from_asset(data):
    """
    Strip PC wrapper if present and return raw DAT1 bytes.
    Works for:
      - Raw DAT1 (starts with 0x44415431)
      - PC-wrapped asset (starts with 0x122BB0AB, DAT1 at offset 0x24)
      - PS4-wrapped asset (starts with 0xBA20AFB5, DAT1 at offset 4)
    """
    if len(data) < 4: raise ValueError("File too small")
    magic = struct.unpack_from('<I', data, 0)[0]
    if magic == DAT1_MAGIC:
        return data, None          # raw DAT1, no wrapper
    elif magic == PC_ASSET_MAGIC:
        asset_id = struct.unpack_from('<I', data, 0)[0]
        return data[PC_HEADER_SIZE:], ('pc', asset_id)
    elif magic == PS4_ASSET_MAGIC:
        return data[4:], ('ps4', None)
    else:
        raise ValueError(f"Unknown asset magic: {magic:#010x}")


def _wrap_asset(dat1, wrapper_info):
    """Re-wrap DAT1 bytes with original wrapper format."""
    if wrapper_info is None:
        return dat1  # raw DAT1
    kind, asset_id = wrapper_info
    if kind == 'pc':
        hdr = struct.pack('<I', PC_ASSET_MAGIC) + struct.pack('<I', len(dat1)) + b'\x00' * 28
        return hdr + dat1
    elif kind == 'ps4':
        return struct.pack('<I', PS4_ASSET_MAGIC) + dat1
    return dat1


def _getstr_utf8(blob, offset):
    """Read null-terminated UTF-8 string from blob at offset."""
    if offset < 0 or offset >= len(blob) or blob[offset] == 0:
        return ''
    try:
        end = blob.index(b'\x00', offset)
        return blob[offset:end].decode('utf-8', 'replace')
    except ValueError:
        return blob[offset:].decode('utf-8', 'replace')


def loc_extract_strings(data):
    """
    Extract all key→value pairs from a localization asset.
    Returns (pairs: list[(key,value)], secs, dat1, wrapper_info)
    """
    dat1, wrapper_info = _get_dat1_from_asset(data)

    # Verify signature
    secs, literals = _parse_dat1_sections(dat1)
    if not literals or literals[0] != LOC_SIGNATURE:
        raise ValueError(f"Not a localization file (signature: {literals[:1]})")

    required = [LOC_KEY_DATA, LOC_KEY_OFF, LOC_VAL_DATA, LOC_VAL_OFF, LOC_COUNT]
    missing = [f'{h:#010x}' for h in required if h not in secs]
    if missing:
        raise ValueError(f"Missing localization sections: {missing}")

    n_keys = struct.unpack_from('<I', dat1, secs[LOC_COUNT][0])[0]

    kd_off, kd_sz = secs[LOC_KEY_DATA]
    ko_off        = secs[LOC_KEY_OFF][0]
    vd_off, vd_sz = secs[LOC_VAL_DATA]
    vo_off        = secs[LOC_VAL_OFF][0]

    kd = dat1[kd_off:kd_off+kd_sz]
    vd = dat1[vd_off:vd_off+vd_sz]

    pairs = []
    for i in range(n_keys):
        k_ptr = struct.unpack_from('<i', dat1, ko_off + i*4)[0]
        v_ptr = struct.unpack_from('<i', dat1, vo_off + i*4)[0]
        key   = _getstr_utf8(kd, k_ptr)
        value = _getstr_utf8(vd, v_ptr)
        pairs.append((key, value))

    return pairs, secs, dat1, wrapper_info


def loc_rebuild_dat1(dat1_orig, secs, new_pairs):
    """
    Rebuild DAT1 with new key→value pairs.
    Only replaces LOC_VAL_DATA and LOC_VAL_OFF sections.
    All other sections (hash tables, key data, etc.) are preserved unchanged.
    """
    # Build new value blob + offsets
    new_vd = bytearray()
    new_vo = []
    orig_kd = dat1_orig[secs[LOC_KEY_DATA][0] : secs[LOC_KEY_DATA][0] + secs[LOC_KEY_DATA][1]]
    orig_ko = secs[LOC_KEY_OFF][0]
    n_keys  = struct.unpack_from('<I', dat1_orig, secs[LOC_COUNT][0])[0]

    # INVALID key (index 0) always maps to offset 0 = empty string
    new_vd.extend(b'\x00')  # empty string sentinel at offset 0

    for i, (key, value) in enumerate(new_pairs):
        if key == 'INVALID' or not value:
            new_vo.append(0)
        else:
            new_vo.append(len(new_vd))
            new_vd.extend(value.encode('utf-8') + b'\x00')

    new_vd_bytes = bytes(new_vd)
    new_vo_bytes = struct.pack(f'<{len(new_vo)}i', *new_vo)

    # Rebuild DAT1: replace only LOC_VAL_DATA and LOC_VAL_OFF, keep everything else
    nsec = struct.unpack_from('<I', dat1_orig, 12)[0]
    sec_headers = []
    for i in range(nsec):
        base = 16 + i*12
        h = struct.unpack_from('<I', dat1_orig, base)[0]
        o = struct.unpack_from('<I', dat1_orig, base+4)[0]
        s = struct.unpack_from('<I', dat1_orig, base+8)[0]
        sec_headers.append((h, o, s))

    # Collect section data (replace updated sections)
    sec_data = {}
    for h, o, s in sec_headers:
        if h == LOC_VAL_DATA:
            sec_data[h] = new_vd_bytes
        elif h == LOC_VAL_OFF:
            sec_data[h] = new_vo_bytes
        else:
            sec_data[h] = dat1_orig[o:o+s]

    # Calculate header size: DAT1 header (16) + nsec*12 + string literals
    # Find string literal area from original
    lit_start = 16 + nsec*12
    lit_end   = lit_start
    while lit_end < len(dat1_orig):
        try: end = dat1_orig.index(b'\x00', lit_end)
        except ValueError: break
        s_lit = dat1_orig[lit_end:end].decode('utf-8','replace')
        lit_end = end + 1
        if not s_lit: break

    literal_bytes = dat1_orig[lit_start:lit_end]
    hdr_size = lit_end  # everything before section data

    # Layout sections in original order (sorted by original offset)
    ordered = sorted(sec_headers, key=lambda x: x[1])
    new_section_data = b''
    new_sec_headers = []
    cur_off = hdr_size

    for h, _, _ in ordered:
        data_chunk = sec_data[h]
        new_sec_headers.append((h, cur_off, len(data_chunk)))
        new_section_data += data_chunk
        cur_off += len(data_chunk)

    # Build final DAT1
    # Header: magic, hash(parent_ref), total_size, nsec
    magic_bytes   = dat1_orig[:4]
    parent_bytes  = dat1_orig[4:8]
    nsec_bytes    = struct.pack('<I', nsec)
    total_size    = struct.pack('<I', hdr_size + len(new_section_data))

    # Section headers sorted by ID for binary search (as in DatFileBase.Save())
    sorted_hdrs = sorted(new_sec_headers, key=lambda x: x[0])
    hdr_bytes = b''.join(struct.pack('<III', h, o, s) for h, o, s in sorted_hdrs)

    new_dat1 = magic_bytes + parent_bytes + total_size + nsec_bytes + hdr_bytes + literal_bytes + new_section_data
    return new_dat1


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _load_toc(args):
    db = load_hashdb(getattr(args, 'hashdb', None))
    return TOC.load(args.toc, db)


def cmd_info(args):
    toc = _load_toc(args)
    counts = defaultdict(int)
    for a in toc.assets: counts[a.archive_index] += 1
    print(f"Archives : {len(toc.archives)}")
    for i, name in enumerate(toc.archives):
        print(f"  [{i:3d}]  {name:<28}  {counts.get(i,0):,} assets")
    total = len(toc.assets)
    named = sum(1 for a in toc.assets if not a.filename.startswith('0x'))
    print(f"\nTotal assets : {total:,}")
    print(f"Named assets : {named:,} ({100*named//total if total else 0}%)")
    print(f"Hex ID only  : {total-named:,}")


def cmd_list(args):
    toc = _load_toc(args)
    filt      = (getattr(args,'filter', None) or '').lower()
    arch_filt = (getattr(args,'archive',None) or '').lower()
    try:
        for a in toc.assets:
            arch = toc.archives[a.archive_index] if a.archive_index < len(toc.archives) else '?'
            if filt and filt not in a.filename.lower() and filt not in f'{a.asset_id:#018x}':
                continue
            if arch_filt and arch_filt not in arch.lower():
                continue
            print(f"{a.asset_id:#018x}  {arch:<22}  {a.file_size:>12,}  {a.filename}")
    except BrokenPipeError:
        pass


def cmd_extract(args):
    toc = _load_toc(args)
    skip_hex = getattr(args, 'skip_hex', False)
    os.makedirs(args.output, exist_ok=True)
    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] Archive '{args.archive}' not found or has no assets.")
        print(f"    Available: {', '.join(dict.fromkeys(toc.archives))}")
        return
    reader = ArchiveReader(args.archive_dir)
    ok = err = skipped = 0
    try:
        for a in assets:
            if skip_hex and a.filename.startswith('0x'): skipped += 1; continue
            try:
                data = reader.read_asset_payload(a, toc.archives)
                safe = a.filename.replace('/', os.sep).replace('\\', os.sep)
                out  = os.path.join(args.output, safe)
                os.makedirs(os.path.dirname(out) or args.output, exist_ok=True)
                with open(out, 'wb') as f: f.write(data)
                ok += 1
            except Exception as e:
                print(f"[ERR] {a.filename}: {e}", file=sys.stderr); err += 1
    finally:
        reader.close_all()
    print(f"Extracted {ok}  Errors {err}  Skipped {skipped}")


def cmd_repack(args):
    """Rebuild archive from original files (byte-identical if unmodified)."""
    toc = _load_toc(args)
    skip_hex = getattr(args, 'skip_hex', False)
    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] Archive '{args.archive}' not found."); return
    if skip_hex:
        assets = [a for a in assets if not a.filename.startswith('0x')]
    assets = sorted(assets, key=lambda a: a.archive_offset)
    reader = ArchiveReader(args.archive_dir)
    ok = err = 0
    print(f"Repacking {len(assets):,} assets -> {args.output_archive}")
    try:
        with open(args.output_archive, 'wb') as out:
            for a in assets:
                try:
                    data = reader.read_asset(a, toc.archives)
                    new_off = out.tell()
                    out.write(data)
                    toc.patch_asset(a, a.archive_index, new_off, len(data))
                    ok += 1
                except Exception as e:
                    print(f"[ERR] {a.filename}: {e}", file=sys.stderr); err += 1
    finally:
        reader.close_all()
    toc.save(args.output_toc)
    print(f"Written {ok}  Errors {err}\nNew TOC -> {args.output_toc}")


def cmd_patch(args):
    """
    Create a new patch.archive containing only modified/added assets.
    Adds a new ArchiveFileEntry to TOC and redirects patched assets to new archive.
    Based on ArchiveDirectory.SaveArchives() from team-waldo/InsomniacArchive.

    Usage:
      --replace <asset_name_or_id>:<file>  (repeat for multiple assets)
      --replace-dir <dir>  scan dir for files matching asset names
    """
    toc = _load_toc(args)
    reader = ArchiveReader(args.archive_dir)

    # Collect replacements: list of (AssetEntry, bytes)
    replacements = []

    # --replace name_or_id:file
    for spec in (getattr(args, 'replace', None) or []):
        if ':' not in spec:
            print(f"[!] Invalid --replace spec '{spec}' (expected name:file)"); continue
        key, filepath = spec.split(':', 1)
        if not os.path.isfile(filepath):
            print(f"[!] File not found: {filepath}"); continue
        with open(filepath, 'rb') as f: new_data = f.read()

        # Find asset by ID or name
        matched = []
        try:
            asset_id = int(key, 16)
            matched = toc.find_by_id(asset_id)
        except ValueError:
            matched = toc.find_by_filename(key)

        if not matched:
            print(f"[!] Asset not found: {key!r}"); continue
        for a in matched:
            replacements.append((a, new_data))
            print(f"  Replace: {a.filename!r} ({a.file_size:,} -> {len(new_data):,} bytes)")

    # --replace-dir dir
    src_dir = getattr(args, 'replace_dir', None)
    if src_dir and os.path.isdir(src_dir):
        for root, _, files in os.walk(src_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                rel   = os.path.relpath(fpath, src_dir).replace('\\', '/')
                matched = toc.find_by_filename(rel)
                if not matched: continue
                with open(fpath, 'rb') as f: new_data = f.read()
                for a in matched:
                    replacements.append((a, new_data))
                    print(f"  Replace: {a.filename!r}")

    if not replacements:
        print("[!] No replacements specified. Use --replace or --replace-dir.")
        return

    # Deduplicate: if same asset_id appears multiple times, keep only one per asset_id
    # (prefer patch.archive entries; otherwise take first match)
    seen_ids = {}
    deduped = []
    for a, nd in replacements:
        if a.asset_id not in seen_ids:
            seen_ids[a.asset_id] = (a, nd)
        else:
            # prefer patch.archive entries (flag=2)
            prev_a, _ = seen_ids[a.asset_id]
            if a.archive_index > prev_a.archive_index:  # later = patch archive
                seen_ids[a.asset_id] = (a, nd)
    replacements = list(seen_ids.values())

    # Add new patch archive entry to TOC
    new_arch_name = getattr(args, 'output_archive_name', None) or 'patch.archive'
    new_arch_idx = toc.add_archive_entry(new_arch_name)
    print(f"\nNew archive entry: [{new_arch_idx}] {new_arch_name!r}")

    # Write new patch archive + update TOC
    os.makedirs(os.path.dirname(os.path.abspath(args.output_archive)), exist_ok=True)
    ok = 0
    try:
        with open(args.output_archive, 'wb') as out:
            for a, new_data in replacements:
                new_off = out.tell()
                # Determine if asset needs PC wrapper:
                # If new_data is raw DAT1, wrap it. If already wrapped or raw binary, use as-is.
                new_magic = struct.unpack_from('<I', new_data, 0)[0] if len(new_data) >= 4 else 0
                if new_magic == DAT1_MAGIC:
                    # Raw DAT1 payload — wrap with PC header
                    hdr = struct.pack('<I', PC_ASSET_MAGIC) + struct.pack('<I', len(new_data)) + b'\x00'*28
                    write_data = hdr + new_data
                elif new_magic == PS4_ASSET_MAGIC:
                    # PS4 format — strip PS4 magic, wrap with PC header
                    dat1 = new_data[4:]
                    hdr = struct.pack('<I', PC_ASSET_MAGIC) + struct.pack('<I', len(dat1)) + b'\x00'*28
                    write_data = hdr + dat1
                else:
                    # Already PC-wrapped or raw binary (GFX etc) — use as-is
                    write_data = new_data
                out.write(write_data)
                toc.patch_asset(a, new_arch_idx, new_off, len(write_data))
                ok += 1
    finally:
        reader.close_all()

    toc.save(args.output_toc)
    print(f"Patched {ok} assets -> {args.output_archive}")
    print(f"New TOC -> {args.output_toc}")
    print("Copy both files to your asset_archive directory (keep backups!)")


def cmd_csv(args):
    toc = _load_toc(args)
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        w = csv_mod.writer(f)
        w.writerow(['asset_id','filename','archive','archive_offset','file_size'])
        for a in toc.assets:
            arch = toc.archives[a.archive_index] if a.archive_index < len(toc.archives) else ''
            w.writerow([f'{a.asset_id:#018x}', a.filename, arch, a.archive_offset, a.file_size])
    print(f"Exported {len(toc.assets):,} rows -> {args.output}")


def cmd_build_hashdb(args):
    print("Building hash DB from DAG...")
    db = load_dag(args.dag)
    with open(args.output, 'w', encoding='utf-8') as f:
        for h, name in sorted(db.items(), key=lambda x: x[1]):
            f.write(f'{h:#018x}\t{name}\n')
    print(f"Written {len(db):,} entries -> {args.output}")
    print("TIP: For better coverage use AssetHashes.txt from github.com/Phew/SMPCTool-src")


def cmd_hash(args):
    h = compute_hash(args.path)
    print(f'{h:#018x}  {args.path}')


def cmd_dag(args):
    print("Loading DAG...", file=sys.stderr)
    db = load_dag(args.dag)
    filt  = (getattr(args,'filter',None) or '').lower()
    names = sorted(n for n in db.values() if filt in n.lower())
    if getattr(args,'export',None):
        with open(args.export,'w',encoding='utf-8') as f:
            for n in names: f.write(n+'\n')
        print(f"Exported {len(names):,} names -> {args.export}")
    else:
        try:
            for n in names: print(n)
        except BrokenPipeError:
            pass
        print(f"\nTotal: {len(db):,}  Shown: {len(names):,}", file=sys.stderr)


def cmd_loc_export(args):
    """
    Export .localization asset to 3-column CSV: key, source, translation
    (Compatible with team-waldo/InsomniacArchive SpidermanLocalizationTool format)
    """
    with open(args.asset,'rb') as f: data = f.read()
    pairs, _, _, _ = loc_extract_strings(data)

    with open(args.output,'w',newline='',encoding='utf-8-sig') as f:
        w = csv_mod.writer(f)
        w.writerow(['key','source','translation'])
        for key, value in pairs:
            w.writerow([key, value, ''])   # translation column left blank for translator

    print(f"Exported {len(pairs):,} strings -> {args.output}")
    print("Fill in the 'translation' column, then use loc-import.")


def cmd_loc_import(args):
    """
    Import translated CSV back into .localization asset.
    CSV format: key, source, translation  (translation column replaces value)
    """
    # Load translations from CSV (column 2 = translation)
    tr = {}
    with open(args.csv,'r',newline='',encoding='utf-8-sig') as f:
        reader = csv_mod.reader(f)
        header = next(reader, None)
        for row in reader:
            if len(row) < 3: continue
            key, src, trans = row[0], row[1], row[2]
            if trans.strip():  # only import non-empty translations
                tr[key] = trans

    print(f"Loaded {len(tr):,} translations from {args.csv}")

    with open(args.asset,'rb') as f: data = f.read()
    pairs, secs, dat1, wrapper_info = loc_extract_strings(data)

    # Merge translations
    new_pairs = []
    replaced = unchanged = 0
    for key, orig_value in pairs:
        if key in tr:
            new_pairs.append((key, tr[key]))
            replaced += 1
        else:
            new_pairs.append((key, orig_value))
            unchanged += 1

    new_dat1 = loc_rebuild_dat1(dat1, secs, new_pairs)
    out_data  = _wrap_asset(new_dat1, wrapper_info)

    with open(args.output,'wb') as f: f.write(out_data)
    print(f"Replaced {replaced:,}  Unchanged {unchanged:,} -> {args.output}")
    print("Use 'patch' command to apply to game archives.")


def cmd_loc_convert(args):
    """
    Convert .localization asset between PC and PS4 format.
    PC  -> PS4 : strip PC header (0x24 bytes), prepend PS4 magic (0xBA20AFB5)
    PS4 -> PC  : strip PS4 magic (4 bytes), prepend PC header
    Auto-detect direction from file magic if --mode not specified.

    Output path rules (when --output is omitted):
      PC/DAT1  → appends .ps4   e.g.  localization_all.localization.ps4
      PS4      → appends .pc    e.g.  localization_all.localization.pc
    When --output is given, that path is used as-is.
    """
    with open(args.asset, 'rb') as f: data = f.read()
    magic = struct.unpack_from('<I', data, 0)[0]

    mode = getattr(args, 'mode', None)
    if not mode:
        if magic in (PC_ASSET_MAGIC, DAT1_MAGIC):
            mode = 'pc2ps4'
        elif magic == PS4_ASSET_MAGIC:
            mode = 'ps42pc'
        else:
            raise ValueError(f"Cannot auto-detect format (magic={magic:#010x}). Use --mode.")

    # Determine output path: explicit > auto-suffix
    out_path = getattr(args, 'output', None) or ''
    if not out_path:
        suffix = '.ps4' if mode == 'pc2ps4' else '.pc'
        out_path = args.asset + suffix

    if mode == 'pc2ps4':
        dat1, _ = _get_dat1_from_asset(data)
        out_data = struct.pack('<I', PS4_ASSET_MAGIC) + dat1
        label = 'PC -> PS4'
    elif mode == 'ps42pc':
        dat1, _ = _get_dat1_from_asset(data)
        hdr = struct.pack('<I', PC_ASSET_MAGIC) + struct.pack('<I', len(dat1)) + b'\x00' * 28
        out_data = hdr + dat1
        label = 'PS4 -> PC'
    else:
        raise ValueError(f"Unknown mode: {mode}")

    with open(out_path, 'wb') as f: f.write(out_data)
    print(f"Converted {label} ({len(data):,} -> {len(out_data):,} bytes) -> {out_path}")


def cmd_dump_archive(args):
    offset = int(args.offset, 0)
    size   = int(args.size)
    with open(args.archive,'rb') as f:
        f.seek(offset); data = f.read(size)
    print(f"Archive: {args.archive}  offset={offset:#x}  size={size}")
    if len(data) >= 4:
        ml = struct.unpack_from('<I',data,0)[0]
        mb = struct.unpack_from('>I',data,0)[0]
        suffix = ''
        if ml == PC_ASSET_MAGIC:
            suffix = f'  [PC wrapper payload={struct.unpack_from("<I",data,4)[0]:,}]'
        print(f"Magic LE={ml:#010x}  BE={mb:#010x}{suffix}")
    for i in range(0,min(len(data),512),16):
        row=data[i:i+16]
        h=' '.join(f'{b:02x}' for b in row)
        a=''.join(chr(b) if 0x20<=b<0x7f else '.' for b in row)
        print(f'  {offset+i:#010x}  {h:<48}  {a}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        prog='smpc_tool',
        description='SMPCTool Python — Spider-Man PC asset tool v3.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s info     --toc toc --hashdb AssetHashes.txt
  %(prog)s list     --toc toc --hashdb AssetHashes.txt --filter .texture
  %(prog)s extract  --toc toc --archive-dir asset_archive --archive g00s000 --output out/
  %(prog)s repack   --toc toc --archive-dir asset_archive --archive patch.archive
                    --output-archive patch_new.archive --output-toc toc_new
  %(prog)s patch    --toc toc --archive-dir asset_archive
                    --replace "localization/localization_all.localization:new_loc.localization"
                    --output-archive patch_new.archive --output-toc toc_new
  %(prog)s loc-export  --asset localization_all.localization --output strings.csv
  %(prog)s loc-import  --asset localization_all.localization --csv strings_th.csv --output loc_th.localization
  %(prog)s loc-convert --asset localization_all.localization            # -> localization_all.localization.ps4
  %(prog)s loc-convert --asset localization_all.localization.ps4        # -> localization_all.localization.ps4.pc
  %(prog)s loc-convert --asset localization_all.localization --output custom_name.localization
        """
    )

    toc_p = argparse.ArgumentParser(add_help=False)
    toc_p.add_argument('--toc',    required=True, metavar='TOC')
    toc_p.add_argument('--hashdb', metavar='HASHDB',
                       help='AssetHashes.txt (Phew/SMPCTool-src) or tab-separated hash DB')

    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('build-hashdb', help='Build hash DB from DAG  ← run first!')
    s.add_argument('--dag', required=True); s.add_argument('--output', required=True)

    s = sub.add_parser('info',  help='TOC summary', parents=[toc_p])
    s = sub.add_parser('list',  help='List assets',  parents=[toc_p])
    s.add_argument('--filter');  s.add_argument('--archive', metavar='ARCHIVE')

    s = sub.add_parser('extract', help='Extract assets from archive', parents=[toc_p])
    s.add_argument('--archive-dir', required=True); s.add_argument('--archive', required=True)
    s.add_argument('--output', required=True); s.add_argument('--skip-hex', action='store_true')

    s = sub.add_parser('repack', help='Rebuild archive + new TOC', parents=[toc_p])
    s.add_argument('--archive-dir', required=True); s.add_argument('--archive', required=True)
    s.add_argument('--output-archive', required=True); s.add_argument('--output-toc', required=True)
    s.add_argument('--skip-hex', action='store_true')

    s = sub.add_parser('patch',
        help='Create patch.archive with only modified assets (faster than full repack)',
        parents=[toc_p])
    s.add_argument('--archive-dir',   required=True, metavar='DIR')
    s.add_argument('--output-archive', required=True, metavar='OUTPUT_ARCHIVE',
                   help='Output patch.archive path')
    s.add_argument('--output-toc',    required=True, metavar='OUTPUT_TOC')
    s.add_argument('--replace', action='append', metavar='NAME:FILE',
                   help='Replace asset: asset_name_or_hex_id:replacement_file (repeat for multiple)')
    s.add_argument('--replace-dir', metavar='DIR',
                   help='Scan directory for replacement assets by filename match')
    s.add_argument('--output-archive-name', default='patch.archive',
                   help='Name for new patch archive entry in TOC (default: patch.archive)')

    s = sub.add_parser('csv', help='Export asset list to CSV', parents=[toc_p])
    s.add_argument('--output', required=True)

    s = sub.add_parser('hash', help='Compute CRC-64 hash for a path string')
    s.add_argument('path')

    s = sub.add_parser('dag',  help='Search DAG names or export list')
    s.add_argument('--dag', required=True); s.add_argument('--filter'); s.add_argument('--export')

    s = sub.add_parser('loc-export',
        help='Export .localization asset -> CSV (key, source, translation)')
    s.add_argument('--asset',  required=True, metavar='ASSET')
    s.add_argument('--output', required=True, metavar='OUTPUT')

    s = sub.add_parser('loc-import',
        help='Import translated CSV -> .localization asset')
    s.add_argument('--asset',  required=True); s.add_argument('--csv', required=True)
    s.add_argument('--output', required=True)

    s = sub.add_parser('loc-convert',
        help='Convert .localization between PC and PS4 format (auto-detect)')
    s.add_argument('--asset',    required=True)
    s.add_argument('--output',   default='',
                   help='Output path (default: <asset>.ps4 or <asset>.pc)')
    s.add_argument('--mode',     choices=['pc2ps4','ps42pc'],
                   help='Conversion direction (auto-detected if omitted)')
    s.add_argument('--asset-id', default='0', metavar='HEX',
                   help='Asset ID for ps42pc (hex, optional)')

    s = sub.add_parser('dump-archive', help='Hexdump bytes from archive (debug)')
    s.add_argument('--archive', required=True)
    s.add_argument('--offset', default='0x0'); s.add_argument('--size', default='256')

    args = p.parse_args()
    {
        'build-hashdb': cmd_build_hashdb,
        'info':         cmd_info,
        'list':         cmd_list,
        'extract':      cmd_extract,
        'repack':       cmd_repack,
        'patch':        cmd_patch,
        'csv':          cmd_csv,
        'hash':         cmd_hash,
        'dag':          cmd_dag,
        'loc-export':   cmd_loc_export,
        'loc-import':   cmd_loc_import,
        'loc-convert':  cmd_loc_convert,
        'dump-archive': cmd_dump_archive,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
