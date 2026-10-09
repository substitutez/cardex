#!/usr/bin/env python3
"""
CarDex - iOS App Store Asset Generator & Launch Screen Optimizer
Generates pixel-perfect 1024x1024 master icon and all required Apple App Store
and iPhone icon assets with zero alpha transparency (24-bit RGB), updates
AppIcon.appiconset/Contents.json, and verifies dark cockpit theme styling.
"""

import json
import math
import os
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

def get_base_dir() -> Path:
    script_path = Path(__file__).resolve()
    if script_path.parent.name == "scripts":
        if (script_path.parent.parent / "ios").exists():
            return script_path.parent.parent
    cwd = Path.cwd()
    if (cwd / "cardex" / "ios").exists():
        return cwd / "cardex"
    if (cwd / "ios").exists():
        return cwd
    return script_path.parent.parent


def create_master_cockpit_icon(size: int = 1024) -> Image.Image:
    """
    Synthesizes a high-resolution dark cockpit tachometer reticle icon (1024x1024 RGB).
    Features:
      - Deep dark background (#0a0b0e) with radial carbon glow
      - Concentric tachometer arcs (Emerald -> Amber -> Blood Redline)
      - Reticle crosshairs and calibration tick marks
      - Central CarDex speed emblem & glowing needle
      - Zero alpha transparency (RGB mode)
    """
    img = Image.new("RGB", (size, size), color=(10, 11, 14)) # #0a0b0e
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    r_outer = int(size * 0.44)

    # 1. Subtle radial background gradient
    for r in range(r_outer, 0, -4):
        ratio = r / r_outer
        c_r = int(10 + (16 * (1.0 - ratio)))
        c_g = int(11 + (32 * (1.0 - ratio)))
        c_b = int(14 + (48 * (1.0 - ratio)))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(c_r, c_g, c_b))

    # 2. Outer tachometer bezel ring
    bezel_r = int(size * 0.42)
    draw.ellipse([cx - bezel_r, cy - bezel_r, cx + bezel_r, cy + bezel_r], outline=(30, 41, 59), width=8)

    # 3. Tachometer dial colored arc (from 135 deg to 405 deg)
    draw.arc([cx - bezel_r + 16, cy - bezel_r + 16, cx + bezel_r - 16, cy + bezel_r - 16],
             start=135, end=270, fill=(16, 185, 129), width=18)
    draw.arc([cx - bezel_r + 16, cy - bezel_r + 16, cx + bezel_r - 16, cy + bezel_r - 16],
             start=270, end=345, fill=(245, 158, 11), width=18)
    draw.arc([cx - bezel_r + 16, cy - bezel_r + 16, cx + bezel_r - 16, cy + bezel_r - 16],
             start=345, end=405, fill=(239, 68, 68), width=22)

    # 4. Tachometer tick marks
    num_ticks = 48
    for i in range(num_ticks):
        angle_deg = 135 + (i / (num_ticks - 1)) * 270
        angle_rad = math.radians(angle_deg)
        is_major = (i % 4 == 0)
        tick_len = 28 if is_major else 14
        tick_w = 4 if is_major else 2

        r1 = bezel_r - 36
        r2 = r1 - tick_len
        x1 = cx + int(r1 * math.cos(angle_rad))
        y1 = cy + int(r1 * math.sin(angle_rad))
        x2 = cx + int(r2 * math.cos(angle_rad))
        y2 = cy + int(r2 * math.sin(angle_rad))

        color = (255, 255, 255) if is_major else (100, 116, 139)
        if angle_deg >= 345:
            color = (239, 68, 68)
        elif angle_deg >= 270:
            color = (245, 158, 11)

        draw.line([(x1, y1), (x2, y2)], fill=color, width=tick_w)

    # 5. Reticle crosshairs (subtle cyan HUD lines)
    reticle_r = int(size * 0.28)
    draw.line([(cx - reticle_r, cy), (cx - 40, cy)], fill=(56, 189, 248), width=2)
    draw.line([(cx + 40, cy), (cx + reticle_r, cy)], fill=(56, 189, 248), width=2)
    draw.line([(cx, cy - reticle_r), (cx, cy - 40)], fill=(56, 189, 248), width=2)
    draw.line([(cx, cy + 40), (cx, cy + reticle_r)], fill=(56, 189, 248), width=2)

    # 6. Central Tach Needle swept at high RPM (330 degrees)
    needle_angle = math.radians(330)
    needle_len = int(bezel_r * 0.78)
    nx = cx + int(needle_len * math.cos(needle_angle))
    ny = cy + int(needle_len * math.sin(needle_angle))

    draw.line([(cx, cy), (nx, ny)], fill=(239, 68, 68), width=10)
    draw.line([(cx, cy), (nx, ny)], fill=(254, 202, 202), width=4)

    # 7. Center Hub Hubcaps
    draw.ellipse([cx - 46, cy - 46, cx + 46, cy + 46], fill=(15, 23, 42), outline=(56, 189, 248), width=5)
    draw.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(239, 68, 68))
    draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=(255, 255, 255))

    # 8. Stylized CarDex speed wings / chevron below center
    chevron_y = cy + int(size * 0.22)
    draw.polygon([
        (cx - 100, chevron_y + 10),
        (cx, chevron_y - 25),
        (cx + 100, chevron_y + 10),
        (cx, chevron_y - 8)
    ], fill=(56, 189, 248))

    draw.polygon([
        (cx - 70, chevron_y + 36),
        (cx, chevron_y + 5),
        (cx + 70, chevron_y + 36),
        (cx, chevron_y + 20)
    ], fill=(14, 165, 233))

    return img


def generate_all_ios_icons(base_dir: Path):
    appicon_dir = base_dir / "ios" / "App" / "App" / "Assets.xcassets" / "AppIcon.appiconset"
    appicon_dir.mkdir(parents=True, exist_ok=True)

    master_icon_path = appicon_dir / "master_icon.png"

    print(f"[*] Generating master 1024x1024 RGB cockpit icon...")
    master_icon = create_master_cockpit_icon(1024)
    master_icon.save(master_icon_path, format="PNG")
    print(f"    Saved: {master_icon_path} (mode: {master_icon.mode}, size: {master_icon.size})")

    icon_specs = [
        ("iphone", "20x20", "2x", "AppIcon-20@2x.png", 40),
        ("iphone", "20x20", "3x", "AppIcon-20@3x.png", 60),
        ("iphone", "29x29", "2x", "AppIcon-29@2x.png", 58),
        ("iphone", "29x29", "3x", "AppIcon-29@3x.png", 87),
        ("iphone", "40x40", "2x", "AppIcon-40@2x.png", 80),
        ("iphone", "40x40", "3x", "AppIcon-40@3x.png", 120),
        ("iphone", "60x60", "2x", "AppIcon-60@2x.png", 120),
        ("iphone", "60x60", "3x", "AppIcon-60@3x.png", 180),
        ("ios-marketing", "1024x1024", "1x", "AppIcon-512@2x.png", 1024),
    ]

    contents_images = []

    print(f"[*] Exporting all required Apple App Store icon sizes...")
    for idiom, size_str, scale_str, filename, px in icon_specs:
        out_path = appicon_dir / filename
        resized = master_icon.resize((px, px), Image.Resampling.LANCZOS)
        if resized.mode != "RGB":
            resized = resized.convert("RGB")
        resized.save(out_path, format="PNG")

        contents_images.append({
            "size": size_str,
            "idiom": idiom,
            "filename": filename,
            "scale": scale_str
        })
        print(f"    ✓ {filename:20} -> {px}x{px}px (idiom: {idiom}, scale: {scale_str})")

    contents_data = {
        "images": contents_images,
        "info": {
            "version": 1,
            "author": "xcode"
        }
    }
    contents_json_path = appicon_dir / "Contents.json"
    with open(contents_json_path, "w", encoding="utf-8") as f:
        json.dump(contents_data, f, indent=2)
    print(f"[*] Updated: {contents_json_path}")

    print(f"[*] Verifying transparency compliance across all generated icons...")
    for item in contents_images:
        path = appicon_dir / item["filename"]
        assert path.exists(), f"Missing icon: {path}"
        with Image.open(path) as img_check:
            assert img_check.mode == "RGB", f"Icon {path.name} must be RGB, got {img_check.mode}"
            if "A" in img_check.getbands():
                raise AssertionError(f"Icon {path.name} contains alpha channel!")
    print("    ✓ All icon assets verified: Mode is RGB with ZERO transparent pixels.")


def update_launchscreen_storyboard(base_dir: Path):
    storyboard_path = base_dir / "ios" / "App" / "App" / "Base.lproj" / "LaunchScreen.storyboard"
    if not storyboard_path.exists():
        print(f"[-] LaunchScreen.storyboard not found at {storyboard_path}")
        return

    print(f"[*] Updating LaunchScreen.storyboard for #0a0b0e dark cockpit theme...")
    content = storyboard_path.read_text(encoding="utf-8")

    dark_color_def = '''        <systemColor name="systemBackgroundColor">
            <color red="0.03921568627" green="0.0431372549" blue="0.05490196078" alpha="1" colorSpace="custom" customColorSpace="sRGB"/>
        </systemColor>'''

    if '<systemColor name="systemBackgroundColor">' in content:
        if 'red="0.03921568627"' not in content:
            import re
            pattern = r'<systemColor name="systemBackgroundColor">\s*<color[^>]+/>\s*</systemColor>'
            content = re.sub(pattern, dark_color_def, content)
            storyboard_path.write_text(content, encoding="utf-8")
            print("    ✓ Updated systemBackgroundColor to #0a0b0e in LaunchScreen.storyboard")
        else:
            print("    ✓ LaunchScreen.storyboard is already styled with dark cockpit theme.")
    else:
        print("    [!] systemBackgroundColor element not found in storyboard, leaving intact.")


def main():
    base_dir = get_base_dir()
    print(f"[*] CarDex iOS Asset Generator targeting base directory: {base_dir}")
    generate_all_ios_icons(base_dir)
    update_launchscreen_storyboard(base_dir)
    print("\n[SUCCESS] Apple App Store asset generation and Launch Screen styling complete!")


if __name__ == "__main__":
    main()
