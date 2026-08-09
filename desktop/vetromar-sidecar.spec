# PyInstaller spec — bundles the Vetromar engine (FastAPI API + full capture
# pipeline) into a self-contained `onedir` binary the Tauri app ships as a
# resource. Built via `pyinstaller vetromar-sidecar.spec` (see build.sh).
#
# The heavy ML libs (torch/whisperx/pyannote/…) load submodules and data files
# dynamically, so PyInstaller can't see them by static analysis — we pull each
# in wholesale with collect_all. Missing one surfaces at runtime as a capture
# failure, not a crash, because the pipeline imports them lazily.

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

datas = []
binaries = []
hiddenimports = []

# --- package METADATA (.dist-info) ------------------------------------------
# collect_all bundles modules but NOT their importlib.metadata records. Several
# ML libs look up `importlib.metadata.version("<pkg>")` at import time (e.g.
# transformers/audio_utils.py does it for torchcodec); a missing .dist-info
# raises PackageNotFoundError, which transformers rethrows as the misleading
# "Could not import module 'Pipeline'". Ship metadata for the whole chain.
_METADATA = [
    "torch", "torchaudio", "torchcodec", "transformers", "tokenizers",
    "huggingface_hub", "safetensors", "accelerate", "sentencepiece",
    "numpy", "tqdm", "regex", "requests", "packaging", "filelock", "pyyaml",
    "whisperx", "faster_whisper", "ctranslate2", "pyannote.audio",
    "speechbrain", "asteroid_filterbanks", "lightning", "pytorch_lightning",
    "scipy", "scikit-learn", "pandas", "av", "soundfile", "librosa", "soxr",
    "onnxruntime",
    # Retrieval layer (engine v2): ONNX embeddings + SQL vector search.
    "fastembed", "sqlite-vec", "mmh3", "py-rust-stemmers", "loguru",
]
for pkg in _METADATA:
    try:
        datas += copy_metadata(pkg, recursive=True)
    except Exception as exc:  # not installed on this host → nothing to copy
        print(f"[spec] no metadata for {pkg}: {exc}")

# Full collection for libraries with dynamic imports / bundled data.
_HEAVY = [
    "torch",
    "torchaudio",
    "whisperx",
    "faster_whisper",
    "ctranslate2",
    "pyannote",
    "pyannote.audio",
    "speechbrain",
    "asteroid_filterbanks",
    "lightning",
    "lightning_fabric",
    "pytorch_lightning",
    "transformers",
    "huggingface_hub",
    "sklearn",
    "scipy",
    "pandas",
    "av",
    "soundfile",
    "sounddevice",
    "uvicorn",
    "fastapi",
    "anthropic",
    "openai",
    "ollama",
    # MCP SDK — both halves ride on it: the sources sync client (M10) and the
    # `serve` stdio server the vetromar-mcp shim execs on customer machines.
    "mcp",
    # Retrieval layer (engine v2). sqlite_vec ships its loadable extension as
    # package data; fastembed pulls onnxruntime + tokenizers.
    "fastembed",
    "sqlite_vec",
    "onnxruntime",
    "tokenizers",
]
for pkg in _HEAVY:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # a lib not installed on this build host is skipped
        print(f"[spec] skipping {pkg}: {exc}")

# The engine's own package data (store/schema.sql) and every submodule.
datas += collect_data_files("vetromar", includes=["**/*.sql"])
hiddenimports += collect_submodules("vetromar")

# uvicorn selects these at runtime by string; make them explicit.
hiddenimports += [
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.loops.auto",
]

a = Analysis(
    ["sidecar_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib.tests", "PySide6", "PyQt5"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vetromar-sidecar",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # stdout carries the `PORT=<n>` handshake to the Tauri shell
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="vetromar-sidecar",
)
