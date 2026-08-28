# llama.cpp Build (gfx1151 / ROCm 7.2.4)

Two builds live on the box:

| Path | Repo | Purpose |
|---|---|---|
| `~/llama.cpp` | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | Serves 9B / Hermes-35B / 27B / Fable-27B (ports 8080–8082) |
| `~/llama-engramhalo` | [Aristo94/EngramHalo.cpp](https://github.com/Aristo94/EngramHalo.cpp) | Serves Qwen3.8-Flash-Next (qwen4exp arch) — see [the fork section](#engramhalocpp--the-qwen38-flash-next-fork) |

Both are built with the same cmake invocation.

## System dependencies

```bash
sudo apt install -y build-essential git cmake ninja-build libomp-dev libcurl4-openssl-dev
```

(Plus the ROCm packages from [rocm-setup.md](rocm-setup.md) — `hipcc`,
`hipblas-dev`, `hipblaslt` must already be installed.)

## The build command

```bash
cd ~/llama.cpp

HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DGGML_HIP=ON \
  -DAMDGPU_TARGETS=gfx1151 \
  -DGGML_HIP_NO_VMM=ON \
  -DGGML_HIP_MMQ_MFMA=ON \
  -DCMAKE_C_COMPILER=/opt/rocm/bin/hipcc \
  -DCMAKE_CXX_COMPILER=/opt/rocm/bin/hipcc

cmake --build build -j"$(nproc)" --target llama-server llama-cli llama-bench
```

The environment prefix (`HIPCXX=...`, `HIP_PATH=...`) just tells cmake where
the HIP toolchain lives; `hipconfig -l` prints `clang` and `hipconfig -R`
prints `/opt/rocm`, so it expands to `HIPCXX=/opt/rocm/lib/llvm/bin/clang
HIP_PATH=/opt/rocm`. Equivalent to hardcoding them; it just survives ROCm
upgrades.

### Flag by flag

| Flag | What it does | Why we set it |
|---|---|---|
| `GGML_HIP=ON` | Enables the AMD GPU backend: ggml's compute backend on top of ROCm HIP (AMD's CUDA-equivalent runtime). | Without it, GGML only builds CPU (and Vulkan/CUDA backends). This is *the* flag that turns on AMD GPU support. |
| `AMDGPU_TARGETS=gfx1151` | Restricts the fat binary to exactly the Strix Halo RDNA 3.5 iGPU ISA. | Strix Halo's gfx1151 is brand-new silicon; the default target list omits it (or builds only `gfx11-generic`, which is slow or missing kernels). Building only for `gfx1151` keeps compile times sane and guarantees the fast, native kernels are the ones loaded. Do **not** substitute `GPU_TARGETS=` (the old name) — current cmake uses `AMDGPU_TARGETS`. |
| `GGML_HIP_NO_VMM=ON` | Disables GGML's HIP **V**irtual **M**emory **M**anager (the `cuMemCreate`-style sparse virtual-memory allocator). | On gfx1151 the VMM path is unstable — allocations through the VMM path fault / hang under the huge multi-tensor allocations these models make (and interact badly with the 96 GiB carve-out + GTT mapping). Disabling it falls back to plain `hipMalloc` allocations, which are reliable on this chip. |
| `GGML_HIP_MMQ_MFMA=ON` | Routes quantized matrix-multiply (MMQ) kernels through **MFMA** (Matrix Core / MFMA instructions) instead of the generic dot-product path. | Big prefill/decode speedup for Q4/IQ4 quants on RDNA 3.x matrix cores. Without it, quantized inference on gfx1151 runs a much slower fallback GEMM. |
| `CMAKE_C_COMPILER` / `CMAKE_CXX_COMPILER` = `/opt/rocm/bin/hipcc` | Makes hipcc the compiler for *all* translation units, not just HIP kernels. | hipcc is a clang wrapper that passes the right `--offload-arch`/HIP flags; using it for host code too avoids the classic HIP+GCC ABI mismatch (libstdc++ symbol mangling) that breaks linking against hip-runtime. |
| `CMAKE_BUILD_TYPE=Release` | `-O3`, no debug info. | Obvious. |

`GGML_HIP_GRAPHS` is left at its default **ON**: decode steps replay a
captured GPU graph ("graphs reused" in the log — see
[monitoring.md](monitoring.md)) instead of re-launching the ~5k-node kernel
graph per token.

### Resulting build cache (reference)

From `build/CMakeCache.txt` on the live box — if yours differs, you built
with different flags:

```
AMDGPU_TARGETS:UNINITIALIZED=gfx1151
CMAKE_BUILD_TYPE:STRING=Release
CMAKE_C_COMPILER:STRING=/opt/rocm/bin/hipcc
CMAKE_CXX_COMPILER:STRING=/opt/rocm/bin/hipcc
GGML_HIP:BOOL=ON
GGML_HIP_GRAPHS:BOOL=ON
GGML_HIP_MMQ_MFMA:BOOL=ON
GGML_HIP_NO_VMM:BOOL=ON
```

### Smoke test

```bash
./build/bin/llama-cli --version
# ggml_build for x86_64 ... HIP   ← must mention HIP
# Run anything tiny and watch for "offloaded N/N layers to GPU" in the log.
```

## Updating the build

```bash
cd ~/llama.cpp
git pull
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1151 \
  -DGGML_HIP_NO_VMM=ON -DGGML_HIP_MMQ_MFMA=ON \
  -DCMAKE_C_COMPILER=/opt/rocm/bin/hipcc -DCMAKE_CXX_COMPILER=/opt/rocm/bin/hipcc \
  && cmake --build build --target llama-server llama-bench -j"$(nproc)" 2>&1 | tee build.log
```

(`tee build.log` because a full rebuild of the HIP kernels takes a long time
and you want the error trail if it dies.)

## EngramHalo.cpp — the Qwen3.8-Flash-Next fork

```bash
git clone https://github.com/Aristo94/EngramHalo.cpp ~/llama-engramhalo
```

### Why the fork exists

Qwen3.8-Flash-Next uses the **qwen4exp** architecture — experimental
features that upstream llama.cpp does not (fully) support:

- **QSA sparse attention.** The model uses QSA (query-sparse-attention)
  sparse attention: decode should *gather* the 2048 top-k selected KV rows.
  Upstream ran this "sparse" attention **dense, with a mask** — paying full
  KV bandwidth at any depth. The fork implements the true gather
  (env-gated `LLAMA_QSA_GATHER`; on by default from 16K context).
- **MTP draft head.** A working MTP speculative-decoding draft head for this
  arch (upstream's MTP head support doesn't cover it); a prebuilt Q8_0 sidecar
  exists on HF
  ([EasiiX/Qwen3.8-Flash-Next-MTP-Strix-Halo-GGUF](https://huggingface.co/EasiiX/Qwen3.8-Flash-Next-MTP-Strix-Halo-GGUF)).
- **Engram table on SSD.** The model's ~27 GB engram table is kept
  memory-mapped on NVMe (~1.2 GiB resident) instead of eating VRAM.
- **gfx1151 kernel patches** for the qwen4exp attention shape (head-dim 256,
  GQA 2, q8_0 KV), and decode-graph reuse instead of a full graph rebuild per
  token.

The fork sits on the `strix-halo-qwen4exp` branch of
[ggml-org/llama.cpp PR #27742](https://github.com/ggml-org/llama.cpp/pull/27742)
lineage. Measured effect on this box: **23.5 → 39.3 tok/s** on code, roughly
doubled depth performance, perplexity delta 0.03%.

> **QSA gather caveat (applies to `--parallel`):** multi-sequence ubatches
> (`--parallel 2`+) also take the gather path on HIP and are not validated
> there — always serve Flash-Next with `--parallel 1`. See
> [launch-flags.md](launch-flags.md).

### Building the fork

Identical flags to stock llama.cpp — the fork is a llama.cpp branch, so ROCm
builds the same way:

```bash
cd ~/llama-engramhalo

HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DGGML_HIP=ON \
  -DAMDGPU_TARGETS=gfx1151 \
  -DGGML_HIP_NO_VMM=ON \
  -DGGML_HIP_MMQ_MFMA=ON \
  -DCMAKE_C_COMPILER=/opt/rocm/bin/hipcc \
  -DCMAKE_CXX_COMPILER=/opt/rocm/bin/hipcc

cmake --build build -j"$(nproc)" --target llama-server
```

Its own `CMakeCache.txt` matches the stock one (`GGML_HIP=ON`,
`AMDGPU_TARGETS=gfx1151`, NO_VMM, MMQ_MFMA, hipcc compilers). The fork's
`docs/strix-halo/README.md` has the full benchmark methodology and recommended
configs — worth reading before tuning Flash-Next.

Next: [models.md](models.md) to pull the weights these binaries load.
