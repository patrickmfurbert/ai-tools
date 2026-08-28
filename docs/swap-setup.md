# Swap Setup (64 GB swapfile)

## Why

The EVO-X2 only shows ~30 GiB of RAM to Linux after the 96 GiB VRAM carve-out
([hardware.md](hardware.md)). Loading **Qwen3.8-Flash-Next** at 131K context
doesn't fit in that:

- The IQ4_XS split is ~88 GB on disk; even with GPU offload, the load path
  stages large tensor buffers through system RAM, the MTP sidecar adds 3.9 GB,
  and the lazy/mmap paging path relies on page-cache headroom the 30 GiB can't
  guarantee.
- During the load, RSS climbs into the 27+ GB range while the page cache is
  simultaneously fighting for the same 30 GiB — without swap the **OOM killer
  kills the server mid-load** (the classic "llama-server died at 73% loaded"
  failure).
- With 64 GB of swap, the kernel spills pages instead of killing: the load
  finishes (slower tail, fine at boot-of-server time), and steady-state paging
  activity is low because the hot working set stays resident.

The three smaller servers (9B/Hermes/27B) don't need swap for normal loads —
it's insurance; Flash-Next genuinely wants it.

## Setup

```bash
# 64 GB swapfile at /swapfile (fallocate is instant on ext4; dd fallback shown)
sudo fallocate -l 64G /swapfile
# if fallocate errors on your filesystem:
#   sudo dd if=/dev/zero of=/swapfile bs=1M count=65536 status=progress

sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

Tunables (`/etc/sysctl.d/99-llm-swap.conf`):

```
# Prefer staying in RAM; only page out under real pressure,
# but never treat swap as forbidden (OOM would be worse).
vm.swappiness=10
# Don't panic when a allocation fails once; let the OOM reaper handle it.
vm.overcommit_memory=0
```

```bash
sudo sysctl --system
```

## Persist across reboots (fstab)

Add to `/etc/fstab`:

```
/swapfile none swap sw 0 0
```

Verify the whole picture:

```bash
swapon --show
# NAME       TYPE SIZE USED PRIO
# /swap.img  file   8G 6.4G   -1     ← Ubuntu's default swap (keep it)
# /swapfile  file  64G   0B   -1     ← ours

cat /etc/fstab | grep swap
# /swap.img   none swap sw 0 0
# /swapfile   none swap sw 0 0

sudo swapon --all --noheadings    # survives `reboot` only if in fstab
```

> **Gotcha found while writing this doc (2026-08-28):** on the live box
> `/swapfile` was active (`swapon --show` lists it) but **absent from
> `/etc/fstab`** — only Ubuntu's default `/swap.img` line was there. After a
> reboot the 64 GB swap silently disappears and Flash-Next loads start OOMing.
> If you're reproducing this setup (or fixing that box), add the fstab line
> above and confirm with `sudo swapon --noauto` semantics: fstab entries are
> what make `swapon` automatic at boot.

## Sizing notes

- **64 GB** because the model dir itself is 88+ GB and worst-case spill during
  a Flash-Next reload at 131K was measured needing tens of GB beyond RAM;
  64 GB with 30 GiB RAM ≈ 94 GiB of virtual headroom, matching the "one full
  model reload" budget without eating SSD space we need for the models.
- Swap lives on the same NVMe as the models — heavy paging competes with
  lazy tensor reads. That's acceptable for load-time spill (the case we
  sized for) and is the same reason `--tensor-read-lazy` is a double-edged
  sword ([launch-flags.md](launch-flags.md)).
- Monitor during a Flash-Next load with `watch -n 2 free -h`
  ([monitoring.md](monitoring.md)): `Swap used` climbing during load is
  expected; climbing steadily **while idle** means a leak or a second server
  started that shouldn't have been.
