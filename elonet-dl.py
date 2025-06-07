import json
import os
import re
import sys
from subprocess import PIPE, Popen
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup as bs

def sanitize_filename(name):
    """Sanitize the filename to ensure it's valid."""
    # Replace invalid characters with underscore
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    # Ensure doesn't start with dot
    if name.startswith('.'):
        name = '_' + name
    return name

def process_elonetplus(soup):
    """Process elonetplus.fi URLs."""
    try:
        name_elem = soup.find("h1", dict(property="name"))
        if not name_elem:
            print("Warning: Could not find video title. Using 'video' as filename.")
            name = "video.mp4"
        else:
            name = name_elem.text.strip() + ".mp4"
            name = sanitize_filename(name)
        
        sources_elem = soup.find("span", dict(id="video-data"))
        if not sources_elem or not sources_elem.has_attr("data-video-sources"):
            raise ValueError("Could not find video sources data")
        
        sources = json.loads(sources_elem["data-video-sources"])
        hls_sources = [s for s in sources if s.get('type') == 'application/x-mpegURL']
        if not hls_sources:
            raise ValueError("No HLS video sources found")
        
        return name, hls_sources[0]['src']
    except Exception as e:
        raise ValueError(f"Error processing elonetplus URL: {str(e)}")

def process_finna(soup):
    """Process elonet.finna.fi URLs."""
    try:
        # Try to find the title
        title_elem = soup.find("h1", class_="title")
        if not title_elem:
            title_elem = soup.find("title")
            if title_elem:
                title = title_elem.text.split(" | ")[0].strip()
            else:
                title = "finna_video"
        else:
            title = title_elem.text.strip()
        
        name = sanitize_filename(title) + ".mp4"

        # Method 1: Look for video-js element
        video_elem = soup.find("video", class_="video-js")
        if video_elem and video_elem.has_attr("data-sources"):
            sources = json.loads(video_elem["data-sources"])
            hls_sources = [s for s in sources if s.get('type') == 'application/x-mpegURL']
            if hls_sources:
                return name, hls_sources[0]['src']

        # Method 2: Look for video player div with data attribute
        player_div = soup.find("div", id=lambda x: x and x.startswith("video-player"))
        if player_div and player_div.has_attr("data-video-sources"):
            sources = json.loads(player_div["data-video-sources"])
            hls_sources = [s for s in sources if s.get('type') == 'application/x-mpegURL']
            if hls_sources:
                return name, hls_sources[0]['src']

        # Method 3: Look for <finna-video> tag with source attribute
        finna_video = soup.find("finna-video", attrs={"source": True})
        if finna_video:
            video_url = finna_video["source"]
            return name, video_url

        # Method 4: Search for any script containing video sources
        scripts = soup.find_all("script")
        for script in scripts:
            if script.string and "videoSources" in script.string:
                match = re.search(r'videoSources\s*=\s*(\[.*?\]);', script.string, re.DOTALL)
                if match:
                    sources = json.loads(match.group(1))
                    hls_sources = [s for s in sources if s.get('type') == 'application/x-mpegURL']
                    if hls_sources:
                        return name, hls_sources[0]['src']

        raise ValueError("Could not find video sources in the page")
    except Exception as e:
        raise ValueError(f"Error processing finna URL: {str(e)}")

def determine_site_type(url):
    """Determine which site type the URL belongs to."""
    if "elonetplus.fi" in url:
        return "elonetplus"
    elif "elonet.finna.fi" in url or "finna.fi" in url:
        return "finna"
    else:
        # Default to elonetplus for backward compatibility
        return "elonetplus"

def download_video(url, name):
    """Download and process the HLS video."""
    try:
        # If the URL is not a direct m3u8, try to extract it from the embed page
        if not url.endswith('.m3u8'):
            resp = requests.get(url)
            if resp.status_code != 200:
                raise ValueError(f"Failed to retrieve embed page: HTTP {resp.status_code}")
            # Find the first .m3u8 URL anywhere in the page
            m = re.search(r'https?://[^\s\'"]+\.m3u8[^\s\'"]*', resp.text)
            if not m:
                raise ValueError("Could not find m3u8 playlist URL in embed page")
            url = m.group(0)

        # Now url should be a direct m3u8 playlist
        playlist_resp = requests.get(url)
        if playlist_resp.status_code != 200:
            raise ValueError(f"Failed to retrieve playlist: HTTP {playlist_resp.status_code}")

        # Parse master playlist to find highest quality stream
        best_quality_url = None
        best_bandwidth = 0
        
        for line in playlist_resp.text.split("\n"):
            line = line.strip()
            if not line or line.startswith('#'):  # Skip empty lines and comments
                continue
            
            # Check if this is a variant stream line
            if line.endswith('.m3u8'):
                # Look for bandwidth information in previous lines
                bandwidth = None
                for prev_line in reversed(playlist_resp.text.split("\n")):
                    if prev_line.startswith('#EXT-X-STREAM-INF:'):
                        # Extract bandwidth from the attributes
                        attrs = dict(attr.split('=') for attr in prev_line.split(':')[1].split(',') if '=' in attr)
                        bandwidth = int(attrs.get('BANDWIDTH', 0))
                        break
                
                if bandwidth and bandwidth > best_bandwidth:
                    best_bandwidth = bandwidth
                    best_quality_url = urljoin(url, line)
        
        if best_quality_url:
            print(f"Selected highest quality stream with bandwidth: {best_bandwidth} bits/s")
            # Get actual playlist of chunks from the best quality stream
            ts_resp = requests.get(best_quality_url)
            if ts_resp.status_code != 200:
                raise ValueError(f"Failed to retrieve TS playlist: HTTP {ts_resp.status_code}")
        else:
            # If no master playlist found, use the original URL
            print("No master playlist found, using direct stream")
            ts_resp = playlist_resp
            
        ts_files = [
            urljoin(best_quality_url or url, line)
            for line in ts_resp.text.split("\n")
            if line.strip().endswith(".ts")
        ]
        
        if not ts_files:
            raise ValueError("No TS files found in playlist")
        
        # Download TS files and remux into MP4
        cmd = "ffmpeg -hide_banner -loglevel warning -y -i - -c copy".split(" ")
        with Popen([*cmd, name], stdin=PIPE) as proc:
            total_chunks = len(ts_files)
            for i, ts in enumerate(ts_files, 1):
                print(f">>> Downloading chunk {i}/{total_chunks}: {ts.split('/')[-1]}")
                ts_resp = requests.get(ts)
                if ts_resp.status_code == 200:
                    proc.stdin.write(ts_resp.content)
                else:
                    print(f"Warning: Failed to download chunk {i}: HTTP {ts_resp.status_code}")
        
        if os.path.exists(name) and os.path.getsize(name) > 0:
            print(f"✓ Successfully downloaded: {name}")
            return True
        else:
            print(f"✗ Failed to create output file: {name}")
            return False
    except Exception as e:
        print(f"Error during download: {str(e)}")
        return False

def main():
    # Get URL from command line or prompt
    url = sys.argv[1] if len(sys.argv) == 2 else input("Elonet URL to download:\n")
    print(f"Downloading from {url}...")
    
    try:
        # Get page content
        response = requests.get(url)
        if response.status_code != 200:
            print(f"Error: Failed to retrieve the page: HTTP {response.status_code}")
            return 1
        
        soup = bs(response.text, "html.parser")
        
        # Determine site type and process accordingly
        site_type = determine_site_type(url)
        print(f"Detected site type: {site_type}")
        
        if site_type == "elonetplus":
            name, video_url = process_elonetplus(soup)
        else:  # finna
            name, video_url = process_finna(soup)
        
        print(f"Video title: {name}")
        print(f"Found video URL: {video_url}")
        
        # Download the video
        success = download_video(video_url, name)
        return 0 if success else 1
        
    except ValueError as e:
        print(f"Error: {str(e)}")
        return 1
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
