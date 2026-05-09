#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
- Direct download of ALL media types (photos, videos, documents).
- 5MB size limit for file downloads.
- Downloads photos, videos, AND documents (all file types).
- Centers media and shows captions in right‑to‑left (RTL) for Persian.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import jdatetime
import requests
from playwright.async_api import async_playwright

# ---- Configuration ----
MAX_FILE_SIZE_MB = 5
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# ---- Paths ----
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

CHANNELS_FILE = REPO_ROOT / "telegram" / "channels.json"
STATE_FILE    = REPO_ROOT / "telegram" / "last_ids.json"
OUTPUT_FILE   = REPO_ROOT / "telegram.md"
CONTENT_DIR   = REPO_ROOT / "telegram" / "content"

IRAN_TZ = ZoneInfo("Asia/Tehran")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://t.me/",
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
# Helpers
# ----------------------------------------------------------------------
def get_github_base_url():
    try:
        import subprocess
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, cwd=REPO_ROOT
        ).stdout.strip()
        if not remote:
            return None, None

        if remote.startswith("git@"):
            remote = re.sub(r"git@([^:]+):(.+)\.git", r"https://\1/\2", remote)
        elif remote.endswith(".git"):
            remote = remote[:-4]

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT
        ).stdout.strip()

        return remote, branch
    except Exception:
        return None, None


def safe_filename(name: str, max_length: int = 100) -> str:
    if len(name) <= max_length:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return name[:max_length]
    keep = max_length - len(ext) - 4
    if keep <= 0:
        return name[:max_length]
    prefix = stem[:keep // 2]
    suffix = stem[-(keep - keep // 2):]
    return f"{prefix}...{suffix}.{ext}"


def get_extension_from_content_type(content_type: str) -> str:
    ct = content_type.lower().split(';')[0].strip()
    mapping = {
        'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
        'image/gif': '.gif', 'video/mp4': '.mp4', 'video/webm': '.webm',
        'video/quicktime': '.mov', 'audio/mpeg': '.mp3', 'audio/ogg': '.ogg',
        'audio/wav': '.wav', 'audio/aac': '.aac', 'audio/mp4': '.m4a',
        'application/pdf': '.pdf', 'application/zip': '.zip',
        'application/x-rar-compressed': '.rar', 'application/x-7z-compressed': '.7z',
        'application/vnd.android.package-archive': '.apk', 'text/plain': '.txt',
        'application/json': '.json', 'application/octet-stream': '.dat',
    }
    return mapping.get(ct, '.dat')


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


def build_nav_buttons(next_page_rel, prev_page_rel, base_url=None):
    button_style = (
        "display:inline-block; padding:6px 12px; margin:0 4px; "
        "background-color:#2ea44f; color:white; text-decoration:none; "
        "border-radius:4px; font-weight:bold;"
    )
    parts = []
    if prev_page_rel:
        href = urljoin(base_url, prev_page_rel) if base_url else prev_page_rel
        parts.append(f'<a href="{href}" style="{button_style}">صفحه قبل</a>')
    if next_page_rel:
        href = urljoin(base_url, next_page_rel) if base_url else next_page_rel
        parts.append(f'<a href="{href}" style="{button_style}">صفحه بعد</a>')
    return " ".join(parts) if parts else ""


def wrap_page(message_block, next_rel=None, prev_rel=None, base_url=None):
    nav_buttons = build_nav_buttons(next_rel, prev_rel, base_url=base_url)
    top_nav_div = f'<div dir="rtl" style="text-align:left; margin-bottom:10px;">{nav_buttons}</div>' if nav_buttons else ""
    bottom_nav_div = f'<div dir="rtl" style="text-align:left; margin-top:10px;">{nav_buttons}</div>' if nav_buttons else ""

    page = HEADER_TEMPLATE.replace(f"{TOP_NAV_START}\n{TOP_NAV_END}", f"{TOP_NAV_START}\n{top_nav_div}\n{TOP_NAV_END}")
    page = page.replace(f"{MSG_START}\n{MSG_END}", f"{MSG_START}\n{message_block}\n{MSG_END}")
    page = page.replace(f"{NAV_START}\n{NAV_END}", f"{NAV_START}\n{bottom_nav_div}\n{NAV_END}")
    return page


def extract_message_md(md_text):
    start = md_text.find(MSG_START)
    end = md_text.find(MSG_END)
    if start == -1 or end == -1:
        return None
    return md_text[start + len(MSG_START):end].strip()


def get_existing_archives():
    archives = []
    if not CONTENT_DIR.exists():
        return archives
    pattern = re.compile(r"^archive_(\d+)\.md$")
    for f in CONTENT_DIR.iterdir():
        m = pattern.match(f.name)
        if m:
            archives.append((int(m.group(1)), f))
    archives.sort(key=lambda x: x[0])
    return archives


def parse_post_header(header_line):
    line = header_line.strip()
    if not line.startswith("## "):
        return None, None
    m = re.search(r"## (.+?) — post (\d+)", line)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return None, None


def deduplicate_messages(old_block, new_ids_set):
    parts = re.split(r"(?=\n## )", old_block)
    kept = []
    for part in parts:
        first_line = part.split("\n")[0]
        ch, pid = parse_post_header(first_line)
        if pid is not None and ch is not None and (ch, pid) in new_ids_set:
            continue
        kept.append(part)
    return "".join(kept)


# ----------------------------------------------------------------------
# SIMPLE DOWNLOAD - No complexity, just works
# ----------------------------------------------------------------------
def simple_download(url, channel_name, post_id, media_type='photo'):
    """
    Ultra-simple download function.
    Downloads the file, checks size, saves it.
    Returns relative path or None.
    """
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    # Generate a filename
    ext = '.jpg' if media_type == 'photo' else ('.mp4' if media_type == 'video' else '.dat')
    local_name = f"{channel_name}_{post_id}_{int(time.time())}{ext}"
    local_path = CONTENT_DIR / local_name

    try:
        print(f"    ⬇️ Downloading: {url[:80]}...")
        resp = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        resp.raise_for_status()

        # Check Content-Length if available
        content_length = resp.headers.get('Content-Length')
        if content_length:
            size = int(content_length)
            if size > MAX_FILE_SIZE_BYTES:
                print(f"    ⚠️ Too large: {size / (1024*1024):.1f}MB > {MAX_FILE_SIZE_MB}MB")
                return None

        # Try to fix extension from Content-Type
        content_type = resp.headers.get('Content-Type', '').lower().split(';')[0].strip()
        correct_ext = get_extension_from_content_type(content_type)
        if correct_ext and correct_ext != '.dat':
            new_local_name = f"{channel_name}_{post_id}_{int(time.time())}{correct_ext}"
            local_path = CONTENT_DIR / new_local_name
            local_name = new_local_name

        # Write file with size check
        total_size = 0
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    total_size += len(chunk)
                    if total_size > MAX_FILE_SIZE_BYTES:
                        f.close()
                        local_path.unlink()
                        print(f"    ⚠️ Too large during download: {total_size / (1024*1024):.1f}MB")
                        return None
                    f.write(chunk)

        print(f"    ✅ Saved: {local_name} ({total_size / 1024:.1f}KB)")
        return f"telegram/content/{local_name}"

    except Exception as e:
        print(f"    ⚠️ Download failed: {e}")
        if local_path.exists():
            local_path.unlink()
        return None


def download_document(post_url, channel_name, post_id):
    """
    Try to download a document from a Telegram post.
    If we can find the direct download URL, download it.
    Otherwise return None.
    """
    print(f"    📄 Processing document post: {post_url}")

    try:
        # Fetch the post page
        resp = requests.get(post_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text

        # Look for document download link
        # Pattern: <a class="tgme_widget_message_document_wrap" href="...">
        match = re.search(r'class="tgme_widget_message_document_wrap"\s+href="([^"]+)"', html)
        if not match:
            # Alternative: just look for any href that looks like a file download
            match = re.search(r'href="(https?://[^"]+\.(npvt|pdf|zip|apk|rar|7z|txt|json|dat)[^"]*)"', html, re.IGNORECASE)
        
        if not match:
            print(f"    ⚠️ No download link found in post page")
            return None

        download_url = match.group(1)
        if download_url.startswith("/"):
            download_url = "https://t.me" + download_url

        print(f"    🔗 Found download URL: {download_url[:80]}...")

        # Try to extract filename from URL
        parsed = urlparse(download_url)
        path = parsed.path
        filename = None
        if path and "/" in path and "." in path.split("/")[-1]:
            filename = safe_filename(path.split("/")[-1], max_length=100)
            print(f"    📝 Filename from URL: {filename}")

        # Download the actual file
        return simple_download(download_url, channel_name, post_id, media_type='document')

    except Exception as e:
        print(f"    ⚠️ Document processing failed: {e}")
        return None


# ----------------------------------------------------------------------
# Archive management
# ----------------------------------------------------------------------
def shift_archives_for_new_page1(message_block_new_page1, repo_url, branch):
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    old_blocks = {}
    for num, path in get_existing_archives():
        content = path.read_text(encoding="utf-8")
        block = extract_message_md(content)
        if block is None:
            block = content.strip()
        old_blocks[num] = block

    existing = sorted(old_blocks.keys(), reverse=True)
    for num in existing:
        old_path = CONTENT_DIR / f"archive_{num}.md"
        new_path = CONTENT_DIR / f"archive_{num+1}.md"
        if old_path.exists():
            old_path.rename(new_path)

    archive_base = f"{repo_url}/blob/{branch}/telegram/content/" if repo_url and branch else None

    new_page1_path = CONTENT_DIR / "archive_1.md"
    prev_rel = "../../telegram.md"
    next_rel = "archive_2.md" if (2 in [n+1 for n in old_blocks]) else None
    new_page1 = wrap_page(message_block_new_page1, next_rel=next_rel, prev_rel=prev_rel, base_url=archive_base)
    new_page1_path.write_text(new_page1, encoding="utf-8")

    total_archives = len(old_blocks) + 1
    for new_num in range(2, total_archives + 1):
        old_num = new_num - 1
        block = old_blocks.get(old_num, "")
        file_path = CONTENT_DIR / f"archive_{new_num}.md"
        prev_rel = f"archive_{new_num-1}.md"
        next_rel = f"archive_{new_num+1}.md" if new_num < total_archives else None
        page = wrap_page(block, next_rel=next_rel, prev_rel=prev_rel, base_url=archive_base)
        file_path.write_text(page, encoding="utf-8")

    print(f"✅ Archives shifted: new archive_1 created, total pages = {total_archives}")


def split_main_page(new_entries_block, old_messages_block, repo_url, branch):
    test_page = wrap_page(new_entries_block, next_rel=None, prev_rel=None)
    if len(test_page.encode("utf-8")) <= 950 * 1024:
        shift_archives_for_new_page1(old_messages_block, repo_url, branch)
        next_rel_main = "telegram/content/archive_1.md"
        main_base = f"{repo_url}/blob/{branch}/" if repo_url and branch else None
        main_page = wrap_page(new_entries_block, next_rel=next_rel_main, prev_rel=None, base_url=main_base)
        OUTPUT_FILE.write_text(main_page, encoding="utf-8")
        print("✅ Main page updated, old content moved to archive_1.md")
    else:
        print("⚠️ New entries alone exceed 950KB – emergency split.")


# ----------------------------------------------------------------------
# Scraping
# ----------------------------------------------------------------------
async def scrape_channel_all(page, channel_name, last_id, max_scrolls):
    url = f"https://t.me/s/{channel_name}"
    print(f"  🌐 Loading {url} ...")
    await page.goto(url, wait_until="networkidle", timeout=30000)

    try:
        await page.wait_for_selector("[data-post]", timeout=15000)
    except:
        print("    ❌ No messages found on initial page.")
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
                    mediaUrl = videoTag.src;
                    mediaType = 'video';
                }

                // Video wrapper
                if (!mediaUrl) {
                    const videoWrap = el.querySelector('.tgme_widget_message_video_wrap');
                    if (videoWrap) {
                        const vid = videoWrap.querySelector('video');
                        if (vid && vid.src && !vid.src.startsWith('blob:')) {
                            mediaUrl = vid.src;
                            mediaType = 'video';
                        } else {
                            const style = videoWrap.getAttribute('style') || '';
                            const match = style.match(/url\\('(.*?)'\\)/);
                            if (match) { mediaUrl = match[1]; mediaType = 'video'; }
                        }
                    }
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
                    id: postId,
                    text: text,
                    media_url: mediaUrl,
                    media_type: mediaType
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

        print(f"    Scroll {scroll_count}: total unique={len(all_messages)}, new={new_added}")

        if all_messages:
            oldest_id = min(msg["id"] for msg in all_messages)
            if oldest_id <= last_id:
                print(f"    Reached last_id ({last_id}) – stopping.")
                break

        if new_added == 0:
            print("    No new messages – end of history.")
            break

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

        try:
            await page.wait_for_function(
                f"document.querySelectorAll('[data-post]').length > {len(seen_ids)}",
                timeout=5000
            )
        except:
            print("    No further messages loaded.")
            break

    filtered = [m for m in all_messages if m["id"] > last_id]
    filtered.sort(key=lambda x: x["id"], reverse=True)
    return filtered


# ----------------------------------------------------------------------
async def main():
    channels = load_channels()
    state = load_state()
    is_first_run = not state
    scroll_limit = 15 if is_first_run else 50

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        all_messages = []
        for ch_name in channels:
            clean_name = ch_name.lstrip("@")
            last_id = state.get(ch_name, 0)

            msgs = await scrape_channel_all(page, clean_name, last_id, max_scrolls=scroll_limit)
            if not msgs:
                print(f"  ℹ️ No new messages for {ch_name}")
                continue

            for m in msgs:
                m["_channel"] = clean_name
            all_messages.extend(msgs)
            print(f"  ✅ {ch_name}: fetched {len(msgs)} new messages")

        await browser.close()

    # Block .webm
    for m in all_messages:
        if m.get("media_type") == "video" and m.get("media_url", "").lower().endswith(".webm"):
            m["media_url"] = None
            m["media_type"] = None

    repo_url, branch = get_github_base_url()

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
            if media_type in ("photo", "video"):
                media_md = simple_download(media_url, ch, pid, media_type=media_type)
            elif media_type == "document":
                media_md = download_document(media_url, ch, pid)
                if not media_md:
                    # Keep the original URL as fallback
                    media_md = media_url

        header = f"## {ch} — post {pid}\n\n"
        media_html = ""

        if media_md:
            if media_type == "photo" and not media_md.startswith("http"):
                media_html = f'<div align="center">\n  <img src="{media_md}" alt="Photo">\n</div>'
            elif media_type in ("video", "document") and not media_md.startswith("http"):
                filename = media_md.split('/')[-1] if '/' in media_md else media_md
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">📎 {filename}</a>\n</div>'
            elif media_md.startswith("http"):
                # Fallback link
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

    old_messages_block = ""
    if OUTPUT_FILE.exists():
        old_raw = OUTPUT_FILE.read_text(encoding="utf-8")
        extracted = extract_message_md(old_raw)
        old_messages_block = extracted if extracted is not None else ""

    if old_messages_block.strip() and new_ids_set:
        old_messages_block = deduplicate_messages(old_messages_block, new_ids_set)

    if new_entries_block or old_messages_block:
        main_base = f"{repo_url}/blob/{branch}/" if repo_url and branch else None
        trial_page = wrap_page(new_entries_block + old_messages_block, next_rel=None, prev_rel=None, base_url=main_base)
        size = len(trial_page.encode("utf-8"))

        if size > 950 * 1024 and old_messages_block.strip():
            split_main_page(new_entries_block, old_messages_block, repo_url, branch)
        else:
            archives = get_existing_archives()
            next_rel_main = f"telegram/content/archive_{archives[0][0]}.md" if archives else None
            main_page = wrap_page(new_entries_block + old_messages_block, next_rel=next_rel_main, prev_rel=None, base_url=main_base)
            OUTPUT_FILE.write_text(main_page, encoding="utf-8")
            print("✅ Main page updated.")
    else:
        if not OUTPUT_FILE.exists():
            OUTPUT_FILE.write_text(wrap_page("", None, None))

    for ch_name in channels:
        clean_name = ch_name.lstrip("@")
        ch_msgs = [m for m in all_messages if m["_channel"] == clean_name]
        if ch_msgs:
            max_id = max(m["id"] for m in ch_msgs)
            state[ch_name] = max(state.get(ch_name, 0), max_id)

    save_state(state)
    print("✅ Done.")


if __name__ == "__main__":
    asyncio.run(main())