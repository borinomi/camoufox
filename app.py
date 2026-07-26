import asyncio
import os
import re
import base64
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

from camoufox.async_api import AsyncCamoufox


CAMOUFOX_HEADLESS = os.getenv("CAMOUFOX_HEADLESS", "virtual")  # 'virtual'|'true'|'false'
CAMOUFOX_OS = os.getenv("CAMOUFOX_OS", "windows")              # windows|macos|linux
CAMOUFOX_LOCALE = os.getenv("CAMOUFOX_LOCALE", "en-US")
CAMOUFOX_HUMANIZE = os.getenv("CAMOUFOX_HUMANIZE", "true").lower() == "true"
CAMOUFOX_GEOIP = os.getenv("CAMOUFOX_GEOIP", "false").lower() == "true"
CAMOUFOX_USER_DATA_DIR = os.getenv("CAMOUFOX_USER_DATA_DIR", "/app/camoufox-profile")
CAMOUFOX_PERSISTENT = os.getenv("CAMOUFOX_PERSISTENT", "true").lower() == "true"


def _parse_headless(v: str):
    if v.lower() == "virtual":
        return "virtual"
    return v.lower() == "true"

class FetchRequest(BaseModel):
    command: str


class GotoRequest(BaseModel):
    url: str


class RenderRequest(BaseModel):
    url: str
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 30000
    wait_for_selector: str | None = None
    auto_scroll: bool = False
    dismiss_banners: bool = False


class ScreenshotRequest(BaseModel):
    url: str
    wait_until: str = "load"
    wait_ms: int = 0
    timeout_ms: int = 30000
    wait_for_selector: str | None = None
    auto_scroll: bool = True
    dismiss_banners: bool = True
    full_page: bool = False
    image_type: str = "png"
    quality: int = 80

class InjectCookieRequest(BaseModel):
    cookie_string: str
    domain: str
    path: str = "/"
    secure: bool = True

class ClearCookieRequest(BaseModel):
    domain: str | None = None  

def now_iso():
    return datetime.now().isoformat()


def extract_referrer(command: str):
    patterns = [
        r'["\']referrer["\']\s*:\s*["\']([^"\']+)["\']',
        r'["\']referer["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


async def run_fetch(page, command: str):
    return await page.evaluate(
        """
        async (command) => {
            const response = await eval(command);
            return await response.text();
        }
        """,
        command,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.camoufox_cm = None       # async context manager
    app.state.browser = None           # AsyncBrowser or BrowserContext
    app.state.page = None
    app.state.lock = asyncio.Lock()

    try:
        yield
    finally:
        try:
            if app.state.camoufox_cm is not None:
                await app.state.camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


async def _launch_camoufox():
    """Camoufox를 launch하고 (browser_or_context, page) 반환."""
    kwargs = {
        "headless": _parse_headless(CAMOUFOX_HEADLESS),
        "os": CAMOUFOX_OS,
        "locale": CAMOUFOX_LOCALE,
        "humanize": CAMOUFOX_HUMANIZE,
        "geoip": CAMOUFOX_GEOIP,
    }

    if CAMOUFOX_PERSISTENT:
        Path(CAMOUFOX_USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = CAMOUFOX_USER_DATA_DIR

    cm = AsyncCamoufox(**kwargs)
    browser_or_context = await cm.__aenter__()

    # persistent_context=True → BrowserContext 반환
    # 그 외 → Browser 반환
    if CAMOUFOX_PERSISTENT:
        context = browser_or_context
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()
    else:
        browser = browser_or_context
        page = await browser.new_page()

    return cm, browser_or_context, page


async def connect_browser(force=False):
    """Camoufox 브라우저 확보. 죽었으면 자동 재시작."""
    if not force:
        page = getattr(app.state, "page", None)
        if page is not None:
            try:
                if not page.is_closed():
                    await page.evaluate("1")
                    return page
            except Exception:
                pass  # 죽었음 → 아래에서 재시작

    # 이전 인스턴스가 살아있다면 정리
    if app.state.camoufox_cm is not None:
        try:
            await app.state.camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        app.state.camoufox_cm = None
        app.state.browser = None
        app.state.page = None

    cm, browser, page = await _launch_camoufox()
    app.state.camoufox_cm = cm
    app.state.browser = browser
    app.state.page = page
    return page


async def ensure_page():
    return await connect_browser(force=False)


async def hide_banners(page):
    await page.add_style_tag(content="""
        [class*="cookie"],
        [class*="consent"],
        [id*="cookie"],
        [id*="consent"],
        [class*="gdpr"],
        [aria-label*="cookie" i],
        [aria-label*="consent" i],
        div[role="dialog"][aria-modal="true"] {
            display: none !important;
        }
        html, body {
            overflow: auto !important;
        }
    """)


async def auto_scroll(page):
    await page.evaluate("""
        async () => {
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const distance = 300;
            const interval = 200;
            const maxIdle = 3;

            let lastHeight = 0;
            let idleCount = 0;

            while (true) {
                window.scrollBy(0, distance);
                await sleep(interval);

                const scrollHeight = document.documentElement.scrollHeight;
                const scrolled = window.scrollY + window.innerHeight;

                if (scrolled >= scrollHeight - 10) {
                    if (scrollHeight === lastHeight) {
                        idleCount++;
                        if (idleCount >= maxIdle) break;
                    } else {
                        idleCount = 0;
                    }
                    lastHeight = scrollHeight;
                    await sleep(300);
                }
            }

            window.scrollTo(0, 0);
            await sleep(300);
        }
    """)


@app.post("/connect")
async def connect_only():
    try:
        async with app.state.lock:
            page = await connect_browser(force=True)
            return {"success": True, "url": page.url, "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/goto")
async def goto_only(request: GotoRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(request.url, wait_until="domcontentloaded")
            return {"success": True, "url": page.url, "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/fetch")
async def execute_fetch(request: FetchRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            data = await run_fetch(page, request.command)
            return {"success": True, "data": data, "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/fetchgoto")
async def execute_fetch_goto(request: FetchRequest):
    try:
        referrer_url = extract_referrer(request.command)
        if not referrer_url:
            return {"success": False, "error": "referrer not found in command", "timestamp": now_iso()}

        async with app.state.lock:
            page = await ensure_page()
            await page.goto(referrer_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            data = await run_fetch(page, request.command)
            return {"success": True, "data": data, "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/render")
async def render_html(request: RenderRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout_ms)

            if request.wait_for_selector:
                await page.wait_for_selector(request.wait_for_selector, timeout=request.timeout_ms)
            if request.auto_scroll:
                await auto_scroll(page); await asyncio.sleep(1)
            if request.dismiss_banners:
                await hide_banners(page)
            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            html = await page.content()
            return {"success": True, "url": page.url, "html": html, "length": len(html), "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/screenshot")
async def screenshot(request: ScreenshotRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            await page.goto(request.url, wait_until=request.wait_until, timeout=request.timeout_ms)

            if request.wait_for_selector:
                await page.wait_for_selector(request.wait_for_selector, timeout=request.timeout_ms)
            if request.auto_scroll:
                await auto_scroll(page); await asyncio.sleep(1)
            if request.dismiss_banners:
                await hide_banners(page)
            if request.wait_ms > 0:
                await asyncio.sleep(request.wait_ms / 1000)

            kwargs = {"full_page": request.full_page, "type": request.image_type}
            if request.image_type == "jpeg":
                kwargs["quality"] = request.quality

            img_bytes = await page.screenshot(**kwargs)
            media_type = "image/png" if request.image_type == "png" else "image/jpeg"
            return Response(content=img_bytes, media_type=media_type)
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}

@app.post("/inject_cookie")
async def inject_cookie(request: InjectCookieRequest):
    try:
        cookies = []
        for pair in request.cookie_string.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": request.domain,
                "path": request.path,
                "secure": request.secure,
                "sameSite": "Lax",
            })

        async with app.state.lock:
            page = await ensure_page()
            await page.context.add_cookies(cookies)

            return {
                "success": True,
                "count": len(cookies),
                "domain": request.domain,
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}

@app.post("/clear_cookies")
async def clear_cookies(request: ClearCookieRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()
            ctx = page.context

            if request.domain:
                # 지정 도메인 및 서브도메인 형태 모두 시도
                targets = {
                    request.domain,
                    "." + request.domain.lstrip("."),
                    "www." + request.domain.lstrip("."),
                }
                for d in targets:
                    try:
                        await ctx.clear_cookies(domain=d)
                    except TypeError:
                        # 구버전 Playwright: domain 필터 미지원 → 아래 fallback으로
                        raise
                mode = f"domain={request.domain}"
            else:
                await ctx.clear_cookies()
                mode = "all"

            return {"success": True, "mode": mode, "timestamp": now_iso()}
    except TypeError:
        # domain 필터 미지원 버전 fallback: 전체 읽어서 대상만 빼고 재설정
        try:
            async with app.state.lock:
                page = await ensure_page()
                ctx = page.context
                all_cookies = await ctx.cookies()
                dom = (request.domain or "").lstrip(".")
                keep = [c for c in all_cookies if dom not in (c.get("domain") or "")]
                await ctx.clear_cookies()
                if keep:
                    await ctx.add_cookies(keep)
                return {"success": True, "mode": f"fallback domain={request.domain}",
                        "kept": len(keep), "timestamp": now_iso()}
        except Exception as e:
            import traceback; traceback.print_exc()
            return {"success": False, "error": str(e), "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8031)
