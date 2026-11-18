"""FastAPI 应用：病区×周次阳性检验安全汇总的上传、预览、确认发布与查看。

流程：
    上传 CSV -> 校验/去重/冲突检测 -> 计算交叉表与隐藏方案
    -> 可发布：保存待确认预览 -> 网页预览安全表 -> 确认发布（原子写入）
    -> 不可发布：仅提示原因，不展示、不保存任何表格内容
确认发布后不再接受替换文件；发布结果存于 SQLite，重启后可查看。

修订核查（已发布页）：
    输入递增修订号 + 上传该修订的完整 CSV -> 按检验号去重、同号冲突整份拒绝
    -> 对照已发布表的病区/周次集合、公开格数值、行列合计
    -> 范围或公开值变化：撤下旧表（页面只显示撤下状态，不展示表格）
    -> 未变化：继续供研究组使用
    核查结论与最高修订号持久化；无效上传不改变现行状态。
"""

from __future__ import annotations

import os
import re

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .csv_import import CsvImportError, build_crosstab, parse_tests
from .db import Database
from .revision import compare_with_publication, content_digest
from .suppression import find_suppression

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(BASE_DIR), "data", "app.db")
)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def build_safe_payload(wards, weeks, counts, hidden, n_records, filename):
    """构造“安全表”payload：隐藏格不含真实计数（服务端也不再保留）。"""
    n_r, n_c = len(wards), len(weeks)
    cells = []
    for i in range(n_r):
        row = []
        for j in range(n_c):
            if (i, j) in hidden:
                row.append({"hidden": True, "count": None})
            else:
                row.append({"hidden": False, "count": counts[i][j]})
        cells.append(row)
    row_totals = [sum(counts[i]) for i in range(n_r)]
    col_totals = [sum(counts[i][j] for i in range(n_r)) for j in range(n_c)]
    return {
        "wards": wards,
        "weeks": weeks,
        "cells": cells,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "grand_total": sum(row_totals),
        "n_records": n_records,
        "n_hidden": len(hidden),
        "source_filename": filename,
    }


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="病区×周次阳性检验安全汇总发布", docs_url=None, redoc_url=None)
    db = Database(db_path or DEFAULT_DB_PATH)
    app.state.db = db

    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
              name="static")

    def render(request: Request, name: str, status: int = 200, **ctx):
        return templates.TemplateResponse(
            request, name, ctx, status_code=status
        )

    # ------------------------------------------------------------ 页面

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        if db.get_pending() is not None:
            return RedirectResponse("/preview", status_code=303)
        return RedirectResponse("/upload", status_code=303)

    @app.get("/upload", response_class=HTMLResponse)
    def upload_form(request: Request):
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        return render(request, "upload.html", errors=[], has_pending=db.get_pending() is not None)

    @app.post("/upload", response_class=HTMLResponse)
    async def upload(request: Request, file: UploadFile = File(...)):
        if db.get_publication() is not None:
            return render(request, "message.html", status=409,
                          title="已发布，不接受替换文件",
                          message="汇总表已确认发布。为保证发布结果不可篡改，"
                                  "系统不再接受新的上传或替换。")

        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return render(request, "upload.html", status=400,
                          errors=[f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"],
                          has_pending=db.get_pending() is not None)

        try:
            records = parse_tests(raw)
        except CsvImportError as exc:
            # 校验失败：整份拒绝，不改变任何已有状态
            return render(request, "upload.html", status=400,
                          errors=exc.errors,
                          has_pending=db.get_pending() is not None)

        wards, weeks, counts = build_crosstab(records)
        hidden, reasons = find_suppression(counts)
        if hidden is None:
            # 无法发布：不展示、不保存任何表格内容（无部分公开结果）
            return render(request, "message.html", status=422,
                          title="无法发布",
                          message="该数据在当前保密规则下无法安全发布。",
                          reasons=reasons)

        payload = build_safe_payload(
            wards, weeks, counts, hidden, len(records), file.filename or "upload.csv"
        )
        db.set_pending(payload)
        return RedirectResponse("/preview", status_code=303)

    @app.get("/preview", response_class=HTMLResponse)
    def preview(request: Request):
        if db.get_publication() is not None:
            return RedirectResponse("/published", status_code=303)
        pending = db.get_pending()
        if pending is None:
            return RedirectResponse("/upload", status_code=303)
        return render(request, "preview.html", t=pending)

    @app.post("/confirm", response_class=HTMLResponse)
    def confirm(request: Request):
        pending = db.get_pending()
        if pending is None:
            if db.get_publication() is not None:
                return RedirectResponse("/published", status_code=303)
            return RedirectResponse("/upload", status_code=303)
        payload = dict(pending)
        payload.pop("created_at", None)
        if not db.publish(payload):
            return render(request, "message.html", status=409,
                          title="已发布，不接受替换文件",
                          message="汇总表已确认发布，本次确认被拒绝。")
        return RedirectResponse("/published", status_code=303)

    @app.post("/discard", response_class=HTMLResponse)
    def discard(request: Request):
        if db.get_publication() is None:
            db.delete_pending()
        return RedirectResponse("/upload", status_code=303)

    @app.get("/published", response_class=HTMLResponse)
    def published(request: Request):
        pub = db.get_publication()
        if pub is None:
            return RedirectResponse("/upload", status_code=303)
        return render(request, "published.html", t=pub,
                      rev=db.get_revision_check(), result=None, errors=[])

    @app.post("/revision-check", response_class=HTMLResponse)
    async def revision_check(
        request: Request,
        revision: str | None = Form(None),
        file: UploadFile | None = File(None),
    ):
        """核对修订后的完整 CSV 与已发布表；结论与最高修订号持久化。"""
        pub = db.get_publication()
        if pub is None:
            return RedirectResponse("/upload", status_code=303)

        def fail(status: int, errors: list[str], state=None):
            # 无效上传：只提示，不改变现行核查状态
            return render(request, "published.html", status=status, t=pub,
                          rev=db.get_revision_check() if state is None else state,
                          result=None, errors=errors)

        rev_text = (revision or "").strip()
        if not re.fullmatch(r"\d+", rev_text) or int(rev_text) < 1:
            return fail(400, ["修订号须为大于等于 1 的整数"])
        rev_no = int(rev_text)

        if file is None:
            return fail(400, ["请选择该修订的完整检验 CSV 文件"])
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return fail(400, [
                f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"
            ])

        try:
            records = parse_tests(raw, enforce_coverage=False)
        except CsvImportError as exc:
            return fail(400, exc.errors)

        wards, weeks, counts = build_crosstab(records)
        digest = content_digest(records)
        reasons = compare_with_publication(pub, wards, weeks, counts)
        outcome = "withdrawn" if reasons else "unchanged"
        reason_text = "；".join(reasons) if reasons else None

        status, current = db.record_revision_check(
            rev_no, digest, outcome, reason_text
        )
        if status == "same_revision":
            return fail(409, [
                f"修订号 {rev_no} 已有不同文件完成核查；"
                "同一修订号必须是同一份文件，本次上传未改变任何状态。"
            ], state=current)
        if status == "stale":
            return fail(409, [
                f"修订号 {rev_no} 小于已核查的最高修订号 "
                f"{current['max_revision']}；"
                "较旧修订不能覆盖较新结论，本次上传未改变任何状态。"
            ], state=current)
        if status == "no_publication":
            return RedirectResponse("/upload", status_code=303)

        result = {
            "revision": rev_no,
            "outcome": outcome,
            "reasons": reasons,
        }
        return render(request, "published.html", t=pub, rev=current,
                      result=result, errors=[])

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
