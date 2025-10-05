from __future__ import annotations

import tempfile
import threading
import time
import uuid
from argparse import ArgumentParser
from pathlib import Path
from typing import *

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from uesave.uesave import (ArrayProperty, Property, StructProperty,
                           load_savefile)

UPLOAD_ROOT = Path(tempfile.gettempdir()) / "uesave_uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
CLEAN_INTERVAL_SECONDS = 300  # every 5 minutes
FILE_TTL_SECONDS = 1800  # 30 minutes


def _clean_loop() -> None:
    while True:
        try:
            now = time.time()
            for p in UPLOAD_ROOT.glob("*"):
                try:
                    if not p.is_file():
                        continue
                    age = now - p.stat().st_mtime
                    if age > FILE_TTL_SECONDS:
                        p.unlink(missing_ok=True)
                except Exception:
                    # best-effort cleanup; ignore any file-level errors
                    pass
        except Exception:
            # keep the loop alive on any unexpected error
            pass
        time.sleep(CLEAN_INTERVAL_SECONDS)


def _ensure_cleaner_started(app: FastAPI) -> None:
    # Start a background daemon thread once
    if not getattr(app.state, "_cleaner_started", False):
        t = threading.Thread(target=_clean_loop,
                             name="uesave-cleaner", daemon=True)
        t.start()
        app.state._cleaner_started = True


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="UE Save Inspector", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _sanitize_filename(name: str) -> str:
    keep = [c for c in name if c.isalnum() or c in (".", "_", "-")]
    sanitized = "".join(keep) or "upload.sav"
    return sanitized[-100:]


def _format_prop_value(prop: Property) -> Optional[str]:
    """Return a concise, human-friendly value preview for leaf properties.
    If the property has children (e.g., Struct or Array of Structs), return None.
    """
    ptype = prop.__class__.__name__

    # Structs are non-leaf; handled as children elsewhere
    if isinstance(prop, StructProperty):
        return None

    # Arrays
    if isinstance(prop, ArrayProperty):
        if prop.inner_type == "ByteProperty":
            # Represent as hex preview
            v = prop.value or {}
            vals = v.get("__values", [])
            try:
                b = bytes(vals)
            except Exception:
                b = bytes([int(x) & 0xFF for x in vals]) if vals else b""
            n = len(b)
            preview = b[:32].hex(" ")
            more = f" +{n-32}b" if n > 32 else ""
            return f"{n} bytes: {preview}{more}" if n else "0 bytes"
        elif prop.inner_type == "StructProperty":
            # children will be expanded; no single value
            return None
        else:
            # generic array summary
            try:
                length = len(prop)
            except Exception:
                length = 0
            return f"Array<{prop.inner_type}> with {length} item(s)"

    # MapProperty and TextProperty use their value payloads for a summary
    if ptype == "MapProperty":
        v = getattr(prop, "value", {}) or {}
        k = v.get("__key_type", "?")
        val = v.get("__value_type", "?")
        raw = v.get("__raw", b"")
        size = len(raw) if isinstance(raw, (bytes, bytearray)) else 0
        return f"Map<{k}, {val}> raw {size} byte(s)"

    if ptype == "TextProperty":
        v = getattr(prop, "value", b"")
        if isinstance(v, (bytes, bytearray)):
            try:
                s = v.decode("utf-8", errors="ignore").strip()
            except Exception:
                s = ""
            if s:
                if len(s) > 200:
                    s = s[:200] + "…"
                return f'"{s}"'
            # fallback to hex/size
            n = len(v)
            preview = bytes(v[:32]).hex(" ")
            more = f" +{n-32}b" if n > 32 else ""
            return f"<Text bytes {n}: {preview}{more}>"
        return str(v)

    # Primitive leaves: bool/int/float/strings
    try:
        val = prop.value
    except Exception:
        return None

    if isinstance(val, str):
        s = val
        if len(s) > 200:
            s = s[:200] + "…"
        return f'"{s}"'
    if isinstance(val, (int, float, bool)):
        return str(val)
    # Fallback generic repr
    try:
        return str(val)
    except Exception:
        return None


def prop_to_tree_node(prop: Property) -> Dict[str, Any]:
    ptype = prop.__class__.__name__
    name = getattr(prop, "name", ptype)
    meta: str = ""
    children: List[Dict[str, Any]] = []

    if isinstance(prop, StructProperty):
        meta = f"{len(prop.fields)} field(s)"
        children = [prop_to_tree_node(f) for f in prop.fields]
    elif isinstance(prop, ArrayProperty):
        if prop.inner_type == "ByteProperty":
            meta = f"{len(prop)} bytes"
            children = []
        elif prop.inner_type == "StructProperty":
            meta = f"{len(prop)} struct(s)"
            children = [prop_to_tree_node(prop[i]) for i in range(len(prop))]
        else:
            meta = f"Array<{prop.inner_type}> x {len(prop)}"
            children = []

    value = None
    if not children:
        value = _format_prop_value(prop)

    return {
        "name": name,
        "type": ptype,
        "meta": meta,
        "children": children if children else None,
        "value": value,
    }


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    if not file.filename.lower().endswith(".sav"):
        raise HTTPException(
            status_code=400, detail="Please upload a .sav file")

    safe_name = _sanitize_filename(file.filename)
    unique = f"{int(time.time())}_{uuid.uuid4().hex}_{safe_name}"
    dest = UPLOAD_ROOT / unique

    try:
        with dest.open('wb') as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to save file: {e}")
    finally:
        await file.close()

    try:
        save = load_savefile(dest)
        nodes = [prop_to_tree_node(p) for p in save.properties]
        return JSONResponse({
            "header": save.header,
            "version": save.version,
            "properties": nodes,
            "uploaded_path": str(dest),
        })
    except Exception as e:
        # On failure, include a short error and leave the file for debugging; cleaner will purge later.
        raise HTTPException(status_code=400, detail=f"Parse error: {e}")


def main() -> None:
    parser = ArgumentParser(prog="uesave_webapp",
                            description="uesave Web App")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Port to bind (default: 8000)")
    args = parser.parse_args()

    import uvicorn  # imported here so fastapi/uvicorn stay optional unless webapp is used
    uvicorn.run("uesave.webapp:app", host=args.host,
                port=args.port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
