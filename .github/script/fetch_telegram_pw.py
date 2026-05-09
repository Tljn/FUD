#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
- Downloads ALL media: photos, videos, documents (npvt, pdf, zip, etc.)
- NO file size limits
- Direct download links (not Telegram post links)
- Fast execution with timeout limits
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
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

# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# Media download — NO size limit, correct extensions
# ----------------------------------------------------------------------
def download_media(url, channel_name, post_id, media_type='photo', filename=None):
    """Download any file from URL. Returns relative path or None."""
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Quick HEAD request to get content-type and size (max 5 sec timeout)
    try:
        head_resp = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True)
        content_type = head_resp.headers.get('Content-Type', '').lower()
    except:
        content_type = ''
    
    # Determine extension from content-type
    ext_map = {
        'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
        'image/gif': '.gif', 'image/svg+xml': '.svg',
        'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
        'video/x-matroska': '.mkv', 'video/x-msvideo': '.avi',
        'audio/mpeg': '.mp3', 'audio/ogg': '.ogg', 'audio/wav': '.wav',
        'audio/mp4': '.m4a', 'audio/aac': '.aac', 'audio/flac': '.flac',
        'application/pdf': '.pdf', 'application/zip': '.zip',
        'application/x-rar-compressed': '.rar', 'application/x-7z-compressed': '.7z',
        'application/vnd.android.package-archive': '.apk',
        'application/octet-stream': '',  # Unknown — keep original
    }
    
    ext = ''
    for mime, extension in ext_map.items():
        if mime in content_type:
            ext = extension
            break
    
    # Generate filename
    if filename:
        safe_name = filename[:100]
    else:
        safe_name = f"{channel_name}_{post_id}_{int(time.time())}"
    
    # Add/correct extension
    if ext:
        if not safe_name.endswith(ext):
            safe_name = Path(safe_name).stem + ext
    else:
        # Try to get extension from URL
        from urllib.parse import urlparse
        path = urlparse(url).path
        url_ext = Path(path).suffix.lower()
        if url_ext and len(url_ext) <= 6:
            if not safe_name.endswith(url_ext):
                safe_name = Path(safe_name).stem + url_ext
        elif not Path(safe_name).suffix:
            safe_name += '.dat'  # fallback
    
    local_path = CONTENT_DIR / safe_name
    
    # Skip if already exists
    if local_path.exists():
        return f"telegram/content/{safe_name}"
    
    # Download with NO size limit
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
        
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return f"telegram/content/{safe_name}"
    except Exception as e:
        print(f"    ⚠️ Download failed: {e}")
        return None


def download_document(post_url, channel_name, post_id):
    """Download document from Telegram post — follows redirect to actual file."""
    try:
        resp = requests.get(post_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
        
        # Find document download link
        match = re.search(
            r'<a\s[^>]*class="tgme_widget_message_document_wrap"[^>]*\shref="([^"]+)"',
            html
        )
        if not match:
            print("    ⚠️ No document link found")
            return None
        
        doc_url = match.group(1)
        if doc_url.startswith("/"):
            doc_url = "https://t.me" + doc_url
        
        # Follow the document page to get direct download URL
        try:
            doc_resp = requests.get(doc_url, headers=HEADERS, timeout=15, allow_redirects=True)
            final_url = doc_resp.url
            
            # Try to get filename from Content-Disposition header
            cd = doc_resp.headers.get('Content-Disposition', '')
            filename = None
            if 'filename=' in cd:
                import cgi
                _, params = cgi.parse_header(cd)
                filename = params.get('filename', None)
            
            if not filename:
                # Extract from URL path
                from urllib.parse import urlparse
                path = urlparse(final_url).path
                filename = Path(path).name or None
            
            if not filename:
                filename = f"{channel_name}_{post_id}"
            
            # Decode URL-encoded filename
            from urllib.parse import unquote
            filename = unquote(filename)
            
            return download_media(final_url, channel_name, post_id, 
                                  media_type='document', filename=filename)
        except Exception as e:
            print(f"    ⚠️ Failed to get direct URL: {e}")
            # Fallback: try downloading from the document page URL
            return download_media(doc_url, channel_name, post_id,
                                  media_type='document')
    
    except Exception as e:
        print(f"    ⚠️ Document page fetch failed: {e}")
        return None


# ----------------------------------------------------------------------
# Scraping — FAST with reduced waits
# ----------------------------------------------------------------------
async def scrape_channel_all(page, channel_name, last_id, max_scrolls):
    url = f"https://t.me/s/{channel_name}"
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)  # Faster than networkidle
    
    try:
        await page.wait_for_selector("[data-post]", timeout=8000)
    except:
        print("    ❌ No messages found")
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

                // Video
                const videoTag = el.querySelector('video');
                if (videoTag && videoTag.src && !videoTag.src.startsWith('blob:')) {
                    mediaUrl = videoTag.src; mediaType = 'video';
                }
                // Photo
                if (!mediaUrl) {
                    const photoWrap = el.querySelector('.tgme_widget_message_photo_wrap');
                    if (photoWrap) {
                        const style = photoWrap.getAttribute('style') || '';
                        const match = style.match(/url\\('(.*?)'\\)/);
                        if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                    }
                }
                // Document
                if (!mediaUrl) {
                    const docWrap = el.querySelector('a.tgme_widget_message_document_wrap');
                    if (docWrap) {
                        mediaUrl = 'https://t.me/' + channel + '/' + postId;
                        mediaType = 'document';
                    }
                }

                msgs.push({
                    id: postId, text: text,
                    media_url: mediaUrl, media_type: mediaType
                });
            });
            return msgs;
        }""")

        new_added = 0
        for m in current_msgs:
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                all_messages.append(m)
                new_added += 1

        if all_messages:
            oldest_id = min(msg["id"] for msg in all_messages)
            if oldest_id <= last_id:
                break

        if new_added == 0:
            break

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)  # Reduced from 2s

        try:
            await page.wait_for_function(
                f"document.querySelectorAll('[data-post]').length > {len(seen_ids)}",
                timeout=4000  # Reduced from 5s
            )
        except:
            break

    filtered = [m for m in all_messages if m["id"] > last_id]
    filtered.sort(key=lambda x: x["id"], reverse=True)
    return filtered


# ----------------------------------------------------------------------
async def main():
    channels = load_channels()
    state = load_state()
    is_first_run = not state
    scroll_limit = 8 if is_first_run else 30  # Reduced for speed

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        all_messages = []
        for ch_name in channels:
            clean_name = ch_name.lstrip("@")
            last_id = state.get(ch_name, 0)
            msgs = await scrape_channel_all(page, clean_name, last_id, max_scrolls=scroll_limit)
            if not msgs:
                continue
            for m in msgs:
                m["_channel"] = clean_name
            all_messages.extend(msgs)
            print(f"  ✅ {ch_name}: {len(msgs)} new")

        await browser.close()

    # Download ALL media
    now_jalali = jdatetime.datetime.now(IRAN_TZ)
    update_header = f"\n---\n📅 بروزرسانی: {now_jalali.strftime('%Y/%m/%d %H:%M')}\n---\n\n"
    new_entries_list = []
    new_ids_set = set()

    for msg in all_messages:
        ch = msg["_channel"]
        pid = msg["id"]
        new_ids_set.add((ch, pid))

        media_md = None
        media_type = msg.get("media_type")
        media_url = msg.get("media_url")

        if media_url:
            if media_type == "document":
                media_md = download_document(media_url, ch, pid)
            elif media_type in ("photo", "video"):
                media_md = download_media(media_url, ch, pid, media_type=media_type)

        header = f"## {ch} — post {pid}\n\n"
        media_html = ""
        if media_md:
            if media_type == "photo":
                media_html = f'<div align="center">\n  <img src="{media_md}" alt="Photo">\n</div>'
            elif media_type == "video":
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">🎬 Download video</a>\n</div>'
            elif media_type == "document":
                # Direct link to downloaded file
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">📎 Download file</a>\n</div>'

        caption = msg.get("text", "")
        caption_style = "dir='rtl' style='font-family: \"Vazirmatn\", Tahoma, sans-serif;'"
        caption_div = f'<div {caption_style}>\n{caption}\n</div>' if caption else ""

        entry = header + media_html + "\n" + caption_div + "\n\n"
        new_entries_list.append(entry)

    new_entries_block = update_header + "".join(new_entries_list)

    if not new_entries_list:
        caption_style = "dir='rtl' style='font-family: \"Vazirmatn\", Tahoma, sans-serif;'"
        new_entries_block += f'<div {caption_style}>\nهیچ پیام جدیدی در این بروزرسانی ارسال نشد.\n</div>\n\n'

    # Load existing content
    old_messages_block = ""
    if OUTPUT_FILE.exists():
        old_raw = OUTPUT_FILE.read_text(encoding="utf-8")
        start = old_raw.find(MSG_START)
        end = old_raw.find(MSG_END)
        if start != -1 and end != -1:
            old_messages_block = old_raw[start + len(MSG_START):end].strip()

    if old_messages_block and new_ids_set:
        # Deduplicate
        parts = re.split(r"(?=\n## )", old_messages_block)
        kept = []
        for part in parts:
            first_line = part.split("\n")[0]
            m = re.search(r"## (.+?) — post (\d+)", first_line)
            if m:
                ch_name = m.group(1).strip()
                post_id = int(m.group(2))
                if (ch_name, post_id) in new_ids_set:
                    continue
            kept.append(part)
        old_messages_block = "".join(kept)

    # Write output
    final_block = new_entries_block + old_messages_block
    page_content = HEADER_TEMPLATE.replace(
        f"{MSG_START}\n{MSG_END}",
        f"{MSG_START}\n{final_block}\n{MSG_END}"
    )
    OUTPUT_FILE.write_text(page_content, encoding="utf-8")

    # Update state
    for ch_name in channels:
        clean_name = ch_name.lstrip("@")
        ch_msgs = [m for m in all_messages if m["_channel"] == clean_name]
        if ch_msgs:
            max_id = max(m["id"] for m in ch_msgs)
            state[ch_name] = max(state.get(ch_name, 0), max_id)
    save_state(state)

    print(f"✅ Done — {len(all_messages)} total new messages")


if __name__ == "__main__":
    asyncio.run(main())