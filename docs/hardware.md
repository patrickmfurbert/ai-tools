# Hardware: GMKtec EVO-X2 (Strix Halo)

- **Box:** GMKtec EVO-X2 mini-PC
- **SoC:** AMD Ryzen AI MAX+ 395 — 16 Zen 5 cores + Radeon 8060S iGPU
  (RDNA 3.5, gfx1151), 128 GB LPDDR5x unified memory
- **OS:** Ubuntu 24.04.4 LTS (server install, no desktop), kernel 7.0.x

Everything about this setup flows from one constraint: the 128 GB of RAM is
*unified*. Whatever you give the GPU as a hard carve-out is gone from the OS.
The whole stack is tuned around a 96 GiB carve-out with the rest paged.

## BIOS: 96 GB VRAM carve-out

Power on, spam `DEL` to enter the AMI BIOS, then:

1. **Advanced → AMD CBS → NBIO Common Options → GFX Configuration**
   (on some firmware revisions: `Advanced → ABIos → GFX Config`)
2. Set **UMA Frame Buffer Size** (a.k.a. "GFX Memory" / "UMA Video Memory")
   to **96G**. The menu offers steps up to 96G on the 128 GB EVO-X2.
3. Save & Exit (`F10`, save changes).

Verify from Linux after booting — the number must be exactly 96 GiB:

```bash
rocm-smi --showmeminfo vram
# GPU[0] : VRAM Total Memory (B): 103079215104   ← 96 GiB (96 × 2^30)
```

Why 96 and not 112 or 64:

- **≥ 96 GiB** — Qwen3.8-27B at UD-Q4_K_XL (~17 GB) plus a 131K-context q8_0
  KV cache (tens of GB) plus the MTP draft head must all be resident, and
  Flash-Next wants as much of its ~88 GB weight file resident as possible.
- **≤ 96 GiB** — the OS still needs ~30 GiB to live: page cache for the SSD
  tensor reads, the 35B orchestrator's working set during load, and headroom
  so the OOM killer never touches a loading llama-server.

With the carve-out set, `free -h` shows ~30 GiB total — that is *expected*,
not a bug.

## Kernel arguments

Append to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`:

```
GRUB_CMDLINE_LINUX_DEFAULT="amd_iommu=off amdgpu.gttsize=94208 ttm.pages_limit=24117248"
```

then:

```bash
sudo update-grub && sudo reboot
```

What each does and why:

| Arg | Value | Purpose |
|---|---|---|
| `amd_iommu=off` | — | Disables the IOMMU. On Strix Halo with ROCm 7.x, enabled IOMMU fragments and slows the huge single allocations llama.cpp makes (and has caused KFD mapping failures). A local, single-user inference box can trade the DMA-isolation property for stability and bandwidth. |
| `amdgpu.gttsize` | `94208` (MiB ≈ 92 GiB) | Sizes the GTT — GPU-accessible *system* memory — just under the 96 GiB region so mmap'd model tensors and the Flash-Next engram table can be mapped into the GPU address space without the driver refusing the mapping. |
| `ttm.pages_limit` | `24117248` (pages × 4 KiB ≈ 92 GiB) | Caps the TTM (memory manager inside amdgpu) page pool at the same ~92 GiB. Without a sane cap, TTM either falls back to allocations that get bounced or triggers the OOM killer under KV-cache growth; with it, VRAM accounting behaves. |

Verify after reboot:

```bash
cat /proc/cmdline                                   # args present
cat /sys/module/ttm/parameters/pages_limit          # 24117248
```

> **Note (2026-08-28):** the live evo-x2 currently boots with an empty
> `GRUB_CMDLINE_LINUX` (TTM reports `pages_limit=4060756`) and the stack runs,
> so these args are belt-and-braces hardening rather than a hard
> requirement on ROCm 7.2.4. If you see allocation failures at long context,
> apply them first.

## EVO-X2 specific notes

- **gfx1151 is new silicon.** It is *not* covered by generic
  `gfx11-generic` code paths for performance-critical paths — every build
  must explicitly target `gfx1151` (see
  [llama-cpp-build.md](llama-cpp-build.md)). Prebuilt binaries generally
  don't include it.
- **`/dev/kfd` + `renderD128`** must be writable by the serving user — add it
  to the `video` and `render` groups (see [rocm-setup.md](rocm-setup.md)).
- **Thermal:** sustained prefill pins the SoC to its power cap. The GMKtec
  cooling is adequate but expect fan noise; if you see clock throttling in
  `rocm-smi --showclocks`, raise the fan curve in BIOS
  (`Quiet` → `Performance`/`Balanced`).
- **SSD matters.** Flash-Next keeps a ~27 GB engram table memory-mapped from
  NVMe (`--tensor-read-lazy`, though see the ROCm caveat in
  [launch-flags.md](launch-flags.md)). A Gen4 NVMe roughly halves the cold
  first-token penalty vs a SATA drive.
- **No second GPU.** One `GPU[0]` in `rocminfo`; multi-model = multiple
  processes sharing the one carve-out, which is exactly what the port scheme
  in [README.md](README.md) organizes.
- **USB/USB4:** the box is headless; USB is only used for the initial OS
  install. All day-to-day access is SSH over the mesh.
