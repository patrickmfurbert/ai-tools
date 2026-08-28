# ROCm Setup (Ubuntu 24.04.4 + ROCm 7.2.4)

Goal: a working KFD compute stack on the EVO-X2 where `rocminfo` reports
`gfx1151` and ROCm 7.2.4, so llama.cpp can be built against it.

Verified state of the live box:

| Package | Version |
|---|---|
| `rocm-core` | `7.2.4.70204-93~24.04` |
| `hip-runtime-amd` / `hip-dev` | `7.2.53211.70204-93~24.04` |
| `amdgpu-install` | `30.30.4.0.30300400-2341068.24.04` |
| Ubuntu | 24.04.4 LTS (Noble) |

## 1. Install via `amdgpu-install`

AMD's own installer is the only reliable way to get the 7.2.x stack + matching
amdgpu DKMS driver in sync:

```bash
# 1. Get the installer repo package
wget https://repo.radeon.com/amdgpu-install/6.4.4/ubuntu/noble/amdgpu-install_latest_all.deb
sudo apt install ./amdgpu-install_latest_all.deb
# (the installed version on this box is 30.30.4.x — whatever "latest" points
#  at for noble is fine; it knows where the 7.2.x repos live)

# 2. Install the ROCm + LLVM + DKMS driver set. `--usecase=rocm` pulls
#    hip-runtime-amd, hipblas, rocblas, rocBLAS/hipBLASLt, miopen, comgr, etc.
sudo amdgpu-install --usecase=rocm --no-dkms -y    # if you want the inbox kernel driver
# — or, to get AMD's DKMS amdgpu (recommended for Strix Halo quirks):
sudo amdgpu-install --usecase=rocm -y

# 3. Reboot if DKMS amdgpu was installed
sudo reboot
```

If apt complains about the repo signing key, re-add it per
[AMD's repo docs](https://repo.radeon.com/amdgpu-install/) — the noble
`rocm-7.2.4` repo key must match the installed `amdgpu-install`.

Verify package versions after install:

```bash
dpkg -l | grep -E "rocm-core|hip-runtime|hipblas" 
# rocm-core          7.2.4.70204-93~24.04
# hip-runtime-amd    7.2.53211.70204-93~24.04
# hipblas-dev        3.2.0.70204-93~24.04
```

## 2. KFD setup (compute access)

ROCm compute goes through `/dev/kfd` (kernel fusion driver — the KFD, not
"kernel font driver") plus a `/dev/dri/renderD*` node:

```bash
ls -l /dev/kfd /dev/dri/renderD*
# crw-rw-rw- 1 root render 236, 0 ... /dev/kfd
# crw-rw---- 1 root render  ... /dev/dri/renderD128

# Put your user in the right groups (one-time, then re-login):
sudo usermod -aG render,video $USER
```

`/dev/kfd` missing entirely means the amdgpu driver didn't load or the IOMMU
is interfering — check `dmesg | grep -i "amdgpu\|kfd"` and the
`amd_iommu=off` kernel arg in [hardware.md](hardware.md).

Session/login quirks: systemd-logind sometimes resets `/dev/dri` group
membership at boot. If after a reboot `ls -l /dev/dri/renderD128` no longer
grants your group, add a udev rule:

```bash
echo 'KERNEL=="renderD*", GROUP="render", MODE="0660"' | sudo tee /etc/udev/rules.d/99-rocm.rules
sudo udevadm control --reload
```

## 3. Environment

Add to `~/.bashrc` (needed at build time; harmless at runtime):

```bash
export PATH="/opt/rocm/bin:$PATH"
export PATH="/opt/rocm/lib:/opt/rocm/lib64:$LD_LIBRARY_PATH"
export HSA_OVERRIDE_GFX_VERSION=   # leave UNSET on gfx1151 — do not spoof
```

> **Never set `HSA_OVERRIDE_GFX_VERSION` here.** On older RDNA cards people
> spoof an older gfx target; on gfx1151 that only hides problems. The whole
> point of this setup is that ROCm 7.2.4 supports gfx1151 natively.

## 4. Verification

### rocminfo — gfx1151 must appear

```bash
rocminfo
```

Expected (relevant lines from the live box):

```
  Name:                    AMD RYZEN AI MAX+ 395 w/ Radeon 8060S
  ...
  Name:                    gfx1151
  Marketing Name:          AMD Radeon Graphics
  ...
      Name:                    amdgcn-amd-amdhsa--gfx1151
      Name:                    amdgcn-amd-amdhsa--gfx11-generic
```

Two things to confirm:

1. The GPU agent reports **`gfx1151`** as its target ID (not
   `gfx11-generic` — if you see generic, your LLVM/ROCm is too old).
2. `ROCk module version` prints (i.e. the KFD is loaded and `rocm-smi` talks
   to it).

### Other sanity checks

```bash
# VRAM total must equal the BIOS carve-out (96 GiB):
rocm-smi --showmeminfo vram
# GPU[0] : VRAM Total Memory (B): 103079215104

# HIP compiler present and identifies itself:
hipcc --version          # HIP version: 7.2.x
hipconfig -l             # -> clang
hipconfig -R             # -> /opt/rocm

# A trivial HIP build works:
echo 'int main(){return 0;}' > t.hip && hipcc t.hip -o /tmp/t && /tmp/t && echo HIP-OK
```

### Confirming gfx1151 is *really* recognized

The definitive test is building one HIP kernel for gfx1151 and running it —
which is what the llama.cpp build in the next chapter does. If
`rocminfo` shows gfx1151 but a build fails with "unsupported GPU target",
the installed HIP toolchain (not the driver) is too old: re-run
`amdgpu-install` and make sure it pulled the **7.2.4** repo, not 6.x.

## 5. hipBLASLt note

We use hipBLASLt (the Tensile-based GEMM backend) for matrix kernels at
runtime — it ships with the stack above (`hipblas-dev`, `rocblas`). Set
`ROCBLAS_USE_HIPBLASLT=1` in the launch environment for noticeably better
quantized GEMM performance on gfx1151 (see
[launch-flags.md](launch-flags.md#environment-variables)).

Next: [llama-cpp-build.md](llama-cpp-build.md).
