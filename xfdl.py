#!/usr/bin/env python3
import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import math
import os
import random
import re
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401
    _BS_PARSER = "lxml"
except ImportError:
    _BS_PARSER = "html.parser"

DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".jpe", ".svg", ".tif", ".tiff", ".jif"
}
VIDEO_EXTS = {
    ".mpeg", ".avchd", ".webm", ".mpv", ".swf", ".avi", ".m4p", ".wmv",
    ".mp2", ".m4v", ".qt", ".mpe", ".mp4", ".flv", ".mov", ".mpg", ".ogg", ".mkv"
}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}

NOISE_IMG_RE = re.compile(
    r"(?:/smilies/|/emojis/|/avatars/|/data/avatars/|/styles/.*/xenforo/|"
    r"reaction|badge|icon|sprite|spacer|pixel|loading|spinner)",
    re.I,
)

LAZY_ATTRS = (
    "data-url",
    "data-src",
    "data-lazy-src",
    "data-zoom-src",
    "data-full-url",
    "data-original",
    "data-lb-sidebar-href",
    "data-lb-caption-href",
    "href",
    "src",
)

RAW_URL_RE = re.compile(
    r"(?:https?|hxxps?|h\*\*ps?):(?://|\[//\])[^\s<>\"']+",
    re.I,
)

DEFANG_DOT_RE = re.compile(r"(?:\[\.\]|\(\.\)|\{\.\})")
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "fbclid", "gclid",
}


class TerminalUI:
    def __init__(self, force_no_color=False, quiet=False):
        env_no_color = bool(os.environ.get("NO_COLOR"))
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        self.enabled = is_tty and not force_no_color and not env_no_color
        self.quiet = quiet
        self._lock = threading.Lock()

    def _style(self, text, code):
        if not self.enabled or not code:
            return text
        return f"\033[{code}m{text}\033[0m"

    def cyan(self, text):
        return self._style(text, "1;36")

    def magenta(self, text):
        return self._style(text, "1;35")

    def green(self, text):
        return self._style(text, "1;32")

    def yellow(self, text):
        return self._style(text, "1;33")

    def red(self, text):
        return self._style(text, "1;31")

    def dim(self, text):
        return self._style(text, "2")

    def bold(self, text):
        return self._style(text, "1")

    def emit(self, msg=""):
        with self._lock:
            print(msg, flush=True)

    def banner(self):
        line = self.cyan("=" * 44)
        title = f"{self.magenta('*')} {self.bold('XFDL')} {self.dim('//')} {self.cyan('MEDIA EXTRACTOR')}"
        self.emit(f"\n{line}\n  {title}\n{line}")

    def phase(self, label, detail=""):
        badge = self.magenta(f"[{label.upper()}]")
        extra = f" {self.dim('-')} {detail}" if detail else ""
        self.emit(f"\n{badge}{extra}")

    def info(self, msg, force=False):
        if not self.quiet or force:
            self.emit(f"  {self.cyan('>')} {msg}")

    def ok(self, msg, force=False):
        if not self.quiet or force:
            self.emit(f"  {self.green('[OK]')} {msg}")

    def warn(self, msg):
        self.emit(f"  {self.yellow('[WARN]')} {msg}")

    def err(self, msg):
        self.emit(f"  {self.red('[ERR]')} {msg}")

    def skip(self, msg):
        if not self.quiet:
            self.emit(f"  {self.dim('[SKIP]')} {self.dim(msg)}")

    def bar(self, current, total, width=16):
        if total and total > 0:
            ratio = max(0.0, min(1.0, current / total))
            filled = int(round(ratio * width))
            pct = f"{int(ratio * 100):3d}%"
        else:
            filled = int(time.monotonic() * 4) % (width + 1)
            pct = " ? "
        blocks = ("#" * filled) + ("-" * (width - filled))
        return f"[{self.cyan(blocks)}] {self.bold(pct)}"

    def summary_card(self, rows):
        width = 44
        header_label = "RUN SUMMARY"
        styled_header = self.bold(header_label)
        header_pad = max(1, (width - 3) - len(header_label))
        self.emit(f"\n{self.cyan('+' + '-' * (width - 2) + '+')}")
        self.emit(f"{self.cyan('|')} {styled_header}{' ' * header_pad}{self.cyan('|')}")
        self.emit(f"{self.cyan('+' + '-' * (width - 2) + '+')}")
        for k, v, color_fn in rows:
            val_str = color_fn(str(v)) if color_fn else str(v)
            plain_len = len(str(k)) + len(str(v)) + 2
            pad = max(1, (width - 3) - plain_len)
            self.emit(f"{self.cyan('|')} {self.dim(k + ':')} {val_str}{' ' * pad}{self.cyan('|')}")
        self.emit(f"{self.cyan('+' + '-' * (width - 2) + '+')}\n")


UI = TerminalUI()


def make_soup(markup):
    try:
        return BeautifulSoup(markup, _BS_PARSER)
    except Exception:
        return BeautifulSoup(markup, "html.parser")


def fmt_size(num_bytes):
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, num_bytes // 1024)} KB"


def fmt_eta(seconds):
    if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return "--:--"
    secs = int(seconds)
    if secs < 60:
        return f"00:{secs:02d}"
    mins, rem = divmod(secs, 60)
    if mins < 60:
        return f"{mins:02d}:{rem:02d}"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h{mins:02d}m"


def sanitize(name, fallback="file"):
    s = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "-", str(name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(". ")
    return (s or fallback)[:180]


def url_ext(url):
    clean = unquote(str(url or "")).lower().split("?")[0].split("#")[0]
    return os.path.splitext(clean)[1]


def media_kind(url):
    ext = url_ext(url)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def parse_cookie_input(raw_cookie):
    text = str(raw_cookie or "").strip()
    if not text:
        return ""
    if "# Netscape HTTP Cookie File" not in text and "\t" not in text:
        return re.sub(r"[\r\n]+", "", text).strip()

    pairs = []
    seen_names = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("# comment"):
            continue
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
        if s.startswith("#"):
            continue
        parts = re.split(r"\s+", s)
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            if name and name not in seen_names:
                seen_names.add(name)
                pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def read_text_file(path):
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def normalize_defanged_url(raw):
    s = html.unescape(str(raw or "")).strip()
    if not s:
        return ""
    s = re.sub(
        r"^hxxps?://",
        lambda m: "https://" if m.group(0).lower().startswith("hxxps") else "http://",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"^h\*\*ps?://",
        lambda m: "https://" if m.group(0).lower().startswith("h**ps") else "http://",
        s,
        flags=re.I,
    )
    s = s.replace("[//]", "//")
    s = DEFANG_DOT_RE.sub(".", s)
    s = s.rstrip(").,;!?]}>\"'")
    return s.strip()


def canonicalize_url(url, base_url=""):
    cleaned = normalize_defanged_url(url)
    if not cleaned:
        return ""
    if cleaned.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        cleaned = f"{scheme}:{cleaned}"
    elif base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
        cleaned = urljoin(base_url, cleaned)

    cleaned = cleaned.split("#")[0].strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return cleaned

    norm_path = re.sub(r"/{2,}", "/", parsed.path) or "/"
    kept_qs = []
    if parsed.query:
        for pair in parsed.query.split("&"):
            if not pair:
                continue
            key = pair.split("=", 1)[0].lower()
            if key in TRACKING_PARAMS:
                continue
            kept_qs.append(pair)

    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        norm_path,
        parsed.params,
        "&".join(kept_qs),
        "",
    ))


def pick_best_srcset(srcset_val, base_url=""):
    if not srcset_val:
        return ""
    best_url = ""
    best_score = -1.0
    for item in str(srcset_val).split(","):
        parts = item.strip().split()
        if not parts:
            continue
        candidate = canonicalize_url(parts[0], base_url)
        if not candidate:
            continue
        score = 1.0
        if len(parts) > 1:
            desc = parts[1].lower()
            m_w = re.match(r"^(\d+(?:\.\d+)?)w$", desc)
            m_x = re.match(r"^(\d+(?:\.\d+)?)x$", desc)
            if m_w:
                score = float(m_w.group(1))
            elif m_x:
                score = float(m_x.group(1)) * 1000.0
        if score >= best_score:
            best_score = score
            best_url = candidate
    return best_url


def decode_redirect(href, base_url):
    try:
        u = urlparse(urljoin(base_url, href))
        p = u.path.lower()
        if not (p.startswith("/redirect") or "link-proxy" in p):
            return href
        qs = parse_qs(u.query)
        to = next((qs[k][0] for k in ("to", "url", "u", "link", "target") if k in qs and qs[k]), None)
        if not to:
            return href
        mode = qs.get("m", [""])[0].lower()
        decoded = None
        if mode in ("b64", "base64") or (re.match(r"^[A-Za-z0-9+/_-]+={0,2}$", to) and len(to) >= 16):
            pad = to.replace("-", "+").replace("_", "/")
            pad += "=" * ((4 - len(pad) % 4) % 4)
            try:
                decoded = base64.b64decode(pad).decode("utf-8", errors="ignore")
            except (binascii.Error, ValueError):
                decoded = None
        if not decoded:
            decoded = unquote(to)
        if "%3a%2f%2f" in decoded.lower():
            decoded = unquote(decoded)
        return decoded if decoded.startswith(("http://", "https://")) else href
    except Exception:
        return href


class Downloader:
    def __init__(
        self,
        cookie_header="",
        user_agent="",
        out_dir="./downloads",
        gofile_token="",
        page_delay=0.15,
        no_keepalive=False,
        pool_size=20,
    ):
        self.out_dir = out_dir
        self.page_delay = max(0.0, float(page_delay))
        self.filename_hints = {}
        self._hints_lock = threading.Lock()
        self._gofile_token = (gofile_token or os.environ.get("GOFILE_TOKEN", "")).strip() or None
        self._redgifs_token = None
        self._token_lock = threading.Lock()

        script_dir = os.path.dirname(os.path.abspath(__file__))

        ua_raw = (user_agent or "").strip()
        if not ua_raw:
            ua_raw = read_text_file("ua.txt") or read_text_file(os.path.join(script_dir, "ua.txt"))
        self.ua = re.sub(r"[\r\n]+", "", ua_raw).strip() or DEFAULT_UA

        if not cookie_header:
            cookie_header = (
                read_text_file("cookie.txt")
                or read_text_file(os.path.join(script_dir, "cookie.txt"))
                or read_text_file("cookies.txt")
                or read_text_file(os.path.join(script_dir, "cookies.txt"))
            )
        clean_cookie = parse_cookie_input(cookie_header)

        self.s = requests.Session()
        if no_keepalive:
            adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1)
        else:
            sz = max(4, int(pool_size or 20))
            adapter = HTTPAdapter(pool_connections=sz, pool_maxsize=sz)
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)

        original_request = self.s.request

        def request_with_timeout(method, url, **kwargs):
            kwargs.setdefault("timeout", (4, 10))
            kwargs.setdefault("allow_redirects", True)
            return original_request(method, url, **kwargs)

        self.s.request = request_with_timeout
        self.s.headers.update({
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://simpcity.cr/",
        })
        if no_keepalive:
            self.s.headers.update({"Connection": "close"})
        if clean_cookie:
            self.s.headers.update({"Cookie": clean_cookie})

    def _set_hint(self, url, name):
        if url and name:
            cleaned = sanitize(name)
            if cleaned:
                with self._hints_lock:
                    self.filename_hints[url] = cleaned
                    self.filename_hints[url.split("#")[0]] = cleaned

    def _get_with_retry(self, url, retries=2, backoff=0.5, retry_403=False, **kwargs):
        last_resp = None
        last_exc = None
        for attempt in range(retries):
            try:
                resp = self.s.get(url, **kwargs)
                last_resp = resp
                should_retry = (
                    resp.status_code == 429
                    or resp.status_code >= 500
                    or (retry_403 and resp.status_code == 403)
                )
                if should_retry and attempt < retries - 1:
                    wait = (backoff * (2 ** attempt)) + random.uniform(0.05, 0.2)
                    time.sleep(wait)
                    continue
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    wait = (backoff * (2 ** attempt)) + random.uniform(0.05, 0.2)
                    time.sleep(wait)
                    continue
                raise last_exc
        return last_resp

    def _get_json(self, url, **kwargs):
        r = self._get_with_retry(url, **kwargs)
        if r.status_code == 429:
            return {"status": "error-rateLimit", "_http_status": 429}
        r.raise_for_status()
        return r.json()

    def _gofile_get_token(self):
        with self._token_lock:
            if self._gofile_token:
                return self._gofile_token
            try:
                r = self.s.post("https://api.gofile.io/accounts", json={}, timeout=8)
                r.raise_for_status()
                payload = r.json()
                token = payload.get("data", {}).get("token")
                if token:
                    self._gofile_token = token
                    self.s.cookies.set("accountToken", token, domain=".gofile.io", path="/")
                return token
            except Exception:
                return None

    def _gofile_wt(self, token, window_offset=0):
        window = math.floor(time.time() / 14400) + window_offset
        salt = os.environ.get("GOFILE_WT_SALT", "").strip()
        raw = f"{self.ua}::{token}::{window}::{salt}" if salt else f"{self.ua}::{token}::{window}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def resolve_gofile(self, url, passwords):
        m = re.search(r"gofile\.io/d/([^/?#]+)", url)
        if not m:
            return []
        cid = m.group(1)
        token = self._gofile_get_token()
        if not token:
            return []

        def fetch(content_id, pw_hash=None, window_offset=0):
            params = {
                "contentFilter": "",
                "page": 1,
                "pageSize": 1000,
                "sortField": "createTime",
                "sortDirection": -1,
            }
            if pw_hash:
                params["password"] = pw_hash
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Website-Token": self._gofile_wt(token, window_offset=window_offset),
                "Referer": "https://gofile.io/",
                "Origin": "https://gofile.io",
            }
            for attempt in range(2):
                data = self._get_json(
                    f"https://api.gofile.io/contents/{content_id}",
                    params=params,
                    headers=headers,
                    timeout=8,
                )
                status = data.get("status")
                if status == "error-rateLimit" or data.get("_http_status") == 429:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if status == "error-notPremium" and window_offset == 0:
                    headers["X-Website-Token"] = self._gofile_wt(token, window_offset=-1)
                    window_offset = -1
                    continue
                return data
            return {}

        active_pw_hash = None
        try:
            data = fetch(cid)
        except Exception:
            return []

        if data.get("status") == "error-passwordRequired":
            for pw in passwords or []:
                h = hashlib.sha256(str(pw).encode("utf-8")).hexdigest()
                try:
                    attempt = fetch(cid, pw_hash=h)
                except Exception:
                    continue
                if attempt.get("status") == "ok":
                    data = attempt
                    active_pw_hash = h
                    break

        results = []
        visited_folders = set()

        def walk(node, pw_hash):
            if not isinstance(node, dict) or node.get("status") != "ok":
                return
            children = node.get("data", {}).get("children")
            if not isinstance(children, dict):
                return
            for child in children.values():
                if not isinstance(child, dict):
                    continue
                ctype = child.get("type")
                if ctype == "file":
                    link = child.get("directLink") or child.get("link") or child.get("downloadLink")
                    if link:
                        if child.get("name"):
                            self._set_hint(link, child["name"])
                        results.append(link)
                elif ctype == "folder":
                    sub_id = child.get("id")
                    if sub_id and sub_id not in visited_folders and len(visited_folders) < 25:
                        visited_folders.add(sub_id)
                        time.sleep(0.1)
                        try:
                            walk(fetch(sub_id, pw_hash=pw_hash), pw_hash)
                        except Exception:
                            continue

        walk(data, active_pw_hash)
        return results

    def _bunkr_sign(self, cdn_url):
        try:
            u = urlparse(cdn_url)
            if not u.path:
                return cdn_url
            encoded_path = requests.utils.quote(unquote(u.path))
            resp = self._get_json(
                f"https://glb-apisign.cdn.cr/sign?path={encoded_path}",
                headers={"Referer": "https://bunkr.cr/", "Origin": "https://bunkr.cr"},
                timeout=5,
            )
            token = resp.get("token")
            ex = resp.get("ex")
            if token and ex:
                sep = "&" if "?" in cdn_url else "?"
                return f"{cdn_url}{sep}token={token}&ex={ex}"
        except Exception:
            pass
        return cdn_url

    def _bunkr_decode_api(self, data):
        if not isinstance(data, dict):
            return None
        raw_url = data.get("url")
        if not raw_url:
            return None
        if not data.get("encrypted"):
            return raw_url
        ts = data.get("timestamp")
        if ts is None:
            return None
        try:
            key = f"SECRET_KEY_{math.floor(float(ts) / 3600)}".encode("utf-8")
            enc = base64.b64decode(raw_url)
            return bytes(b ^ key[i % len(key)] for i, b in enumerate(enc)).decode("utf-8", errors="ignore")
        except Exception:
            return None

    def resolve_bunkr_file(self, url):
        clean = url.split("#")[0]
        if (
            re.search(r"\.(mp4|m4v|webm|mov|mkv|jpg|jpeg|png|gif|webp|zip|rar|7z|pdf)(?:$|\?)", clean, re.I)
            and not re.search(r"/[vfd]/", clean)
        ):
            return [clean]

        u = urlparse(clean)
        parts = [p for p in u.path.split("/") if p]
        slug = parts[-1] if parts else ""
        if not slug:
            return []

        file_id = None
        page_title = None
        bases = [f"{u.scheme}://{u.netloc}"] if u.scheme and u.netloc else []
        for fallback_base in ("https://bunkr.pk", "https://bunkr.cr", "https://bunkr.si"):
            if fallback_base not in bases:
                bases.append(fallback_base)

        for base in bases:
            for prefix in ("/f/", "/v/", "/d/"):
                try:
                    r = self._get_with_retry(f"{base}{prefix}{slug}", retries=1, timeout=6)
                    if r.status_code == 200 and "Just a moment" not in r.text:
                        soup = make_soup(r.text)
                        el = soup.select_one("[data-file-id]")
                        if el and el.get("data-file-id"):
                            file_id = el.get("data-file-id").strip()
                            title = soup.select_one("h1") or soup.select_one("title")
                            if title:
                                t = re.sub(r"\s*\|\s*Bunkr\s*$", "", title.get_text(strip=True))
                                if t and "Just a moment" not in t:
                                    page_title = t
                            break
                except Exception:
                    continue
            if file_id:
                break

        if not file_id:
            return []

        try:
            r = self.s.post(
                "https://apidl.bunkr.ru/api/_001_v2",
                json={"id": file_id},
                headers={
                    "Referer": f"https://get.bunkrr.su/file/{file_id}",
                    "Origin": "https://get.bunkrr.su",
                },
                timeout=6,
            )
            r.raise_for_status()
            api = r.json()
            dec = self._bunkr_decode_api(api)
            if dec:
                if dec.startswith("//"):
                    dec = "https:" + dec
                signed = self._bunkr_sign(dec)
                if page_title:
                    self._set_hint(signed, page_title)
                return [signed]
        except Exception:
            pass
        return []

    def resolve_bunkr_album(self, url):
        base = url.split("?")[0].rstrip("/")
        results = []
        seen = set()
        for page in range(1, 25):
            try:
                r = self._get_with_retry(f"{base}?page={page}", retries=1, timeout=6)
            except Exception:
                break
            if r.status_code != 200 or "Just a moment" in r.text:
                break
            soup = make_soup(r.text)
            links = soup.select(
                ".grid-images a[href*='/f/'], .grid-images a[href*='/v/'], "
                "a[href*='/f/'], a[href*='/v/']"
            )
            if not links:
                break
            added = 0
            for a in links:
                if len(results) >= 300:
                    break
                href = urljoin(base + "/", a.get("href", ""))
                if "/a/" in href or href in seen:
                    continue
                seen.add(href)
                added += 1
                results.extend(self.resolve_bunkr_file(href))
            if added == 0 or len(results) >= 300:
                break
        return results

    def resolve_cyberdrop(self, url):
        if "/a/" in url:
            try:
                r = self._get_with_retry(url, retries=1, timeout=6)
                r.raise_for_status()
            except Exception:
                return []
            slugs = sorted(set(re.findall(r"/f/([A-Za-z0-9_-]+)", r.text)))[:200]
            out = []
            for slug in slugs:
                out.extend(self.resolve_cyberdrop(f"https://cyberdrop.cr/f/{slug}"))
            return out

        m = re.search(r"/[ef]/([^/?#]+)", url)
        if not m:
            return [url]
        slug = m.group(1)
        for endpoint in (
            f"https://api.cyberdrop.cr/api/file/info/{slug}",
            f"https://api.cyberdrop.cr/api/file/auth/{slug}",
        ):
            try:
                j = self._get_json(
                    endpoint,
                    headers={"Origin": "https://cyberdrop.cr", "Referer": "https://cyberdrop.cr/"},
                    timeout=6,
                )
                if not isinstance(j, dict):
                    continue
                data = j.get("data") if isinstance(j.get("data"), dict) else {}
                direct = j.get("url") or data.get("url")
                name = j.get("name") or data.get("name")
                if direct:
                    if name:
                        self._set_hint(direct, name)
                    return [direct]
            except Exception:
                continue
        return []

    def resolve_turbo(self, url):
        if "/a/" in url:
            try:
                r = self._get_with_retry(url, retries=1, timeout=6)
                r.raise_for_status()
            except Exception:
                return []
            ids = sorted(set(re.findall(r'href="/v/([^"?#]+)"', r.text)))[:200]
            out = []
            for tid in ids:
                out.extend(self.resolve_turbo(f"https://turbo.cr/v/{tid}"))
            return out

        m = re.search(r"/(?:v|d|embed)/([^/?#]+)", url)
        if not m:
            return []
        tid = m.group(1)
        try:
            j = self._get_json(
                f"https://turbo.cr/api/sign?v={tid}",
                headers={"Referer": f"https://turbo.cr/embed/{tid}"},
                timeout=6,
            )
            if isinstance(j, dict) and j.get("url"):
                if j.get("original_filename"):
                    self._set_hint(j["url"], j["original_filename"])
                return [j["url"]]
        except Exception:
            pass
        return []

    def resolve_jpgx(self, url, passwords):
        if re.search(r"/(?:a|album)/", url):
            clean = url.split("?")[0]
            try:
                r = self._get_with_retry(clean, retries=1, timeout=6)
                r.raise_for_status()
            except Exception:
                return []
            soup = make_soup(r.text)
            if "Please enter your password to continue" in r.text:
                token_el = soup.select_one('input[name="auth_token"]')
                token = token_el.get("value", "") if token_el else ""
                for pw in (passwords or []) + ["ramona"]:
                    try:
                        r2 = self.s.post(
                            clean,
                            data={"auth_token": token, "content-password": pw},
                            timeout=6,
                        )
                        if "Please enter your password to continue" not in r2.text:
                            soup = make_soup(r2.text)
                            break
                    except Exception:
                        continue

            imgs = []
            current_url = clean
            visited_pages = set()
            while soup and current_url not in visited_pages and len(visited_pages) < 50:
                visited_pages.add(current_url)
                for img in soup.select(".list-item-image > a > img, .list-item-image img"):
                    src = img.get("data-src") or img.get("src") or ""
                    src = re.sub(r"\.(?:md|th)\.", ".", src)
                    if src:
                        imgs.append(urljoin(current_url, src))
                nxt = soup.select_one('a[data-pagination="next"]')
                if nxt and nxt.get("href"):
                    next_url = urljoin(current_url, nxt["href"])
                    try:
                        nr = self._get_with_retry(next_url, retries=1, timeout=6)
                        nr.raise_for_status()
                        current_url = next_url
                        soup = make_soup(nr.text)
                    except Exception:
                        break
                else:
                    break
            return imgs

        return [re.sub(r"\.(?:md|th)\.", ".", url)]

    def resolve_goonbox(self, url):
        if "/a/" in url:
            slug = url.split("?")[0].rstrip("/").split("/")[-1]
            out = []
            page = 1
            while page <= 50:
                try:
                    j = self._get_json(
                        f"https://goonbox.cr/api/albums/{slug}/images?page={page}",
                        headers={"Referer": url},
                        timeout=6,
                    )
                except Exception:
                    break
                if not isinstance(j, dict):
                    break
                imgs = j.get("images") or []
                out.extend(i["original_url"] for i in imgs if isinstance(i, dict) and i.get("original_url"))
                last_page = int((j.get("pagination") or {}).get("last_page") or 1)
                if page >= last_page:
                    break
                page += 1
            return out

        img_id = url.split("/")[-1].split("?")[0]
        try:
            j = self._get_json(
                f"https://goonbox.cr/api/images/{img_id}",
                headers={"Referer": url},
                timeout=6,
            )
            orig = (j.get("image") or {}).get("original_url") if isinstance(j, dict) else None
            return [orig] if orig else []
        except Exception:
            return []

    def resolve_redgifs(self, url):
        with self._token_lock:
            if not self._redgifs_token:
                try:
                    r = self._get_json("https://api.redgifs.com/v2/auth/temporary", timeout=6)
                    if isinstance(r, dict):
                        self._redgifs_token = r.get("token")
                except Exception:
                    return []
            token = self._redgifs_token
        if not token:
            return []

        m = re.search(
            r"redgifs\.com/(?:ifr/|watch/|gifs/(?:detail|watch)/)?([a-zA-Z0-9_-]+)",
            url,
        )
        if not m:
            return []
        try:
            j = self._get_json(
                f"https://api.redgifs.com/v2/gifs/{m.group(1)}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Referer": "https://www.redgifs.com/",
                    "Origin": "https://www.redgifs.com",
                },
                timeout=6,
            )
            urls = ((j.get("gif") or {}).get("urls") or {}) if isinstance(j, dict) else {}
            for k in ("hd", "hd1080", "hd720", "sd", "mp4"):
                if urls.get(k):
                    return [urls[k]]
        except Exception:
            pass
        return []

    def resolve_url(self, url, passwords):
        low = url.lower()
        if "gofile.io/d/" in low:
            return self.resolve_gofile(url, passwords)
        if re.search(r"bunkrr?r?\.[a-z]+/a/", low):
            return self.resolve_bunkr_album(url)
        if re.search(r"bunkrr?r?\.[a-z]+", low):
            return self.resolve_bunkr_file(url)
        if "cyberdrop." in low:
            return self.resolve_cyberdrop(url)
        if "turbo.cr/" in low:
            return self.resolve_turbo(url)
        if re.search(r"(jpg\d?\.(church|fish|fishing|pet|su|cr)|cuckcapital\.cr)/", low):
            return self.resolve_jpgx(url, passwords)
        if "goonbox.cr/" in low:
            return self.resolve_goonbox(url)
        if "redgifs.com/" in low:
            return self.resolve_redgifs(url)
        if re.search(r"(pixeldrain\.com|pixeldrain\.net|pixeldra\.in)/[lu]/", low):
            u = url.replace("/u/", "/api/file/").replace("/l/", "/api/list/")
            return [f"{u}/zip" if "/api/list" in u else f"{u}?download"]
        if "pixhost.to/" in low and "/gallery/" not in low:
            return [re.sub(r"/t(\d+)\.", r"/img\1.", url, flags=re.I).replace("thumbs/", "images/")]
        if "imgbox.com/" in low and "/g/" not in low:
            return [re.sub(r"_t\.", "_o.", url, flags=re.I).replace("thumbs", "images")]
        if "ibb.co/" in low and "/album/" not in low and not low.startswith("https://i.ibb.co"):
            try:
                r = self._get_with_retry(url, retries=1, timeout=6)
                soup = make_soup(r.text)
                a = soup.select_one(".header-content-right > a")
                return [a["href"]] if a and a.get("href") else []
            except Exception:
                return []
        if "postimg.cc/" in low or "pixxxels.cc/" in low:
            u = re.sub(
                r"https?://(www\.)?i\.?(postimg|pixxxels)\.cc/(.{8}).*",
                r"https://postimg.cc/\3",
                url,
            )
            try:
                r = self._get_with_retry(u, retries=1, timeout=6)
                soup = make_soup(r.text)
                a = soup.select_one(".controls > nobr > a")
                return [a["href"]] if a and a.get("href") else []
            except Exception:
                return []
        if any(
            d in low
            for d in (
                "coomer.st/data",
                "kemono.cr/data",
                "i.ibb.co/",
                "twimg.com/",
                "redd.it/",
                "pomf2.lain.la/",
                "/attachments/",
                "/data/video/",
            )
        ):
            return [url]
        if media_kind(url) in ("image", "video"):
            return [url]
        return []

    def _page_url_for(self, clean_base, page):
        return clean_base + "/" if page == 1 else f"{clean_base}/page-{page}"

    def _detect_last_page(self, soup, base_url, start_page, max_pages):
        last_page = start_page
        for a in soup.select(
            ".pageNav a[href], a.pageNav-jump[href], .pageNav-page a[href], "
            "link[rel='next'][href], a[rel='next'][href]"
        ):
            href = a.get("href", "")
            m = re.search(r"/page-(\d+)", href)
            if m:
                last_page = max(last_page, int(m.group(1)))
            else:
                qs = parse_qs(urlparse(urljoin(base_url + "/", href)).query)
                if "page" in qs and qs["page"] and qs["page"][0].isdigit():
                    last_page = max(last_page, int(qs["page"][0]))
        return min(max(start_page, last_page), max_pages)

    def _extract_post_passwords_from_text(self, body, text):
        spoilers = [
            s.get_text(" ", strip=True)
            for s in body.select(
                ".bbCodeBlock--spoiler .bbCodeBlock-content, "
                ".bbCodeSpoiler-content, .bbCodeInlineSpoiler, details > *:not(summary)"
            )
        ]
        spoilers += re.findall(
            r"(?:pw|pass|passwd|password|key|code)\s*[:=-]?\s*([^\s\r\n]+)",
            text,
            re.I,
        )
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for idx, line in enumerate(lines[:-1]):
            if re.fullmatch(r"(?:pw|pass|passwd|password|key|code)\s*[:=-]?", line, re.I):
                spoilers.append(lines[idx + 1].split()[0])
        return list(dict.fromkeys(s.strip("`'\".,;") for s in spoilers if s and s.strip("`'\".,;")))

    def _extract_post_urls_from_text(self, body, page_url, post_text):
        candidates = []

        for a in body.select("a[href]"):
            decoded = decode_redirect(a.get("href", ""), page_url)
            c = canonicalize_url(decoded, page_url)
            if c:
                candidates.append(c)

        for tag in body.select("img, video, source, iframe, embed"):
            if tag.name == "img" and tag.find_parent("a", href=re.compile(r"/attachments/|goonbox\.cr")):
                continue
            if tag.get("srcset"):
                best_ss = pick_best_srcset(tag.get("srcset"), page_url)
                if best_ss and not (tag.name == "img" and NOISE_IMG_RE.search(best_ss)):
                    candidates.append(best_ss)

            for attr in LAZY_ATTRS:
                val = tag.get(attr)
                if not val:
                    continue
                if tag.name == "a" and attr == "href":
                    val = decode_redirect(val, page_url)
                c = canonicalize_url(val, page_url)
                if not c:
                    continue
                if tag.name == "img" and NOISE_IMG_RE.search(c):
                    continue
                candidates.append(c)

        for raw_match in RAW_URL_RE.findall(post_text):
            c = canonicalize_url(raw_match, page_url)
            if c:
                candidates.append(c)

        return candidates

    def _parse_page_entries(self, soup, page_url, include_quotes=False):
        page_entries = []
        for article in soup.select("article.message"):
            body = article.select_one(".message-userContent")
            if not body:
                continue
            if not include_quotes:
                for q in body.select("blockquote"):
                    if q.select_one(".bbCodeBlock-title") or "bbCodeBlock--quote" in (q.get("class") or []):
                        q.decompose()

            post_text = body.get_text("\n")
            post_passwords = self._extract_post_passwords_from_text(body, post_text)
            for u in self._extract_post_urls_from_text(body, page_url, post_text):
                page_entries.append((u, post_passwords))
        return page_entries

    def _fetch_and_parse_page(self, page, page_url, referer_url, include_quotes=False):
        if self.page_delay > 0:
            time.sleep(self.page_delay + random.uniform(0.02, 0.12))
        r = self._get_with_retry(
            page_url,
            retries=2,
            backoff=0.8,
            retry_403=True,
            headers={"Referer": referer_url},
            timeout=12,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        soup = make_soup(r.text)
        return page, self._parse_page_entries(soup, str(r.url or page_url), include_quotes=include_quotes)

    def scrape_thread(
        self, thread_url, max_pages=500, single_page=False, include_quotes=False, workers=4
    ):
        clean_base = re.sub(r"/page-\d+/?$", "", thread_url.split("#")[0].split("?")[0]).rstrip("/")
        start_match = re.search(r"/page-(\d+)", thread_url)
        start_page = int(start_match.group(1)) if (single_page and start_match) else 1
        thread_root = clean_base + "/"

        initial_url = thread_url if single_page else self._page_url_for(clean_base, start_page)
        r = self._get_with_retry(
            initial_url,
            retries=2,
            backoff=0.8,
            retry_403=True,
            headers={"Referer": thread_root},
            timeout=12,
        )
        if r.status_code != 200:
            soup_err = make_soup(r.text)
            notice = soup_err.select_one(".blockMessage")
            detail = f" — {notice.get_text(' ', strip=True)[:120]}" if notice else ""
            UI.err(f"Failed to load thread (HTTP {r.status_code}{detail})")
            sys.exit(1)

        effective_page_url = str(r.url or initial_url)
        soup = make_soup(r.text)
        title_el = soup.select_one(".p-title-value")
        if title_el:
            for badge in title_el.select(".label, .labelLink, .prefix"):
                badge.decompose()
            performer = sanitize(title_el.get_text(" ", strip=True), "Performer")
        else:
            performer = "Performer"

        last_page = (
            start_page
            if single_page
            else self._detect_last_page(soup, clean_base, start_page, max_pages)
        )

        UI.info(
            f"Thread: {UI.bold(performer)} {UI.dim('|')} Pages: {UI.cyan(f'{start_page}..{last_page}')} {UI.dim(f'({_BS_PARSER}, w={max(1, int(workers or 1))})')}",
            force=True,
        )

        ordered_page_items = {}
        first_items = self._parse_page_entries(soup, effective_page_url, include_quotes=include_quotes)
        ordered_page_items[start_page] = first_items
        UI.info(f"Page {start_page}/{last_page} -> +{len(first_items)} candidate links")

        remaining_pages = list(range(start_page + 1, last_page + 1))
        if remaining_pages:
            max_workers = max(1, min(int(workers or 1), 8, len(remaining_pages)))
            if max_workers > 1:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(
                            self._fetch_and_parse_page,
                            p,
                            self._page_url_for(clean_base, p),
                            thread_root,
                            include_quotes,
                        ): p
                        for p in remaining_pages
                    }
                    for fut in as_completed(futures):
                        p = futures[fut]
                        try:
                            _, p_items = fut.result()
                            ordered_page_items[p] = p_items
                            UI.info(f"Page {p}/{last_page} -> +{len(p_items)} candidate links")
                        except Exception as e:
                            UI.warn(f"Page {p}/{last_page} skipped ({e})")
            else:
                for p in remaining_pages:
                    p_url = self._page_url_for(clean_base, p)
                    try:
                        _, p_items = self._fetch_and_parse_page(
                            p, p_url, thread_root, include_quotes=include_quotes
                        )
                        ordered_page_items[p] = p_items
                        UI.info(f"Page {p}/{last_page} -> +{len(p_items)} candidate links")
                    except Exception as e:
                        UI.warn(f"Page {p}/{last_page} skipped ({e})")

        entries = []
        link_to_idx = {}
        for p in sorted(ordered_page_items.keys()):
            for u, pws in ordered_page_items[p]:
                if u not in link_to_idx:
                    link_to_idx[u] = len(entries)
                    entries.append((u, list(pws)))
                else:
                    existing_idx = link_to_idx[u]
                    existing_url, existing_pws = entries[existing_idx]
                    merged_pws = list(dict.fromkeys(existing_pws + list(pws)))
                    entries[existing_idx] = (existing_url, merged_pws)

        return performer, entries

    def _build_target_path(self, dest_folder, url, resolved_url, headers):
        with self._hints_lock:
            fname = (
                self.filename_hints.get(resolved_url)
                or self.filename_hints.get(url)
                or None
            )
        if not fname:
            cd = headers.get("Content-Disposition", "")
            m = (
                re.search(r"filename\*=UTF-8''([^;\r\n]+)", cd, re.I)
                or re.search(r'filename="?([^";\r\n]+)"?', cd, re.I)
            )
            if m:
                fname = unquote(m.group(1))
        if not fname:
            fname = os.path.basename(urlparse(resolved_url).path) or "file"

        fname = sanitize(fname)
        stem, ext = os.path.splitext(fname)
        short_hash = hashlib.sha1(resolved_url.encode("utf-8")).hexdigest()[:8]
        candidate = os.path.join(dest_folder, fname)
        if os.path.exists(candidate):
            tagged = f"{stem or 'file'}_{short_hash}{ext}"
            return os.path.join(dest_folder, tagged), tagged
        return candidate, fname

    def download_file(self, url, dest_folder=None, index_label="", show_progress_bar=True):
        folder = dest_folder or self.out_dir
        os.makedirs(folder, exist_ok=True)

        low = url.lower()
        referer = url
        headers = {}
        if "bunkr" in low:
            referer = "https://bunkr.si/"
        elif "turbocdn.st" in low:
            referer = "https://turbo.cr/"
        elif "pomf2" in low:
            referer = "https://pomf2.lain.la/"
        elif "redgifs.com" in low:
            referer = "https://www.redgifs.com/"
            headers["Origin"] = "https://www.redgifs.com"

        headers["Referer"] = referer
        part_path = None
        prefix = f"{UI.dim(index_label)} " if index_label else ""
        show_live_bar = show_progress_bar and UI.enabled and not UI.quiet

        try:
            with self.s.get(url, headers=headers, stream=True, timeout=(5, 25)) as r:
                if r.status_code >= 400:
                    UI.err(f"{prefix}HTTP {r.status_code} - {url[:75]}")
                    return "error"
                ct = r.headers.get("Content-Type", "").lower()
                if "text/html" in ct:
                    UI.warn(f"{prefix}HTML gate skipped - {url[:75]}")
                    return "error"

                target, fname = self._build_target_path(folder, url, r.url, r.headers)
                if os.path.exists(target):
                    UI.skip(f"{prefix}Already exists: {fname}")
                    return "skipped"

                part_path = f"{target}.part"
                total = int(r.headers.get("Content-Length") or 0)
                downloaded = 0
                start_t = time.monotonic()
                last_draw = 0.0
                short_name = (fname[:24] + "...") if len(fname) > 25 else fname

                with open(part_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=262144):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if show_live_bar:
                            now = time.monotonic()
                            if now - last_draw >= 0.25 or (total > 0 and downloaded >= total):
                                last_draw = now
                                elapsed = max(0.05, now - start_t)
                                speed_kbps = (downloaded / 1024) / elapsed
                                if total > 0:
                                    rem_bytes = max(0, total - downloaded)
                                    eta = fmt_eta(rem_bytes / max(1.0, downloaded / elapsed))
                                    line = (
                                        f"\r  {prefix}{UI.bold(short_name):<26} "
                                        f"{UI.bar(downloaded, total)} "
                                        f"{UI.dim(f'{fmt_size(downloaded)}/{fmt_size(total)}')} "
                                        f"{UI.cyan(f'{speed_kbps:.0f}KB/s')} "
                                        f"{UI.dim(f'ETA {eta}')}"
                                    )
                                else:
                                    line = (
                                        f"\r  {prefix}{UI.bold(short_name):<26} "
                                        f"{UI.bar(downloaded, 0)} "
                                        f"{UI.dim(fmt_size(downloaded))} "
                                        f"{UI.cyan(f'{speed_kbps:.0f}KB/s')}"
                                    )
                                sys.stdout.write(line)
                                sys.stdout.flush()

                os.replace(part_path, target)
                elapsed = max(0.05, time.monotonic() - start_t)
                avg_speed = (downloaded / 1024) / elapsed
                if show_live_bar:
                    sys.stdout.write("\r" + (" " * 95) + "\r")
                    sys.stdout.flush()
                UI.ok(
                    f"{prefix}{UI.bold(fname)} "
                    f"{UI.dim(f'({fmt_size(downloaded)}, {avg_speed:.0f} KB/s)')}"
                )
                return "downloaded"
        except Exception as e:
            if part_path and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            if show_live_bar:
                sys.stdout.write("\r" + (" " * 95) + "\r")
                sys.stdout.flush()
            UI.err(f"{prefix}{type(e).__name__}: {e}")
            return "error"


def matches_media_filter(url, media_filter):
    if media_filter == "all":
        return True
    kind = media_kind(url)
    if kind:
        return kind == media_filter
    if url_ext(url) in ARCHIVE_EXTS:
        return False
    return True


def _resolve_single_candidate(dl, idx, total, url, spoilers, media_filter):
    label = f"[{idx}/{total}]"
    t0 = time.monotonic()
    try:
        found = dl.resolve_url(url, spoilers)
        elapsed = time.monotonic() - t0
        if elapsed > 3.0:
            UI.warn(f"{UI.dim(label)} Slow resolve ({elapsed:.1f}s) <- {url[:64]}")
        filtered = []
        for final_url in found:
            canon_final = canonicalize_url(final_url, url) or final_url
            if canon_final and matches_media_filter(canon_final, media_filter):
                filtered.append(canon_final)
        return idx, url, filtered, elapsed, None
    except Exception as e:
        elapsed = time.monotonic() - t0
        if elapsed > 3.0:
            UI.warn(f"{UI.dim(label)} Slow resolve ({elapsed:.1f}s) <- {url[:64]}")
        return idx, url, [], elapsed, e


def load_existing_links(links_path):
    existing = []
    if os.path.isfile(links_path):
        try:
            with open(links_path, "r", encoding="utf-8") as f:
                for line in f:
                    u = line.strip()
                    if u and u.startswith(("http://", "https://")):
                        existing.append(u)
        except OSError:
            pass
    return list(dict.fromkeys(existing))


def main():
    parser = argparse.ArgumentParser(description="Headless media downloader for iSH / a-Shell")
    parser.add_argument("url", help="Thread URL (or /page-N URL)")
    parser.add_argument("-o", "--out", default="./downloads", help="Output directory")
    parser.add_argument("-c", "--cookie", default="", help="Forum Cookie header string or Netscape cookie text")
    parser.add_argument("--user-agent", default="", help="Custom User-Agent string (or reads ua.txt)")
    parser.add_argument("-m", "--media", choices=["all", "image", "video"], default="all", help="Filter media type")
    parser.add_argument("--max-pages", type=int, default=500, help="Maximum thread pages to scan")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent page fetch workers (1-8)")
    parser.add_argument("--resolve-workers", type=int, default=6, help="Concurrent link resolver workers (1-12)")
    parser.add_argument("--dl-workers", type=int, default=6, help="Concurrent download workers (1-12)")
    parser.add_argument("--pool-size", type=int, default=20, help="HTTP connection pool size")
    parser.add_argument("--page-delay", type=float, default=0.15, help="Base delay in seconds between page fetches")
    parser.add_argument("--include-quotes", action="store_true", help="Include links inside quoted blocks")
    parser.add_argument("--gofile-token", default="", help="Optional Gofile API token")
    parser.add_argument("--single-page", action="store_true", help="Only scrape the given page")
    parser.add_argument("--links-only", action="store_true", help="Save resolved URLs to links.txt")
    parser.add_argument("--resume", action="store_true", help="Load existing links.txt and append new resolved URLs")
    parser.add_argument("--no-keepalive", action="store_true", help="Disable HTTP keep-alive connection pooling")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("-q", "--quiet", action="store_true", help="Compact output for long threads")
    args = parser.parse_args()

    raw_arg = args.url.strip()
    md_match = re.match(r"^\[[^\]]*\]\((https?://[^)\s]+)\)$", raw_arg)
    if md_match:
        args.url = md_match.group(1)
    else:
        args.url = raw_arg.strip("<>\"'")

    global UI
    UI = TerminalUI(force_no_color=args.no_color, quiet=args.quiet)
    UI.banner()
    UI.info(f"Output dir: {UI.dim(args.out)} {UI.dim('|')} Filter: {UI.yellow(args.media)}", force=True)

    dl = Downloader(
        cookie_header=args.cookie,
        user_agent=args.user_agent,
        out_dir=args.out,
        gofile_token=args.gofile_token,
        page_delay=args.page_delay,
        no_keepalive=args.no_keepalive,
        pool_size=args.pool_size,
    )

    t_start_total = time.monotonic()
    UI.phase("Scrape", "Scanning thread posts, media tags & spoilers")
    performer, raw_entries = dl.scrape_thread(
        args.url,
        max_pages=max(1, args.max_pages),
        single_page=args.single_page,
        include_quotes=args.include_quotes,
        workers=args.workers,
    )
    dest_folder = os.path.join(dl.out_dir, performer)
    links_path = os.path.join(dest_folder, "links.txt")

    resolved_urls = load_existing_links(links_path) if args.resume else []
    seen_resolved = set(resolved_urls)
    if args.resume and resolved_urls:
        UI.info(f"Loaded {UI.cyan(len(resolved_urls))} existing URLs from {UI.dim(links_path)}", force=True)

    links_fh = None
    if args.links_only:
        os.makedirs(dest_folder, exist_ok=True)
        mode = "a" if args.resume else "w"
        links_fh = open(links_path, mode, encoding="utf-8")

    resolve_workers = max(1, min(int(args.resolve_workers or 1), 12))
    UI.phase(
        "Resolve",
        f"Expanding {len(raw_entries)} candidate links (w={resolve_workers})",
    )

    resolve_errors = 0
    completed_count = 0
    last_heartbeat = time.monotonic()

    def record_outcome(idx, url, found_urls, err):
        nonlocal resolve_errors, completed_count, last_heartbeat
        completed_count += 1
        label = f"[{idx}/{len(raw_entries)}]"
        if err is not None:
            resolve_errors += 1
            UI.err(f"{UI.dim(label)} {type(err).__name__}: {err}")
            last_heartbeat = time.monotonic()
        else:
            added_here = 0
            for canon_final in found_urls:
                if canon_final in seen_resolved:
                    continue
                seen_resolved.add(canon_final)
                resolved_urls.append(canon_final)
                added_here += 1
                if links_fh is not None:
                    links_fh.write(canon_final + "\n")
            if added_here > 0:
                if links_fh is not None:
                    links_fh.flush()
                UI.ok(f"{UI.dim(label)} +{added_here} media <- {url[:68]}")
                last_heartbeat = time.monotonic()

        now = time.monotonic()
        if (
            completed_count % 25 == 0
            or (now - last_heartbeat) >= 4.0
            or completed_count == len(raw_entries)
        ):
            UI.info(
                f"Progress {UI.cyan(f'{completed_count}/{len(raw_entries)}')} "
                f"{UI.dim('|')} Media found: {UI.green(len(resolved_urls))}",
                force=True,
            )
            last_heartbeat = now

    try:
        if raw_entries:
            if resolve_workers > 1 and len(raw_entries) > 1:
                with ThreadPoolExecutor(max_workers=resolve_workers) as pool:
                    futures = [
                        pool.submit(
                            _resolve_single_candidate,
                            dl,
                            idx,
                            len(raw_entries),
                            url,
                            spoilers,
                            args.media,
                        )
                        for idx, (url, spoilers) in enumerate(raw_entries, 1)
                    ]
                    for fut in as_completed(futures):
                        idx, url, found_urls, _, err = fut.result()
                        record_outcome(idx, url, found_urls, err)
            else:
                for idx, (url, spoilers) in enumerate(raw_entries, 1):
                    _, _, found_urls, _, err = _resolve_single_candidate(
                        dl, idx, len(raw_entries), url, spoilers, args.media
                    )
                    record_outcome(idx, url, found_urls, err)
    finally:
        if links_fh is not None:
            links_fh.close()

    UI.info(
        f"Resolved {UI.green(len(resolved_urls))} direct URLs "
        f"{UI.dim('|')} Resolver errors: {UI.yellow(resolve_errors) if resolve_errors else UI.dim('0')}",
        force=True,
    )

    if not resolved_urls:
        UI.warn("No direct media URLs found.")
        UI.summary_card([
            ("Thread", performer, UI.bold),
            ("Resolved", 0, UI.yellow),
            ("Downloaded", 0, UI.dim),
            ("Errors", resolve_errors, UI.red if resolve_errors else UI.dim),
        ])
        return

    if args.links_only:
        UI.ok(f"Exported {UI.bold(len(resolved_urls))} links to {UI.cyan(links_path)}", force=True)
        UI.summary_card([
            ("Thread", performer, UI.bold),
            ("Exported", len(resolved_urls), UI.green),
            ("Manifest", links_path, UI.cyan),
        ])
        return

    dl_workers = max(1, min(int(args.dl_workers or 1), 12))
    UI.phase("Download", f"Saving to {dest_folder} (w={dl_workers})")
    downloaded_count = 0
    skipped_count = 0
    dl_errors = 0
    dl_done = 0
    last_dl_beat = time.monotonic()

    if dl_workers > 1 and len(resolved_urls) > 1:
        with ThreadPoolExecutor(max_workers=dl_workers) as pool:
            futures = {
                pool.submit(
                    dl.download_file,
                    url,
                    dest_folder,
                    f"[{idx}/{len(resolved_urls)}]",
                    False,
                ): idx
                for idx, url in enumerate(resolved_urls, 1)
            }
            for fut in as_completed(futures):
                dl_done += 1
                try:
                    status = fut.result()
                except Exception as e:
                    status = "error"
                    UI.err(f"{type(e).__name__}: {e}")
                if status == "downloaded":
                    downloaded_count += 1
                elif status == "skipped":
                    skipped_count += 1
                else:
                    dl_errors += 1

                now = time.monotonic()
                if dl_done % 25 == 0 or (now - last_dl_beat) >= 4.0 or dl_done == len(resolved_urls):
                    elapsed = max(0.1, now - t_start_total)
                    UI.info(
                        f"Downloads {UI.cyan(f'{dl_done}/{len(resolved_urls)}')} "
                        f"{UI.dim('|')} OK: {UI.green(downloaded_count)} "
                        f"{UI.dim('|')} Elapsed: {UI.dim(fmt_eta(elapsed))}",
                        force=True,
                    )
                    last_dl_beat = now
    else:
        for idx, url in enumerate(resolved_urls, 1):
            status = dl.download_file(
                url, dest_folder, index_label=f"[{idx}/{len(resolved_urls)}]", show_progress_bar=True
            )
            if status == "downloaded":
                downloaded_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                dl_errors += 1

    total_elapsed = fmt_eta(time.monotonic() - t_start_total)
    UI.summary_card([
        ("Thread", performer, UI.bold),
        ("Resolved", len(resolved_urls), UI.cyan),
        ("Downloaded", downloaded_count, UI.green),
        ("Skipped", skipped_count, UI.dim),
        ("Errors", resolve_errors + dl_errors, UI.red if (resolve_errors + dl_errors) else UI.dim),
        ("Time", total_elapsed, UI.cyan),
        ("Folder", dest_folder, UI.cyan),
    ])


if __name__ == "__main__":
    main()
