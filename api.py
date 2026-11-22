"""API lấy nội dung sạch của một bài viết.

Playwright tải trang (vượt qua JS + Shadow DOM) -> làm phẳng HTML -> Trafilatura
lọc rác/chân trang/quảng cáo -> trả về markdown (hoặc txt/json).

Chạy:
    python article_api.py
    # hoặc production trên Windows:
    waitress-serve --listen=0.0.0.0:8000 --threads=8 article_api:app

Gọi API:
    POST /extract   {"url": "https://...", "format": "markdown"}
    GET  /extract?url=https://...
"""

from __future__ import annotations

import atexit
import ipaddress
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlparse

import trafilatura
from flask import Flask, jsonify, request
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- cấu hình ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

WORKERS = int(os.getenv("WORKERS", "2"))          # số browser chạy song song
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "8"))      # số request được phép chờ
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "30000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "90"))  # giây, cho cả job
ALLOW_PRIVATE_URLS = os.getenv("ALLOW_PRIVATE_URLS", "0") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]
BLOCKED_RESOURCES = "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf,mp4,webm,mp3}"
ALLOWED_FORMATS = {"markdown", "txt", "json", "xml", "xmltei"}

# Làm phẳng Shadow DOM, giữ lại vài attribute hữu ích cho Trafilatura
# (class/id giúp nó nhận diện footer, comment, sidebar... chính xác hơn).
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


# ------------------------------------------------------- browser theo thread ---
# Playwright sync API gắn chặt với thread đã khởi tạo nó, nên mỗi worker thread
# giữ một browser riêng (thread-local) và tái sử dụng cho các request sau.
_tls = threading.local()
_live_browsers: list[tuple[object, object]] = []
_live_lock = threading.Lock()


def _launch_browser():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
    with _live_lock:
        # dọn những browser đã chết để list không phình ra
        _live_browsers[:] = [(p, b) for p, b in _live_browsers if b.is_connected()]
        _live_browsers.append((pw, browser))
    return pw, browser


def _dispose_thread_browser():
    pw = getattr(_tls, "pw", None)
    browser = getattr(_tls, "browser", None)
    _tls.pw = None
    _tls.browser = None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def _get_browser():
    browser = getattr(_tls, "browser", None)
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
    _dispose_thread_browser()
    _tls.pw, _tls.browser = _launch_browser()
    return _tls.browser


@atexit.register
def _shutdown():
    with _live_lock:
        pairs = list(_live_browsers)
        _live_browsers.clear()
    for pw, browser in pairs:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


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


def _fetch_html(url: str, *, scroll: bool, timeout_ms: int) -> str:
    browser = _get_browser()
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="vi-VN",
        viewport={"width": 1366, "height": 900},
    )
    try:
        page = context.new_page()
        page.route(BLOCKED_RESOURCES, lambda route: route.abort())

        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is not None and response.status >= 400:
            raise FetchError(f"Trang trả về HTTP {response.status}")

        page.wait_for_timeout(2000)
        _accept_cookies(page)
        if scroll:
            _auto_scroll(page)

        return page.evaluate(FLATTEN_JS)
    except PlaywrightError as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        raise FetchError(first_line) from exc
    finally:
        try:
            context.close()
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

    # Trafilatura lo phần lọc rác, bỏ chân trang, bỏ menu...
    content = trafilatura.extract(
        flattened_html,
        url=url,
        output_format=fmt,
        include_comments=include_comments,
        include_tables=True,
        include_links=include_links,
        include_images=include_images,
        favor_precision=True,  # cắt mạnh tay các đoạn râu ria cuối bài
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
        alive = sum(1 for _, browser in _live_browsers if browser.is_connected())
    return jsonify(
        {
            "ok": True,
            "workers": WORKERS,
            "max_queue": MAX_QUEUE,
            "browsers_alive": alive,
            "page_timeout_ms": PAGE_TIMEOUT_MS,
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
        except Exception as exc:  # lỗi lạ: vẫn trả JSON thay vì HTML 500
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
    # debug/reloader phải tắt: Playwright sync API không hợp với process reload
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)
