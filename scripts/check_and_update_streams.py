#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Live Stream Health Checker & Auto-Updater
----------------------------------------------
- Checks reachability, response time, and stream integrity of live.m3u.
- Automatically fails over to backup candidate streams in sources_pool.json if a channel is down.
- Automatically fails back (restores) the primary backbone stream (candidates[0]) for Line 1 when it recovers,
  while preserving distinct backup URLs on secondary lines (Line 2+) of the same channel.
- Updates M3U files by exact line number instead of global substring replacement.
- Synchronizes legacy playlists (tv.m3u, hk_tw.m3u) when matching channel URLs are updated.
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
LEGACY_M3U_PATHS = [
    os.path.join(BASE_DIR, "tv.m3u"),
    os.path.join(BASE_DIR, "hk_tw.m3u"),
]
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
    if "mov3.co" in url:
        headers["Referer"] = "https://mov3.co/"

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

            # Reject JSON/HTML error payloads masquerading as 200 OK
            if content_str.strip().startswith("{") and "error" in content_str.lower():
                return False, round(latency, 1), "JSON error payload"

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
    Parses M3U file into structured list of channels with exact line numbers and multi-line rank.
    """
    if not os.path.exists(file_path):
        return []

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    channels = []
    current_extinf = None
    extinf_line_idx = None
    seen_counts = {}

    for line_idx, line in enumerate(lines):
        line_clean = line.strip()
        if not line_clean:
            continue
        if line_clean.startswith("#EXTINF:"):
            current_extinf = line_clean
            extinf_line_idx = line_idx
        elif not line_clean.startswith("#") and current_extinf is not None:
            # Extract attributes
            name_match = re.search(r'tvg-name="([^"]+)"', current_extinf)
            name = name_match.group(1) if name_match else ""
            parts = current_extinf.split(",", 1)
            display_name = parts[1].strip() if len(parts) > 1 else "Unknown"
            if not name:
                name = display_name

            group_match = re.search(r'group-title="([^"]+)"', current_extinf)
            group = group_match.group(1) if group_match else "默认"

            group_ch_key = (group, name)
            line_rank = seen_counts.get(group_ch_key, 0)
            seen_counts[group_ch_key] = line_rank + 1

            channels.append({
                "extinf": current_extinf,
                "extinf_line_idx": extinf_line_idx,
                "url": line_clean,
                "url_line_idx": line_idx,
                "name": name,
                "display_name": display_name,
                "group": group,
                "line_rank": line_rank,  # 0 for Line 1 (primary), 1 for Line 2 (backup), etc.
                "raw_lines": (current_extinf, line_clean)
            })
            current_extinf = None
            extinf_line_idx = None

    return channels


def load_pool():
    if os.path.exists(POOL_JSON_PATH):
        try:
            with open(POOL_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to load sources pool: {e}")
    return {}


def get_candidates_for_channel(pool: dict, name: str) -> list[str]:
    candidates = pool.get(name, [])
    if not candidates:
        for pool_k, pool_v in pool.items():
            if pool_k in name or name in pool_k:
                candidates = pool_v
                break
    return candidates


def apply_line_updates_to_m3u(file_path: str, line_updates: dict[int, str]):
    """
    Replaces URL lines by exact 0-based line index, avoiding global string replacement collisions.
    """
    if not line_updates or not os.path.exists(file_path):
        return False

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    for line_idx, new_url in line_updates.items():
        if 0 <= line_idx < len(lines):
            lines[line_idx] = new_url + "\n"

    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True


def sync_legacy_playlists(pool: dict, probe_cache: dict[str, tuple[bool, float, str]]):
    """
    Also checks and updates legacy playlists (tv.m3u, hk_tw.m3u) using the already-probed
    stream results and pool candidates so they do not drift behind live.m3u.
    """
    for legacy_path in LEGACY_M3U_PATHS:
        if not os.path.exists(legacy_path):
            continue
        legacy_channels = parse_m3u(legacy_path)
        line_updates = {}
        for ch in legacy_channels:
            u = ch["url"]
            name = ch["name"]
            line_rank = ch["line_rank"]
            candidates = get_candidates_for_channel(pool, name)

            if u not in probe_cache:
                probe_cache[u] = check_stream(u, timeout=3.5)
            is_valid, _, _ = probe_cache[u]

            # Primary line failback to backbone candidates[0] if healthy
            if line_rank == 0 and candidates and u != candidates[0]:
                pref_u = candidates[0]
                if pref_u not in probe_cache:
                    probe_cache[pref_u] = check_stream(pref_u, timeout=3.5)
                if probe_cache[pref_u][0]:
                    line_updates[ch["url_line_idx"]] = pref_u
                    continue

            # Failover if dead
            if not is_valid and candidates:
                for cand_url in candidates:
                    if cand_url == u:
                        continue
                    if cand_url not in probe_cache:
                        probe_cache[cand_url] = check_stream(cand_url, timeout=3.5)
                    if probe_cache[cand_url][0]:
                        line_updates[ch["url_line_idx"]] = cand_url
                        break

        if line_updates:
            apply_line_updates_to_m3u(legacy_path, line_updates)
            print(f"Synchronized {len(line_updates)} stream updates to {legacy_path}.")


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
    probe_cache = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ch = {
            executor.submit(check_stream, ch["url"]): idx
            for idx, ch in enumerate(channels)
        }
        for future in as_completed(future_to_ch):
            idx = future_to_ch[future]
            ch = channels[idx]
            is_valid, latency, reason = future.result()
            probe_cache[ch["url"]] = (is_valid, latency, reason)
            results[idx] = {
                "channel": ch,
                "is_valid": is_valid,
                "latency": latency,
                "reason": reason,
                "swapped": False,
                "original_url": ch["url"],
                "active_url": ch["url"]
            }

    # Track active URLs already assigned per (group, name) to avoid duplicate multi-line URLs
    assigned_urls_per_channel = {}
    updated_count = 0
    line_updates = {}

    for idx in sorted(results.keys()):
        res = results[idx]
        ch = res["channel"]
        name = ch["name"]
        group = ch["group"]
        line_rank = ch["line_rank"]
        group_ch_key = (group, name)
        used_urls = assigned_urls_per_channel.setdefault(group_ch_key, set())
        candidates = get_candidates_for_channel(pool, name)

        # 1. Failback logic: if this is Line 1 (line_rank == 0) and candidates[0] is the preferred
        # backbone stream, restore candidates[0] when it is healthy even if the current relay is alive.
        if line_rank == 0 and candidates and res["active_url"] != candidates[0]:
            preferred_url = candidates[0]
            if preferred_url not in probe_cache:
                probe_cache[preferred_url] = check_stream(preferred_url, timeout=3.5)
            pref_valid, pref_lat, pref_reason = probe_cache[preferred_url]
            if pref_valid:
                print(f"[FAILBACK TO BACKBONE] {name} Line 1 restored to preferred source: {preferred_url} ({pref_lat}ms)")
                res["active_url"] = preferred_url
                res["is_valid"] = True
                res["latency"] = pref_lat
                res["reason"] = f"Restored primary: {pref_reason}"
                res["swapped"] = True
                line_updates[ch["url_line_idx"]] = preferred_url
                updated_count += 1
                used_urls.add(preferred_url)
                continue

        # 2. Deduplication logic for Line 2+ (line_rank > 0): if a secondary line has the exact same URL
        # as Line 1 (e.g. from previous global replace bug), switch it to the next healthy distinct backup.
        if line_rank > 0 and res["active_url"] in used_urls and candidates:
            for cand_url in candidates:
                if cand_url in used_urls:
                    continue
                if cand_url not in probe_cache:
                    probe_cache[cand_url] = check_stream(cand_url, timeout=3.5)
                cand_valid, cand_lat, cand_reason = probe_cache[cand_url]
                if cand_valid:
                    print(f"[DEDUP MULTI-LINE] {name} Line {line_rank + 1} switched to distinct backup: {cand_url} ({cand_lat}ms)")
                    res["active_url"] = cand_url
                    res["is_valid"] = True
                    res["latency"] = cand_lat
                    res["reason"] = f"Distinct backup: {cand_reason}"
                    res["swapped"] = True
                    line_updates[ch["url_line_idx"]] = cand_url
                    updated_count += 1
                    break

        # 3. Failover logic: if current stream is dead / invalid, switch to first working candidate
        if not res["is_valid"]:
            print(f"[FAILOVER NEEDED] {name} ({res['reason']}) URL: {ch['url']}")
            swapped = False
            # Prefer a candidate not already used on another line of the same channel, fallback to any working candidate
            for allow_duplicate in (False, True):
                if swapped:
                    break
                for cand_url in candidates:
                    if cand_url == ch["url"]:
                        continue
                    if not allow_duplicate and cand_url in used_urls:
                        continue
                    if cand_url not in probe_cache:
                        probe_cache[cand_url] = check_stream(cand_url, timeout=3.5)
                    cand_valid, cand_lat, cand_reason = probe_cache[cand_url]
                    if cand_valid:
                        print(f"  -> Successfully switched {name} to working backup: {cand_url} ({cand_lat}ms)")
                        res["active_url"] = cand_url
                        res["is_valid"] = True
                        res["latency"] = cand_lat
                        res["reason"] = f"Swapped to backup: {cand_reason}"
                        res["swapped"] = True
                        line_updates[ch["url_line_idx"]] = cand_url
                        swapped = True
                        updated_count += 1
                        break

            if not swapped:
                print(f"  -> No working backup found for {name}")

        used_urls.add(res["active_url"])

    # Regenerate live.m3u by exact line index if any URLs swapped
    if line_updates:
        print(f"Applying {len(line_updates)} line-indexed stream updates to {LIVE_M3U_PATH}...")
        apply_line_updates_to_m3u(LIVE_M3U_PATH, line_updates)
        print("Updated live.m3u saved.")

    # Also synchronize legacy playlists (tv.m3u, hk_tw.m3u)
    sync_legacy_playlists(pool, probe_cache)

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
