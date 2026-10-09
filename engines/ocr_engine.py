"""
Image OCR & Pixel Status Inspection Engine for GreyOrange OpsBot.
Provides dual-pipeline OCR (Tesseract + Windows Native OCR) and visual pixel color detection for Grafana gauges.
"""

import os
import sys
import subprocess
import re

from core.config import pytesseract, Image


def extract_text_via_windows_ocr(image_path):
    if sys.platform != "win32" or not image_path or not os.path.exists(image_path):
        return None

    abs_path = os.path.abspath(image_path).replace("'", "''")
    ps_cmd = f"""
    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null
    try {{
        $fOp = [Windows.Storage.StorageFile]::GetFileFromPathAsync('{abs_path}')
        while ($fOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $file = $fOp.GetResults()

        $sOp = $file.OpenAsync(0)
        while ($sOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $stream = $sOp.GetResults()

        $dOp = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
        while ($dOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $decoder = $dOp.GetResults()

        $bOp = $decoder.GetSoftwareBitmapAsync()
        while ($bOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $bitmap = $bOp.GetResults()

        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) {{
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
        }}
        if ($null -eq $engine) {{
            $engine = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | Select-Object -First 1 | ForEach-Object {{ [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($_) }}
        }}

        $rOp = $engine.RecognizeAsync($bitmap)
        while ($rOp.Status -eq 0) {{ [System.Threading.Thread]::Sleep(15) }}
        $ocr = $rOp.GetResults()

        $ocr.Lines | ForEach-Object {{ $_.Text }}
    }} catch {{
        Write-Error $_
    }}
    """
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=25
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if lines:
                return lines
    except Exception:
        pass
    return None


def extract_text_via_tesseract(image_path):
    if not image_path or not os.path.exists(image_path):
        return None

    tess_bin = "tesseract"
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
            r"C:\msys64\ucrt64\bin\tesseract.exe",
            r"C:\msys64\mingw64\bin\tesseract.exe",
            r"C:\tools\tesseract\tesseract.exe",
            r"C:\ProgramData\chocolatey\bin\tesseract.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                tess_bin = c
                if pytesseract:
                    pytesseract.pytesseract.tesseract_cmd = c
                break

    if pytesseract and Image:
        try:
            img = Image.open(image_path)
            raw_text = pytesseract.image_to_string(img)
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            if lines:
                return lines
        except Exception:
            pass

    try:
        res = subprocess.run([tess_bin, image_path, "stdout"], capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout.strip():
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if lines:
                return lines
    except Exception:
        pass

    return None


def analyze_image_pixels(image_path):
    if not Image or not image_path or not os.path.exists(image_path):
        return {"colors": [], "has_red": False, "has_yellow": False}
    try:
        with Image.open(image_path) as img:
            rgb_img = img.convert("RGB")
            small = rgb_img.resize((150, 150))
            pixels = list(small.getdata())
            red_count = 0
            yellow_count = 0
            total = len(pixels)
            for r, g, b in pixels:
                if r > 140 and g < 65 and b < 65:
                    red_count += 1
                elif r > 180 and g > 130 and b < 65:
                    yellow_count += 1

            detected = []
            if red_count >= (total * 0.008):
                detected.append("red")
            if yellow_count >= (total * 0.008):
                detected.append("yellow")

            return {
                "colors": detected,
                "has_red": "red" in detected,
                "has_yellow": "yellow" in detected,
                "red_pixel_ratio": round(red_count / total, 3)
            }
    except Exception:
        return {"colors": [], "has_red": False, "has_yellow": False}


def extract_image_ocr(image_path):
    if not image_path or not os.path.exists(image_path):
        return {"success": False, "engine": "none", "lines": [], "numbers": [], "raw_text": "", "pixel_colors": []}

    lines = extract_text_via_tesseract(image_path)
    engine_used = "Tesseract OCR" if lines else "none"

    if not lines and sys.platform == "win32":
        lines = extract_text_via_windows_ocr(image_path)
        if lines:
            engine_used = "Windows Native OCR"

    pixel_info = analyze_image_pixels(image_path)
    pixel_colors = pixel_info.get("colors", [])

    if not lines:
        if pixel_colors:
            return {
                "success": True,
                "engine": f"Visual Pixel Inspector ({', '.join(pixel_colors).upper()})",
                "lines": [f"Visual Color Signal: {', '.join(pixel_colors).upper()} status detected from gauge pixels."],
                "numbers": [],
                "raw_text": f"Visual Color Signal: {', '.join(pixel_colors).upper()}",
                "pixel_colors": pixel_colors
            }
        return {"success": False, "engine": "none", "lines": [], "numbers": [], "raw_text": "", "pixel_colors": []}

    raw_text = "\n".join(lines)
    numbers = []
    num_pattern = re.compile(r"([A-Za-z0-9_\-\s]{2,22}[:=]?\s*[-+]?\d+(?:\.\d+)?\s*(?:%|k|m|g|totes|orders|ms|s)?)", re.IGNORECASE)
    for line in lines:
        matches = num_pattern.findall(line)
        for m in matches:
            clean_m = m.strip()
            if clean_m and clean_m not in numbers:
                numbers.append(clean_m)

    return {
        "success": True,
        "engine": engine_used,
        "lines": lines,
        "numbers": numbers,
        "raw_text": raw_text,
        "pixel_colors": pixel_colors
    }


def extract_numbers_from_text(text_or_lines):
    """Extract numeric metrics and key-value patterns from text or a list of lines."""
    if not text_or_lines:
        return []
    if isinstance(text_or_lines, str):
        lines = [line.strip() for line in text_or_lines.splitlines() if line.strip()]
    else:
        lines = text_or_lines
    numbers = []
    num_pattern = re.compile(r"([A-Za-z0-9_\-\s]{2,22}[:=]?\s*[-+]?\d+(?:\.\d+)?\s*(?:%|k|m|g|totes|orders|ms|s)?)", re.IGNORECASE)
    for line in lines:
        matches = num_pattern.findall(line)
        for m in matches:
            clean_m = m.strip()
            if clean_m and clean_m not in numbers:
                numbers.append(clean_m)
    return numbers


# Aliases for backward compatibility
inspect_screenshot_pixels = analyze_image_pixels
run_ocr_on_screenshot = extract_image_ocr

