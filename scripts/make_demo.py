#!/usr/bin/env python3
"""Build a demo project with one file of every kind peek can open.

usage: python3 scripts/make_demo.py <dir>
Needs magick + ffmpeg for the media files; skips anything it can't make.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile


def sh(*cmd):
    if not shutil.which(cmd[0]):
        print("skip (no %s): %s" % (cmd[0], cmd[-1]))
        return
    subprocess.run(cmd, check=True)


def login(path, dark):
    bg, card, ink, mute = ("#16161c", "#22222b", "#ecebe6", "#7c7c88") if dark else ("#f4f1ea", "#ffffff", "#1b1b1f", "#8a8a96")
    sh(
        "magick", "-size", "1440x900", "xc:" + bg,
        "-fill", "#f08049", "-draw", "circle 1180,170 1180,330",
        "-fill", "#5aa0e6", "-draw", "circle 230,760 230,900",
        "-fill", card, "-stroke", mute, "-strokewidth", "1", "-draw", "roundrectangle 520,190 920,710 24,24", "-stroke", "none",
        "-fill", ink, "-font", "DejaVu-Sans-Bold", "-pointsize", "40", "-annotate", "+558+290", "Welcome back",
        "-fill", mute, "-font", "DejaVu-Sans", "-pointsize", "20", "-annotate", "+600+330", "Sign in to your workspace",
        "-fill", "none", "-stroke", mute, "-strokewidth", "2",
        "-draw", "roundrectangle 570,380 870,436 10,10", "-draw", "roundrectangle 570,460 870,516 10,10", "-stroke", "none",
        "-fill", mute, "-pointsize", "18", "-annotate", "+590+415", "you@example.com", "-annotate", "+590+495", "**********",
        "-fill", "#f08049", "-draw", "roundrectangle 570,560 870,620 12,12",
        "-fill", "white", "-font", "DejaVu-Sans-Bold", "-pointsize", "22", "-annotate", "+672+598", "Sign in",
        path,
    )


def main(root):
    for sub in ("shots", "docs", "data"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    j = lambda *p: os.path.join(root, *p)  # noqa: E731

    login(j("shots", "login-light.png"), False)
    login(j("shots", "login-dark.png"), True)

    frames = []
    colors = ["#f08049", "#f0a070", "#ffd27a", "#5cc47e", "#5aa0e6", "#c88cdc"]
    for i in range(12):
        frames += ["(", "-size", "480x270", "xc:#14141a"]
        for k, c in enumerate(colors):
            r = 14 + (24 if (i % len(colors)) == k else 0)
            x = 90 + k * 60
            frames += ["-fill", c, "-draw", "circle %d,135 %d,%d" % (x, x, 135 + r)]
        frames += [")"]
    sh("magick", "-delay", "10", *frames, "-loop", "0", j("shots", "loading.gif"))

    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=6",
       "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", j("shots", "flow.mp4"))
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=4",
       "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
       "-filter_complex", "[0][1]amix=inputs=2,volume=2,afade=t=in:d=0.5,afade=t=out:st=3:d=1",
       "-metadata", "title=Test tone", "-metadata", "artist=peek", j("data", "tone.wav"))

    md = """# Login flow report

Screenshots of the **login screen** in both themes, captured by the agent.

## Results

| Theme | Status | Time |
|-------|--------|------|
| light | pass   | 1.2s |
| dark  | pass   | 1.1s |

- [x] renders the card
- [x] focus ring on inputs
- [ ] error state for wrong password

> Tip: run `npm run shots` to refresh the images.

```ts
await page.goto("/login");
await page.screenshot({ path: "shots/login-dark.png" });
```
"""
    with open(j("docs", "report.md"), "w") as fh:
        fh.write(md)

    html = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font:18px/1.5 system-ui,sans-serif;background:#f6f5f1;color:#1b1b1f;margin:0;padding:48px}
h1{font-size:44px;margin:0 0 8px} .k{color:#d9622b} .card{background:#fff;border:1px solid #e3e1da;border-radius:14px;padding:24px;margin-top:24px;max-width:720px}
.bar{height:14px;border-radius:7px;background:linear-gradient(90deg,#d9622b,#ffd27a)}
</style></head><body><h1><span class="k">herdr-</span>peek</h1><p>Rendered offline with headless Chromium.</p>
<div class="card"><b>Coverage</b><div class="bar" style="width:82%"></div><p>82% of lines covered</p></div></body></html>"""
    with open(j("docs", "coverage.html"), "w") as fh:
        fh.write(html)

    with open(j("data", "results.csv"), "w") as fh:
        fh.write("run,theme,status,duration_ms,screenshots\n")
        for i in range(1, 41):
            fh.write("%d,%s,%s,%d,%d\n" % (i, "dark" if i % 2 else "light", "pass" if i % 7 else "fail", 900 + (i * 37) % 400, 2 + i % 3))

    with open(j("data", "config.json"), "w") as fh:
        json.dump({"name": "web-app", "version": "1.4.0", "private": True, "scripts": {"dev": "vite", "shots": "playwright test"},
                   "viewport": {"width": 1440, "height": 900}, "themes": ["light", "dark"], "retries": 2, "baseURL": None}, fh, indent=2)

    with open(j("data", "events.jsonl"), "w") as fh:
        for i in range(5):
            fh.write(json.dumps({"t": 1700000000 + i, "event": "screenshot", "file": "shots/login-%s.png" % ("dark" if i % 2 else "light")}) + "\n")

    db = j("data", "app.db")
    if os.path.exists(db):
        os.unlink(db)
    con = sqlite3.connect(db)
    con.execute("create table users(id integer primary key, email text, theme text, created text)")
    con.executemany("insert into users(email, theme, created) values (?,?,?)", [("user%d@example.com" % i, "dark" if i % 2 else "light", "2026-10-0%d" % (1 + i % 5)) for i in range(25)])
    con.execute("create table sessions(id integer primary key, user_id int, ok int)")
    con.executemany("insert into sessions(user_id, ok) values (?,?)", [(i % 25, i % 4 != 0) for i in range(60)])
    con.commit()
    con.close()

    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Timing analysis"]},
            {"cell_type": "code", "execution_count": 1, "metadata": {}, "source": ["import statistics\n", "times = [1.2, 1.1, 1.3]\n", "statistics.mean(times)"],
             "outputs": [{"output_type": "execute_result", "execution_count": 1, "data": {"text/plain": ["1.2"]}, "metadata": {}}]},
        ],
    }
    with open(j("docs", "analysis.ipynb"), "w") as fh:
        json.dump(nb, fh)

    with open(j("src.py"), "w") as fh:
        fh.write('"""Take screenshots of the login flow."""\n\nimport asyncio\n\n\nasync def shoot(page, theme):\n    await page.goto("/login?theme=" + theme)\n    path = f"shots/login-{theme}.png"\n    await page.screenshot(path=path)\n    return path\n\n\nasync def main(browser):\n    page = await browser.new_page(viewport={"width": 1440, "height": 900})\n    return [await shoot(page, t) for t in ("light", "dark")]\n')

    with zipfile.ZipFile(j("data", "shots.zip"), "w") as zf:
        zf.write(j("shots", "login-light.png"), "login-light.png")
        zf.write(j("shots", "login-dark.png"), "login-dark.png")
    with tarfile.open(j("data", "docs.tar.gz"), "w:gz") as tf:
        tf.add(j("docs"), "docs")

    with open(j("data", "blob.bin"), "wb") as fh:
        fh.write(bytes(range(256)) * 8)

    # PDF via LibreOffice from the markdown-ish text, or via ImageMagick
    if shutil.which("magick"):
        sh("magick", j("shots", "login-light.png"), j("shots", "login-dark.png"), "-resize", "1224x1584", "-gravity", "center", "-background", "white", "-extent", "1224x1584", j("docs", "report.pdf"))
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if soffice:
        with open(j("docs", "summary.txt"), "w") as fh:
            fh.write("Login flow summary\n\nBoth themes pass. Dark theme renders 0.1s faster.\n")
        subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", j("docs"), j("docs", "summary.txt")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.unlink(j("docs", "summary.txt"))
    print("demo ready in", root)


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "demo"))
