#!/usr/bin/env python3
"""
SMPCTool Python — PC Version v2.1
Marvel's Spider-Man Remastered / Spider-Man 2 PC asset tool.
Pure Python 3, zero dependencies.

Adapted from SMPS4Tool v2 (PS4) with format details confirmed from
binary analysis of actual PC game files (toc, dag, patch.archive)
and cross-referenced with Phew/SMPCTool-src (C# original for PC).

ข้อมูล Format PC ที่ยืนยันแล้ว:
  TOC       : Single zlib stream จาก byte 8 (ใช้ decompressobj)
  Archive   : 36-byte PC wrapper + raw DAT1 payload (ไม่ compress)
  Archive   : magic 0x122BB0AB (PC), stride 72 bytes per entry
  DAG       : magic 0x891F77AF, strings เริ่มที่ byte 88
  Loc asset : sec[2]=keys, sec[3]=values(UTF-8), sec[8]=offsets

ดู README.md สำหรับรายละเอียดเพิ่มเติม
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

TOC_MAGIC      = 0xAF12AF77
DAT1_MAGIC     = 0x44415431
PC_ASSET_MAGIC = 0x122BB0AB
PC_HEADER_SIZE = 36
ARCH_STRIDE    = 72
DAG_MAGIC      = 0x891F77AF
DAG_STR_OFFSET = 88

# Localization DAT1 section hashes (confirmed from patch.archive)
LOC_SEC_KEYS    = 0x4d73cebd  # ASCII key strings
LOC_SEC_VALUES  = 0x70a382b8  # UTF-8 translated value strings
LOC_SEC_OFFSETS = 0xf80deeb4  # uint32 per-key offset into value section
LOC_SEC_COUNT   = 0xd540a903  # 4-byte key count

# ---------------------------------------------------------------------------
# CRC-64 (Insomniac) — best-effort matching; use AssetHashes.txt for coverage
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
    norm, prev_slash = [], False
    for ch in path:
        c = ord(ch)
        if 0x41 <= c <= 0x5A: c += 0x20
        if c == 0x5C: c = 0x2F
        if c == 0x2F:
            if not prev_slash: norm.append(c)
            prev_slash = True
        else:
            norm.append(c); prev_slash = False
    crc = 0xC96C5795D7870F42
    for b in norm:
        crc = _CRC64[(crc ^ b) & 0xFF] ^ (crc >> 8)
        crc &= 0xFFFFFFFFFFFFFFFF
    return ((crc >> 2) | 0x8000000000000000) & 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# TOC
# ---------------------------------------------------------------------------

def _toc_decompress(raw):
    if struct.unpack_from('>I', raw, 0)[0] != TOC_MAGIC:
        raise ValueError(f"Bad TOC magic: {struct.unpack_from('>I',raw,0)[0]:#010x}")
    expected = struct.unpack_from('<I', raw, 4)[0]
    dec = zlib.decompressobj().decompress(raw[8:])
    return dec[:expected] if len(dec) != expected else dec


def _toc_compress(dec):
    return struct.pack('>I', TOC_MAGIC) + struct.pack('<I', len(dec)) + zlib.compress(dec, 9)


class AssetEntry:
    __slots__ = ('asset_id', 'filename', 'file_size', 'archive_index',
                 'archive_offset', '_ai_toc', '_ao_toc', '_sz_toc')
    def __init__(self):
        self.asset_id = self.file_size = self.archive_index = 0
        self.archive_offset = self._ai_toc = self._ao_toc = self._sz_toc = 0
        self.filename = ''
    def __repr__(self):
        return f'<Asset {self.asset_id:#018x} {self.filename!r} arch={self.archive_index} off={self.archive_offset:#x} sz={self.file_size}>'


class TOC:
    def __init__(self):
        self.archives = []
        self.assets   = []
        self.dec_data = b''

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
        n = struct.unpack_from('<I', dec, 12)[0]
        if n != 6:
            raise ValueError(f"Expected 6 TOC sections, got {n}")
        secs = [struct.unpack_from('<III', dec, 16 + i*12) for i in range(6)]

        # Identify sections by size relationships
        archtoc   = next((s for s in secs if s[2] == 2048), None)
        archfiles = min((s for s in secs if s[2] % ARCH_STRIDE == 0 and s[2] < 200_000), key=lambda s: s[2], default=None)
        n8s       = {s[2]//8 for s in secs if s[2] % 8 == 0 and s[2] > 100_000}
        sizeent   = next((s for s in secs if s[2] % 12 == 0 and (s[2]//12) in n8s and s[2] > 100_000), None)
        n_assets  = sizeent[2] // 12 if sizeent else 0
        large8    = [s for s in secs if s[2] == n_assets * 8 and s[2] > 100_000]
        assetids = offsetent = None
        if len(large8) == 2:
            for s in large8:
                (offsetent if struct.unpack_from('<I', dec, s[1])[0] < 256 else assetids).__class__  # dummy
            a, b = large8
            if struct.unpack_from('<I', dec, a[1])[0] < 256: offsetent, assetids = a, b
            else:                                              assetids, offsetent = a, b
        elif large8: assetids = large8[0]

        if not (archfiles and assetids and sizeent and offsetent):
            raise ValueError("Cannot identify required TOC sections")

        arch_off = archfiles[1]; asid_off = assetids[1]
        size_off = sizeent[1];   oe_off   = offsetent[1]

        self.archives = []
        for i in range(archfiles[2] // ARCH_STRIDE):
            e = dec[arch_off + i*ARCH_STRIDE : arch_off + (i+1)*ARCH_STRIDE]
            self.archives.append(e[8:].split(b'\x00')[0].decode('ascii', 'replace'))

        self.assets = []
        for i in range(n_assets):
            aid = struct.unpack_from('<Q', dec, asid_off + i*8)[0]
            sp  = size_off + i*12
            _, fsz, oe_i = struct.unpack_from('<III', dec, sp)
            op = oe_off + oe_i*8
            ai, ao = struct.unpack_from('<II', dec, op)
            a = AssetEntry()
            a.asset_id = aid; a.filename = hashdb.get(aid, f'{aid:#018x}')
            a.file_size = fsz; a.archive_index = ai; a.archive_offset = ao
            a._ai_toc = op; a._ao_toc = op + 4; a._sz_toc = sp + 4
            self.assets.append(a)

    def by_archive(self, name):
        idxs = {i for i, n in enumerate(self.archives) if n == name}
        return [a for a in self.assets if a.archive_index in idxs]

    def patch_redirect(self, asset, new_idx, new_off, new_sz):
        buf = bytearray(self.dec_data)
        struct.pack_into('<I', buf, asset._ai_toc, new_idx)
        struct.pack_into('<I', buf, asset._ao_toc, new_off)
        struct.pack_into('<I', buf, asset._sz_toc, new_sz)
        self.dec_data = bytes(buf)

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
        """Return full on-disk bytes (36-byte PC wrapper + DAT1 payload)."""
        fh = self._open(archives[asset.archive_index])
        fh.seek(asset.archive_offset)
        data = fh.read(asset.file_size)
        if len(data) != asset.file_size:
            raise IOError(f"Short read: {len(data)}/{asset.file_size} from {archives[asset.archive_index]}")
        return data

    def read_asset_payload(self, asset, archives):
        """Return DAT1 payload only (strips 36-byte PC wrapper if present)."""
        data = self.read_asset(asset, archives)
        if len(data) >= 4 and struct.unpack_from('<I', data, 0)[0] == PC_ASSET_MAGIC:
            return data[PC_HEADER_SIZE:]
        return data


# ---------------------------------------------------------------------------
# DAG parser
# ---------------------------------------------------------------------------

def load_dag(path):
    """Parse DAG → {crc64_hash: path_string}. Note: CRC-64 may not match PC IDs."""
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
        name = dec[pos:end].decode('ascii', 'replace')
        db[compute_hash(name)] = name
        pos = end + 1
    return db


# ---------------------------------------------------------------------------
# Hash DB (supports both formats)
# ---------------------------------------------------------------------------

def load_hashdb(path):
    """
    Load asset name DB.  Two formats supported:
      Tab-separated  :  0x<hex>\\t<path>         (this tool's format)
      C# SMPCTool    :  <path>,<decimal_uint64>  (AssetHashes.txt from Phew/SMPCTool-src)
    """
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
# Localization helpers
# ---------------------------------------------------------------------------

def _parse_loc_dat1(dat1):
    """
    Parse a .localization DAT1 block.
    Returns (keys, values, sec_map) where:
      keys   : list[str]   — ASCII string keys in order
      values : list[str]   — translated values in order (UTF-8)
      sec_map: dict        — {LOC_SEC_*: (offset, size)} for rebuilding
    """
    if struct.unpack_from('<I', dat1, 0)[0] != DAT1_MAGIC:
        raise ValueError("Not a DAT1 block")
    nsec = struct.unpack_from('<I', dat1, 12)[0]
    secs = {struct.unpack_from('<III', dat1, 16+i*12)[0]:
            (struct.unpack_from('<III', dat1, 16+i*12)[1],
             struct.unpack_from('<III', dat1, 16+i*12)[2])
            for i in range(nsec)}

    if LOC_SEC_KEYS not in secs or LOC_SEC_VALUES not in secs or LOC_SEC_OFFSETS not in secs:
        raise ValueError("Missing required localization sections in DAT1")

    # Keys
    k_off, k_sz = secs[LOC_SEC_KEYS]
    k_data = dat1[k_off:k_off+k_sz]
    keys = []
    pos = 0
    while pos < len(k_data):
        if k_data[pos] == 0: pos += 1; continue
        end = k_data.index(b'\x00', pos)
        keys.append(k_data[pos:end].decode('ascii', 'replace'))
        pos = end + 1

    # Values via offset table
    v_off, v_sz = secs[LOC_SEC_VALUES]
    v_data = dat1[v_off:v_off+v_sz]
    o_off, o_sz = secs[LOC_SEC_OFFSETS]
    values = []
    for i in range(len(keys)):
        if o_off + i*4 + 4 > len(dat1): values.append(''); continue
        vptr = struct.unpack_from('<I', dat1, o_off + i*4)[0]
        if vptr >= len(v_data) or v_data[vptr] == 0:
            values.append('')
        else:
            end = v_data.index(b'\x00', vptr)
            values.append(v_data[vptr:end].decode('utf-8', 'replace'))

    return keys, values, secs, dat1


def _rebuild_loc_dat1(dat1_orig, keys, new_values, secs):
    """
    Rebuild DAT1 block with new translated values.
    Reconstructs the value string section and updates offset table.
    All other sections (hash tables, etc.) are preserved as-is.
    """
    # Build new value blob
    new_v_blob = b'\x00'  # INVALID = empty at offset 0
    new_offsets = []
    for val in new_values:
        enc = val.encode('utf-8') if val else b''
        if not enc:
            new_offsets.append(0)
        else:
            new_offsets.append(len(new_v_blob))
            new_v_blob += enc + b'\x00'

    # Rebuild: replace value section and offset table in-place
    # We need to adjust section offsets if sizes change
    # Strategy: rebuild entire DAT1 with updated sections

    nsec = struct.unpack_from('<I', dat1_orig, 12)[0]
    sec_headers = []
    for i in range(nsec):
        h, o, s = struct.unpack_from('<III', dat1_orig, 16+i*12)
        sec_headers.append([h, o, s])

    # Collect original section data, replace changed sections
    orig_sections = {}
    for h, o, s in sec_headers:
        orig_sections[h] = dat1_orig[o:o+s]

    orig_sections[LOC_SEC_VALUES] = new_v_blob
    orig_sections[LOC_SEC_OFFSETS] = struct.pack(f'<{len(new_offsets)}I', *new_offsets)

    # Recalculate layout: header(16) + nsec*12 + section_data
    HDR_SIZE = 16 + nsec * 12
    cur_off = HDR_SIZE
    new_sec_headers = []
    new_section_data = b''
    for h, o, s in sec_headers:
        data = orig_sections[h]
        new_sec_headers.append((h, cur_off, len(data)))
        new_section_data += data
        cur_off += len(data)

    # Build final DAT1
    dat1_header = dat1_orig[:16]  # magic, parent_ref, size placeholder, nsec
    sec_header_bytes = b''.join(struct.pack('<III', h, o, s) for h, o, s in new_sec_headers)
    new_dat1 = dat1_header + sec_header_bytes + new_section_data
    # Update dec_size field (byte 8)
    new_dat1 = new_dat1[:8] + struct.pack('<I', len(new_dat1)) + new_dat1[12:]
    return new_dat1


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _get_toc(args):
    db = load_hashdb(getattr(args, 'hashdb', None))
    return TOC.load(args.toc, db)


def cmd_info(args):
    toc = _get_toc(args)
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
    toc = _get_toc(args)
    filt      = (getattr(args, 'filter',  None) or '').lower()
    arch_filt = (getattr(args, 'archive', None) or '').lower()
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
    toc = _get_toc(args)
    skip_hex = getattr(args, 'skip_hex', False)
    os.makedirs(args.output, exist_ok=True)
    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] No assets found for archive '{args.archive}'.")
        print(f"    Available: {', '.join(dict.fromkeys(toc.archives))}")
        return
    reader = ArchiveReader(args.archive_dir)
    ok = err = skipped = 0
    try:
        for a in assets:
            if skip_hex and a.filename.startswith('0x'):
                skipped += 1; continue
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
    """Rebuild archive from original game files (byte-identical if unmodified)."""
    toc = _get_toc(args)
    skip_hex = getattr(args, 'skip_hex', False)
    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] No assets found for archive '{args.archive}'."); return
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
                    toc.patch_redirect(a, a.archive_index, new_off, len(data))
                    ok += 1
                except Exception as e:
                    print(f"[ERR] {a.filename}: {e}", file=sys.stderr); err += 1
    finally:
        reader.close_all()
    toc.save(args.output_toc)
    print(f"Written {ok} assets  Errors {err}")
    print(f"New TOC -> {args.output_toc}")


def cmd_repack_dir(args):
    """
    Repack from a directory of extracted assets.
    Matches filenames against TOC; supports language-suffix archives (a00s034.us etc).
    """
    toc = _get_toc(args)
    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] No assets found for archive '{args.archive}'."); return
    assets = sorted(assets, key=lambda a: a.archive_offset)

    ok = err = unchanged = 0
    print(f"Repacking from dir '{args.src_dir}' -> {args.output_archive}")

    orig_reader = ArchiveReader(args.archive_dir)
    try:
        with open(args.output_archive, 'wb') as out:
            for a in assets:
                new_off = out.tell()
                # Try to find replacement file in src_dir
                safe = a.filename.replace('/', os.sep).replace('\\', os.sep)
                candidate = os.path.join(args.src_dir, safe)
                if os.path.isfile(candidate):
                    # Load replacement and re-wrap with PC header
                    with open(candidate, 'rb') as f: payload = f.read()
                    # If the file starts with DAT1 magic, re-wrap it
                    if len(payload) >= 4 and struct.unpack_from('<I', payload, 0)[0] == DAT1_MAGIC:
                        wrapper = struct.pack('<I', PC_ASSET_MAGIC) + \
                                  struct.pack('<I', len(payload)) + b'\x00' * 28
                        data = wrapper + payload
                    else:
                        data = payload  # raw asset (GFX etc), no wrapper
                    out.write(data)
                    toc.patch_redirect(a, a.archive_index, new_off, len(data))
                    ok += 1
                else:
                    # Keep original
                    try:
                        data = orig_reader.read_asset(a, toc.archives)
                        out.write(data)
                        toc.patch_redirect(a, a.archive_index, new_off, len(data))
                        unchanged += 1
                    except Exception as e:
                        print(f"[ERR] {a.filename}: {e}", file=sys.stderr); err += 1
    finally:
        orig_reader.close_all()

    toc.save(args.output_toc)
    print(f"Replaced {ok}  Unchanged {unchanged}  Errors {err}")
    print(f"New TOC -> {args.output_toc}")


def cmd_csv(args):
    toc = _get_toc(args)
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        w = csv_mod.writer(f)
        w.writerow(['asset_id', 'filename', 'archive', 'archive_offset', 'file_size'])
        for a in toc.assets:
            arch = toc.archives[a.archive_index] if a.archive_index < len(toc.archives) else ''
            w.writerow([f'{a.asset_id:#018x}', a.filename, arch,
                        a.archive_offset, a.file_size])
    print(f"Exported {len(toc.assets):,} rows -> {args.output}")


def cmd_build_hashdb(args):
    """Build hash DB from DAG file (CRC-64). Note: may not match all PC asset IDs."""
    print("Building hash DB from DAG...")
    db = load_dag(args.dag)
    with open(args.output, 'w', encoding='utf-8') as f:
        for h, name in sorted(db.items(), key=lambda x: x[1]):
            f.write(f'{h:#018x}\t{name}\n')
    print(f"Written {len(db):,} entries -> {args.output}")
    print("TIP: For better coverage, use AssetHashes.txt from github.com/Phew/SMPCTool-src")


def cmd_hash(args):
    h = compute_hash(args.path)
    print(f'{h:#018x}  {args.path}')


def cmd_dag(args):
    print("Loading DAG...", file=sys.stderr)
    db = load_dag(args.dag)
    filt  = (getattr(args, 'filter', None) or '').lower()
    names = sorted(n for n in db.values() if filt in n.lower())
    if getattr(args, 'export', None):
        with open(args.export, 'w', encoding='utf-8') as f:
            for n in names: f.write(n + '\n')
        print(f"Exported {len(names):,} names -> {args.export}")
    else:
        try:
            for n in names: print(n)
        except BrokenPipeError:
            pass
        print(f"\nTotal: {len(db):,}  Shown: {len(names):,}", file=sys.stderr)


def cmd_loc_export(args):
    """
    Export a .localization asset to CSV for translation.

    CSV columns: key, value
    The asset file should be extracted first with the extract command.
    """
    with open(args.asset, 'rb') as f: data = f.read()

    # Strip PC wrapper if present
    if len(data) >= 4 and struct.unpack_from('<I', data, 0)[0] == PC_ASSET_MAGIC:
        dat1 = data[PC_HEADER_SIZE:]
    else:
        dat1 = data

    keys, values, _, _ = _parse_loc_dat1(dat1)
    out_path = args.output

    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv_mod.writer(f)
        w.writerow(['key', 'value'])
        for k, v in zip(keys, values):
            w.writerow([k, v])

    print(f"Exported {len(keys):,} strings -> {out_path}")
    print("Edit the 'value' column, then use loc-import to apply.")


def cmd_loc_import(args):
    """
    Import translated CSV back into a .localization asset.

    Reads key→value from CSV, rebuilds the DAT1 value section,
    and writes a new asset file ready for repack.
    """
    # Load translated CSV
    new_vals_map = {}
    with open(args.csv, 'r', newline='', encoding='utf-8-sig') as f:
        for row in csv_mod.DictReader(f):
            if 'key' in row and 'value' in row:
                new_vals_map[row['key']] = row['value']

    with open(args.asset, 'rb') as f: data = f.read()
    has_wrapper = len(data) >= 4 and struct.unpack_from('<I', data, 0)[0] == PC_ASSET_MAGIC
    dat1 = data[PC_HEADER_SIZE:] if has_wrapper else data

    keys, orig_values, secs, _ = _parse_loc_dat1(dat1)

    # Build new value list: use translated value if available, else keep original
    new_values = []
    replaced = unchanged = 0
    for k, orig in zip(keys, orig_values):
        if k in new_vals_map and new_vals_map[k] != orig:
            new_values.append(new_vals_map[k])
            replaced += 1
        else:
            new_values.append(orig)
            unchanged += 1

    new_dat1 = _rebuild_loc_dat1(dat1, keys, new_values, secs)

    # Re-wrap with PC header if original had it
    if has_wrapper:
        wrapper = (struct.pack('<I', PC_ASSET_MAGIC) +
                   struct.pack('<I', len(new_dat1)) + b'\x00' * 28)
        output_data = wrapper + new_dat1
    else:
        output_data = new_dat1

    out_path = args.output
    with open(out_path, 'wb') as f: f.write(output_data)
    print(f"Replaced {replaced:,} strings  Unchanged {unchanged:,} -> {out_path}")
    print("Now use repack-dir or repack to apply to archive.")


def cmd_dump_archive(args):
    """Hexdump raw bytes at offset in an archive (for debugging)."""
    offset = int(args.offset, 0)
    size   = int(args.size)
    with open(args.archive, 'rb') as f:
        f.seek(offset); data = f.read(size)
    print(f"Archive: {args.archive}  offset={offset:#x}  size={size}")
    if len(data) >= 4:
        ml = struct.unpack_from('<I', data, 0)[0]
        mb = struct.unpack_from('>I', data, 0)[0]
        suffix = ''
        if ml == PC_ASSET_MAGIC:
            psz = struct.unpack_from('<I', data, 4)[0]
            suffix = f'  [PC wrapper payload={psz:,} total={psz+PC_HEADER_SIZE:,}]'
        print(f"Magic LE={ml:#010x}  BE={mb:#010x}{suffix}")
    for i in range(0, min(len(data), 512), 16):
        row = data[i:i+16]
        h = ' '.join(f'{b:02x}' for b in row)
        a = ''.join(chr(b) if 0x20 <= b < 0x7f else '.' for b in row)
        print(f'  {offset+i:#010x}  {h:<48}  {a}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog='smpc_tool',
        description='SMPCTool Python — Spider-Man PC asset tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python smpc_tool.py info --toc toc --hashdb AssetHashes.txt
  python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --filter .texture
  python smpc_tool.py extract --toc toc --archive-dir asset_archive --archive g00s000 --output out/
  python smpc_tool.py repack  --toc toc --archive-dir asset_archive --archive patch.archive \\
                              --output-archive patch_new.archive --output-toc toc_new
  python smpc_tool.py loc-export --asset out/localization/ui/ui.localization --output ui_en.csv
  python smpc_tool.py loc-import --asset out/localization/ui/ui.localization \\
                                 --csv ui_th.csv --output ui_th.localization
        """
    )

    # Shared args
    toc_args = argparse.ArgumentParser(add_help=False)
    toc_args.add_argument('--toc',    required=True, metavar='TOC')
    toc_args.add_argument('--hashdb', metavar='HASHDB',
                          help='AssetHashes.txt (C# format) or tab-separated hash DB')

    sub = p.add_subparsers(dest='cmd', required=True)

    # build-hashdb
    s = sub.add_parser('build-hashdb', help='Build hash DB from DAG  ← run this first!')
    s.add_argument('--dag',    required=True, metavar='DAG')
    s.add_argument('--output', required=True, metavar='OUTPUT')

    # info
    s = sub.add_parser('info', help='TOC summary', parents=[toc_args])

    # list
    s = sub.add_parser('list', help='List assets', parents=[toc_args])
    s.add_argument('--filter',  metavar='FILTER', help='Substring filter on name or ID')
    s.add_argument('--archive', metavar='ARCHIVE', help='Filter by archive name')

    # extract
    s = sub.add_parser('extract', help='Extract assets from an archive', parents=[toc_args])
    s.add_argument('--archive-dir', required=True, metavar='DIR')
    s.add_argument('--archive',     required=True, metavar='ARCHIVE')
    s.add_argument('--output',      required=True, metavar='OUTPUT')
    s.add_argument('--skip-hex',    action='store_true', help='Skip assets with no name')

    # repack
    s = sub.add_parser('repack', help='Repack archive -> new archive + new TOC', parents=[toc_args])
    s.add_argument('--archive-dir',     required=True, metavar='DIR')
    s.add_argument('--archive',         required=True, metavar='ARCHIVE')
    s.add_argument('--output-archive',  required=True, metavar='OUTPUT_ARCHIVE')
    s.add_argument('--output-toc',      required=True, metavar='OUTPUT_TOC')
    s.add_argument('--skip-hex',        action='store_true')

    # repack-dir
    s = sub.add_parser('repack-dir', help='Repack from extracted dir (supports lang suffixes)', parents=[toc_args])
    s.add_argument('--archive-dir',     required=True, metavar='DIR',     help='Original archive dir')
    s.add_argument('--src-dir',         required=True, metavar='SRC_DIR', help='Directory with replacement assets')
    s.add_argument('--archive',         required=True, metavar='ARCHIVE')
    s.add_argument('--output-archive',  required=True, metavar='OUTPUT_ARCHIVE')
    s.add_argument('--output-toc',      required=True, metavar='OUTPUT_TOC')

    # csv
    s = sub.add_parser('csv', help='Export asset list to CSV', parents=[toc_args])
    s.add_argument('--output', required=True, metavar='OUTPUT')

    # hash
    s = sub.add_parser('hash', help='Compute CRC-64 hash for a path string')
    s.add_argument('path')

    # dag
    s = sub.add_parser('dag', help='Search DAG names or export list')
    s.add_argument('--dag',    required=True, metavar='DAG')
    s.add_argument('--filter', metavar='FILTER')
    s.add_argument('--export', metavar='EXPORT')

    # loc-export
    s = sub.add_parser('loc-export', help='Export .localization asset -> CSV')
    s.add_argument('--asset',  required=True, metavar='ASSET',  help='Extracted .localization file')
    s.add_argument('--output', required=True, metavar='OUTPUT', help='Output CSV path')

    # loc-import
    s = sub.add_parser('loc-import', help='Import translated CSV -> .localization asset')
    s.add_argument('--asset',  required=True, metavar='ASSET',  help='Original extracted .localization file')
    s.add_argument('--csv',    required=True, metavar='CSV',    help='Translated CSV (from loc-export)')
    s.add_argument('--output', required=True, metavar='OUTPUT', help='Output .localization file')

    # dump-archive
    s = sub.add_parser('dump-archive', help='Hexdump bytes from archive (debug)')
    s.add_argument('--archive', required=True)
    s.add_argument('--offset',  default='0x0')
    s.add_argument('--size',    default='256')

    args = p.parse_args()
    {
        'build-hashdb': cmd_build_hashdb,
        'info':         cmd_info,
        'list':         cmd_list,
        'extract':      cmd_extract,
        'repack':       cmd_repack,
        'repack-dir':   cmd_repack_dir,
        'csv':          cmd_csv,
        'hash':         cmd_hash,
        'dag':          cmd_dag,
        'loc-export':   cmd_loc_export,
        'loc-import':   cmd_loc_import,
        'dump-archive': cmd_dump_archive,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
