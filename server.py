"""
轻量级 Web 后端：支持输入股票代码实时分析
基于项目的 StockAnalysisPipeline 核心流程
"""
import os
import sys
import re
import json
import asyncio
import traceback
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 path 中
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="股票智能分析看板")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_HTML = PROJECT_ROOT / "dashboard.html"


def _parse_stock_input(raw: str) -> list:
    """解析用户输入的股票代码，支持逗号/空格/换行分隔"""
    if not raw:
        return []
    parts = re.split(r"[,\s\n]+", raw.strip())
    codes = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 补全 A 股前导零（6位）
        if p.isdigit() and len(p) < 6:
            p = p.zfill(6)
        codes.append(p)
    return codes


def _run_analysis_subprocess(stock_codes: list, dry_run: bool = False) -> dict:
    """通过 subprocess 调用 main.py 运行分析，返回结果"""
    import subprocess

    code_str = ",".join(stock_codes)
    cmd = [
        sys.executable, "main.py",
        "--stocks", code_str,
        "--no-notify",
        "--force-run",
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-8000:] if len(proc.stdout) > 8000 else proc.stdout,
            "stderr": proc.stderr[-4000:] if len(proc.stderr) > 4000 else proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "分析超时（超过10分钟）"}
    except Exception as e:
        return {"returncode": -2, "stdout": "", "stderr": str(e)}


def _load_latest_report(stock_codes: list) -> dict:
    """读取最新生成的报告"""
    reports_dir = PROJECT_ROOT / "reports"
    result = {}
    today = datetime.now().strftime("%Y%m%d")

    if not reports_dir.exists():
        return result

    # 读取合并报告
    merged_report = reports_dir / f"report_{today}.md"
    if merged_report.exists():
        try:
            result["merged_report"] = merged_report.read_text(encoding="utf-8")
        except Exception:
            pass

    # 读取个股报告
    for code in stock_codes:
        # 尝试多种命名格式
        candidates = list(reports_dir.glob(f"*{code}*{today}*.md"))
        candidates += list(reports_dir.glob(f"*{today}*{code}*.md"))
        if not candidates:
            # 也查找最近修改的包含该代码的文件
            candidates = list(reports_dir.glob(f"*{code}*.md"))
        if candidates:
            # 取最新修改的
            candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            try:
                result[code] = candidates[0].read_text(encoding="utf-8")
            except Exception:
                pass

    return result


def _parse_report_to_struct(report_md: str) -> dict:
    """将 Markdown 报告解析为结构化数据（尽力解析）"""
    if not report_md:
        return {}

    info = {
        "decision": "未知",
        "score": 0,
        "trend": "未知",
        "price": "-",
        "change_pct": "-",
        "reason": "",
        "risks": [],
        "plan": {},
        "raw": report_md[:3000],
    }

    # 决策
    m = re.search(r"决策[:：]\s*(买入|加仓|观望|减仓|卖出|持有)", report_md)
    if m:
        info["decision"] = m.group(1)
    m = re.search(r"(买入|加仓|观望|减仓|卖出|持有)\s*[（(]", report_md)
    if m and info["decision"] == "未知":
        info["decision"] = m.group(1)

    # 评分
    m = re.search(r"评分[:：]\s*(\d+)", report_md)
    if m:
        info["score"] = int(m.group(1))
    m = re.search(r"综合评分[：:]\s*(\d+)", report_md)
    if m:
        info["score"] = int(m.group(1))

    # 趋势
    m = re.search(r"趋势[:：]\s*([^\n]{2,30})", report_md)
    if m:
        info["trend"] = m.group(1).strip()

    # 价格
    m = re.search(r"(?:当前价|现价|最新价|收盘价)[:：]?\s*([\d,.]+)", report_md)
    if m:
        info["price"] = m.group(1)
    m = re.search(r"涨跌幅[:：]?\s*(-?[\d.]+%)", report_md)
    if m:
        info["change_pct"] = m.group(1)

    # 风险
    risk_section = re.search(r"风险[提示警报]*[:：]([\s\S]{10,500}?)(?:\n##|\n---|\n#|\Z)", report_md, re.IGNORECASE)
    if risk_section:
        risks = re.findall(r"[-*•]\s*([^\n]{5,200})", risk_section.group(1))
        info["risks"] = risks[:5]

    # 操作计划
    plan_patterns = [
        (r"(?:止损位|止损)[:：]?\s*([\d.元]+)", "stop_loss"),
        (r"(?:目标位|目标价)[:：]?\s*([\d.元]+)", "target"),
        (r"(?:买入点|买入价|理想买入)[:：]?\s*([\d.元]+)", "buy_point"),
        (r"(?:建议仓位|仓位)[:：]?\s*([^\n]{2,50})", "position"),
    ]
    for pat, key in plan_patterns:
        m = re.search(pat, report_md)
        if m:
            info["plan"][key] = m.group(1).strip()

    return info


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回仪表盘页面"""
    if DASHBOARD_HTML.exists():
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>dashboard.html 未找到</h1>", status_code=404)


@app.post("/api/analyze")
async def analyze(request: Request):
    """分析股票接口：接收股票代码，运行分析，返回结果"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_codes = body.get("stocks", "")
    dry_run = body.get("dry_run", False)

    stock_codes = _parse_stock_input(raw_codes)
    if not stock_codes:
        return JSONResponse({"success": False, "error": "请输入至少一个股票代码"}, status_code=400)

    if len(stock_codes) > 10:
        return JSONResponse({"success": False, "error": "最多支持10支股票"}, status_code=400)

    # 运行分析
    run_result = _run_analysis_subprocess(stock_codes, dry_run=dry_run)
    success = run_result["returncode"] == 0

    # 读取报告
    reports = _load_latest_report(stock_codes)
    structured = {}
    for code, md in reports.items():
        if code == "merged_report":
            continue
        structured[code] = _parse_report_to_struct(md)

    return JSONResponse({
        "success": success,
        "stock_codes": stock_codes,
        "structured": structured,
        "merged_report": reports.get("merged_report", ""),
        "log": run_result["stdout"][-3000:],
        "error": run_result["stderr"][-2000:] if not success else "",
    })


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


if __name__ == "__main__":
    print("=" * 50)
    print("  股票智能分析看板 - 交互式 Web 服务")
    print("  访问 http://127.0.0.1:8080 体验")
    print("=" * 50)
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="info")
