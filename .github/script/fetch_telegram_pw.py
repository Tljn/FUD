#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
Downloads ALL file types (photos, videos, documents) with correct extensions.
Size limit: 1 MB per file.
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

# ---- CONFIG ----
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB

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
    nav_buttons = build_nav_buttons(next_rel, prev_rel, base_url)
    top = f'<div dir="rtl" style="text-align:left; margin-bottom:10px;">{nav_buttons}</div>' if nav_buttons else ""
    bottom = f'<div dir="rtl" style="text-align:left; margin-top:10px;">{nav_buttons}</div>' if nav_buttons else ""
    page = HEADER_TEMPLATE.replace(f"{TOP_NAV_START}\n{TOP_NAV_END}", f"{TOP_NAV_START}\n{top}\n{TOP_NAV_END}")
    page = page.replace(f"{MSG_START}\n{MSG_END}", f"{MSG_START}\n{message_block}\n{MSG_END}")
    page = page.replace(f"{NAV_START}\n{NAV_END}", f"{NAV_START}\n{bottom}\n{NAV_END}")
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
# DOWNLOAD FUNCTIONS (SYNC - for use with requests)
# ----------------------------------------------------------------------
def download_media(url, channel_name, post_id, filename=None):
    """Download photos and videos directly from URL."""
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    if filename is None:
        ext = '.jpg' if 'photo' in url else '.mp4'
        local_name = f"{channel_name}_{post_id}_{int(time.time())}{ext}"
    else:
        local_name = safe_filename(filename, max_length=100) if len(filename) > 100 else filename

    local_path = CONTENT_DIR / local_name

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()

        # Check size
        content_length = resp.headers.get('Content-Length')
        if content_length and int(content_length) > MAX_FILE_SIZE:
            print(f"    ⚠️ File too large ({int(content_length) / 1024 / 1024:.1f} MB) - saving link only")
            return url

        # Fix extension from Content-Type
        content_type = resp.headers.get('Content-Type', '').lower()
        ext_map = {
            'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
            'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
            'application/pdf': '.pdf', 'application/zip': '.zip',
            'application/x-rar': '.rar', 'application/x-7z-compressed': '.7z',
            'application/vnd.android.package-archive': '.apk',
            'application/octet-stream': None,  # Don't guess
        }
        correct_ext = None
        for mime, ext in ext_map.items():
            if mime in content_type:
                correct_ext = ext
                break

        if correct_ext and not local_name.endswith(correct_ext):
            new_name = str(Path(local_name).stem) + correct_ext
            local_path = CONTENT_DIR / new_name
            local_name = new_name
            print(f"    ℹ️ Fixed extension -> {local_name}")

        local_path.write_bytes(resp.content)
        print(f"    ✅ Downloaded: {local_name} ({len(resp.content)/1024:.1f} KB)")
        return f"telegram/content/{local_name}"

    except Exception as e:
        print(f"    ⚠️ Download failed: {e}")
        return url  # Return original URL as fallback


async def download_document_with_playwright(browser, post_url, channel_name, post_id):
    """
    Open the Telegram post page with Playwright, find the actual download button,
    and download the document file with the correct filename and extension.
    """
    print(f"    📄 Opening post: {post_url}")

    page = await browser.new_page()
    try:
        await page.goto(post_url, wait_until="networkidle", timeout=20000)

        # Wait for the document widget to appear
        try:
            await page.wait_for_selector('.tgme_widget_message_document_wrap', timeout=10000)
        except:
            print("    ⚠️ No document widget found on page")
            await page.close()
            return post_url  # Fallback to post link

        # Extract the download URL and filename
        result = await page.evaluate("""() => {
            const docLink = document.querySelector('a.tgme_widget_message_document_wrap');
            if (!docLink) return null;

            const href = docLink.getAttribute('href');
            if (!href) return null;

            // Get filename from the document title
            const titleEl = document.querySelector('.tgme_widget_message_document_title');
            const filename = titleEl ? titleEl.textContent.trim() : null;

            // Get file size from the document extra info
            const extraEl = document.querySelector('.tgme_widget_message_document_extra');
            const sizeStr = extraEl ? extraEl.textContent.trim() : '';

            return {
                url: href.startsWith('/') ? 'https://t.me' + href : href,
                filename: filename,
                sizeStr: sizeStr
            };
        }""")

        await page.close()

        if not result or not result.get('url'):
            print("    ⚠️ Could not extract download URL")
            return post_url

        doc_url = result['url']
        filename = result.get('filename')
        size_str = result.get('sizeStr', '')

        print(f"    📎 Found: {filename or 'unknown'} ({size_str})")
        print(f"    ⬇️ Download URL: {doc_url}")

        # Parse size to check against limit
        size_match = re.search(r'([\d.]+)\s*(MB|KB|GB|B)', size_str, re.IGNORECASE)
        if size_match:
            size_val = float(size_match.group(1))
            size_unit = size_match.group(2).upper()
            if size_unit == 'GB':
                size_bytes = size_val * 1024 * 1024 * 1024
            elif size_unit == 'MB':
                size_bytes = size_val * 1024 * 1024
            elif size_unit == 'KB':
                size_bytes = size_val * 1024
            else:
                size_bytes = size_val

            if size_bytes > MAX_FILE_SIZE:
                print(f"    ⚠️ File too large ({size_bytes/1024/1024:.1f} MB) - providing direct link")
                return doc_url

        # Download the file
        CONTENT_DIR.mkdir(parents=True, exist_ok=True)

        if filename:
            local_name = safe_filename(filename, max_length=100)
        else:
            # Extract from URL
            parsed = urlparse(doc_url)
            path = parsed.path
            if path and '/' in path:
                local_name = path.split('/')[-1] or f"{channel_name}_{post_id}.dat"
            else:
                local_name = f"{channel_name}_{post_id}.dat"

        local_path = CONTENT_DIR / local_name

        try:
            resp = requests.get(doc_url, headers=HEADERS, timeout=60, allow_redirects=True)
            resp.raise_for_status()

            # Check actual downloaded size
            if len(resp.content) > MAX_FILE_SIZE:
                print(f"    ⚠️ Downloaded file too large ({len(resp.content)/1024/1024:.1f} MB) - providing direct link")
                return doc_url

            # Try to fix extension from Content-Type
            ct = resp.headers.get('Content-Type', '').lower()
            ct_map = {
                'application/pdf': '.pdf',
                'application/zip': '.zip',
                'application/x-rar': '.rar',
                'application/vnd.android.package-archive': '.apk',
                'application/x-msdownload': '.exe',
                'text/plain': '.txt',
                'application/json': '.json',
            }

            # Check if filename already has a valid extension
            current_ext = Path(local_name).suffix.lower()
            if current_ext in ['.npvt', '.pdf', '.zip', '.rar', '.apk', '.txt', '.json', '.exe']:
                pass  # Keep existing extension
            else:
                for mime, ext in ct_map.items():
                    if mime in ct:
                        new_name = str(Path(local_name).stem) + ext
                        local_path = CONTENT_DIR / new_name
                        local_name = new_name
                        break

            local_path.write_bytes(resp.content)
            print(f"    ✅ Downloaded: {local_name} ({len(resp.content)/1024:.1f} KB)")
            return f"telegram/content/{local_name}"

        except Exception as e:
            print(f"    ⚠️ Download failed: {e}")
            return doc_url  # Return direct URL as fallback

    except Exception as e:
        print(f"    ⚠️ Playwright extraction failed: {e}")
        try:
            await page.close()
        except:
            pass
        return post_url


# ----------------------------------------------------------------------
# ARCHIVE SHIFTING
# ----------------------------------------------------------------------
def shift_archives_for_new_page1(message_block, repo_url, branch):
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
    page = wrap_page(message_block, next_rel=next_rel, prev_rel=prev_rel, base_url=archive_base)
    new_page1_path.write_text(page, encoding="utf-8")

    total = len(old_blocks) + 1
    for new_num in range(2, total + 1):
        old_num = new_num - 1
        block = old_blocks.get(old_num, "")
        file_path = CONTENT_DIR / f"archive_{new_num}.md"
        prev_rel = f"archive_{new_num-1}.md"
        next_rel = f"archive_{new_num+1}.md" if new_num < total else None
        page = wrap_page(block, next_rel=next_rel, prev_rel=prev_rel, base_url=archive_base)
        file_path.write_text(page, encoding="utf-8")

    print(f"✅ Archives shifted: new archive_1 created, total pages = {total}")


def split_main_page(new_entries_block, old_messages_block, repo_url, branch):
    test_page = wrap_page(new_entries_block)
    if len(test_page.encode("utf-8")) <= 950 * 1024:
        shift_archives_for_new_page1(old_messages_block, repo_url, branch)
        main_base = f"{repo_url}/blob/{branch}/" if repo_url and branch else None
        main_page = wrap_page(new_entries_block, next_rel="telegram/content/archive_1.md", base_url=main_base)
        OUTPUT_FILE.write_text(main_page, encoding="utf-8")
        print("✅ Main page updated, old content moved to archive_1.md")
    else:
        print("⚠️ New entries too large – truncating.")


# ----------------------------------------------------------------------
# SCRAPING
# ----------------------------------------------------------------------
async def scrape_channel_all(page, channel_name, last_id, max_scrolls):
    url = f"https://t.me/s/{channel_name}"
    print(f"  🌐 Loading {url} ...")
    await page.goto(url, wait_until="networkidle", timeout=30000)

    try:
        await page.wait_for_selector("[data-post]", timeout=15000)
    except:
        print("    ❌ No messages found.")
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

                // Video tag
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

                // Photo wrap
                if (!mediaUrl) {
                    const photoWrap = el.querySelector('.tgme_widget_message_photo_wrap');
                    if (photoWrap) {
                        const style = photoWrap.getAttribute('style') || '';
                        const match = style.match(/url\\('(.*?)'\\)/);
                        if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                    }
                }

                // Link photo wrap (video with poster)
                if (!mediaUrl) {
                    const linkPhoto = el.querySelector('a.tgme_widget_message_photo_wrap');
                    if (linkPhoto) {
                        const videoInside = linkPhoto.querySelector('video');
                        if (videoInside && videoInside.src && !videoInside.src.startsWith('blob:')) {
                            mediaUrl = videoInside.src;
                            mediaType = 'video';
                        } else {
                            const style = linkPhoto.getAttribute('style') || '';
                            const match = style.match(/url\\('(.*?)'\\)/);
                            if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                        }
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

        print(f"    Scroll {scroll_count}: total={len(all_messages)}, new={new_added}")

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
            print("    No more messages loaded.")
            break

    filtered = [m for m in all_messages if m["id"] > last_id]
    filtered.sort(key=lambda x: x["id"], reverse=True)
    return filtered


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
async def main():
    channels = load_channels()
    state = load_state()
    is_first_run = not state
    scroll_limit = 15 if is_first_run else 50

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ---- Phase 1: Scrape all channels ----
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
            print(f"  ✅ {ch_name}: {len(msgs)} new messages")

        await page.close()

        # ---- Phase 2: Download media and documents ----
        print(f"\n📥 Downloading media for {len(all_messages)} messages...")

        for msg in all_messages:
            ch = msg["_channel"]
            pid = msg["id"]
            media_type = msg.get("media_type")
            media_url = msg.get("media_url")

            if not media_url:
                continue

            if media_type == "photo":
                result = download_media(media_url, ch, pid)
                msg["_downloaded"] = result
                msg["_media_type"] = "photo"
                print(f"  📷 Photo: {result}")

            elif media_type == "video":
                # Skip .webm (animated stickers)
                if media_url.lower().endswith('.webm'):
                    msg["_downloaded"] = None
                    msg["_media_type"] = None
                    print(f"  ⏭️ Skipping .webm: {media_url}")
                else:
                    result = download_media(media_url, ch, pid)
                    msg["_downloaded"] = result
                    msg["_media_type"] = "video"
                    print(f"  🎬 Video: {result}")

            elif media_type == "document":
                # Download using Playwright to get the real file
                result = await download_document_with_playwright(
                    browser, media_url, ch, pid
                )
                msg["_downloaded"] = result
                msg["_media_type"] = "document"
                print(f"  📎 Document: {result}")

        await browser.close()

    # ---- Phase 3: Build markdown ----
    repo_url, branch = get_github_base_url()
    now_jalali = jdatetime.datetime.now(IRAN_TZ)
    update_header = f"\n---\n📅 بروزرسانی: {now_jalali.strftime('%Y/%m/%d %H:%M')}\n---\n\n"

    new_entries_list = []
    new_ids_set = set()

    for msg in all_messages:
        ch = msg["_channel"]
        pid = msg["id"]
        new_ids_set.add((ch, pid))

        downloaded = msg.get("_downloaded")
        media_type = msg.get("_media_type")

        header = f"## {ch} — post {pid}\n\n"
        media_html = ""

        if downloaded and media_type:
            if media_type == "photo":
                media_html = f'<div align="center">\n  <img src="{downloaded}" alt="Photo">\n</div>'
            elif media_type == "video":
                media_html = f'<div align="center">\n  <a href="{downloaded}" target="_blank">🎬 Download video</a>\n</div>'
            elif media_type == "document":
                # Icon based on extension
                ext = downloaded.split('.')[-1].lower() if '.' in downloaded else ''
                icons = {'npvt': '🔧', 'pdf': '📕', 'zip': '📦', 'rar': '📦', 'apk': '📱', 'mp3': '🎵'}
                icon = icons.get(ext, '📎')
                media_html = f'<div align="center">\n  <a href="{downloaded}" target="_blank">{icon} Download file</a>\n</div>'

        caption = msg.get("text", "") or ({"photo": "📷 Photo", "video": "🎬 Video", "document": "📎 Document"}.get(media_type, ""))
        caption_style = "dir='rtl' style='font-family: \"Vazirmatn\", Tahoma, sans-serif;'"
        caption_div = f'<div {caption_style}>\n{caption}\n</div>' if caption else ""

        entry = header + media_html + "\n" + caption_div + "\n\n"
        new_entries_list.append(entry)

    new_entries_block = update_header + "".join(new_entries_list)

    if not new_entries_list:
        caption_style = "dir='rtl' style='font-family: \"Vazirmatn\", Tahoma, sans-serif;'"
        new_entries_block += f'<div {caption_style}>\nهیچ پیام جدیدی در این بروزرسانی ارسال نشد.\n</div>\n\n'

    # ---- Load existing content ----
    old_messages_block = ""
    if OUTPUT_FILE.exists():
        old_raw = OUTPUT_FILE.read_text(encoding="utf-8")
        extracted = extract_message_md(old_raw)
        old_messages_block = extracted if extracted is not None else ""

    if old_messages_block.strip() and new_ids_set:
        old_messages_block = deduplicate_messages(old_messages_block, new_ids_set)

    # ---- Write output ----
    if new_entries_block or old_messages_block:
        main_base = f"{repo_url}/blob/{branch}/" if repo_url and branch else None
        combined = new_entries_block + old_messages_block
        trial = wrap_page(combined, base_url=main_base)

        if len(trial.encode("utf-8")) > 950 * 1024 and old_messages_block.strip():
            split_main_page(new_entries_block, old_messages_block, repo_url, branch)
        else:
            next_rel = None
            archives = get_existing_archives()
            if archives:
                next_rel = f"telegram/content/archive_{archives[0][0]}.md"
            final_page = wrap_page(combined, next_rel=next_rel, base_url=main_base)
            OUTPUT_FILE.write_text(final_page, encoding="utf-8")
            print("✅ Main page updated.")
    else:
        if not OUTPUT_FILE.exists():
            OUTPUT_FILE.write_text(wrap_page("", base_url=f"{repo_url}/blob/{branch}/" if repo_url and branch else None))

    # ---- Update state ----
    for ch_name in channels:
        clean_name = ch_name.lstrip("@")
        ch_msgs = [m for m in all_messages if m["_channel"] == clean_name]
        if ch_msgs:
            max_id = max(m["id"] for m in ch_msgs)
            state[ch_name] = max(state.get(ch_name, 0), max_id)

    save_state(state)
    print("✅ State saved.")


if __name__ == "__main__":
    asyncio.run(main())