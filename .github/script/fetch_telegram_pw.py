#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
Fast & lightweight version — optimized for speed.
"""

import asyncio, json, os, re, time
from pathlib import Path
from zoneinfo import ZoneInfo
import jdatetime, requests
from playwright.async_api import async_playwright

# ---- Paths ----
REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_FILE = REPO_ROOT / "telegram" / "channels.json"
STATE_FILE    = REPO_ROOT / "telegram" / "last_ids.json"
OUTPUT_FILE   = REPO_ROOT / "telegram.md"
CONTENT_DIR   = REPO_ROOT / "telegram" / "content"

IRAN_TZ = ZoneInfo("Asia/Tehran")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}

MSG_START, MSG_END = "<!-- MSG START -->", "<!-- MSG END -->"
TOP_NAV_START, TOP_NAV_END = "<!-- TOP_NAV START -->", "<!-- TOP_NAV END -->"
NAV_START, NAV_END = "<!-- NAV START -->", "<!-- NAV END -->"
MAX_FILE_SIZE = 1_000_000  # 1 MB

# ----------------------------------------------------------------------
def load_channels(): return json.load(open(CHANNELS_FILE, "r", encoding="utf-8"))
def load_state():
    return json.load(open(STATE_FILE, "r", encoding="utf-8")) if STATE_FILE.exists() else {}
def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ----------------------------------------------------------------------
def download_file(url, channel_name, post_id, filename=None):
    """Download file with size limit, preserve original extension."""
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        resp.raise_for_status()
        
        # Check content-length
        cl = resp.headers.get('Content-Length')
        if cl and int(cl) > MAX_FILE_SIZE:
            print(f"    ⚠️ File too large ({int(cl)} bytes), skipping")
            return None
        
        # Determine filename
        if not filename:
            # Try from Content-Disposition
            cd = resp.headers.get('Content-Disposition', '')
            fn_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', cd)
            if fn_match:
                filename = fn_match.group(1).strip('"\'')
        
        if not filename:
            # Use URL path
            from urllib.parse import urlparse
            path = urlparse(url).path
            filename = path.split('/')[-1] if path else f"file_{post_id}"
        
        if not filename or '.' not in filename:
            # Guess from content-type
            ct = resp.headers.get('Content-Type', '').lower()
            ext = {
                'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                'video/mp4': '.mp4', 'video/webm': '.webm',
                'audio/mpeg': '.mp3', 'audio/ogg': '.ogg',
                'application/pdf': '.pdf', 'application/zip': '.zip',
                'application/x-npvt': '.npvt', 'application/octet-stream': '.npvt'
            }.get(ct.split(';')[0], '.dat')
            filename = f"{channel_name}_{post_id}_{int(time.time())}{ext}"
        
        # Truncate long filenames
        if len(filename) > 100:
            stem, ext = os.path.splitext(filename)
            filename = stem[:95] + ext
        
        local_path = CONTENT_DIR / filename
        
        # Download with size check
        downloaded = 0
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE:
                        f.close()
                        local_path.unlink()
                        print(f"    ⚠️ Exceeded size limit, skipping")
                        return None
                    f.write(chunk)
        
        print(f"    ✅ Downloaded: {filename} ({downloaded} bytes)")
        return f"telegram/content/{filename}"
    
    except Exception as e:
        print(f"    ⚠️ Download failed: {e}")
        return None

# ----------------------------------------------------------------------
async def scrape_channel(page, channel_name, last_id, max_scrolls=8):
    """Scrape with fewer scrolls for speed."""
    url = f"https://t.me/s/{channel_name}"
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    
    try:
        await page.wait_for_selector("[data-post]", timeout=8000)
    except:
        return []
    
    all_messages, seen_ids = [], set()
    
    for scroll in range(1, max_scrolls + 1):
        msgs = await page.evaluate("""() => {
            return [...document.querySelectorAll('[data-post]')].map(el => {
                const dp = el.getAttribute('data-post');
                if (!dp) return null;
                const [ch, pid] = dp.split('/');
                if (!pid) return null;
                
                const text = el.querySelector('.tgme_widget_message_text')?.innerText || '';
                
                // Photo
                const pw = el.querySelector('.tgme_widget_message_photo_wrap');
                let photoUrl = null;
                if (pw) {
                    const m = (pw.getAttribute('style')||'').match(/url\\('(.*?)'\\)/);
                    if (m) photoUrl = m[1];
                }
                
                // Video
                const vw = el.querySelector('.tgme_widget_message_video_wrap');
                let videoUrl = null;
                if (vw) {
                    const v = vw.querySelector('video');
                    if (v?.src && !v.src.startsWith('blob:')) videoUrl = v.src;
                    else {
                        const m = (vw.getAttribute('style')||'').match(/url\\('(.*?)'\\)/);
                        if (m) videoUrl = m[1];
                    }
                }
                
                // Document
                const dw = el.querySelector('.tgme_widget_message_document_wrap');
                let docUrl = null;
                if (dw) {
                    const href = dw.getAttribute('href');
                    if (href) docUrl = href.startsWith('http') ? href : 'https://t.me' + href;
                }
                
                return {id: parseInt(pid), text, photoUrl, videoUrl, docUrl};
            }).filter(Boolean);
        }""")
        
        for m in msgs:
            if m and m['id'] not in seen_ids:
                seen_ids.add(m['id'])
                all_messages.append(m)
        
        if all_messages and min(m['id'] for m in all_messages) <= last_id:
            break
        
        if scroll < max_scrolls:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
    
    return [m for m in all_messages if m['id'] > last_id]

# ----------------------------------------------------------------------
async def main():
    channels = load_channels()
    state = load_state()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        all_new = []
        for ch in channels:
            clean = ch.lstrip("@")
            last_id = state.get(ch, 0)
            
            msgs = await scrape_channel(page, clean, last_id)
            if msgs:
                for m in msgs:
                    m['_channel'] = clean
                all_new.extend(msgs)
                print(f"  ✅ {clean}: {len(msgs)} new")
        
        await browser.close()
    
    # ---- Process messages ----
    now = jdatetime.datetime.now(IRAN_TZ)
    update_header = f"\n---\n📅 بروزرسانی: {now.strftime('%Y/%m/%d %H:%M')}\n---\n\n"
    
    new_entries = []
    new_ids = set()
    
    for msg in sorted(all_new, key=lambda x: x['id'], reverse=True):
        ch, pid = msg['_channel'], msg['id']
        new_ids.add((ch, pid))
        
        # Download media
        media_md = None
        media_type = None
        
        if msg.get('photoUrl'):
            fn = f"{ch}_{pid}_{int(time.time())}.jpg"
            media_md = download_file(msg['photoUrl'], ch, pid, fn)
            media_type = 'photo'
        elif msg.get('videoUrl'):
            # Keep original extension
            ext = '.mp4'
            if '.webm' in msg['videoUrl'].lower():
                ext = '.webm'
            fn = f"{ch}_{pid}_{int(time.time())}{ext}"
            media_md = download_file(msg['videoUrl'], ch, pid, fn)
            media_type = 'video'
        elif msg.get('docUrl'):
            # Download document directly with original filename
            media_md = download_file(msg['docUrl'], ch, pid)
            media_type = 'document'
        
        # Build entry
        header = f"## {ch} — post {pid}\n\n"
        media_html = ""
        if media_md:
            if media_type == 'photo':
                media_html = f'<div align="center">\n  <img src="{media_md}" alt="Photo">\n</div>'
            elif media_type == 'video':
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">🎬 Download video</a>\n</div>'
            elif media_type == 'document':
                media_html = f'<div align="center">\n  <a href="{media_md}" target="_blank">📎 Download file</a>\n</div>'
        
        caption = msg.get('text', '') or ''
        caption_div = f'<div dir=\'rtl\' style=\'font-family: "Vazirmatn", Tahoma, sans-serif;\'>\n{caption}\n</div>' if caption else ''
        
        new_entries.append(header + media_html + '\n' + caption_div + '\n\n')
    
    new_block = update_header + ''.join(new_entries)
    
    # ---- Combine with existing ----
    old_block = ""
    if OUTPUT_FILE.exists():
        raw = OUTPUT_FILE.read_text(encoding="utf-8")
        start = raw.find(MSG_START)
        end = raw.find(MSG_END)
        if start != -1 and end != -1:
            old_block = raw[start+len(MSG_START):end].strip()
    
    # Simple deduplicate
    if old_block and new_ids:
        parts = re.split(r'(?=\n## )', old_block)
        kept = []
        for part in parts:
            pm = re.match(r'## (.+?) — post (\d+)', part.strip())
            if pm and (pm.group(1).strip(), int(pm.group(2))) in new_ids:
                continue
            kept.append(part)
        old_block = ''.join(kept)
    
    final = new_block + old_block
    page_content = f"# خواننده تلگرام\n\n{TOP_NAV_START}\n{TOP_NAV_END}\n\n{MSG_START}\n{final}\n{MSG_END}\n\n{NAV_START}\n{NAV_END}"
    
    OUTPUT_FILE.write_text(page_content, encoding="utf-8")
    print(f"✅ Wrote {OUTPUT_FILE} ({len(page_content)} chars)")
    
    # ---- Update state ----
    for ch in channels:
        clean = ch.lstrip("@")
        ch_msgs = [m for m in all_new if m['_channel'] == clean]
        if ch_msgs:
            state[ch] = max(state.get(ch, 0), max(m['id'] for m in ch_msgs))
    save_state(state)
    print("✅ Done")

if __name__ == "__main__":
    asyncio.run(main())