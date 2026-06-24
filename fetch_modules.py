"""
Fetch module repositories from Xposed-Modules-Repo and publish the static API.

The output mirrors the legacy Gatsby release layout:
  public/modules.json
  public/module/<package>.json

For compatibility with the existing repository workflow, a copy of the module
list is also written to ./modules.json.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import markdown
import requests

ORG = "Xposed-Modules-Repo"
ROOT_MODULES_JSON = Path("modules.json")
PUBLIC_DIR = Path("public")
PUBLIC_MODULES_JSON = PUBLIC_DIR / "modules.json"
PUBLIC_MODULE_DIR = PUBLIC_DIR / "module"
PER_PAGE = 100
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.15"))
API_ROOT = "https://api.github.com"
APK_CONTENT_TYPE = "application/vnd.android.package-archive"

TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def check_rate_limit(resp: requests.Response) -> None:
    """Sleep until rate limit resets if GitHub says the bucket is empty."""
    remaining = int(resp.headers.get("X-RateLimit-Remaining", 1))
    if remaining == 0:
        reset_at = int(resp.headers.get("X-RateLimit-Reset", 0))
        wait = max(reset_at - time.time(), 0) + 1
        print(f"Rate limit exhausted. Waiting {wait:.0f}s...")
        time.sleep(wait)


def github_get(url: str, **params: Any) -> Any:
    while True:
        resp = requests.get(url, headers=HEADERS, params=params or None, timeout=60)
        check_rate_limit(resp)
        if resp.status_code in (403, 429):
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = int(retry_after) + 1
                print(f"GitHub asked us to retry after {wait}s: {url}")
                time.sleep(wait)
                continue
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            print(f"ERROR {resp.status_code} for {url}: {resp.text[:300]}")
            resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        return resp.json()


def fetch_repos() -> list[dict[str, Any]]:
    """Paginate through all public repos in ORG."""
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        print(f"Fetching repository page {page}...")
        data = github_get(
            f"{API_ROOT}/orgs/{ORG}/repos",
            per_page=PER_PAGE,
            page=page,
            sort="updated",
            type="public",
        )
        if not data:
            break
        repos.extend(data)
        if len(data) < PER_PAGE:
            break
        page += 1
    print(f"Total repos fetched: {len(repos)}")
    return repos


def fetch_file_text(repo_name: str, filename: str) -> str:
    data = github_get(f"{API_ROOT}/repos/{ORG}/{repo_name}/contents/{filename}")
    if not data or data.get("type") != "file":
        return ""
    encoded = data.get("content", "")
    if not encoded:
        return ""
    return base64.b64decode(encoded).decode("utf-8", errors="replace")


def fetch_collaborators(repo_name: str) -> list[dict[str, str | None]]:
    url = f"{API_ROOT}/repos/{ORG}/{repo_name}/collaborators"
    resp = requests.get(
        url,
        headers=HEADERS,
        params={"affiliation": "direct", "per_page": 100},
        timeout=60,
    )
    check_rate_limit(resp)
    if resp.status_code in (401, 403, 404):
        print(f"  Collaborators unavailable for {repo_name}: HTTP {resp.status_code}")
        time.sleep(REQUEST_DELAY)
        return []
    if resp.status_code >= 400:
        print(f"ERROR {resp.status_code} for {url}: {resp.text[:300]}")
        resp.raise_for_status()
    time.sleep(REQUEST_DELAY)
    data = resp.json()
    if not data:
        return []
    return [{"login": item.get("login"), "name": item.get("name")} for item in data]


def fetch_releases(repo_name: str) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    while True:
        data = github_get(
            f"{API_ROOT}/repos/{ORG}/{repo_name}/releases",
            per_page=100,
            page=page,
        )
        if not data:
            break
        releases.extend(data)
        if len(data) < 100:
            break
        page += 1
    return releases


def extract_summary(markdown_text: str) -> str:
    """Extract the first meaningful paragraph from README as a fallback summary."""
    if not markdown_text:
        return ""
    text = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.DOTALL).lstrip()
    if text.startswith("---"):
        idx = text.find("---", 3)
        if idx != -1:
            text = text[idx + 3 :].lstrip()

    lines: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue
        if stripped.startswith(("#", ">", "|", "[!")):
            continue
        if re.match(r"^[!<\[]", stripped):
            continue

        cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", stripped)
        cleaned = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", cleaned)
        cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()
        if cleaned and len(cleaned) > 10:
            lines.append(cleaned)
        if len(lines) >= 3:
            break

    summary = " ".join(lines)
    return summary[:509].rstrip() + "..." if len(summary) > 512 else summary


def convert_to_html(markdown_text: str) -> str:
    if not markdown_text:
        return ""
    return markdown.markdown(
        markdown_text,
        extensions=["extra", "codehilite", "toc", "sane_lists", "smarty"],
        output_format="html5",
    )


def parse_json_file(text: str) -> Any:
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def trim_single_line(text: str) -> str:
    return text.replace("\r", "").replace("\n", "").strip()


def release_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": asset.get("name"),
        "contentType": asset.get("content_type"),
        "downloadUrl": asset.get("browser_download_url"),
        "downloadCount": asset.get("download_count"),
        "size": asset.get("size"),
    }


def release_object(release: dict[str, Any]) -> dict[str, Any]:
    body = release.get("body") or ""
    return {
        "name": release.get("name") or release.get("tag_name"),
        "url": release.get("html_url"),
        "isDraft": release.get("draft", False),
        "descriptionHTML": convert_to_html(body),
        "createdAt": release.get("created_at"),
        "publishedAt": release.get("published_at"),
        "updatedAt": release.get("updated_at"),
        "tagName": release.get("tag_name"),
        "isPrerelease": release.get("prerelease", False),
        "releaseAssets": [release_asset(asset) for asset in release.get("assets", [])],
    }


def has_apk_asset(release: dict[str, Any]) -> bool:
    for asset in release.get("assets", []):
        content_type = asset.get("content_type")
        name = asset.get("name") or ""
        if content_type == APK_CONTENT_TYPE or name.lower().endswith(".apk"):
            return True
    return False


def is_valid_release(release: dict[str, Any]) -> bool:
    tag_name = release.get("tag_name") or ""
    return (
        not release.get("draft", False)
        and re.match(r"^\d+-.+$", tag_name) is not None
        and has_apk_asset(release)
    )


def select_latest_releases(releases: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    stable = next((release for release in releases if not release["isPrerelease"]), None)
    beta = next(
        (
            release
            for release in releases
            if release["isPrerelease"] and not re.match(r"^(snapshot|nightly).*", release["name"] or "", flags=re.I)
        ),
        stable,
    )
    snapshot = next(
        (
            release
            for release in releases
            if release["isPrerelease"] and re.match(r"^(snapshot|nightly).*", release["name"] or "", flags=re.I)
        ),
        beta,
    )
    return stable, beta, snapshot


def should_skip_repo(repo: dict[str, Any]) -> bool:
    name = repo.get("name") or ""
    return name in {"org.meowcat.example", ".github"} or "." not in name or not repo.get("description")


def build_module(repo: dict[str, Any], index: int, total: int) -> dict[str, Any] | None:
    name = repo["name"]
    print(f"[{index}/{total}] Processing {name}...")
    if should_skip_repo(repo):
        print(f"  Skipped: not a module-shaped repository.")
        return None

    readme = fetch_file_text(name, "README.md")
    summary_file = fetch_file_text(name, "SUMMARY").strip()
    scope = parse_json_file(fetch_file_text(name, "SCOPE"))
    source_url = trim_single_line(fetch_file_text(name, "SOURCE_URL"))
    additional_authors = parse_json_file(fetch_file_text(name, "ADDITIONAL_AUTHORS"))
    hide = bool(fetch_file_text(name, "HIDE"))
    collaborators = fetch_collaborators(name)
    raw_releases = [release for release in fetch_releases(name) if is_valid_release(release)]

    if not raw_releases:
        print("  Skipped: no published APK release with a valid tag.")
        return None

    releases = [release_object(release) for release in raw_releases]
    latest_release, latest_beta_release, latest_snapshot_release = select_latest_releases(releases)
    if latest_release:
        latest_release["isLatest"] = True
    if latest_beta_release:
        latest_beta_release["isLatestBeta"] = True
    if latest_snapshot_release:
        latest_snapshot_release["isLatestSnapshot"] = True

    module = {
        "name": name,
        "description": repo.get("description") or "",
        "url": repo.get("html_url"),
        "homepageUrl": repo.get("homepage") or "",
        "collaborators": collaborators,
        "latestRelease": latest_release,
        "latestBetaRelease": latest_beta_release,
        "latestSnapshotRelease": latest_snapshot_release,
        "latestReleaseTime": latest_release["publishedAt"] if latest_release else "1970-01-01T00:00:00Z",
        "latestBetaReleaseTime": latest_beta_release["publishedAt"] if latest_beta_release else "1970-01-01T00:00:00Z",
        "latestSnapshotReleaseTime": latest_snapshot_release["publishedAt"] if latest_snapshot_release else "1970-01-01T00:00:00Z",
        "releases": releases,
        "readme": readme,
        "readmeHTML": convert_to_html(readme),
        "summary": summary_file[:512] if summary_file else extract_summary(readme),
        "scope": scope,
        "sourceUrl": source_url or None,
        "hide": hide,
        "additionalAuthors": additional_authors if isinstance(additional_authors, list) else None,
        "updatedAt": repo.get("updated_at"),
        "createdAt": repo.get("created_at"),
        "stargazerCount": repo.get("stargazers_count"),
    }
    print(f"  Included: {len(releases)} release(s), hidden={hide}.")
    return module


def public_list_module(module: dict[str, Any]) -> dict[str, Any]:
    latest_release = module.get("latestRelease")
    latest_beta_release = module.get("latestBetaRelease")
    latest_snapshot_release = module.get("latestSnapshotRelease")

    item = dict(module)
    item["latestRelease"] = latest_release.get("tagName") if latest_release else None
    item["latestBetaRelease"] = (
        latest_beta_release.get("tagName")
        if latest_beta_release and item["latestRelease"] != latest_beta_release.get("tagName")
        else None
    )
    item["latestSnapshotRelease"] = (
        latest_snapshot_release.get("tagName")
        if latest_snapshot_release
        and item["latestRelease"] != latest_snapshot_release.get("tagName")
        and item["latestBetaRelease"] != latest_snapshot_release.get("tagName")
        else None
    )
    item["releases"] = [latest_release] if latest_release else []
    if item["latestBetaRelease"]:
        item["betaReleases"] = [latest_beta_release]
    if item["latestSnapshotRelease"]:
        item["snapshotReleases"] = [latest_snapshot_release]
    item.pop("readme", None)
    return item


def write_outputs(modules: list[dict[str, Any]]) -> None:
    visible_modules = [module for module in modules if not module.get("hide")]

    if PUBLIC_DIR.exists():
        shutil.rmtree(PUBLIC_DIR)
    PUBLIC_MODULE_DIR.mkdir(parents=True, exist_ok=True)

    public_modules: list[dict[str, Any]] = []
    for module in visible_modules:
        detail = dict(module)
        detail["latestRelease"] = detail["latestRelease"]["tagName"] if detail.get("latestRelease") else None
        detail["latestBetaRelease"] = (
            detail["latestBetaRelease"]["tagName"]
            if detail.get("latestBetaRelease") and detail["latestRelease"] != detail["latestBetaRelease"]["tagName"]
            else None
        )
        detail["latestSnapshotRelease"] = (
            detail["latestSnapshotRelease"]["tagName"]
            if detail.get("latestSnapshotRelease")
            and detail["latestRelease"] != detail["latestSnapshotRelease"]["tagName"]
            and detail["latestBetaRelease"] != detail["latestSnapshotRelease"]["tagName"]
            else None
        )
        (PUBLIC_MODULE_DIR / f"{module['name']}.json").write_text(
            json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        public_modules.append(public_list_module(module))

    payload = json.dumps(public_modules, ensure_ascii=False, separators=(",", ":"))
    PUBLIC_MODULES_JSON.write_text(payload, encoding="utf-8")
    ROOT_MODULES_JSON.write_text(json.dumps(public_modules, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(public_modules)} visible modules to {PUBLIC_MODULES_JSON}")
    print(f"Wrote {len(public_modules)} module detail files to {PUBLIC_MODULE_DIR}")
    print(f"Wrote compatibility copy to {ROOT_MODULES_JSON}")


def main() -> None:
    repos = fetch_repos()
    if not repos:
        print("No repos found, aborting.")
        sys.exit(1)

    modules: list[dict[str, Any]] = []
    for index, repo in enumerate(repos, 1):
        module = build_module(repo, index, len(repos))
        if module:
            modules.append(module)

    write_outputs(modules)


if __name__ == "__main__":
    main()
