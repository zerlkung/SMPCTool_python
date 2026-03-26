# SMPCTool Python

Python port of [SMPCTool](https://github.com/Phew/SMPCTool-src) for Marvel's Spider-Man Remastered / Spider-Man 2 on PC.

Pure Python 3, zero dependencies. All format details reverse-engineered from actual PC game files and cross-referenced with the original C# source.

## Usage

```bash
# Show TOC summary
python smpc_tool.py info --toc toc --hashdb AssetHashes.txt

# List assets in an archive
python smpc_tool.py list --toc toc --hashdb AssetHashes.txt --archive g00s000

# Extract assets
python smpc_tool.py extract --toc toc --archive-dir asset_archive \
    --archive g00s000 --output out/ --hashdb AssetHashes.txt

# Rebuild archive + new TOC (for modding)
python smpc_tool.py repack --toc toc --archive-dir asset_archive \
    --archive patch.archive --output-archive patch_new.archive \
    --output-toc toc_new --hashdb AssetHashes.txt

# Export full asset list to CSV
python smpc_tool.py csv --toc toc --output assets.csv --hashdb AssetHashes.txt

# Build hash DB from DAG
python smpc_tool.py build-hashdb --dag dag --output hashdb.txt

# Search asset names in DAG
python smpc_tool.py dag --dag dag --filter .texture --export textures.txt

# Compute CRC-64 hash of a path
python smpc_tool.py hash "characters/hero/hero_spiderman/spider_man.model"
```

## Asset Hash Database

For name resolution, download **AssetHashes.txt** from [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src) (48 MB) and pass it with `--hashdb AssetHashes.txt`.

Both formats are supported automatically:
- C# original format: `path,decimal_uint64` (AssetHashes.txt)
- Tab-separated format: `0xhex_id<TAB>path` (produced by `build-hashdb`)

Coverage: ~212,921 / 771,677 assets named (30%) — the remaining 70% use structured `0xe0`-prefix IDs with no known path names.

## PC Format Notes

| Component | PS4 | PC |
|---|---|---|
| TOC magic | `0xAF12AF77` | Same |
| TOC compression | Single zlib (decompressobj) | Same |
| Archive entry stride | 24 bytes | **72 bytes** |
| Archive magic | `0xBA20AFB5` | **`0x122BB0AB`** |
| Archive compression | Raw | Wrapper(36B) + DAT1; custom LZ in g-archives |
| DAG magic | `0x891F77AF` | Same |
| DAG string offset | 102 | **88** |
| Hash algorithm | CRC-64 | Pre-built table (AssetHashes.txt) |

## Credits

- Original C# SMPCTool: [Phew/SMPCTool-src](https://github.com/Phew/SMPCTool-src)
- PS4 Python port this was adapted from: [zerlkung/SMPCTool-PS4_python](https://github.com/zerlkung/SMPCTool-PS4_python)
