"""A tiny help center whose pages change by "day", for the 10 URL benchmark cases (Step 4).

Day 1: a sitemap index with 2 nested sitemaps; /articles/1 is listed in both (a duplicate).
Day 2: /articles/3 answers 404; /articles/4 moved for good (301 to /articles/4-new);
       /articles/5 is no longer in the sitemap.
Day 3: the same again (a second miss in a row).
Day 4: /articles/3 works again; /articles/5 is back in the sitemap.

    uvicorn benchmark.url_site.app:app --port 8083     (the fixture-site service)
POST /_control/day/{n} changes the day; tests call the app in-process, no network.
"""

import zlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

app = FastAPI(title="URL benchmark fixture site", docs_url=None, redoc_url=None)
STATE = {"day": 1}

TOPICS = {
    1: (
        "Add a custom domain",
        "Open Settings, then Domains. Enter your domain and save. Point the A record to the address shown.",
    ),
    2: (
        "Reset a member password",
        "Open Members, find the member, then choose Send password reset. The member gets an email link.",
    ),
    3: (
        "Export the member list",
        "Open Members, choose Export, then CSV. The file downloads with every active member.",
    ),
    4: (
        "Change the site logo",
        "Open Design, then Logo. Upload a PNG up to 2 MB and publish the change.",
    ),
    5: (
        "Turn on reviews",
        "Open Settings, then Reviews. Switch reviews on and choose who may post them.",
    ),
    6: (
        "Create a coupon",
        "Open Billing, then Coupons. Set the code, the discount and the end date.",
    ),
}
FAQ = {
    1: (
        "Can I use my own email address?",
        "Yes. Open Email settings and add your sender address, then verify it.",
    ),
    2: ("Is there a mobile app?", "There is no mobile app. The site works in any mobile browser."),
}
BASE = "http://fixture-site:8083"


def _page(title: str, body: str) -> str:
    return (
        f"<html><head><title>{title} | Fixture Help</title></head><body>"
        f'<nav class="breadcrumb"><a href="/">Home</a> <a href="/c/basics">Basics</a></nav>'
        f"<main><article><h1>{title}</h1><p>{body}</p><p>{body} This paragraph repeats the steps so the page "
        f"has enough text to count as an article for the parser, which wants real content.</p>"
        f'<img src="/img/{zlib.crc32(title.encode()) % 100}.png" alt="{title} screen"></article></main></body></html>'
    )


def _urlset(urls: list[str]) -> str:
    items = "".join(
        f"<url><loc>{BASE}{u}</loc><lastmod>2026-11-0{STATE['day']}</lastmod></url>" for u in urls
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> str:
    return "User-agent: *\nAllow: /\n"


@app.get("/sitemap_index.xml")
async def sitemap_index() -> Response:
    body = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{BASE}/sitemap-articles.xml</loc></sitemap>"
        f"<sitemap><loc>{BASE}/sitemap-faq.xml</loc></sitemap></sitemapindex>"
    )
    return Response(body, media_type="application/xml")


@app.get("/sitemap-articles.xml")
async def sitemap_articles() -> Response:
    day = STATE["day"]
    numbers = [n for n in TOPICS if not (n == 5 and day in (2, 3))]
    urls = [f"/articles/{n}" for n in numbers]
    if day >= 2:
        urls.append("/articles/4-new")
    return Response(_urlset(urls), media_type="application/xml")


@app.get("/sitemap-faq.xml")
async def sitemap_faq() -> Response:
    # /articles/1 is listed here too: a duplicate URL across two sitemaps
    return Response(_urlset(["/faq/1", "/faq/2", "/articles/1"]), media_type="application/xml")


@app.get("/articles/4-new", response_class=HTMLResponse)
async def moved_article() -> str:
    title, body = TOPICS[4]
    return _page(title, body + " (This page has a new address.)")


@app.get("/articles/{number}", response_class=HTMLResponse)
async def article(number: int) -> Response:
    day = STATE["day"]
    if number == 3 and day in (2, 3):
        raise HTTPException(404)
    if number == 4 and day >= 2:
        return RedirectResponse(f"{BASE}/articles/4-new", status_code=301)
    if number not in TOPICS:
        raise HTTPException(404)
    title, body = TOPICS[number]
    return HTMLResponse(_page(title, body))


@app.get("/faq/{number}", response_class=HTMLResponse)
async def faq(number: int) -> str:
    if number not in FAQ:
        raise HTTPException(404)
    return _page(*FAQ[number])


@app.get("/img/{name}")
async def image(name: str) -> Response:
    # A valid 1x1 PNG: enough for the screenshot pipeline to download and hash
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
    )
    return Response(png, media_type="image/png")


@app.post("/_control/day/{day}")
async def set_day(day: int) -> dict[str, int]:
    STATE["day"] = max(1, min(day, 4))
    return STATE
