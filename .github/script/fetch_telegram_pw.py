#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
Fixed: Direct file downloads with correct extensions.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs
from zoneinfo import ZoneInfo

import jdatetime
import requests
from playwright.async_api import async_playwright

# ---- Paths ----
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

CHANNELS_FILE = REPO_ROOT / "telegram" / "channels.json"
STATE_FILE    = REPO_ROOT / "telegram" / "last_ids.json"
OUTPUT_FILE   = REPO_ROOT / "telegram.md"
CONTENT_DIR   = REPO_ROOT / "telegram" / "content"

IRAN_TZ = ZoneInfo("Asia/Tehran")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

MSG_START = "<!-- MSG START -->"
MSG_END   = "<!-- MSG END -->"
TOP_NAV_START = "<!-- TOP_NAV START -->"
TOP_NAV_END   = "<!-- TOP_NAV END -->"
NAV_START = "<!-- NAV START -->"
NAV_END   = "<!-- NAV END -->"

HEADER_TEMPLATE = f"""\
# خواننده تلگرام

{TOP_NAV_START}
{TOP_NAV_END}

{MSG_START}
{MSG_END}

{NAV_START}
{NAV_END}
"""


def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ======================================================================
# DOWNLOAD HELPERS — Fixed to get correct filenames
# ======================================================================

# Map MIME types to extensions
MIME_TO_EXT = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
    'image/gif': '.gif', 'image/svg+xml': '.svg', 'image/bmp': '.bmp',
    'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
    'video/x-matroska': '.mkv', 'video/x-msvideo': '.avi',
    'audio/mpeg': '.mp3', 'audio/ogg': '.ogg', 'audio/wav': '.wav',
    'audio/mp4': '.m4a', 'audio/aac': '.aac', 'audio/flac': '.flac',
    'audio/webm': '.weba',
    'application/pdf': '.pdf', 'application/zip': '.zip',
    'application/x-rar-compressed': '.rar', 'application/x-7z-compressed': '.7z',
    'application/vnd.android.package-archive': '.apk',
    'application/x-msdownload': '.exe',
    'text/plain': '.txt', 'text/html': '.html',
    'application/json': '.json', 'application/xml': '.xml',
}


def get_ext_from_content_type(content_type: str) -> str:
    """Get file extension from Content-Type header."""
    if not content_type:
        return ''
    ct = content_type.split(';')[0].strip().lower()
    return MIME_TO_EXT.get(ct, '')


def get_ext_from_url(url: str) -> str:
    """Extract file extension from URL path."""
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext and len(ext) <= 6 and not any(c in ext for c in ['?', '&', '#']):
        return ext
    return ''


def get_filename_from_headers(headers) -> str | None:
    """Try to get filename from Content-Disposition header."""
    cd = headers.get('Content-Disposition', '') or headers.get('content-disposition', '')
    if not cd:
        return None
    
    # Try filename*= (RFC 5987)
    match = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')(.+?)(?:;|$)", cd, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    
    # Try filename=
    match = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
    if match:
        return match.group(1)
    
    match = re.search(r"filename\s*=\s*'([^']+)'", cd, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


def download_direct(url: str, channel: str, post_id: int, 
                    content_type: str = '', filename: str = None) -> str | None:
    """
    Download a file directly from URL.
    Determines correct extension from Content-Type header or URL.
    Returns relative path like 'telegram/content/filename.ext'
    """
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Build base name
    if filename:
        base = Path(filename).stem[:80]
    else:
        base = f"{channel}_{post_id}"
    
    # Get extension
    ext = ''
    
    # Priority 1: Content-Type header
    if content_type:
        ext = get_ext_from_content_type(content_type)
    
    # Priority 2: URL extension
    if not ext:
        ext = get_ext_from_url(url)
    
    # Priority 3: Original filename extension
    if not ext and filename:
        orig_ext = Path(filename).suffix.lower()
        if orig_ext and len(orig_ext) <= 6:
            ext = orig_ext
    
    # Fallback
    if not ext:
        ext = '.dat'
    
    # Clean base — Remove any existing extension to avoid double extensions
    existing_ext = Path(base).suffix.lower()
    if existing_ext and existing_ext == ext:
        base = Path(base).stem
    
    final_name = f"{base}{ext}"
    # Ensure safe length
    if len(final_name) > 120:
        final_name = f"{base[:80]}{ext}"
    
    local_path = CONTENT_DIR / final_name
    
    # Skip if exists
    if local_path.exists():
        return f"telegram/content/{final_name}"
    
    # Download
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
        
        # Re-check content-type from actual response
        actual_ct = resp.headers.get('Content-Type', '').lower()
        actual_ext = get_ext_from_content_type(actual_ct)
        
        # If actual extension differs, update filename
        if actual_ext and actual_ext != ext:
            ext = actual_ext
            final_name = f"{base}{ext}"
            local_path = CONTENT_DIR / final_name
        
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"    ✅ Downloaded: {final_name}")
        return f"telegram/content/{final_name}"
    
    except Exception as e:
        print(f"    ⚠️ Download failed for {url}: {e}")
        return None


def download_document(post_url: str, channel: str, post_id: int) -> str | None:
    """
    For document-type posts: Visit the post page, find the actual download link,
    then download the file directly.
    """
    try:
        # Step 1: Get the post page
        resp = requests.get(post_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
        
        # Step 2: Find the document wrapper link
        # Pattern: <a class="tgme_widget_message_document_wrap" href="...">
        match = re.search(
            r'<a\s+[^>]*class="tgme_widget_message_document_wrap"[^>]*\s+href="([^"]+)"',
            html
        )
        if not match:
            print(f"    ⚠️ No document wrap link found in {post_url}")
            return None
        
        doc_page_url = match.group(1)
        if doc_page_url.startswith("/"):
            doc_page_url = "https://t.me" + doc_page_url
        
        print(f"    📄 Document page: {doc_page_url}")
        
        # Step 3: Get the document page (this usually redirects to the actual file)
        doc_resp = requests.get(doc_page_url, headers=HEADERS, timeout=20, 
                                allow_redirects=False)
        
        # Check for redirect (most common case)
        if doc_resp.status_code in (301, 302, 303, 307, 308):
            redirect_url = doc_resp.headers.get('Location', '')
            if redirect_url:
                if redirect_url.startswith("/"):
                    redirect_url = "https://t.me" + redirect_url
                print(f"    ↪️ Redirect to: {redirect_url[:100]}...")
                return download_direct(redirect_url, channel, post_id)
        
        # If no redirect, parse the page for download link
        doc_html = doc_resp.text
        content_type = doc_resp.headers.get('Content-Type', '')
        
        # If it's already a file (not HTML), download directly
        if 'text/html' not in content_type:
            filename = get_filename_from_headers(doc_resp.headers)
            return download_direct(doc_page_url, channel, post_id, 
                                   content_type=content_type, filename=filename)
        
        # Search for download button/link in the page
        # Pattern 1: <a class="tgme_widget_message_document_link" href="...">
        dl_match = re.search(
            r'<a\s+[^>]*class="[^"]*document[^"]*"[^>]*\s+href="([^"]+)"',
            doc_html
        )
        if dl_match:
            dl_url = dl_match.group(1)
            if dl_url.startswith("/"):
                dl_url = "https://t.me" + dl_url
            return download_direct(dl_url, channel, post_id)
        
        # Pattern 2: Any direct file URL in the page
        file_match = re.search(r'href="(https?://[^"]+\.(?:npvt|pdf|zip|apk|rar|7z)[^"]*)"', 
                               doc_html, re.IGNORECASE)
        if file_match:
            return download_direct(file_match.group(1), channel, post_id)
        
        # Last resort: download the page URL itself (might be the file)
        print(f"    ⚠️ No direct link found, trying page URL as file")
        return download_direct(doc_page_url, channel, post_id, content_type=content_type)
    
    except Exception as e:
        print(f"    ⚠️ Document download failed: {e}")
        return None


# ======================================================================
# SCRAPING
# ======================================================================
async def scrape_channel_all(page, channel_name, last_id, max_scrolls):
    url = f"https://t.me/s/{channel_name}"
    print(f"  🌐 {channel_name}")
    await page.goto(url, wait_until="domcontentloaded", timeout=12000)

    try:
        await page.wait_for_selector("[data-post]", timeout=6000)
    except:
        print("    ❌ No messages")
        return []

    all_messages = []
    seen_ids = set()

    for scroll_count in range(1, max_scrolls + 1):
        current_msgs = await page.evaluate("""() => {
            const containers = document.querySelectorAll('[data-post]');
            const msgs = [];
            containers.forEach(el => {
                const dataPost = el.getAttribute('data-post');
                if (!dataPost) return;
                const parts = dataPost.split('/');
                if (parts.length < 2) return;
                const channel = parts[0];
                const postId = parseInt(parts[1]);
                if (isNaN(postId)) return;

                const textEl = el.querySelector('.tgme_widget_message_text');
                const text = textEl ? textEl.innerText : '';
                let mediaUrl = null, mediaType = null;

                const videoTag = el.querySelector('video');
                if (videoTag && videoTag.src && !videoTag.src.startsWith('blob:')) {
                    mediaUrl = videoTag.src; mediaType = 'video';
                }
                if (!mediaUrl) {
                    const photoWrap = el.querySelector('.tgme_widget_message_photo_wrap');
                    if (photoWrap) {
                        const style = photoWrap.getAttribute('style') || '';
                        const match = style.match(/url\\('(.*?)'\\)/);
                        if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                    }
                }
                if (!mediaUrl) {
                    const docWrap = el.querySelector('a.tgme_widget_message_document_wrap');
                    if (docWrap) {
                        mediaUrl = 'https://t.me/' + channel + '/' + postId;
                        mediaType = 'document';
                    }
                }
                msgs.push({id: postId, text, media_url: mediaUrl, media_type: mediaType});
            });
            return msgs;
        }""")

        for m in current_msgs:
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                all_messages.append(m)

        if all_messages:
            oldest = min(m["id"] for m in all_messages)
            if oldest <= last_id:
                break

        if scroll_count < max_scrolls:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.2)
            try:
                await page.wait_for_function(
                    f"document.querySelectorAll('[data-post]').length > {len(seen_ids)}",
                    timeout=3000
                )
            except:
                break

    filtered = [m for m in all_messages if m["id"] > last_id]
    filtered.sort(key=lambda x: x["id"], reverse=True)
    return filtered


# ======================================================================
# MAIN
# ======================================================================
async def main():
    channels = load_channels()
    state = load_state()
    is_first_run = not state
    scroll_limit = 8 if is_first_run else 25

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        all_messages = []

        for ch_name in channels:
            clean = ch_name.lstrip("@")
            last_id = state.get(ch_name, 0)
            msgs = await scrape_channel_all(page, clean, last_id, scroll_limit)
            if msgs:
                for m in msgs:
                    m["_channel"] = clean
                all_messages.extend(msgs)
                print(f"  ✅ {ch_name}: {len(msgs)} new")
            else:
                print(f"  ℹ️ {ch_name}: no new")

        await browser.close()

    # Build output
    now = jdatetime.datetime.now(IRAN_TZ)
    update_header = f"\n---\n📅 بروزرسانی: {now.strftime('%Y/%m/%d %H:%M')}\n---\n\n"

    new_entries = []
    new_ids = set()

    for msg in all_messages:
        ch = msg["_channel"]
        pid = msg["id"]
        new_ids.add((ch, pid))

        media_md = None
        mt = msg.get("media_type")
        mu = msg.get("media_url")

        if mu:
            if mt == "document":
                media_md = download_document(mu, ch, pid)
            elif mt in ("photo", "video"):
                media_md = download_direct(mu, ch, pid)

        header = f"## {ch} — post {pid}\n\n"
        media_html = ""
        if media_md:
            if mt == "photo":
                media_html = f'<div align="center">\n  <img src="{media_md}" alt="Photo">\n</div>'
            elif mt == "video":
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">🎬 Download video</a>\n</div>'
            elif mt == "document":
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">📎 Download file</a>\n</div>'

        cap = msg.get("text", "")
        cap_div = f'<div dir="rtl" style="font-family: \\"Vazirmatn\\", Tahoma, sans-serif;">\n{cap}\n</div>' if cap else ""

        new_entries.append(header + media_html + "\n" + cap_div + "\n\n")

    new_block = update_header + "".join(new_entries)

    if not new_entries:
        new_block += '<div dir="rtl" style="font-family: \\"Vazirmatn\\", Tahoma, sans-serif;">\nهیچ پیام جدیدی در این بروزرسانی ارسال نشد.\n</div>\n\n'

    # Load + deduplicate old content
    old_block = ""
    if OUTPUT_FILE.exists():
        raw = OUTPUT_FILE.read_text(encoding="utf-8")
        s = raw.find(MSG_START)
        e = raw.find(MSG_END)
        if s != -1 and e != -1:
            old_block = raw[s + len(MSG_START):e].strip()

    if old_block and new_ids:
        parts = re.split(r"(?=\n## )", old_block)
        kept = []
        for part in parts:
            m = re.search(r"## (.+?) — post (\d+)", part.split("\n")[0])
            if m:
                if (m.group(1).strip(), int(m.group(2))) in new_ids:
                    continue
            kept.append(part)
        old_block = "".join(kept)

    final = new_block + old_block
    page = HEADER_TEMPLATE.replace(
        f"{MSG_START}\n{MSG_END}",
        f"{MSG_START}\n{final}\n{MSG_END}"
    )
    OUTPUT_FILE.write_text(page, encoding="utf-8")

    # Update state
    for ch_name in channels:
        clean = ch_name.lstrip("@")
        ids = [m["id"] for m in all_messages if m["_channel"] == clean]
        if ids:
            state[ch_name] = max(state.get(ch_name, 0), max(ids))
    save_state(state)

    print(f"✅ Done — {len(all_messages)} new posts")


if __name__ == "__main__":
    asyncio.run(main())