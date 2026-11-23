"""
title: Live Market & Finance
author: Local AI Stack
description: Real-time stocks, ETFs, indices, crypto, and FX quotes with 300-DPI price and comparison charts (zero API keys or web-search credits).
version: 1.0.0
license: MIT
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("market_finance_tool")

try:
    from fastapi import UploadFile
    from starlette.datastructures import Headers
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler

    _HAS_OWUI_FILES = True
except Exception:
    _HAS_OWUI_FILES = False

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _find_system_python312() -> str:
    for p in (
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
        Path(r"C:\Program Files\Python312\python.exe"),
    ):
        if p.is_file():
            return str(p)
    found = shutil.which("python")
    if found:
        return found
    raise FileNotFoundError("System Python 3.12 not found.")


async def _upload_png(raw_bytes: bytes, filename: str, request: Any, user_dict: Optional[dict]) -> Optional[str]:
    if _HAS_OWUI_FILES and request is not None and isinstance(user_dict, dict) and user_dict.get("id"):
        try:
            user_obj = Users.get_user_by_id(user_dict["id"])
            if hasattr(user_obj, "__await__"):
                user_obj = await user_obj
            if user_obj is not None:
                import io

                upload = UploadFile(file=io.BytesIO(raw_bytes), filename=filename, headers=Headers({"content-type": "image/png"}))
                file_obj = upload_file_handler(request=request, file=upload, metadata={"source": "market_finance"}, process=False, user=user_obj)
                if hasattr(file_obj, "__await__"):
                    file_obj = await file_obj
                fid = getattr(file_obj, "id", None) or (file_obj.get("id") if isinstance(file_obj, dict) else None)
                if fid:
                    return f"/api/v1/files/{fid}/content"
        except Exception as e:
            log.warning("Chart upload failed: %s", e)
    fallback = Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"
    fallback.mkdir(parents=True, exist_ok=True)
    out_p = fallback / f"{uuid.uuid4().hex[:8]}_{filename}"
    out_p.write_bytes(raw_bytes)
    return str(out_p)


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(default=True, description="Show market lookup status pills in Open WebUI.")

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def get_market_quote(
        self,
        symbol_or_query: str,
        period: str = "1mo",
        include_chart: bool = True,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Fetch real-time market price, period return, 52-week range, and a 300-DPI price chart for any stock, ETF, index, crypto, or currency pair.

        :param symbol_or_query: Ticker symbol or name (e.g. 'NVDA', 'AAPL', 'FTSE 100', 'Bitcoin', 'GBP/EUR', 'LVMH').
        :param period: Chart timeframe: '1d', '5d', '1mo', '3mo', '6mo', '1y', or '5y'.
        :param include_chart: Render and return a 300-DPI price chart image (default true).
        :return: Market metrics JSON + inline chart image markdown.
        """
        cli = Path.home() / "AI" / "market-finance" / "finance_cli.py"
        if not cli.is_file():
            return "[TOOL_RESULT]\nMarket finance CLI missing."

        await self._emit(__event_emitter__, f"📈 Fetching live market data for {symbol_or_query[:30]}…", done=False)
        py312 = _find_system_python312()
        chart_name = f"chart_{uuid.uuid4().hex[:8]}.png"

        def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                chart_p = tdp / chart_name
                cmd = [py312, "-I", "-X", "utf8", str(cli), "quote", "--symbol", symbol_or_query, "--period", period or "1mo"]
                if include_chart:
                    cmd.extend(["--chart-out", str(chart_p)])
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
                data = json.loads((res.stdout or res.stderr).strip())
                if chart_p.is_file():
                    data["chart_bytes"] = chart_p.read_bytes()
                return data

        try:
            data = await asyncio.to_thread(_run)
        except Exception as e:
            await self._emit(__event_emitter__, "⚠️ Market lookup failed", done=True)
            return f"[TOOL_RESULT]\nMarket lookup error: {e}"

        if not data.get("ok"):
            await self._emit(__event_emitter__, "⚠️ Market symbol not found", done=True)
            return f"[TOOL_RESULT]\n{data.get('error') or 'Unknown error'}"

        chart_bytes = data.pop("chart_bytes", None)
        data.pop("chart_path", None)
        chart_url = await _upload_png(chart_bytes, chart_name, __request__, __user__) if chart_bytes else None

        pct = f" ({data['day_change_pct']:+}%)" if data.get("day_change_pct") is not None else ""
        await self._emit(__event_emitter__, f"📈 {data['symbol']} · {data['price']} {data['currency']}{pct}", done=True)
        parts = [json.dumps(data, ensure_ascii=False)]
        if chart_url:
            parts.append(f"\n![{data['symbol']} {data['period']} chart]({chart_url})")
        return "\n".join(parts)

    async def compare_tickers(
        self,
        symbols: str,
        period: str = "6mo",
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Compare 2 to 5 tickers/indices/currencies side-by-side with normalized % performance and a 300-DPI multi-line chart.

        :param symbols: Comma-separated tickers or names (e.g. 'NVDA, MSFT, AAPL' or 'BTC, Gold, S&P 500').
        :param period: Comparison timeframe: '1mo', '3mo', '6mo', '1y', or '5y'.
        :return: Side-by-side metrics JSON + inline comparison chart image markdown.
        """
        cli = Path.home() / "AI" / "market-finance" / "finance_cli.py"
        if not cli.is_file():
            return "[TOOL_RESULT]\nMarket finance CLI missing."

        await self._emit(__event_emitter__, f"📊 Comparing {symbols[:40]} ({period})…", done=False)
        py312 = _find_system_python312()
        chart_name = f"compare_{uuid.uuid4().hex[:8]}.png"

        def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as td:
                chart_p = Path(td) / chart_name
                cmd = [py312, "-I", "-X", "utf8", str(cli), "compare", "--symbols", symbols, "--period", period or "6mo", "--chart-out", str(chart_p)]
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40, creationflags=NO_WINDOW)
                data = json.loads((res.stdout or res.stderr).strip())
                if chart_p.is_file():
                    data["chart_bytes"] = chart_p.read_bytes()
                return data

        try:
            data = await asyncio.to_thread(_run)
        except Exception as e:
            await self._emit(__event_emitter__, "⚠️ Market comparison failed", done=True)
            return f"[TOOL_RESULT]\nComparison error: {e}"

        if not data.get("ok"):
            await self._emit(__event_emitter__, "⚠️ Market comparison failed", done=True)
            return f"[TOOL_RESULT]\n{data.get('error') or 'Unknown error'}"

        chart_bytes = data.pop("chart_bytes", None)
        data.pop("chart_path", None)
        chart_url = await _upload_png(chart_bytes, chart_name, __request__, __user__) if chart_bytes else None

        await self._emit(__event_emitter__, f"📊 Compared {len(data.get('items') or [])} tickers ({period})", done=True)
        parts = [json.dumps(data, ensure_ascii=False)]
        if chart_url:
            parts.append(f"\n![Performance comparison ({period})]({chart_url})")
        return "\n".join(parts)
