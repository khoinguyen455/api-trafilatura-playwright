"""API lấy nội dung sạch của một bài viết.

Playwright tải trang (vượt qua JS + Shadow DOM) -> làm phẳng HTML -> Trafilatura
lọc rác/chân trang/quảng cáo -> trả về markdown (hoặc txt/json).

Chạy:
    python article_api.py
    # hoặc production:
    waitress-serve --listen=0.0.0.0:8000 --threads=8 article_api:app

Gọi API:
    POST /extract   {"url": "https://...", "format": "markdown"}
    GET  /extract?url=https://...

Chống chặn bot (DataDome: masslive, cleveland, nj.com...):
    - Dùng Chrome thật nếu có + profile persistent giữ cookie.
    - Không abort ảnh/font trước khi trang tải xong (bị coi là ad-blocker -> 403).
    - Tự thử lại khi gặp 403/429/503.

Tự phát hiện môi trường:
    - Có DISPLAY (X11 thật hoặc Xvfb)  -> headful (giống người dùng thật)
    - Không có DISPLAY                  -> headless
    - Có google-chrome trong PATH       -> channel="chrome"
    - Không có                          -> Chromium bundled của Playwright

Biến môi trường (đều optional, có default hợp lý):
    HEADLESS=0|1          ghi đè chế độ headless
    BROWSER_CHANNEL=      "chrome" | "" (bundled); ghi đè auto-detect
    BLOCK_RESOURCES=1     chặn ảnh/font (nhanh hơn nhưng dễ bị 403)
    NAV_RETRIES=3         số lần thử lại khi bị chặn
    WORKERS=2             số browser chạy song song
    MAX_QUEUE=8           số request được phép chờ
    PAGE_TIMEOUT_MS=30000 timeout mỗi lần goto
    REQUEST_TIMEOUT=90    timeout cả job (giây)
    ALLOW_PRIVATE_URLS=1  cho phép URL nội bộ (mặc định chặn — SSRF)
"""

from __future__ import annotations

import atexit
import ipaddress
import logging
import os
import platform
import shutil
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlparse

import trafilatura
from flask import Flask, jsonify, request
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

log = logging.getLogger("article_api")

# ---------------------------------------------------------------- cấu hình ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

WORKERS = int(os.getenv("WORKERS", "2"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "8"))
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "30000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "90"))
ALLOW_PRIVATE_URLS = os.getenv("ALLOW_PRIVATE_URLS", "0") == "1"

# --- phát hiện môi trường đồ họa + Chrome binary ----------------------------
IS_LINUX = platform.system() == "Linux"
IN_CONTAINER = os.path.exists("/.dockerenv") or bool(os.getenv("KUBERNETES_SERVICE_HOST"))
HAS_DISPLAY = bool(os.getenv("DISPLAY"))


def _chrome_installed() -> bool:
    """Playwright channel='chrome' cần google-chrome thật trong PATH."""
    return bool(shutil.which("google-chrome") or shutil.which("google-chrome-stable"))


# Có X server (thật hoặc Xvfb) -> headful để giống người dùng thật.
# Không có X server -> headless (buộc phải vậy).
_default_headless = "0" if HAS_DISPLAY else "1"
# Cài Chrome thật -> dùng luôn; không -> bundled Chromium của Playwright.
_default_channel = "chrome" if _chrome_installed() else ""

HEADLESS = os.getenv("HEADLESS", _default_headless) == "1"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", _default_channel)
NAV_RETRIES = int(os.getenv("NAV_RETRIES", "3"))
BLOCK_RESOURCES = os.getenv("BLOCK_RESOURCES", "0") == "1"

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
]
# Container root -> Chromium từ chối chạy nếu không có --no-sandbox.
# Xvfb = software rendering -> thêm --disable-gpu để khỏi cà giựt.
if IS_LINUX and (IN_CONTAINER or (hasattr(os, "geteuid") and os.geteuid() == 0)):
    BROWSER_ARGS += ["--no-sandbox", "--disable-gpu"]

BLOCKED_RESOURCES = "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf,mp4,webm,mp3}"
RETRY_STATUSES = {403, 429, 503}
ALLOWED_FORMATS = {"markdown", "txt", "json", "xml", "xmltei"}

# Che các dấu hiệu automation đơn giản.
# KHÔNG patch navigator.permissions.query — DataDome kiểm tra và trả 403.
STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
Object.defineProperty(navigator, 'languages', {
  get: () => ['vi-VN', 'vi', 'en-US', 'en'],
});
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

# Làm phẳng Shadow DOM, giữ class/id để Trafilatura nhận diện footer/sidebar.
FLATTEN_JS = r"""
() => {
  const SKIP = new Set(['script', 'style', 'noscript', 'svg', 'iframe',
                        'template', 'canvas', 'object', 'embed']);
  const KEEP = new Set(['class', 'id', 'role', 'aria-label', 'href',
                        'title', 'alt', 'datetime', 'itemprop']);
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escAttr = s => esc(s).replace(/"/g, '&quot;');

  function serialize(node) {
    if (node.nodeType === Node.TEXT_NODE) return esc(node.textContent);
    if (node.nodeType !== Node.ELEMENT_NODE) return '';

    const tag = node.tagName.toLowerCase();
    if (SKIP.has(tag)) return '';

    let attrs = '';
    for (const attr of node.attributes) {
      if (!KEEP.has(attr.name) || !attr.value) continue;
      attrs += ' ' + attr.name + '="' + escAttr(attr.value) + '"';
    }

    let inner = '';
    if (node.shadowRoot) {
      for (const child of node.shadowRoot.childNodes) inner += serialize(child);
    }
    for (const child of node.childNodes) inner += serialize(child);
    return '<' + tag + attrs + '>' + inner + '</' + tag + '>';
  }

  return serialize(document.body);
}
"""


class FetchError(Exception):
    """Lỗi khi tải trang (mạng, HTTP 4xx/5xx, Playwright...)."""


class UnsafeUrlError(ValueError):
    """URL trỏ vào mạng nội bộ (chặn SSRF)."""


# --------------------------------------------------------- browser theo thread ---
# Playwright sync API gắn chặt với thread đã khởi tạo nó -> mỗi worker thread
# giữ một persistent context riêng (thread-local) và tái sử dụng.
_tls = threading.local()
_live_contexts: list[tuple[object, object, str]] = []
_live_lock = threading.Lock()

# Nếu channel="chrome" đã fail 1 lần thì đừng thử lại ở các worker khác.
_channel_lock = threading.Lock()
_channel_failed = False


def _context_alive(context) -> bool:
    try:
        context.pages  # ném lỗi nếu context đã đóng
        return True
    except Exception:
        return False


def _launch_context():
    global _channel_failed
    pw = sync_playwright().start()
    profile_dir = tempfile.mkdtemp(prefix="article_api_profile_")
    launch_kwargs = {
        "headless": HEADLESS,
        "args": BROWSER_ARGS,
        "locale": "vi-VN",
        "timezone_id": "Asia/Ho_Chi_Minh",
        "viewport": {"width": 1366, "height": 900},
        "extra_http_headers": {
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        # Không tự đặt User-Agent: Chrome thật gửi UA rút gọn khớp sec-ch-ua.
    }

    context = None
    if BROWSER_CHANNEL:
        with _channel_lock:
            try_channel = not _channel_failed
        if try_channel:
            try:
                context = pw.chromium.launch_persistent_context(
                    profile_dir, channel=BROWSER_CHANNEL, **launch_kwargs
                )
                log.info("Browser: channel=%s headless=%s", BROWSER_CHANNEL, HEADLESS)
            except Exception as exc:
                with _channel_lock:
                    _channel_failed = True
                first = str(exc).splitlines()[0] if str(exc).strip() else repr(exc)
                log.warning("Không mở được channel=%s (%s) — fallback bundled", BROWSER_CHANNEL, first)
                context = None

    if context is None:
        try:
            context = pw.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
            log.info("Browser: bundled-chromium headless=%s", HEADLESS)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise

    context.add_init_script(STEALTH_JS)
    with _live_lock:
        _live_contexts[:] = [item for item in _live_contexts if _context_alive(item[1])]
        _live_contexts.append((pw, context, profile_dir))
    return pw, context, profile_dir


def _close_context(pw, context, profile_dir) -> None:
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
    if profile_dir:
        shutil.rmtree(profile_dir, ignore_errors=True)


def _dispose_thread_context():
    pw = getattr(_tls, "pw", None)
    context = getattr(_tls, "context", None)
    profile_dir = getattr(_tls, "profile_dir", None)
    _tls.pw = None
    _tls.context = None
    _tls.profile_dir = None
    _close_context(pw, context, profile_dir)


def _get_context():
    context = getattr(_tls, "context", None)
    if context is not None and _context_alive(context):
        return context
    _dispose_thread_context()
    _tls.pw, _tls.context, _tls.profile_dir = _launch_context()
    return _tls.context


@atexit.register
def _shutdown():
    with _live_lock:
        items = list(_live_contexts)
        _live_contexts.clear()
    for pw, context, profile_dir in items:
        _close_context(pw, context, profile_dir)


# ------------------------------------------------------------- lấy nội dung ---
def _accept_cookies(page) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Tôi chấp nhận')",
        "button:has-text('Chấp nhận tất cả')",
        "button:has-text('Chấp nhận')",
        "button:has-text('Đồng ý')",
        "button:has-text('I accept')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=500):
                button.click(timeout=2000)
                page.wait_for_timeout(1500)
                return
        except PlaywrightError:
            continue


def _auto_scroll(page) -> None:
    try:
        page.evaluate(
            """async () => {
                await new Promise(resolve => {
                    let y = 0;
                    const step = () => {
                        window.scrollBy(0, 800);
                        y += 800;
                        if (y < document.body.scrollHeight && y < 30000) {
                            setTimeout(step, 120);
                        } else {
                            resolve();
                        }
                    };
                    step();
                });
            }"""
        )
        page.wait_for_timeout(1000)
    except PlaywrightError:
        pass


def _is_challenge_page(page) -> bool:
    """Trang chặn bot kiểu DataDome (script captcha-delivery / #cmsg)."""
    try:
        return page.locator(
            'script[src*="captcha-delivery.com"], #cmsg'
        ).count() > 0
    except PlaywrightError:
        return False


def _goto_with_retry(page, url: str, timeout_ms: int):
    """Điều hướng, tự thử lại khi bị chặn tạm thời (403/429/503 hoặc challenge)."""
    response = None
    for attempt in range(NAV_RETRIES):
        last = attempt == NAV_RETRIES - 1
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightError:
            if last:
                raise
            page.wait_for_timeout(1500 * (attempt + 1))
            continue

        blocked = response is not None and response.status >= 400
        if blocked and response.status not in RETRY_STATUSES:
            return response  # 404/410... thử lại vô ích
        if not blocked:
            if _is_challenge_page(page):
                page.wait_for_timeout(3000)
                if not _is_challenge_page(page):
                    return response
                if last:
                    return response
            else:
                return response
        page.wait_for_timeout(2500 * (attempt + 1))
    return response


def _fetch_html(url: str, *, scroll: bool, timeout_ms: int) -> str:
    context = _get_context()
    page = context.new_page()
    try:
        response = _goto_with_retry(page, url, timeout_ms)
        if response is not None and response.status >= 400:
            raise FetchError(f"Trang trả về HTTP {response.status}")
        if _is_challenge_page(page):
            raise FetchError(
                "Trang chặn bot (DataDome/CAPTCHA) — thử lại sau hoặc dùng proxy."
            )

        page.wait_for_timeout(2000)
        _accept_cookies(page)

        # Chỉ chặn ảnh/font SAU khi trang đã tải xong.
        if BLOCK_RESOURCES:
            try:
                page.route(BLOCKED_RESOURCES, lambda route: route.abort())
            except PlaywrightError:
                pass

        if scroll:
            _auto_scroll(page)

        return page.evaluate(FLATTEN_JS)
    except PlaywrightError as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        raise FetchError(first_line) from exc
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_clean_article(
    url: str,
    *,
    fmt: str = "markdown",
    include_comments: bool = False,
    include_links: bool = False,
    include_images: bool = False,
    scroll: bool = False,
    timeout_ms: int = PAGE_TIMEOUT_MS,
) -> dict:
    """Tải trang rồi lọc nội dung. Trả về dict {content, metadata, ...}."""
    flattened_html = _fetch_html(url, scroll=scroll, timeout_ms=timeout_ms)

    content = trafilatura.extract(
        flattened_html,
        url=url,
        output_format=fmt,
        include_comments=include_comments,
        include_tables=True,
        include_links=include_links,
        include_images=include_images,
        favor_precision=True,
    )
    if not content or not content.strip():
        raise FetchError("Không trích xuất được nội dung (trang có thể chặn bot hoặc chỉ toàn JS).")

    metadata: dict = {}
    try:
        doc = trafilatura.extract_metadata(flattened_html, default_url=url)
        if doc is not None:
            for key in ("title", "author", "date", "sitename", "description", "hostname", "url"):
                value = getattr(doc, key, None)
                if value:
                    metadata[key] = value
    except Exception:
        pass

    return {
        "content": content,
        "metadata": metadata,
        "format": fmt,
        "length": len(content),
        "html_length": len(flattened_html),
    }


# ------------------------------------------------------------ kiểm tra input ---
def _assert_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Chỉ hỗ trợ http:// và https://")
    if not parsed.hostname:
        raise ValueError("URL không hợp lệ (thiếu hostname)")
    if ALLOW_PRIVATE_URLS:
        return

    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Không phân giải được tên miền: {host}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrlError("Không cho phép truy cập địa chỉ nội bộ / private IP")


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# -------------------------------------------------------------------- Flask ---
app = Flask(__name__)
app.json.ensure_ascii = False

_executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="scrape")
_slots = threading.BoundedSemaphore(WORKERS + MAX_QUEUE)

INDEX_HTML = """<!doctype html>
<html lang="vi">
<meta charset="utf-8">
<title>Article Extractor API</title>
<style>
  body { font: 15px/1.6 system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; }
  input, select, button { font: inherit; padding: 8px 10px; }
  input[type=url] { width: 100%; box-sizing: border-box; margin-bottom: 8px; }
  textarea { width: 100%; height: 320px; box-sizing: border-box; margin-top: 12px; font-family: monospace; }
  label { margin-right: 12px; }
</style>
<h1>Article Extractor API</h1>
<form method="get" action="/extract">
  <input type="url" name="url" placeholder="https://..." required>
  <select name="format">
    <option value="markdown">markdown</option>
    <option value="txt">txt</option>
    <option value="json">json</option>
  </select>
  <button type="submit">Lấy nội dung</button>
  <p>
    <label><input type="checkbox" name="scroll" value="true"> auto scroll</label>
    <label><input type="checkbox" name="include_links" value="true"> giữ link</label>
    <label><input type="checkbox" name="include_comments" value="true"> giữ comment</label>
  </p>
</form>
<textarea readonly placeholder="Kết quả..."></textarea>
<script>
  const out = document.querySelector('textarea');
  document.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    out.value = 'Đang tải...';
    const qs = new URLSearchParams(new FormData(e.target)).toString();
    try {
      const res = await fetch('/extract?' + qs);
      const data = await res.json();
      out.value = data.content ?? JSON.stringify(data, null, 2);
    } catch (err) {
      out.value = 'Lỗi: ' + err;
    }
  });
</script>
</html>
"""


def _error(message: str, status: int, **extra):
    return jsonify({"ok": False, "error": message, **extra}), status


@app.get("/")
def index():
    return INDEX_HTML


@app.get("/health")
def health():
    with _live_lock:
        alive = sum(1 for _, context, _ in _live_contexts if _context_alive(context))
    return jsonify(
        {
            "ok": True,
            "workers": WORKERS,
            "max_queue": MAX_QUEUE,
            "browsers_alive": alive,
            "page_timeout_ms": PAGE_TIMEOUT_MS,
            "headless": HEADLESS,
            "display": os.getenv("DISPLAY") or None,
            "browser_channel": BROWSER_CHANNEL or "bundled-chromium",
            "channel_failed": _channel_failed,
        }
    )


@app.route("/extract", methods=["GET", "POST"])
def extract_endpoint():
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if payload is None:
            payload = request.form.to_dict()
    else:
        payload = request.args.to_dict()
    if not isinstance(payload, dict):
        return _error("Body phải là JSON object", 400)

    url = str(payload.get("url") or "").strip()
    if not url:
        return _error("Thiếu tham số 'url'", 400)

    fmt = str(payload.get("format") or "markdown").strip().lower()
    if fmt not in ALLOWED_FORMATS:
        return _error(f"'format' phải là một trong: {', '.join(sorted(ALLOWED_FORMATS))}", 400)

    try:
        timeout_ms = int(payload.get("timeout_ms") or PAGE_TIMEOUT_MS)
    except (TypeError, ValueError):
        return _error("'timeout_ms' phải là số nguyên", 400)
    timeout_ms = max(5000, min(timeout_ms, 90000))

    options = {
        "fmt": fmt,
        "include_comments": _as_bool(payload.get("include_comments")),
        "include_links": _as_bool(payload.get("include_links")),
        "include_images": _as_bool(payload.get("include_images")),
        "scroll": _as_bool(payload.get("scroll")),
        "timeout_ms": timeout_ms,
    }

    try:
        _assert_safe_url(url)
    except UnsafeUrlError as exc:
        return _error(str(exc), 403)
    except ValueError as exc:
        return _error(str(exc), 400)

    if not _slots.acquire(blocking=False):
        return _error("Hàng đợi đầy, thử lại sau", 429, retry_after=5)

    started = time.perf_counter()
    try:
        future = _executor.submit(fetch_clean_article, url, **options)
        try:
            result = future.result(timeout=REQUEST_TIMEOUT)
        except FutureTimeout:
            future.cancel()
            return _error(f"Hết thời gian xử lý sau {REQUEST_TIMEOUT:.0f}s", 504)
        except FetchError as exc:
            return _error(str(exc), 502)
        except Exception as exc:
            app.logger.exception("extract failed: %s", url)
            return _error(f"Lỗi xử lý: {exc}", 500)
    finally:
        _slots.release()

    return jsonify(
        {
            "ok": True,
            "url": url,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            **result,
        }
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info(
        "Khởi động: headless=%s channel=%s display=%s workers=%d",
        HEADLESS, BROWSER_CHANNEL or "bundled-chromium",
        os.getenv("DISPLAY") or "-", WORKERS,
    )
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)