# Runbook: a local model is refused or runs out of GPU memory

The render box has one GPU (an RTX 5090, 32 GB) shared by 3ds Max, V-Ray, Vantage and PLATE's local models. PLATE protects the render: it asks the GPU how much memory is free before it loads a model and refuses if the answer is too small, rather than crashing in the middle of someone's render.

## Symptoms

- The pixels worker answers **HTTP 503** with a reason: the GPU cannot take the model now.
- A local engine run (Qwen-Image-Edit, Wan 2.2 previs, Real-ESRGAN for AI layers) ends FAILED with a CUDA out-of-memory message.
- Max or V-Ray itself reports out of memory while a local model is loaded.

## What the admission rules are

- The worker asks NVML for free memory and keeps a reserve (2 GB by default) for everything else.
- **With no NVML the headroom cannot be measured, so admission is refused** unless `PLATE_ALLOW_UNMANAGED_GPU=1` says you accept an unmanaged GPU. (On the Mac there is no NVML; local models are stubs there.)
- The heavy generative models (Qwen-Image-Edit, Wan 2.2) run only inside a scheduled window, `PLATE_GPU_WINDOWS` (for example `01:00-06:00,13:00-14:00`), or with `PLATE_FORCE_LOCAL=1`.
- A model sits on the GPU only for the duration of a job (a slot loads on enter and unloads on exit, flushing the CUDA cache).

Ask the worker directly: `POST http://127.0.0.1:4611/admission` with `{"required_mb": 24000, "model": "wan2.2"}` and the bearer token from `config.json`. It returns `ok`, a `reason` and a snapshot of free and total memory.

## What to do

1. **Look at the GPU** (`nvidia-smi`). Is Vantage or a V-Ray GPU render running? Is a Max viewport using Nano-sized textures? A render in progress always wins: wait for it, or run the local job in the window.
2. **Outside the window?** Either wait for it, change `PLATE_GPU_WINDOWS` (restart the worker), or set `PLATE_FORCE_LOCAL=1` for one session when nothing else is rendering.
3. **Memory not released after a failed job?** Restart the pixels worker (`make pixels`, or `python -m plate_pixels.server`); that frees everything it held. Restarting Max is the last resort.
4. **Make the job smaller.** For stills, turn on **region crop** in the Make bar (the mask's bounding box plus context instead of the whole frame). For previs, run the 480p draft, not the 1080p pass. Never upscale the plate to get a bigger model input.
5. **Use another route if custody allows.** On a non-NDA project a cloud engine for the same task may be quoted instead; on an NDA project the local engine is the only one, so the answer is the window or a render.
6. If it keeps happening at the same size, write down the model, the resolution and what else was running, and raise it: the admission reserve or the window schedule needs changing, not the code.

## Never

Never set `PLATE_ALLOW_UNMANAGED_GPU=1` or `PLATE_FORCE_LOCAL=1` in the daemon's permanent environment: both exist for a deliberate one-off, not as a default. And never run a heavy local model during a V-Ray GPU delivery render.
