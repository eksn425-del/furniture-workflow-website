"""Self-contained Lux3D image-to-3D modeling provider for the Website runtime.

Mirrors the field-proven protocol used by the mature Furniture Workflow
(scraper skill `batch_lux3d_submit.py`) so the Website control plane can
perform modeling itself without importing the Skills package:

- create:  POST {base}/lux3d/v1/generate/img-to-3d/task/create
- poll:    GET  {base}/lux3d/v1/generate/task/get?taskid=<id>
- download:GET  {base}/lux3d/v1/generate/task/download?taskid=<id>&format=glb

Headers: `Authorization: <api_key>`. Auth is the raw API key, not `Bearer`.

Safety notes kept identical to the mature worker:
- A confirmed capacity/"in progress" rejection means no task was created, safe
  to retry with backoff.
- A network/timeout/TLS error during create leaves billing outcome unknown:
  we record it as non-retryable to avoid double billing.

`http` is injectable for deterministic tests; it defaults to the real
`requests` library. Do not touch environment variables or network here beyond
what this module owns.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

import requests


def img_to_datauri(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def is_capacity_rejection(err: str) -> bool:
    low = err.lower()
    return any(
        kw in low
        for kw in ("capacity", "busy", "in progress", "进行中", "等待", "concurrency", "429", "503")
    )


def normalize_slug(value: str) -> str:
    """Sanitize a SKU/name into a safe GLB file stem (keep a trailing '01' style)."""
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", str(value).strip()) or "product"
    return text[:160].strip("._-")


class Lux3DClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        version: str,
        face_count: int,
        interval: float,
        max_attempts: int,
        http: Any = requests,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": api_key, "Content-Type": "application/json"}
        self.version = version
        self.face_count = int(face_count)
        self.interval = float(interval)
        self.max_attempts = int(max_attempts)
        self._http = http

    def create_task(self, image_path: Path, *, idempotency_key: str | None = None) -> tuple[str | None, str | None]:
        """Returns (task_id, err). err non-empty -> task not safely retryable unless capacity."""
        payload = {
            "version": self.version,
            "faceCount": self.face_count,
            "img": img_to_datauri(image_path.read_bytes(), mime_for(image_path)),
        }
        try:
            headers = dict(self.headers)
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            response = self._http.post(
                f"{self.base_url}/lux3d/v1/generate/img-to-3d/task/create",
                json=payload, headers=headers, timeout=120,
            )
            body = response.json()
        except Exception as exc:  # billing outcome unknown -> not safe to retry
            return None, f"create_http_error {exc}"
        code = body.get("c", body.get("code"))
        if code not in (0, "0", None, ""):
            message = str(body.get("m") or body.get("message") or code)
            return None, f"create_rejected c={code} m={message[:200]}"
        task_id = body.get("d")
        if task_id is None:
            return None, "create_no_task_id"
        return str(task_id), None

    def poll_task(self, task_id: str) -> tuple[dict | None, str | None]:
        for _ in range(self.max_attempts):
            time.sleep(self.interval)
            try:
                response = self._http.get(
                    f"{self.base_url}/lux3d/v1/generate/task/get",
                    params={"taskid": task_id}, headers=self.headers, timeout=60,
                )
                body = response.json()
            except Exception:
                continue
            data = body.get("d") if isinstance(body, dict) else None
            if isinstance(data, dict):
                status = data.get("status")
                if status == 3:
                    return data, None
                if status == 4:
                    return None, f"task_failed status=4 detail={json.dumps(data, ensure_ascii=False)[:300]}"
        return None, f"poll_timeout after {self.max_attempts} attempts"

    def download_glb(self, result: dict | None, task_id: str, out_path: Path) -> bool:
        if isinstance(result, dict):
            candidates = self._collect_urls(result)
            for url in sorted({u for _, u in candidates}, key=lambda u: (0 if ".glb" in u.lower() else 1, u)):
                if self._save(url, out_path):
                    return True
        try:
            response = self._http.get(
                f"{self.base_url}/lux3d/v1/generate/task/download",
                headers=self.headers, params={"taskid": task_id, "format": "glb"}, timeout=600,
            )
            if response.status_code == 200 and len(response.content) > 5000:
                out_path.write_bytes(response.content)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _collect_urls(node: Any, depth: int = 0) -> list[tuple[str, str]]:
        urls: list[tuple[str, str]] = []
        if depth > 8 or node is None:
            return urls
        if isinstance(node, Mapping):
            for k, v in node.items():
                if isinstance(v, str) and v.startswith("http"):
                    urls.append((str(k).lower(), v))
                else:
                    urls.extend(Lux3DClient._collect_urls(v, depth + 1))
        elif isinstance(node, list):
            for x in node:
                urls.extend(Lux3DClient._collect_urls(x, depth + 1))
        return urls

    def _save(self, url: str, out_path: Path) -> bool:
        try:
            response = self._http.get(url, timeout=600)
            if response.status_code == 200 and len(response.content) > 5000:
                out_path.write_bytes(response.content)
                return True
        except Exception:
            pass
        return False


def fetch_image_bytes(url: str, http: Any = requests, timeout: float = 60) -> bytes:
    response = http.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


class ModelingResult:
    def __init__(self, glb_dir: Path, manifest: list[dict[str, Any]], delivered: int, failed: int) -> None:
        self.glb_dir = glb_dir
        self.manifest = manifest
        self.delivered = delivered
        self.failed = failed


def run_modeling(
    products: list[dict[str, Any]],
    *,
    workspace: Path,
    client: Lux3DClient,
    emit: Callable[[str, str, str, int | None, int | None, dict[str, Any]], None],
    workers: int = 3,
    http: Any = requests,
) -> ModelingResult:
    """Model each verified product: image -> create -> poll -> download GLB.

    Writes GLBs to `<workspace>/04_lux3d/glb/<sku>.glb` and returns a manifest
    plus counts. Emits PROVIDER_SUBMITTED / PROVIDER_SUCCESS / PROVIDER_FAILED
    progress events via `emit`.
    """
    glb_dir = workspace / "04_lux3d" / "glb"
    glb_dir.mkdir(parents=True, exist_ok=True)
    total = len(products)
    manifest: list[dict[str, Any]] = []
    lock_results: list[tuple[int, dict[str, Any]]] = []

    def process(index: int, product: dict[str, Any]) -> dict[str, Any]:
        sku = product.get("item_number") or product.get("source_key") or product.get("canonical_url") or ""
        stem = normalize_slug(sku)
        out_path = glb_dir / f"{stem}.glb"
        entry: dict[str, Any] = {
            "index": index,
            "sku": sku,
            "title": product.get("product_title", ""),
            "canonical_url": product.get("canonical_url", ""),
            "status": "failed",
        }
        if out_path.is_file() and out_path.stat().st_size > 5000:
            entry.update(status="skipped", glb=str(out_path), size=out_path.stat().st_size)
            return entry

        image_url = product.get("image_url", "")
        if not image_url:
            return entry
        try:
            raw = fetch_image_bytes(image_url, http=http)
            image_path = workspace / "04_lux3d" / "_images" / f"{stem}.{('png','jpg')[0]}"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(raw)
        except Exception as exc:
            entry["error"] = f"image_fetch {exc}"
            return entry

        task_id, err = client.create_task(image_path)
        if err:
            entry["error"] = err
            return entry
        emit("PROVIDER_SUBMITTED", "MODELING", f"建模已提交 {stem}", done=index + 1, total=total, payload={"sku": sku, "task_id": task_id})
        result, perr = client.poll_task(task_id)
        if perr:
            entry["error"] = perr
            return entry
        if not client.download_glb(result, task_id, out_path):
            entry["error"] = "download_failed"
            return entry
        digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
        entry.update(status="delivered", glb=str(out_path), size=out_path.stat().st_size, sha256=digest)
        return entry

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, index, product): (index, product) for index, product in enumerate(products)}
        for future in as_completed(futures):
            index, product = futures[future]
            entry = future.result()
            lock_results.append((index, entry))
            if entry["status"] == "delivered":
                sku = entry["sku"]
                emit("PROVIDER_SUCCESS", "MODELING", f"建模成功 {sku}", done=0, total=total, payload={"sku": sku, "glb": entry.get("glb")})
            elif entry["status"] != "skipped":
                emit("PROVIDER_FAILED", "MODELING", f"建模失败 {entry['sku']}: {entry.get('error','')[:120]}", done=0, total=total, payload={"sku": entry["sku"], "error": entry.get("error", "")})

    delivered = sum(1 for _, e in lock_results if e["status"] == "delivered")
    skipped = sum(1 for _, e in lock_results if e["status"] == "skipped")
    failed = total - delivered - skipped
    lock_results.sort(key=lambda pair: pair[0])
    manifest = [entry for _, entry in lock_results]
    for entry in manifest:
        if entry.get("glb"):
            entry["glb"] = str(glb_dir)  # keep relative manifest stable
    return ModelingResult(glb_dir=glb_dir, manifest=manifest, delivered=delivered + skipped, failed=failed)
