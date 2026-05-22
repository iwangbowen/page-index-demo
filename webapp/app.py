import asyncio
import json
import sys
import uuid
from pathlib import Path

import aiofiles
import PyPDF2
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ── paths ──────────────────────────────────────────────────────────────────────
WEBAPP_DIR = Path(__file__).parent
ROOT = WEBAPP_DIR.parent
UPLOAD_DIR = WEBAPP_DIR / "uploads"
WORKSPACE_DIR = WEBAPP_DIR / "workspace"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Ensure pageindex package is importable
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from pageindex.client import PageIndexClient  # noqa: E402
from pageindex.utils import extract_json, llm_completion  # noqa: E402

# ── init ───────────────────────────────────────────────────────────────────────
client = PageIndexClient(workspace=str(WORKSPACE_DIR))

app = FastAPI(title="PageIndex Web")

# In-memory state (cleared on restart)
upload_registry: dict[str, dict] = (
    {}
)  # file_id → {filename, file_path, page_count}
indexing_tasks: dict[str, dict] = {}  # task_id → {status, doc_id, error}


# ── static ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(WEBAPP_DIR / "static" / "index.html")


# ── upload ─────────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 格式")

    file_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{file_id}.pdf"

    content = await file.read()
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    try:
        reader = PyPDF2.PdfReader(str(file_path))
        page_count = len(reader.pages)
    except Exception:
        page_count = 0

    upload_registry[file_id] = {
        "filename": file.filename,
        "file_path": str(file_path),
        "page_count": page_count,
    }

    return {
        "file_id": file_id,
        "filename": file.filename,
        "page_count": page_count,
        "preview_url": f"/api/files/{file_id}",
    }


@app.get("/api/files/{file_id}")
async def get_uploaded_file(file_id: str):
    if file_id in upload_registry:
        fp = upload_registry[file_id]["file_path"]
    else:
        fp = str(UPLOAD_DIR / f"{file_id}.pdf")
        if not Path(fp).exists():
            raise HTTPException(404, "文件不存在")
    return FileResponse(
        fp,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@app.get("/api/pdf/{doc_id}")
async def get_doc_pdf(doc_id: str):
    """Serve the original PDF for an indexed document."""
    doc = client.documents.get(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    fp = doc.get("path")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "原始 PDF 文件不存在")
    return FileResponse(
        fp,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


# ── indexing ───────────────────────────────────────────────────────────────────
class IndexRequest(BaseModel):
    file_id: str


@app.post("/api/index")
async def start_index(req: IndexRequest):
    info = upload_registry.get(req.file_id)
    if not info:
        # Fallback: server may have reloaded, check disk directly
        file_path = UPLOAD_DIR / f"{req.file_id}.pdf"
        if not file_path.exists():
            raise HTTPException(404, "文件不存在，请重新上传")
        try:
            reader = PyPDF2.PdfReader(str(file_path))
            page_count = len(reader.pages)
        except Exception:
            page_count = 0
        info = {
            "filename": file_path.name,  # fallback, 但优先用 upload_registry
            "file_path": str(file_path),
            "page_count": page_count,
        }
        upload_registry[req.file_id] = info  # Restore to registry

    task_id = str(uuid.uuid4())
    indexing_tasks[task_id] = {
        "status": "pending",
        "doc_id": None,
        "error": None,
    }

    async def _do_index():
        try:
            indexing_tasks[task_id]["status"] = "indexing"
            # 传递原始文件名
            doc_id = await asyncio.to_thread(
                client.index, info["file_path"], "auto", info.get("filename")
            )
            indexing_tasks[task_id]["status"] = "done"
            indexing_tasks[task_id]["doc_id"] = doc_id
        except Exception as exc:
            import traceback

            traceback.print_exc()
            indexing_tasks[task_id]["status"] = "error"
            indexing_tasks[task_id]["error"] = str(exc)

    # Store task reference to prevent garbage collection
    task = asyncio.create_task(_do_index())
    indexing_tasks[task_id]["_task"] = task
    return {"task_id": task_id}


@app.get("/api/index-status/{task_id}")
async def get_index_status(task_id: str):
    task = indexing_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    # Exclude non-serializable asyncio Task object
    result = {k: v for k, v in task.items() if k != "_task"}
    if task["status"] == "done" and task["doc_id"]:
        doc = client.documents.get(task["doc_id"], {})
        result["filename"] = doc.get("filename") or doc.get("doc_name", "")
        result["doc_name"] = doc.get("doc_name", "")
        result["page_count"] = doc.get("page_count", 0)
    return result


# ── documents ──────────────────────────────────────────────────────────────────
@app.get("/api/documents")
async def list_documents():
    docs_out = []
    for doc_id, doc in client.documents.items():
        filename = doc.get("filename")
        if not filename:
            full_path = WORKSPACE_DIR / f"{doc_id}.json"
            if full_path.exists():
                try:
                    async with aiofiles.open(
                        full_path, "r", encoding="utf-8"
                    ) as f:
                        full = json.loads(await f.read())
                    filename = full.get("filename")
                    if filename:
                        doc["filename"] = filename
                except Exception:
                    pass
        docs_out.append(
            {
                "doc_id": doc_id,
                "filename": filename or doc.get("doc_name", ""),
                "doc_name": doc.get("doc_name", ""),
                "type": doc.get("type", ""),
                "page_count": doc.get("page_count", 0),
            }
        )
    return docs_out


# ── doc structure API ──
@app.get("/api/doc-structure/{doc_id}")
async def get_doc_structure(doc_id: str):
    doc = client.documents.get(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    if hasattr(client, "_ensure_doc_loaded"):
        await asyncio.to_thread(client._ensure_doc_loaded, doc_id)
    return doc.get("structure") or []


@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str):
    if doc_id not in client.documents:
        raise HTTPException(404, "文档不存在")

    del client.documents[doc_id]

    doc_file = WORKSPACE_DIR / f"{doc_id}.json"
    if doc_file.exists():
        doc_file.unlink()

    meta_file = WORKSPACE_DIR / "_meta.json"
    if meta_file.exists():
        try:
            async with aiofiles.open(meta_file, "r", encoding="utf-8") as f:
                meta = json.loads(await f.read())
            meta.pop(doc_id, None)
            async with aiofiles.open(meta_file, "w", encoding="utf-8") as f:
                await f.write(json.dumps(meta, ensure_ascii=False, indent=2))
        except Exception:
            pass

    return {"status": "deleted"}


# ── chat (SSE) ─────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    doc_id: str
    question: str
    history: list = []


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.doc_id not in client.documents:
        raise HTTPException(404, "文档不存在")

    async def _generate():
        import litellm

        def _sse(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            yield _sse({"type": "status", "content": "正在定位相关页面..."})

            # ── Step 1: locate pages ──────────────────────────────────────────
            structure = await asyncio.to_thread(
                client.get_document_structure, req.doc_id
            )
            locate_prompt = (
                "你是文档检索助手。根据文档树形目录结构和用户问题，"
                "判断哪些页码范围最可能包含答案。\n"
                f"文档结构：\n{structure}\n\n"
                f"用户问题：{req.question}\n\n"
                "请只返回JSON（不要其他内容）："
                '{"pages": "起始页-结束页", "reason": "选择原因"}'
            )
            locate_result = await asyncio.to_thread(
                llm_completion, model=client.model, prompt=locate_prompt
            )
            locate_data = extract_json(locate_result) or {}
            pages = locate_data.get("pages", "1-5")
            reason = locate_data.get("reason", "")

            yield _sse({"type": "source", "pages": pages, "reason": reason})
            yield _sse(
                {"type": "status", "content": f"正在读取第 {pages} 页内容..."}
            )

            # ── Step 2: fetch page content ────────────────────────────────────
            page_content = await asyncio.to_thread(
                client.get_page_content, req.doc_id, pages
            )

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是专业的文档问答助手，根据提供的文档内容准确回答问题，"
                        "适当引用具体页码。"
                    ),
                }
            ]
            for h in req.history[-6:]:
                messages.append(h)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"文档内容（第{pages}页）：\n{page_content}\n\n"
                        f"问题：{req.question}"
                    ),
                }
            )

            # ── Step 3: stream answer ─────────────────────────────────────────
            response = await litellm.acompletion(
                model=client.retrieve_model,
                messages=messages,
                stream=True,
            )
            async for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield _sse({"type": "answer", "content": delta})

        except Exception as exc:
            import traceback

            traceback.print_exc()
            yield _sse({"type": "error", "content": str(exc)})

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
