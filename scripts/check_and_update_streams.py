#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Live Stream Health Checker & Auto-Updater
----------------------------------------------
- Checks reachability, response time, and stream integrity of live.m3u.
- Automatically fails over to backup candidate streams in sources_pool.json if a channel is down.
- Generates HEALTH_REPORT.md and purges CDN caches.
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_M3U_PATH = os.path.join(BASE_DIR, "live.m3u")
POOL_JSON_PATH = os.path.join(BASE_DIR, "scripts", "sources_pool.json")
REPORT_MD_PATH = os.path.join(BASE_DIR, "HEALTH_REPORT.md")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 APTV/1.3.0"
TIMEOUT_SECONDS = 4.0
MAX_WORKERS = 12


def check_stream(url: str, timeout: float = TIMEOUT_SECONDS) -> tuple[bool, float, str]:
    """
    Test an HLS stream URL.
    Returns: (is_valid, latency_ms, details)
    """
    if not url or not url.startswith(("http://", "https://")):
        return False, 0.0, "Invalid URL schema"

    start_t = time.time()
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.getcode()
            if status != 200:
                return False, 0.0, f"HTTP status {status}"

            content_bytes = response.read(65536)
            latency = (time.time() - start_t) * 1000.0

            try:
                content_str = content_bytes.decode("utf-8", errors="ignore")
            except Exception:
                content_str = ""

            # Check if it is a valid M3U8 HLS playlist
            if "#EXTM3U" in content_str:
                # If master playlist, or media playlist
                if "#EXT-X-STREAM-INF" in content_str or "#EXTINF" in content_str or ".ts" in content_str or ".m3u8" in content_str or ".m4s" in content_str:
                    return True, round(latency, 1), "OK (HLS Verified)"
                return True, round(latency, 1), "OK (M3U8 Header Present)"
            
            # Check if direct TS / media stream
            content_type = response.headers.get("Content-Type", "")
            if "video" in content_type or "mpegurl" in content_type or "octet-stream" in content_type:
                return True, round(latency, 1), f"OK ({content_type})"

            if len(content_bytes) > 500:
                return True, round(latency, 1), "OK (Stream Data Received)"

            return False, round(latency, 1), "Empty or non-HLS payload"

    except urllib.error.HTTPError as e:
        latency = (time.time() - start_t) * 1000.0
        return False, round(latency, 1), f"HTTP Error {e.code}"
    except urllib.error.URLError as e:
        latency = (time.time() - start_t) * 1000.0
        return False, round(latency, 1), f"Network Error: {e.reason}"
    except Exception as e:
        latency = (time.time() - start_t) * 1000.0
        return False, round(latency, 1), f"Error: {type(e).__name__}"


def parse_m3u(file_path: str):
    """
    Parses M3U file into structured list of channels and comments.
    """
    if not os.path.exists(file_path):
        return []

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    channels = []
    current_extinf = None

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        if line_clean.startswith("#EXTINF:"):
            current_extinf = line_clean
        elif not line_clean.startswith("#") and current_extinf:
            # Extract attributes
            name_match = re.search(r'tvg-name="([^"]+)"', current_extinf)
            name = name_match.group(1) if name_match else ""
            if not name:
                # Fallback to display name after comma
                parts = current_extinf.split(",", 1)
                name = parts[1].strip() if len(parts) > 1 else "Unknown"

            group_match = re.search(r'group-title="([^"]+)"', current_extinf)
            group = group_match.group(1) if group_match else "默认"

            channels.append({
                "extinf": current_extinf,
                "url": line_clean,
                "name": name,
                "group": group,
                "raw_lines": (current_extinf, line_clean)
            })
            current_extinf = None

    return channels


def load_pool():
    if os.path.exists(POOL_JSON_PATH):
        try:
            with open(POOL_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to load sources pool: {e}")
    return {}


def purge_cdn_caches():
    """
    Purge jsDelivr cache for repository playlists.
    """
    urls_to_purge = [
        "https://purge.jsdelivr.net/gh/heme9999/iptv-live@main/live.m3u",
        "https://purge.jsdelivr.net/gh/heme9999/iptv-live@main/tv.m3u",
        "https://purge.jsdelivr.net/gh/heme9999/iptv-live@main/hk_tw.m3u"
    ]
    for url in urls_to_purge:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"[CDN Purge] {url} -> {resp.getcode()}")
        except Exception as e:
            print(f"[CDN Purge Error] {url} -> {e}")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting IPTV live stream check & auto-failover...")
    channels = parse_m3u(LIVE_M3U_PATH)
    pool = load_pool()

    print(f"Total channels to check: {len(channels)}")

    # Check primary streams concurrently
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ch = {
            executor.submit(check_stream, ch["url"]): idx
            for idx, ch in enumerate(channels)
        }
        for future in as_completed(future_to_ch):
            idx = future_to_ch[future]
            ch = channels[idx]
            is_valid, latency, reason = future.result()
            results[idx] = {
                "channel": ch,
                "is_valid": is_valid,
                "latency": latency,
                "reason": reason,
                "swapped": False,
                "original_url": ch["url"],
                "active_url": ch["url"]
            }

    # Process failovers for failed channels
    updated_count = 0
    for idx in sorted(results.keys()):
        res = results[idx]
        ch = res["channel"]
        name = ch["name"]

        # Only failover if current stream is dead / invalid
        if not res["is_valid"]:
            print(f"[FAILOVER NEEDED] {name} ({res['reason']}) URL: {ch['url']}")
            # Find candidate in pool
            candidates = pool.get(name, [])
            if not candidates:
                # Try partial matching in pool
                for pool_k, pool_v in pool.items():
                    if pool_k in name or name in pool_k:
                        candidates = pool_v
                        break

            # Test candidates
            swapped = False
            for cand_url in candidates:
                if cand_url == ch["url"]:
                    continue
                cand_valid, cand_lat, cand_reason = check_stream(cand_url, timeout=3.5)
                if cand_valid:
                    print(f"  -> Successfully switched {name} to working backup: {cand_url} ({cand_lat}ms)")
                    res["active_url"] = cand_url
                    res["is_valid"] = True
                    res["latency"] = cand_lat
                    res["reason"] = f"Swapped to backup: {cand_reason}"
                    res["swapped"] = True
                    swapped = True
                    updated_count += 1
                    break

            if not swapped:
                print(f"  -> No working backup found for {name}")

    # Regenerate live.m3u if any URLs swapped
    if updated_count > 0:
        print(f"Applying {updated_count} stream updates to {LIVE_M3U_PATH}...")
        with open(LIVE_M3U_PATH, "r", encoding="utf-8", errors="ignore") as f:
            full_content = f.read()

        for idx, res in results.items():
            if res["swapped"]:
                orig = res["original_url"]
                new_u = res["active_url"]
                full_content = full_content.replace(orig, new_u)

        with open(LIVE_M3U_PATH, "w", encoding="utf-8") as f:
            f.write(full_content)
        print("Updated live.m3u saved.")

    # Calculate statistics
    total = len(results)
    online_count = sum(1 for r in results.values() if r["is_valid"])
    failed_count = total - online_count
    valid_latencies = [r["latency"] for r in results.values() if r["is_valid"] and r["latency"] > 0]
    avg_latency = sum(valid_latencies) / len(valid_latencies) if valid_latencies else 0.0
    health_pct = (online_count / total * 100.0) if total > 0 else 0.0

    # Times
    now_utc = datetime.now(timezone.utc)
    cst = timezone(timedelta(hours=8))
    now_cst = now_utc.astimezone(cst)
    ts_str = now_cst.strftime("%Y-%m-%d %H:%M:%S CST (UTC+8)")

    # Generate HEALTH_REPORT.md
    report_lines = [
        "# 📡 IPTV 直播源全天候健康监控与可用性报告",
        "",
        f"> **最后检测时间**: `{ts_str}`  ",
        f"> **检测状态**: `{'🟢 全部正常' if failed_count == 0 else '🟡 部分源已自动切换/维护中'}`  ",
        f"> **在线率**: `{health_pct:.1f}%` ({online_count}/{total}) | **平均延迟**: `{avg_latency:.1f}ms` | **自动切换成功**: `{updated_count}` 路",
        "",
        "## 📊 核心指标概览",
        "",
        "| 指标项 | 数值 | 状态 |",
        "| :--- | :--- | :--- |",
        f"| **总频道数** | `{total}` | 涵盖央视/卫视/体育/港澳/国际 |",
        f"| **有效在线频道** | `{online_count}` | 🟢 正常播放 |",
        f"| **失效/维护中** | `{failed_count}` | {'🟢 0 失效' if failed_count == 0 else '🔴 待补充新源'} |",
        f"| **平均首包延迟** | `{avg_latency:.1f}ms` | {'🟢 极速 (<500ms)' if avg_latency < 500 else '🟡 良好'} |",
        f"| **自动更新触发** | `{updated_count}` 条新源已写入 | 自动同步 CDN |",
        "",
        "## 📺 全频道实时健康度明细表",
        "",
        "| 频道名称 | 分类分组 | 运行状态 | 响应延迟 | 检测结果 / 备注 |",
        "| :--- | :--- | :---: | :---: | :--- |",
    ]

    for idx in sorted(results.keys()):
        r = results[idx]
        ch = r["channel"]
        name = ch["name"].replace("|", "\\|")
        group = ch["group"].replace("|", "\\|")
        if r["swapped"]:
            status_badge = "🟡 已自动换源"
        elif r["is_valid"]:
            status_badge = "🟢 在线"
        else:
            status_badge = "🔴 失效"

        lat_str = f"{r['latency']:.0f}ms" if r["latency"] > 0 else "-"
        reason_clean = r["reason"].replace("|", "\\|")
        report_lines.append(f"| {name} | {group} | {status_badge} | {lat_str} | {reason_clean} |")

    report_lines.extend([
        "",
        "---",
        "💡 *本报告由 GitHub Actions 自动化流水线定期（每6小时）巡检并自动更新发布。*",
        "🔗 **M3U 订阅直链**: `https://raw.githubusercontent.com/heme9999/iptv-live/main/live.m3u`  ",
        "⚡ **国内 CDN 加速直链**: `https://fastly.jsdelivr.net/gh/heme9999/iptv-live@main/live.m3u`"
    ])

    with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    print(f"Health report generated at {REPORT_MD_PATH}")
    print(f"Summary: Total={total}, Online={online_count}, Failed={failed_count}, Swapped={updated_count}, AvgLatency={avg_latency:.1f}ms")

    # Purge CDN caches
    purge_cdn_caches()

    return 0


if __name__ == "__main__":
    sys.exit(main())
