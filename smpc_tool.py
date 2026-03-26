#!/usr/bin/env python3
"""
SMPCTool — PC Version  v2.1
Marvel's Spider-Man Remastered / Spider-Man 2 PC asset tool.
Pure Python 3, zero dependencies.

Adapted from SMPS4Tool v2 (PS4) with format details confirmed from
binary analysis of actual PC game files (toc, dag, patch.archive)
and cross-referenced with Phew/SMPCTool-src (C# original for PC).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PC FORMAT (confirmed from binary + C# source)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOC file:
  Magic:       0xAF12AF77 (big-endian, same as PS4)
  Compression: Single zlib stream from byte 8  (use decompressobj)
  DAT1 header: [00] DAT1  [04] parent_ref  [08] dec_size  [0c] n_sections=6
  Sections:    identified by content/size at runtime (order may vary per build)

  Section roles:
    ArchiveTOC    size=2048          per-archive asset index ranges (SpansEntries)
    AssetIDs      size= n x 8        uint64 asset identifiers
    SizeEntries   size= n x 12       (FileCtrInc:u32, FileSize:u32, FileCtr:u32)
    KeyAssets     size= k x 8        uint64 subset of AssetIDs (important assets)
    OffsetEntries size= n x 8        (ArchiveIndex:u32, ArchiveOffset:u32)
    ArchiveFiles  size= n_arch x 72  archive descriptors (72-byte stride)

Archive entry (72 bytes, PC; PS4 was 24):
  [0-2]   reserved/padding
  [3]     InstallBucket (uint8)
  [4-7]   Chunkmap (uint32)
  [8-71]  Filename: null-terminated ASCII, padded to 64 bytes

Archive file format (patch.archive style -- store-only / already decompressed):
  Outer wrapper (36 bytes):
    [0x00]  magic = 0x122BB0AB (LE)   -- PC-specific (PS4: 0xBA20AFB5)
    [0x04]  payload_size = TOC_file_size - 36
    [0x08]  28 zero bytes (padding)
  [0x24]  DAT1 payload -- raw, uncompressed sections

  Main archives (g00sXXX): use custom LZ compression (see AssetDecompress2.cs).
  patch.archive: store-only LZ (raw data, no backref).
  This tool returns the payload starting at byte 36 for wrapped assets.

DAG file:
  Magic:    0x891F77AF (little-endian, same as PS4)
  Compress: zlib from byte 12 via decompressobj
  Strings:  null-terminated ASCII from byte 88
            first entry = "DependencyDag" (self-name, skipped)
            terminator = 4 consecutive zero bytes

Asset name resolution:
  The C# tool uses a prebuilt AssetHashes.txt (path,decimal_id pairs).
  Source: https://github.com/Phew/SMPCTool-src
  Coverage: ~212,921 / 771,677 assets (30%) -- 0x80-0xbf IDs only.
  The 0xe0-prefix IDs (69%) are structured/procedural and have no names.

  Supported --hashdb formats:
    1. C# original: AssetHashes.txt  (path,decimal_uint64)
    2. Our format:  <hex_id>TAB<path>  (produced by build-hashdb)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Quick start:
  1. Download AssetHashes.txt from github.com/Phew/SMPCTool-src
  2. python smpc_tool.py info --toc toc --hashdb AssetHashes.txt
  3. python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --filter .texture
  4. python smpc_tool.py extract --toc toc --archive-dir asset_archive
         --archive g00s000 --output out/ --hashdb AssetHashes.txt

All commands:
  info         --toc <toc> [--hashdb <db>]
  list         --toc <toc> [--hashdb <db>] [--filter <str>] [--archive <name>]
  extract      --toc <toc> --archive-dir <dir> --archive <name>
               --output <dir> [--hashdb <db>] [--skip-hex]
  repack       --toc <toc> --archive-dir <dir> --archive <name>
               --output-archive <file> --output-toc <file> [--hashdb <db>] [--skip-hex]
  csv          --toc <toc> --output <file.csv> [--hashdb <db>]
  build-hashdb --dag <dag> --output <db.txt>
  hash         <path_string>
  dag          --dag <dag> [--filter <str>] [--export <file>]
  dump-archive --archive <file> [--offset <hex>] [--size <n>]
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

TOC_MAGIC       = 0xAF12AF77   # big-endian in file
DAT1_MAGIC      = 0x44415431   # b'DAT1'
PC_ASSET_MAGIC  = 0x122BB0AB   # LE -- PC archive wrapper
PC_HEADER_SIZE  = 36           # outer wrapper before DAT1 payload
ARCH_STRIDE     = 72           # ArchiveFiles entry size (PS4=24)
DAG_MAGIC       = 0x891F77AF   # little-endian (same as PS4)
DAG_STR_OFFSET  = 88           # string table starts here in decompressed DAG

# ---------------------------------------------------------------------------
# CRC-64 (Insomniac) -- kept for best-effort name matching
# ---------------------------------------------------------------------------

def _build_crc64_table():
    POLY = 0xAD93D23594C935A9
    tbl = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ POLY if crc & 1 else crc >> 1
        tbl.append(crc & 0xFFFFFFFFFFFFFFFF)
    return tbl

_CRC64 = _build_crc64_table()


def compute_hash(path):
    """Insomniac CRC-64 with final transform. May not match all PC asset IDs."""
    norm = []
    for ch in path:
        c = ord(ch)
        if 0x41 <= c <= 0x5A:
            c += 0x20
        if c == 0x5C:
            c = 0x2F
        norm.append(c)
    cleaned = []
    prev_slash = False
    for c in norm:
        if c == 0x2F:
            if not prev_slash:
                cleaned.append(c)
            prev_slash = True
        else:
            cleaned.append(c)
            prev_slash = False
    crc = 0xC96C5795D7870F42
    for b in cleaned:
        crc = _CRC64[(crc ^ b) & 0xFF] ^ (crc >> 8)
        crc &= 0xFFFFFFFFFFFFFFFF
    return ((crc >> 2) | 0x8000000000000000) & 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# TOC decompression / compression
# ---------------------------------------------------------------------------

def _toc_decompress(raw):
    """
    Decompress PC TOC.
    Layout: magic(4) + dec_size(4) + zlib_stream
    Must use decompressobj -- zlib.decompress() raises on this stream.
    """
    magic = struct.unpack_from('>I', raw, 0)[0]
    if magic != TOC_MAGIC:
        raise ValueError(f"Bad TOC magic: {magic:#010x} (expected {TOC_MAGIC:#010x})")
    expected = struct.unpack_from('<I', raw, 4)[0]
    dec = zlib.decompressobj().decompress(raw[8:])
    if len(dec) != expected:
        dec = dec[:expected]
    return dec


def _toc_compress(dec_data):
    """Recompress decompressed TOC back to on-disk format."""
    comp = zlib.compress(dec_data, level=9)
    header = struct.pack('>I', TOC_MAGIC) + struct.pack('<I', len(dec_data))
    return header + comp


# ---------------------------------------------------------------------------
# TOC parser
# ---------------------------------------------------------------------------

class AssetEntry:
    __slots__ = ('asset_id', 'filename', 'file_size', 'archive_index',
                 'archive_offset', '_ai_toc', '_ao_toc', '_sz_toc')

    def __init__(self):
        self.asset_id      = 0
        self.filename      = ''
        self.file_size     = 0
        self.archive_index = 0
        self.archive_offset = 0
        self._ai_toc = 0  # byte offset for archive_index in dec_data
        self._ao_toc = 0  # byte offset for archive_offset in dec_data
        self._sz_toc = 0  # byte offset for file_size in dec_data

    def __repr__(self):
        return (f"<Asset {self.asset_id:#018x} arch={self.archive_index}"
                f" off={self.archive_offset:#010x} sz={self.file_size:,}"
                f" name={self.filename!r}>")


class TOC:
    def __init__(self):
        self.archives = []
        self.assets   = []
        self._raw_header = b''
        self.dec_data    = b''

    @classmethod
    def load(cls, path, hashdb=None):
        with open(path, 'rb') as f:
            raw = f.read()
        toc = cls()
        toc._raw_header = raw[:8]
        toc.dec_data = _toc_decompress(raw)
        toc._build(hashdb or {})
        return toc

    def _build(self, hashdb):
        dec = self.dec_data

        if struct.unpack_from('<I', dec, 0)[0] != DAT1_MAGIC:
            raise ValueError("Bad DAT1 magic in decompressed TOC")
        n_sections = struct.unpack_from('<I', dec, 12)[0]
        if n_sections != 6:
            raise ValueError(f"Expected 6 TOC sections, got {n_sections}")

        secs = []
        for i in range(6):
            base = 16 + i * 12
            secs.append(struct.unpack_from('<III', dec, base))
        # secs[i] = (section_hash, offset, size)

        # -- Identify sections by their size and content --
        # ArchiveTOC: exactly 2048 bytes
        archivetoc = next((s for s in secs if s[2] == 2048), None)

        # ArchiveFiles: small, divisible by ARCH_STRIDE (72)
        arch_cands = [s for s in secs if s[2] % ARCH_STRIDE == 0 and s[2] < 200_000]
        archfiles = min(arch_cands, key=lambda s: s[2]) if arch_cands else None

        # SizeEntries: divisible by 12; n_assets = size//12 must also appear as size//8 in another sec
        size8s = {s[2] // 8 for s in secs if s[2] % 8 == 0 and s[2] > 100_000}
        sizeentries = next(
            (s for s in secs if s[2] % 12 == 0 and (s[2] // 12) in size8s and s[2] > 100_000),
            None)

        n_assets = sizeentries[2] // 12 if sizeentries else 0

        # Two large uint64 sections (AssetIDs and OffsetEntries)
        large8 = [s for s in secs if s[2] == n_assets * 8 and s[2] > 100_000]
        assetids = offsets = None
        if len(large8) == 2:
            # OffsetEntries: first uint32 = arch_idx < 256
            for s in large8:
                v = struct.unpack_from('<I', dec, s[1])[0]
                if v < 256:
                    offsets = s
                else:
                    assetids = s
        elif len(large8) == 1:
            assetids = large8[0]

        # KeyAssets: remaining unaccounted small section
        used = {id(s) for s in [archivetoc, archfiles, sizeentries, assetids, offsets] if s}
        keyassets = next((s for s in secs if id(s) not in used), None)

        if not (archfiles and assetids and sizeentries and offsets):
            raise ValueError("Could not identify all required TOC sections.")

        arch_off = archfiles[1]
        asid_off = assetids[1]
        size_off = sizeentries[1]
        oe_off   = offsets[1]

        # Parse ArchiveFiles
        n_arch = archfiles[2] // ARCH_STRIDE
        self.archives = []
        for i in range(n_arch):
            e = dec[arch_off + i*ARCH_STRIDE : arch_off + (i+1)*ARCH_STRIDE]
            nm = e[8:].split(b'\x00')[0].decode('ascii', 'replace')
            self.archives.append(nm)

        # Parse assets
        self.assets = []
        for i in range(n_assets):
            asset_id = struct.unpack_from('<Q', dec, asid_off + i*8)[0]
            se_pos   = size_off + i*12
            _unk, file_size, oe_idx = struct.unpack_from('<III', dec, se_pos)
            oe_pos = oe_off + oe_idx*8
            arch_idx, arch_offset = struct.unpack_from('<II', dec, oe_pos)

            a = AssetEntry()
            a.asset_id       = asset_id
            a.filename       = hashdb.get(asset_id, f'{asset_id:#018x}')
            a.file_size      = file_size
            a.archive_index  = arch_idx
            a.archive_offset = arch_offset
            a._ai_toc        = oe_pos
            a._ao_toc        = oe_pos + 4
            a._sz_toc        = se_pos + 4
            self.assets.append(a)

    def by_archive(self, name):
        """Return all assets belonging to archives whose name matches.
        Handles duplicate archive names (e.g. patch.archive appears 3 times on PC)
        by collecting assets from ALL matching indices."""
        indices = {i for i, n in enumerate(self.archives) if n == name}
        if not indices:
            return []
        return [a for a in self.assets if a.archive_index in indices]

    def patch_redirect(self, asset, new_idx, new_off, new_sz):
        buf = bytearray(self.dec_data)
        struct.pack_into('<I', buf, asset._ai_toc, new_idx)
        struct.pack_into('<I', buf, asset._ao_toc, new_off)
        struct.pack_into('<I', buf, asset._sz_toc, new_sz)
        self.dec_data = bytes(buf)

    def save(self, path):
        with open(path, 'wb') as f:
            f.write(_toc_compress(self.dec_data))


# ---------------------------------------------------------------------------
# Archive reader -- PC format
# ---------------------------------------------------------------------------

class ArchiveReader:
    """
    Read assets from PC archive files.

    PC archive format per asset:
      Outer wrapper (36 bytes):
        [0x00]  magic        = 0x122BB0AB (LE)
        [0x04]  payload_size = TOC_file_size - 36
        [0x08]  28 zero bytes
      DAT1 payload (payload_size bytes):
        Raw DAT1 -- NO compression applied.

    Some assets use different magics (e.g. 0x4746580e for GFX/Scaleform).
    These are stored raw and returned as-is.
    """

    def __init__(self, archive_dir):
        self.archive_dir = archive_dir
        self._handles = {}

    def _open(self, name):
        if name not in self._handles:
            path = os.path.join(self.archive_dir, name)
            self._handles[name] = open(path, 'rb')
        return self._handles[name]

    def close_all(self):
        for fh in self._handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._handles.clear()

    def read_asset(self, asset, archives):
        """Return full on-disk bytes (wrapper + payload)."""
        arch_name = archives[asset.archive_index]
        fh = self._open(arch_name)
        fh.seek(asset.archive_offset)
        data = fh.read(asset.file_size)
        if len(data) != asset.file_size:
            raise IOError(
                f"Short read: expected {asset.file_size}, got {len(data)} "
                f"from {arch_name} @ {asset.archive_offset:#x}")
        return data

    def read_asset_payload(self, asset, archives):
        """Return DAT1 payload (strips 36-byte PC wrapper if present)."""
        data = self.read_asset(asset, archives)
        if len(data) >= 4 and struct.unpack_from('<I', data, 0)[0] == PC_ASSET_MAGIC:
            if len(data) >= PC_HEADER_SIZE:
                return data[PC_HEADER_SIZE:]
        return data


# ---------------------------------------------------------------------------
# DAG parser
# ---------------------------------------------------------------------------

def load_dag(path):
    """
    Parse PC DAG; return {crc64_hash: path_string}.
    Note: CRC-64 hashes may not match PC TOC asset IDs (different ID scheme).
    """
    with open(path, 'rb') as f:
        raw = f.read()
    magic = struct.unpack_from('<I', raw, 0)[0]
    if magic != DAG_MAGIC:
        raise ValueError(f"Bad DAG magic: {magic:#010x} (expected {DAG_MAGIC:#010x})")

    dec = zlib.decompressobj().decompress(raw[12:])

    pos = DAG_STR_OFFSET
    try:
        pos = dec.index(b'\x00', pos) + 1   # skip "DependencyDag\0"
    except ValueError:
        pass

    hashdb = {}
    while pos < len(dec) - 3:
        if dec[pos:pos+4] == b'\x00\x00\x00\x00':
            break
        if dec[pos] == 0:
            pos += 1
            continue
        if not (0x20 <= dec[pos] <= 0x7E):
            break
        try:
            end = dec.index(b'\x00', pos)
        except ValueError:
            break
        name = dec[pos:end].decode('ascii', 'replace')
        hashdb[compute_hash(name)] = name
        pos = end + 1
    return hashdb


# ---------------------------------------------------------------------------
# Hash DB (tab-separated text)
# ---------------------------------------------------------------------------

def load_hashdb(path):
    """
    Load asset name database.  Two formats are supported:

    1. Our format (tab-separated):   0x<hex_id>TAB<path>
       Produced by this tool's build-hashdb command.

    2. C# SMPCTool format (CSV):     <path>,<decimal_uint64>
       The AssetHashes.txt from github.com/Phew/SMPCTool-src.
       Drop that file next to your toc and pass it with --hashdb.
    """
    db = {}
    if not path or not os.path.isfile(path):
        return db
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n\r')
            if not line:
                continue
            if '\t' in line:
                # Our format: hex_id<TAB>name
                hex_part, name = line.split('\t', 1)
                try:
                    db[int(hex_part.strip(), 16)] = name
                except ValueError:
                    pass
            elif ',' in line:
                # C# format: name,decimal_id  (AssetHashes.txt)
                parts = line.rsplit(',', 1)
                if len(parts) == 2:
                    name, id_str = parts
                    try:
                        db[int(id_str)] = name
                    except ValueError:
                        pass
    return db


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_info(args):
    db  = load_hashdb(getattr(args, 'hashdb', None))
    toc = TOC.load(args.toc, db)
    counts = defaultdict(int)
    for a in toc.assets:
        counts[a.archive_index] += 1
    print(f"Archives : {len(toc.archives)}")
    for i, name in enumerate(toc.archives):
        print(f"  [{i:3d}]  {name:<28}  {counts.get(i,0):,} assets")
    total = len(toc.assets)
    named = sum(1 for a in toc.assets if not a.filename.startswith('0x'))
    print(f"\nTotal assets : {total:,}")
    print(f"Named assets : {named:,} ({100*named//total if total else 0}%)")
    print(f"Hex ID only  : {total-named:,}")


def cmd_list(args):
    db  = load_hashdb(getattr(args, 'hashdb', None))
    toc = TOC.load(args.toc, db)
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
    db  = load_hashdb(getattr(args, 'hashdb', None))
    toc = TOC.load(args.toc, db)
    skip_hex = getattr(args, 'skip_hex', False)
    os.makedirs(args.output, exist_ok=True)

    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] No assets found for archive '{args.archive}'.")
        print(f"    Available archives: {', '.join(toc.archives)}")
        return

    reader = ArchiveReader(args.archive_dir)
    ok = err = skipped = 0
    try:
        for a in assets:
            if skip_hex and a.filename.startswith('0x'):
                skipped += 1
                continue
            try:
                data = reader.read_asset_payload(a, toc.archives)
                safe = a.filename.replace('/', os.sep).replace('\\', os.sep)
                out_path = os.path.join(args.output, safe)
                parent = os.path.dirname(out_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out_path, 'wb') as f:
                    f.write(data)
                ok += 1
            except Exception as e:
                print(f"[ERR] {a.filename}: {e}", file=sys.stderr)
                err += 1
    finally:
        reader.close_all()

    print(f"Extracted {ok}  Errors {err}  Skipped {skipped}")


def cmd_repack(args):
    """
    Rebuild archive: read raw on-disk bytes, write contiguously, patch TOC offsets.
    PC archives need no re-compression -- assets are stored raw.
    """
    db  = load_hashdb(getattr(args, 'hashdb', None))
    toc = TOC.load(args.toc, db)
    skip_hex = getattr(args, 'skip_hex', False)

    assets = toc.by_archive(args.archive)
    if not assets:
        print(f"[!] No assets found for archive '{args.archive}'.")
        return

    if skip_hex:
        assets = [a for a in assets if not a.filename.startswith('0x')]

    # Sort by original archive_offset so the new archive preserves reading order.
    # This also makes roundtrip byte-identical when no assets are modified.
    assets = sorted(assets, key=lambda a: a.archive_offset)

    reader = ArchiveReader(args.archive_dir)
    ok = err = 0

    print(f"Repacking {len(assets):,} assets -> {args.output_archive}")
    try:
        with open(args.output_archive, 'wb') as out:
            for a in assets:
                try:
                    data    = reader.read_asset(a, toc.archives)
                    new_off = out.tell()
                    out.write(data)
                    toc.patch_redirect(a, a.archive_index, new_off, len(data))
                    ok += 1
                except Exception as e:
                    print(f"[ERR] {a.filename}: {e}", file=sys.stderr)
                    err += 1
    finally:
        reader.close_all()

    toc.save(args.output_toc)
    print(f"Written {ok} assets  Errors {err}")
    print(f"New TOC -> {args.output_toc}")


def cmd_csv(args):
    db  = load_hashdb(getattr(args, 'hashdb', None))
    toc = TOC.load(args.toc, db)
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        w = csv_mod.writer(f)
        w.writerow(['asset_id', 'filename', 'archive', 'archive_offset', 'file_size'])
        for a in toc.assets:
            arch = toc.archives[a.archive_index] if a.archive_index < len(toc.archives) else ''
            w.writerow([f'{a.asset_id:#018x}', a.filename, arch,
                        a.archive_offset, a.file_size])
    print(f"Exported {len(toc.assets):,} rows -> {args.output}")


def cmd_build_hashdb(args):
    """
    Build name DB from DAG file (CRC-64 hashes).
    WARNING: On PC, CRC-64 hashes may not match TOC asset IDs.
    The output is still useful for --filter/search and may partially match
    future game versions that use CRC-64 for some asset types.
    """
    print("Building hash DB from DAG (CRC-64 -- may not match all PC asset IDs)...")
    hashdb = load_dag(args.dag)
    with open(args.output, 'w', encoding='utf-8') as f:
        for h, name in sorted(hashdb.items(), key=lambda x: x[1]):
            f.write(f'{h:#018x}\t{name}\n')
    print(f"Written {len(hashdb):,} entries -> {args.output}")


def cmd_hash(args):
    h = compute_hash(args.path)
    print(f'{h:#018x}  {args.path}')


def cmd_dag(args):
    print("Loading DAG...", file=sys.stderr)
    hashdb = load_dag(args.dag)
    filt   = (getattr(args, 'filter', None) or '').lower()
    names  = sorted(n for n in hashdb.values() if filt in n.lower())
    if getattr(args, 'export', None):
        with open(args.export, 'w', encoding='utf-8') as f:
            for name in names:
                f.write(name + '\n')
        print(f"Exported {len(names):,} names -> {args.export}")
    else:
        try:
            for name in names:
                print(name)
        except BrokenPipeError:
            pass
        print(f"\nTotal: {len(hashdb):,}  Shown: {len(names):,}", file=sys.stderr)


def cmd_dump_archive(args):
    """Hexdump raw bytes from an archive (for format debugging)."""
    offset = int(args.offset, 16) if args.offset.startswith('0x') else int(args.offset, 0)
    size   = int(args.size)
    with open(args.archive, 'rb') as f:
        f.seek(offset)
        data = f.read(size)
    print(f"Archive: {args.archive}  offset={offset:#x}  reading {size} bytes")
    if len(data) >= 4:
        ml = struct.unpack_from('<I', data, 0)[0]
        mb = struct.unpack_from('>I', data, 0)[0]
        print(f"Magic LE={ml:#010x}  BE={mb:#010x}", end='')
        if ml == PC_ASSET_MAGIC:
            payload = struct.unpack_from('<I', data, 4)[0]
            print(f"  [PC wrapper, payload={payload:,}, total={payload+PC_HEADER_SIZE:,}]", end='')
        print()
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
        description='Spider-Man PC asset tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('info');    s.add_argument('--toc', required=True); s.add_argument('--hashdb')
    s = sub.add_parser('list');    s.add_argument('--toc', required=True); s.add_argument('--hashdb'); s.add_argument('--filter'); s.add_argument('--archive')
    s = sub.add_parser('extract'); s.add_argument('--toc', required=True); s.add_argument('--archive-dir', required=True); s.add_argument('--archive', required=True); s.add_argument('--output', required=True); s.add_argument('--hashdb'); s.add_argument('--skip-hex', action='store_true')
    s = sub.add_parser('repack');  s.add_argument('--toc', required=True); s.add_argument('--archive-dir', required=True); s.add_argument('--archive', required=True); s.add_argument('--output-archive', required=True); s.add_argument('--output-toc', required=True); s.add_argument('--hashdb'); s.add_argument('--skip-hex', action='store_true')
    s = sub.add_parser('csv');     s.add_argument('--toc', required=True); s.add_argument('--output', required=True); s.add_argument('--hashdb')
    s = sub.add_parser('build-hashdb'); s.add_argument('--dag', required=True); s.add_argument('--output', required=True)
    s = sub.add_parser('hash');    s.add_argument('path')
    s = sub.add_parser('dag');     s.add_argument('--dag', required=True); s.add_argument('--filter'); s.add_argument('--export')
    s = sub.add_parser('dump-archive'); s.add_argument('--archive', required=True); s.add_argument('--offset', default='0x0'); s.add_argument('--size', default='256')

    args = p.parse_args()
    {
        'info':          cmd_info,
        'list':          cmd_list,
        'extract':       cmd_extract,
        'repack':        cmd_repack,
        'csv':           cmd_csv,
        'build-hashdb':  cmd_build_hashdb,
        'hash':          cmd_hash,
        'dag':           cmd_dag,
        'dump-archive':  cmd_dump_archive,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
