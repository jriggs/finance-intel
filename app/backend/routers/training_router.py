"""Training / fine-tuning endpoints — LoRA jobs managed by the trainer subprocess."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from trainer import (
    get_log as trainer_log,
    get_status as trainer_status,
    list_adapters,
    list_datasets,
    save_dataset_file,
    start_training,
    stop_training,
)

router = APIRouter()


class TrainRequest(BaseModel):
    dataset_name: str
    model_id: str = "mistralai/Mistral-7B-Instruct-v0.2"
    iters: int = 500
    batch_size: int = 4
    learning_rate: float = 1e-4
    lora_rank: int = 8
    lora_layers: int = 16
    max_seq_len: int = 2048


class DatasetUploadRequest(BaseModel):
    name: str
    content: str        # Raw file content as string
    fmt: str = "jsonl"  # "jsonl", "csv", or "txt"


@router.get("/api/train/status")
async def train_status():
    return trainer_status()


@router.get("/api/train/log")
async def train_log(last_n: int = 100):
    return {"log": trainer_log(last_n)}


@router.get("/api/train/datasets")
async def get_datasets():
    return {"datasets": list_datasets()}


@router.get("/api/train/adapters")
async def get_adapters():
    return {"adapters": list_adapters()}


@router.post("/api/train/datasets/upload")
async def upload_dataset(req: DatasetUploadRequest):
    if len(req.content.encode()) > 20 * 1024 * 1024:
        raise HTTPException(413, "Dataset exceeds 20MB limit")
    result = save_dataset_file(req.name, req.content, req.fmt)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/train/start")
async def start_train(req: TrainRequest):
    result = start_training(
        dataset_name=req.dataset_name,
        model_id=req.model_id,
        iters=req.iters,
        batch_size=req.batch_size,
        learning_rate=req.learning_rate,
        lora_rank=req.lora_rank,
        lora_layers=req.lora_layers,
        max_seq_len=req.max_seq_len,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/train/stop")
async def stop_train():
    return stop_training()


@router.get("/api/train/stream")
async def train_stream():
    """SSE stream of live training log lines."""
    async def _stream():
        seen = 0
        while True:
            status   = trainer_status()
            logs     = trainer_log(200)
            new_lines = logs[seen:]
            for line in new_lines:
                yield f"data: {json.dumps(line)}\n\n"
            seen = len(logs)
            if status.get("status") in ("done", "error", "stopped", "idle"):
                yield f"data: {json.dumps({'event': 'end', 'status': status})}\n\n"
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
