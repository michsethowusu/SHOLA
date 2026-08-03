#!/usr/bin/env python3
"""Render the SHOLA brand assets.

Each asset is an HTML template rendered by headless Chrome at an exact pixel
size, so the artwork uses the same typeface and palette as the site and can be
regenerated whenever the brand changes.

    python3 brand/build.py

Sizes are chosen for how people actually share things in Ghana: WhatsApp Status
first, then TikTok and Instagram, then X, Facebook and YouTube.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Written into static so the site can hand them out directly: an
# influencer should not need a GitHub account to get a logo.
OUT = HERE.parent / "shola" / "static" / "brand"
SITE = "shola.inkika.org"

# Every Ghanaian language is open, so the artwork counts them rather than
# naming a few — naming four read as a promise that the rest were shut out.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shola.config import ALL_LANGUAGES  # noqa: E402

N_LANG = len(ALL_LANGUAGES)

RED = "#c0392b"
INK = "#1a1815"
SAND = "#faf7f2"
GOLD = "#d99b2b"
FOREST = "#1a635a"

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?'
         'family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,700;12..96,800'
         '&display=swap" rel="stylesheet">')

BASE_CSS = f"""
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Bricolage Grotesque','Trebuchet MS',sans-serif;
         -webkit-font-smoothing:antialiased; }}
  .sheet {{ width:100vw; height:100vh; display:flex; flex-direction:column;
           justify-content:space-between; background:{SAND}; color:{INK}; }}
  .mark {{ display:flex; align-items:baseline; font-weight:800;
          letter-spacing:-.04em; }}
  .mark .o {{ color:{RED}; }}
  .url {{ font-weight:700; color:{RED}; }}
  .glyphs {{ color:{RED}; letter-spacing:.14em; font-weight:700; opacity:.9; }}
  .tag {{ display:inline-block; background:{RED}; color:#fff; font-weight:700;
         border-radius:999px; }}
"""


def logo(size):
    """The wordmark, sized in px."""
    return (f'<div class="mark" style="font-size:{size}px">'
            f'SH<span class="o">Ɔ</span>LA</div>')


def story(headline, sub, kicker="Share Your Language"):
    """1080x1920 — WhatsApp Status, Instagram and TikTok stories."""
    return f"""<div class="sheet" style="padding:120px 90px">
  <div>
    {logo(76)}
    <div style="font-size:30px;color:#57514a;margin-top:10px">{kicker}</div>
  </div>
  <div>
    <div style="font-size:96px;font-weight:800;line-height:1.05;
                letter-spacing:-.035em">{headline}</div>
    <div style="font-size:40px;line-height:1.4;color:#57514a;margin-top:38px">
      {sub}</div>
  </div>
  <div>
    <div class="glyphs" style="font-size:66px;margin-bottom:30px">
      ɛ ɔ ŋ ɖ ƒ ɣ ʋ ʒ</div>
    <div class="tag" style="font-size:44px;padding:26px 46px">{SITE}</div>
  </div>
</div>"""


def square(headline, sub):
    """1080x1080 — Instagram and Facebook feed."""
    return f"""<div class="sheet" style="padding:90px">
  <div>{logo(64)}</div>
  <div>
    <div style="font-size:84px;font-weight:800;line-height:1.06;
                letter-spacing:-.035em">{headline}</div>
    <div style="font-size:36px;line-height:1.4;color:#57514a;margin-top:30px">
      {sub}</div>
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div class="glyphs" style="font-size:52px">ɛ ɔ ŋ ɖ ƒ ɣ ʋ ʒ</div>
    <div class="url" style="font-size:38px">{SITE}</div>
  </div>
</div>"""


def wide(headline, sub, big=False):
    """1600x900 for X, 1200x630 for Facebook link previews."""
    scale = 1.25 if big else 1.0
    return f"""<div class="sheet" style="padding:{int(80*scale)}px">
  <div>{logo(int(56*scale))}</div>
  <div>
    <div style="font-size:{int(78*scale)}px;font-weight:800;line-height:1.05;
                letter-spacing:-.035em;max-width:20ch">{headline}</div>
    <div style="font-size:{int(32*scale)}px;line-height:1.4;color:#57514a;
                margin-top:{int(24*scale)}px;max-width:44ch">{sub}</div>
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div class="glyphs" style="font-size:{int(44*scale)}px">ɛ ɔ ŋ ɖ ƒ ɣ ʋ ʒ</div>
    <div class="url" style="font-size:{int(34*scale)}px">{SITE}</div>
  </div>
</div>"""


def thumbnail():
    """1280x720 — YouTube. Big type, readable as a small thumbnail."""
    return f"""<div class="sheet" style="padding:64px;background:{INK};
              color:{SAND}">
  <div class="mark" style="font-size:46px;color:{SAND}">
    SH<span style="color:{RED}">Ɔ</span>LA</div>
  <div>
    <div style="font-size:34px;font-weight:700;color:#a89f93;
                letter-spacing:.02em">DO YOU SPEAK</div>
    <div style="font-size:88px;font-weight:800;line-height:1.02;
                letter-spacing:-.04em;margin-top:6px">
      A GHANAIAN<br>LANGUAGE?</div>
    <div style="font-size:36px;color:{GOLD};margin-top:20px;font-weight:700">
      Your language needs you — 2 minutes a day</div>
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div style="color:{RED};font-size:52px;letter-spacing:.14em;font-weight:700">
      ɛ ɔ ŋ ɣ</div>
    <div style="font-size:34px;font-weight:700;color:{SAND}">{SITE}</div>
  </div>
</div>"""


def lower_third():
    """1920x1080, transparent. Art sits in the lower-left third.

    For talking-head video: the graphic goes on the timeline full-frame and the
    speaker, and whatever else is on screen, stays visible around it.
    """
    return f"""<div style="width:100vw;height:100vh;position:relative">
  <div style="position:absolute;left:70px;bottom:80px;width:840px;
              background:{SAND};border-radius:26px;padding:38px 44px;
              box-shadow:0 18px 60px rgba(0,0,0,.35);
              border-left:12px solid {RED}">
    <div style="display:flex;align-items:baseline;gap:18px">
      <div class="mark" style="font-size:52px;color:{INK}">
        SH<span class="o">Ɔ</span>LA</div>
      <div style="font-size:26px;color:#57514a">Share Your Language</div>
    </div>
    <div style="font-size:40px;font-weight:700;letter-spacing:-.02em;
                margin-top:14px;color:{INK};line-height:1.15">
      Check translations in your language</div>
    <div style="display:flex;align-items:center;justify-content:space-between;
                margin-top:18px">
      <div class="glyphs" style="font-size:34px">ɛ ɔ ŋ ɖ ƒ ɣ ʋ ʒ</div>
      <div class="url" style="font-size:32px">{SITE}</div>
    </div>
  </div>
</div>"""


def side_panel():
    """1920x1080, transparent. A vertical panel down the left side.

    Leaves roughly two thirds of the frame free, so it can sit alongside a
    speaker for a whole segment rather than a few seconds.
    """
    return f"""<div style="width:100vw;height:100vh;position:relative">
  <div style="position:absolute;left:0;top:0;bottom:0;width:620px;
              background:{SAND};padding:80px 60px;display:flex;
              flex-direction:column;justify-content:space-between;
              box-shadow:26px 0 70px rgba(0,0,0,.32);
              border-right:10px solid {RED}">
    <div>
      <div class="mark" style="font-size:64px;color:{INK}">
        SH<span class="o">Ɔ</span>LA</div>
      <div style="font-size:27px;color:#57514a;margin-top:8px">
        Share Your Language</div>
    </div>
    <div>
      <div style="font-size:60px;font-weight:800;line-height:1.08;
                  letter-spacing:-.03em;color:{INK}">
        Your language,<br>checked by<br>you.</div>
      <div style="font-size:29px;color:#57514a;margin-top:24px;line-height:1.4">
        {N_LANG} Ghanaian languages.<br>Two minutes a day.</div>
    </div>
    <div>
      <div class="glyphs" style="font-size:42px;margin-bottom:22px">
        ɛ ɔ ŋ<br>ɖ ƒ ɣ ʋ ʒ</div>
      <div class="tag" style="font-size:30px;padding:18px 30px">{SITE}</div>
    </div>
  </div>
</div>"""


def avatar():
    """1080x1080 profile picture."""
    return f"""<div style="width:100vw;height:100vh;background:{RED};
              display:flex;align-items:center;justify-content:center">
  <svg width="62%" height="62%" viewBox="0 0 32 32">
    <path d="M 8.37 20.77 A 9 9 0 1 0 8.37 11.23" fill="none" stroke="#fff"
          stroke-width="4.6" stroke-linecap="round"/>
  </svg>
</div>"""


def wordmark(colour, bg):
    return (f'<div style="width:100vw;height:100vh;background:{bg};display:flex;'
            f'align-items:center;justify-content:center">'
            f'<div class="mark" style="font-size:200px;color:{colour}">'
            f'SH<span class="o">Ɔ</span>LA</div></div>')


ASSETS = [
    ("story-why", 1080, 1920, False,
     story("Keep your<br>language<br>alive.",
           f"{N_LANG} Ghanaian languages.<br>A few words a day, by email.")),
    ("story-how", 1080, 1920, False,
     story("2 minutes<br>a day.",
           "Tap the right translation. Skip what you<br>are unsure of. "
           "That is the whole job.")),
    ("story-ask", 1080, 1920, False,
     story("Do you speak<br>a Ghanaian<br>language?",
           f"All {N_LANG} of them are open.<br>Nobody has added yours yet? "
           "Then<br>you go first.")),
    ("square-why", 1080, 1080, False,
     square("Keep your language alive.",
            f"Help confirm translations in {N_LANG} Ghanaian languages. "
            "A few words a day.")),
    ("square-ask", 1080, 1080, False,
     square("Speak a Ghanaian language?",
            "Two minutes a day keeps your language alive.")),
    ("x-post", 1600, 900, False,
     wide("Your language, checked by the people who speak it.",
          f"Confirm translations in {N_LANG} Ghanaian languages. "
          "A few words a day, by email.")),
    ("facebook-link", 1200, 630, True,
     wide("Keep your language alive.",
          f"A few words a day, in any of {N_LANG} Ghanaian languages.")),
    ("youtube-thumbnail", 1280, 720, False, thumbnail()),
    ("youtube-lowerthird", 1920, 1080, False, lower_third()),
    ("youtube-sidepanel", 1920, 1080, False, side_panel()),
    ("avatar", 1080, 1080, False, avatar()),
    ("wordmark-light", 1200, 400, False, wordmark(INK, SAND)),
    ("wordmark-dark", 1200, 400, False, wordmark(SAND, INK)),
]


def chrome():
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return name
    sys.exit("no Chrome or Chromium found")


TRANSPARENT = {"youtube-lowerthird", "youtube-sidepanel"}


def render(name, w, h, body, tmpdir, browser):
    html = (f"<!doctype html><html><head><meta charset='utf-8'>{FONTS}"
            f"<style>{BASE_CSS}</style></head><body>{body}</body></html>")
    page = Path(tmpdir) / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    out = OUT / f"{name}-{w}x{h}.png"
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--hide-scrollbars", f"--window-size={w},{h}",
           "--virtual-time-budget=4000", f"--screenshot={out}", f"file://{page}"]
    if name in TRANSPARENT:
        cmd.insert(1, "--default-background-color=00000000")
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return out


ZIP = OUT / "shola-brand-kit.zip"

# The mark on its own, for anyone who needs it as vector. Same file as the
# favicon, written here so the kit is never missing it.
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="SHOLA">
  <!-- Filled tile so the mark reads at 16px in a browser tab. -->
  <rect width="32" height="32" rx="7" fill="#c0392b"/>
  <!-- Open O (U+0186), drawn as an arc rather than text: a text favicon needs
       the viewer to have a font containing the glyph, and many do not. -->
  <path d="M 8.37 20.77 A 9 9 0 1 0 8.37 11.23"
        fill="none" stroke="#ffffff" stroke-width="4.6" stroke-linecap="round"/>
</svg>
"""


def bundle():
    """Pack the kit for download.

    Built here rather than by hand, so the zip cannot fall behind the artwork
    and the captions it ships with.
    """
    extras = [OUT / "logo-mark.svg", Path(__file__).with_name("README.md")]
    # Write to a temporary name and move it into place, so nothing ever reads a
    # half-written archive.
    tmp_zip = ZIP.with_suffix(".zip.new")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for png in sorted(OUT.glob("*.png")):
            z.write(png, f"shola-brand-kit/{png.name}")
        for extra in extras:
            if extra.exists():
                z.write(extra, f"shola-brand-kit/{extra.name}")
    tmp_zip.replace(ZIP)
    with zipfile.ZipFile(ZIP) as z:
        assert z.testzip() is None
        return len(z.namelist())


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "logo-mark.svg").write_text(LOGO_SVG, encoding="utf-8")
    browser = chrome()
    with tempfile.TemporaryDirectory() as tmp:
        for name, w, h, _big, body in ASSETS:
            out = render(name, w, h, body, tmp, browser)
            print(f"  {out.name:38s} {out.stat().st_size // 1024:>5} KB")
    print(f"\n{len(ASSETS)} assets in {OUT}")
    n = bundle()
    print(f"{ZIP.name}: {n} files, {ZIP.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
