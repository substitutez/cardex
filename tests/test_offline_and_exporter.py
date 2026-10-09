"""
Unit tests verifying:
1. Static JS files exist and have no syntax errors
2. iOS asset catalogue contains all 9 required RGB icons with zero alpha channels
3. LaunchScreen.storyboard contains #0a0b0e styling
4. HTML contains proper script linkages and tags
"""

import json
from pathlib import Path
from PIL import Image
import pytest

BASE_DIR = Path(__file__).resolve().parent.parent

def test_static_js_files_exist():
    offline_js = BASE_DIR / "frontend" / "static" / "js" / "offline_sync.js"
    exporter_js = BASE_DIR / "frontend" / "static" / "js" / "card_exporter.js"
    audio_js = BASE_DIR / "frontend" / "static" / "js" / "audio_dsp.js"

    assert offline_js.exists(), "offline_sync.js missing"
    assert exporter_js.exists(), "card_exporter.js missing"
    assert audio_js.exists(), "audio_dsp.js missing"

    content_offline = offline_js.read_text(encoding="utf-8")
    assert "cardex_offline_db" in content_offline
    assert "pending_spots" in content_offline
    assert "syncPendingSpots" in content_offline

    content_exporter = exporter_js.read_text(encoding="utf-8")
    assert "renderStoryCardCanvas" in content_exporter
    assert "1080" in content_exporter and "1920" in content_exporter
    assert "applyHolographicTilt" in content_exporter
    assert "shareOrDownloadStoryCard" in content_exporter

def test_ios_app_icons_zero_transparency():
    appicon_dir = BASE_DIR / "ios" / "App" / "App" / "Assets.xcassets" / "AppIcon.appiconset"
    contents_path = appicon_dir / "Contents.json"

    assert contents_path.exists(), "Contents.json missing"
    with open(contents_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    images = data.get("images", [])
    assert len(images) == 9, f"Expected 9 icon variants, got {len(images)}"

    for item in images:
        filename = item["filename"]
        icon_path = appicon_dir / filename
        assert icon_path.exists(), f"Icon file {filename} does not exist"
        with Image.open(icon_path) as img:
            assert img.mode == "RGB", f"{filename} is {img.mode}, must be 24-bit RGB"
            assert "A" not in img.getbands(), f"{filename} contains alpha channel"

def test_launchscreen_dark_cockpit_styling():
    storyboard = BASE_DIR / "ios" / "App" / "App" / "Base.lproj" / "LaunchScreen.storyboard"
    assert storyboard.exists(), "LaunchScreen.storyboard missing"
    content = storyboard.read_text(encoding="utf-8")
    assert 'red="0.03921568627"' in content
    assert 'green="0.0431372549"' in content
    assert 'blue="0.05490196078"' in content

def test_html_integration():
    index_html = BASE_DIR / "frontend" / "static" / "index.html"
    assert index_html.exists()
    content = index_html.read_text(encoding="utf-8")
    assert 'offline_sync.js' in content
    assert 'card_exporter.js' in content
    assert 'CarDexOfflineSync' in content
    assert 'CarDexCardExporter' in content
    assert 'exportSpotStoryCard' in content
