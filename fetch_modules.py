"""
Fetch all module repos from Xposed-Modules-Repo org and produce modules.json.
Fields: pkg, title (about), desc (summary), url, update, readmd (HTML).
"""
import json
import os
import base64
import time
import re
import sys

import requests
import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.tables import TableExtension
from markdown.extensions.fenced_code import FencedCodeExtension

ORG = "Xposed-Modules-Repo"
OUTPUT = "modules.json"
PER_PAGE = 100
REQUEST_DELAY = 0.15  # between API calls

# Build auth headers from GITHUB_TOKEN (set by Actions)
TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def check_rate_limit(resp):
    """Sleep until rate limit resets if exhausted."""
    remaining = int(resp.headers.get("X-RateLimit-Remaining", 1))
    if remaining == 0:
        reset_at = int(resp.headers.get("X-RateLimit-Reset", 0))
        wait = max(reset_at - time.time(), 0) + 1
        print(f"Rate limit exhausted. Waiting {wait:.0f}s...")
        time.sleep(wait)


def fetch_repos():
    """Paginate through all public repos in ORG."""
    repos = []
    page = 1
    while True:
        url = (f"https://api.github.com/orgs/{ORG}/repos"
               f"?per_page={PER_PAGE}&page={page}&sort=updated&type=public")
        print(f"Fetching page {page}...")
        resp = requests.get(url, headers=HEADERS)
        check_rate_limit(resp)
        if resp.status_code != 200:
            print(f"  ERROR {resp.status_code}: {resp.text[:200]}")
            break
        data = resp.json()
        if not data:
            break
        repos.extend(data)
        if len(data) < PER_PAGE:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    print(f"Total repos fetched: {len(repos)}")
    return repos


def fetch_readme(name):
    """Fetch README.md base64 content and convert to HTML."""
    url = f"https://api.github.com/repos/{ORG}/{name}/readme"
    resp = requests.get(url, headers=HEADERS)
    check_rate_limit(resp)
    time.sleep(REQUEST_DELAY)
    if resp.status_code == 200:
        data = resp.json()
        raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return raw
    return ""


def extract_summary(markdown_text):
    """Extract the first meaningful paragraph from markdown as summary."""
    if not markdown_text:
        return ""
    # Remove HTML comments and front-matter
    text = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.DOTALL)
    text = text.lstrip()
    if text.startswith("---"):
        idx = text.find("---", 3)
        if idx != -1:
            text = text[idx + 3:].lstrip()
    # Remove headings
    lines = []
    in_code_block = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped.startswith("#") or stripped.startswith(">") or stripped.startswith("|"):
            continue
        if stripped.startswith("[!"):
            continue
        if not stripped:
            continue
        # Skip image-only lines and badges
        if re.match(r"^[!<\[]", stripped):
            continue
        # Clean inline links / images
        cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", stripped)
        cleaned = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", cleaned)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = cleaned.strip()
        if cleaned and len(cleaned) > 10:
            lines.append(cleaned)
        if len(lines) >= 3:
            break
    summary = " ".join(lines)
    if len(summary) > 500:
        summary = summary[:497] + "..."
    return summary


def convert_to_html(markdown_text):
    """Convert markdown to HTML with extensions."""
    if not markdown_text:
        return ""
    return markdown.markdown(
        markdown_text,
        extensions=[
            "extra",
            "codehilite",
            "toc",
            "sane_lists",
            "smarty",
        ],
        output_format="html5",
    )


def build_modules(repos):
    """Iterate repos and build the module list."""
    modules = []
    total = len(repos)
    for i, repo in enumerate(repos, 1):
        name = repo["name"]
        print(f"[{i}/{total}] Processing {name}...")
        readme_md = fetch_readme(name)
        readme_html = convert_to_html(readme_md)
        summary = extract_summary(readme_md)
        # Use repo description as title (about); summary from README as desc
        title = repo.get("description") or ""
        desc = summary if summary else title
        modules.append({
            "pkg": name,
            "title": title,
            "desc": desc,
            "url": repo["html_url"],
            "update": repo["pushed_at"],
            "readmd": readme_html,
        })
    return modules


def main():
    repos = fetch_repos()
    if not repos:
        print("No repos found, aborting.")
        sys.exit(1)
    modules = build_modules(repos)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(modules, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(modules)} modules to {OUTPUT}")


if __name__ == "__main__":
    main()
