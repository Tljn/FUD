#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
- Direct download of documents (npvt, pdf, zip, etc.) with 5MB size limit.
- Downloads photos, videos, AND documents using Playwright's built-in download.
- Adds a Hijri‑Shamsi update timestamp for each script run.
- Sorts messages by ID (newest first) across channels.
- Handles file size limit with archive pages.
- Deduplicates posts based on (channel, post_id) to prevent repeats.
- Centers media and shows captions in right‑to‑left (RTL) for Persian.
- Captions use inline Vazirmatn font (falls back to Tahoma if not installed).
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import jdatetime
from playwright.async_api import async_playwright

# ---- Configuration ----
MAX_FILE_SIZE_MB = 5  # Maximum file size to download in MB
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# ---- Paths ----
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

CHANNELS_FILE = REPO_ROOT / "telegram" / "channels.json"
STATE_FILE    = REPO_ROOT / "telegram" / "last_ids.json"
OUTPUT_FILE   = REPO_ROOT / "telegram.md"
CONTENT_DIR   = REPO_ROOT / "telegram" / "content"

IRAN_TZ = ZoneInfo("Asia/Tehran")

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


def build_nav_buttons(next_page_rel: str | None, prev_page_rel: str | None,
                      base_url: str | None = None) -> str:
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


def wrap_page(message_block: str, next_rel: str | None, prev_rel: str | None,
              base_url: str | None = None) -> str:
    nav_buttons = build_nav_buttons(next_rel, prev_rel, base_url=base_url)
    top_nav_div = (
        f'<div dir="rtl" style="text-align:left; margin-bottom:10px;">{nav_buttons}</div>'
        if nav_buttons else ""
    )
    bottom_nav_div = (
        f'<div dir="rtl" style="text-align:left; margin-top:10px;">{nav_buttons}</div>'
        if nav_buttons else ""
    )
    page = HEADER_TEMPLATE.replace(
        f"{TOP_NAV_START}\n{TOP_NAV_END}",
        f"{TOP_NAV_START}\n{top_nav_div}\n{TOP_NAV_END}"
    )
    page = page.replace(
        f"{MSG_START}\n{MSG_END}",
        f"{MSG_START}\n{message_block}\n{MSG_END}"
    )
    page = page.replace(
        f"{NAV_START}\n{NAV_END}",
        f"{NAV_START}\n{bottom_nav_div}\n{NAV_END}"
    )
    return page


def extract_message_md(md_text: str) -> str | None:
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


def parse_post_header(header_line: str):
    line = header_line.strip()
    if not line.startswith("## "):
        return None, None
    m = re.search(r"## (.+?) — post (\d+)", line)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return None, None


def deduplicate_messages(old_block: str, new_ids_set: set[tuple[str, int]]) -> str:
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
# Scraping with Playwright (handles downloads natively)
# ----------------------------------------------------------------------
async def scrape_channel_all(context, channel_name, last_id, max_scrolls):
    """
    Scrape channel using Playwright context.
    Returns list of messages with media info.
    Downloads photos, videos, and documents using Playwright's built-in download.
    """
    page = await context.new_page()
    url = f"https://t.me/s/{channel_name}"
    print(f"  🌐 Loading {url} ...")
    
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        print(f"    ❌ Failed to load page: {e}")
        await page.close()
        return []

    try:
        await page.wait_for_selector("[data-post]", timeout=15000)
    except:
        print("    ❌ No messages found on initial page.")
        await page.close()
        return []

    all_messages = []
    seen_ids = set()

    for scroll_count in range(1, max_scrolls + 1):
        # Extract messages from current view
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

                // Check for media types
                const videoTag = el.querySelector('video');
                const photoWrap = el.querySelector('.tgme_widget_message_photo_wrap');
                const docWrap = el.querySelector('.tgme_widget_message_document_wrap');
                const linkPhoto = el.querySelector('a.tgme_widget_message_photo_wrap');
                
                let mediaUrl = null, mediaType = null;

                // 1) Document
                if (docWrap) {
                    mediaUrl = 'https://t.me/' + channel + '/' + postId + '?embed=1';
                    mediaType = 'document';
                }
                // 2) Video element
                else if (videoTag && videoTag.src && !videoTag.src.startsWith('blob:')) {
                    mediaUrl = videoTag.src;
                    mediaType = 'video';
                }
                // 3) Video wrapper
                else {
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
                // 4) Photo wrap
                if (!mediaUrl) {
                    if (linkPhoto) {
                        const vidInside = linkPhoto.querySelector('video');
                        if (vidInside && vidInside.src && !vidInside.src.startsWith('blob:')) {
                            mediaUrl = vidInside.src;
                            mediaType = 'video';
                        } else {
                            const style = linkPhoto.getAttribute('style') || '';
                            const match = style.match(/url\\('(.*?)'\\)/);
                            if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                        }
                    } else if (photoWrap) {
                        const style = photoWrap.getAttribute('style') || '';
                        const match = style.match(/url\\('(.*?)'\\)/);
                        if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
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

        print(f"    Scroll {scroll_count}: unique={len(all_messages)}, new={new_added}")

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
            print("    No further messages loaded after scroll.")
            break

    await page.close()

    filtered = [m for m in all_messages if m["id"] > last_id]
    filtered.sort(key=lambda x: x["id"], reverse=True)
    return filtered


async def download_media_with_playwright(context, url, channel_name, post_id, media_type):
    """
    Download media file using Playwright's download capabilities.
    Especially useful for documents which require proper browser context.
    """
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    
    page = await context.new_page()
    try:
        # For documents, we need to open the post page and click download
        if media_type == 'document':
            print(f"    📄 Opening document page: {url}")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Wait for the document widget to load
            try:
                await page.wait_for_selector('.tgme_widget_message_document_wrap', timeout=10000)
            except:
                print("    ⚠️ Document widget not found")
                await page.close()
                return None
            
            # Click the document link to trigger download
            try:
                doc_link = await page.query_selector('.tgme_widget_message_document_wrap')
                if doc_link:
                    # Start waiting for download before clicking
                    async with page.expect_download(timeout=60000) as download_info:
                        await doc_link.click()
                        download = await download_info.value
                        
                        # Check file size
                        suggested_filename = download.suggested_filename
                        if not suggested_filename:
                            suggested_filename = f"{channel_name}_{post_id}"
                        
                        # Check if file size is within limit by downloading to memory first
                        try:
                            # Get file size from headers if available
                            temp_path = await download.path()
                            if temp_path:
                                file_size = os.path.getsize(temp_path)
                                if file_size > MAX_FILE_SIZE_BYTES:
                                    print(f"    ⚠️ File too large: {file_size / (1024*1024):.1f}MB > {MAX_FILE_SIZE_MB}MB")
                                    os.unlink(temp_path)
                                    await page.close()
                                    return None
                            
                            # Save to content directory
                            filename = safe_filename(suggested_filename, max_length=100)
                            local_path = CONTENT_DIR / filename
                            
                            # Avoid overwriting
                            counter = 1
                            while local_path.exists():
                                stem, ext = os.path.splitext(filename)
                                local_path = CONTENT_DIR / f"{stem}_{counter}{ext}"
                                counter += 1
                            
                            await download.save_as(str(local_path))
                            final_size = os.path.getsize(str(local_path))
                            
                            # Final size check
                            if final_size > MAX_FILE_SIZE_BYTES:
                                print(f"    ⚠️ Downloaded file too large: {final_size / (1024*1024):.1f}MB")
                                os.unlink(str(local_path))
                                await page.close()
                                return None
                            
                            print(f"    ✅ Downloaded document: {local_path.name} ({final_size / 1024:.1f}KB)")
                            await page.close()
                            return f"telegram/content/{local_path.name}"
                            
                        except Exception as e:
                            print(f"    ⚠️ Error checking download: {e}")
                            await page.close()
                            return None
                else:
                    print("    ⚠️ Could not find document link")
            except Exception as e:
                print(f"    ⚠️ Download failed: {e}")
        
        else:
            # For photos and videos, just fetch the URL directly
            print(f"    📥 Downloading {media_type}: {url}")
            try:
                response = await page.goto(url, wait_until="networkidle", timeout=30000)
                if response and response.ok:
                    body = await response.body()
                    
                    # Check size
                    if len(body) > MAX_FILE_SIZE_BYTES:
                        print(f"    ⚠️ File too large: {len(body) / (1024*1024):.1f}MB > {MAX_FILE_SIZE_MB}MB")
                        await page.close()
                        return None
                    
                    # Determine extension from content-type
                    content_type = response.headers.get('content-type', '').lower()
                    ext_map = {
                        'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                        'image/gif': '.gif', 'video/mp4': '.mp4', 'video/webm': '.webm',
                        'video/quicktime': '.mov'
                    }
                    ext = ext_map.get(content_type, '.jpg' if media_type == 'photo' else '.mp4')
                    
                    filename = f"{channel_name}_{post_id}_{int(time.time())}{ext}"
                    local_path = CONTENT_DIR / filename
                    local_path.write_bytes(body)
                    
                    print(f"    ✅ Downloaded: {filename} ({len(body) / 1024:.1f}KB)")
                    await page.close()
                    return f"telegram/content/{filename}"
                else:
                    print(f"    ⚠️ Failed to fetch {url}")
            except Exception as e:
                print(f"    ⚠️ Download error: {e}")
    
    except Exception as e:
        print(f"    ⚠️ Error: {e}")
    
    await page.close()
    return None


# ----------------------------------------------------------------------
async def main():
    channels = load_channels()
    state = load_state()
    is_first_run = not state
    scroll_limit = 15 if is_first_run else 50

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        all_messages = []
        for ch_name in channels:
            clean_name = ch_name.lstrip("@")
            last_id = state.get(ch_name, 0)
            msgs = await scrape_channel_all(context, clean_name, last_id, max_scrolls=scroll_limit)
            
            if not msgs:
                print(f"  ℹ️ No new messages for {ch_name}")
                continue

            # Download media for each message
            for m in msgs:
                m["_channel"] = clean_name
                if m.get("media_url") and m.get("media_type") in ("photo", "video", "document"):
                    # Filter out .webm (stickers)
                    if m.get("media_type") == "video" and m["media_url"].lower().endswith(".webm"):
                        m["media_url"] = None
                        m["media_type"] = None
                        continue
                    
                    print(f"  📥 Downloading media for {clean_name} post {m['id']} ({m['media_type']})...")
                    result = await download_media_with_playwright(
                        context, m["media_url"], clean_name, m["id"], m["media_type"]
                    )
                    if result:
                        m["downloaded_path"] = result
                    else:
                        m["downloaded_path"] = None

            all_messages.extend(msgs)
            print(f"  ✅ {ch_name}: {len(msgs)} new messages")

        await browser.close()

    # ---- Generate markdown ----
    repo_url, branch = get_github_base_url()
    now_jalali = jdatetime.datetime.now(IRAN_TZ)
    update_header = f"\n---\n📅 بروزرسانی: {now_jalali.strftime('%Y/%m/%d %H:%M')}\n---\n\n"

    new_entries_list = []
    new_ids_set = set()

    for msg in all_messages:
        ch = msg["_channel"]
        pid = msg["id"]
        new_ids_set.add((ch, pid))

        header = f"## {ch} — post {pid}\n\n"
        media_html = ""
        
        media_path = msg.get("downloaded_path")
        media_type = msg.get("media_type")

        if media_path:
            if media_type == "photo":
                media_html = f'<div align="center">\n  <img src="{media_path}" alt="Photo">\n</div>'
            elif media_type == "video":
                media_html = f'<div align="center">\n  <a href="{media_path}" target="_blank">🎬 Download video</a>\n</div>'
            elif media_type == "document":
                filename = media_path.split('/')[-1]
                media_html = f'<div align="center">\n  <a href="{media_path}" target="_blank" download>📎 دانلود فایل: {filename}</a>\n</div>'
        elif msg.get("media_url") and not media_path:
            # Fallback: couldn't download, link to original
            if media_type == "document":
                media_html = f'<div align="center">\n  <a href="{msg["media_url"]}" target="_blank">📎 فایل ضمیمه (دانلود نشد)</a>\n</div>'

        caption = msg.get("text", "")
        caption_style = "dir='rtl' style='font-family: \"Vazirmatn\", Tahoma, sans-serif;'"
        caption_div = f'<div {caption_style}>\n{caption}\n</div>' if caption else ""

        entry = header + media_html + "\n" + caption_div + "\n\n"
        new_entries_list.append(entry)

    new_entries_block = update_header + "".join(new_entries_list)

    if not new_entries_list:
        caption_style = "dir='rtl' style='font-family: \"Vazirmatn\", Tahoma, sans-serif;'"
        new_entries_block += f'<div {caption_style}>\nهیچ پیام جدیدی در این بروزرسانی ارسال نشد.\n</div>\n\n'

    # ---- Combine with old messages ----
    old_messages_block = ""
    if OUTPUT_FILE.exists():
        old_raw = OUTPUT_FILE.read_text(encoding="utf-8")
        extracted = extract_message_md(old_raw)
        old_messages_block = extracted if extracted is not None else ""

    if old_messages_block.strip() and new_ids_set:
        old_messages_block = deduplicate_messages(old_messages_block, new_ids_set)

    # ---- Write output ----
    main_base = f"{repo_url}/blob/{branch}/" if repo_url and branch else None
    
    trial_page = wrap_page(new_entries_block + old_messages_block, None, None, main_base)
    size = len(trial_page.encode("utf-8"))
    
    if size > 950 * 1024 and old_messages_block.strip():
        # Too big - split
        test_page = wrap_page(new_entries_block, None, None)
        if len(test_page.encode("utf-8")) <= 950 * 1024:
            # Move old to archive
            shift_archives_for_new_page1(old_messages_block, repo_url, branch)
            next_rel_main = "telegram/content/archive_1.md"
            main_page = wrap_page(new_entries_block, next_rel_main, None, main_base)
            OUTPUT_FILE.write_text(main_page, encoding="utf-8")
            print("✅ Main page updated, old content archived")
        else:
            print("⚠️ New entries too large, truncating...")
            OUTPUT_FILE.write_text(wrap_page(new_entries_block[:len(new_entries_block)//2], None, None, main_base))
    else:
        archives = get_existing_archives()
        next_rel_main = f"telegram/content/archive_{archives[0][0]}.md" if archives else None
        OUTPUT_FILE.write_text(wrap_page(new_entries_block + old_messages_block, next_rel_main, None, main_base))
        print("✅ Main page updated")

    # ---- Update state ----
    for ch_name in channels:
        clean_name = ch_name.lstrip("@")
        ch_msgs = [m for m in all_messages if m["_channel"] == clean_name]
        if ch_msgs:
            state[ch_name] = max(state.get(ch_name, 0), max(m["id"] for m in ch_msgs))

    save_state(state)
    print("✅ Done!")


def shift_archives_for_new_page1(message_block, repo_url, branch):
    """Move existing archives up, create archive_1"""
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
    new_page1 = wrap_page(message_block, next_rel, prev_rel, archive_base)
    new_page1_path.write_text(new_page1, encoding="utf-8")
    print(f"✅ archive_1.md created")


if __name__ == "__main__":
    asyncio.run(main())