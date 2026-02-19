#!/usr/bin/env python3
"""bluedash — static dashboard for bootc image build status."""

import base64
import os
import re
import sys
import yaml
import requests
from datetime import datetime, timezone
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
if GITHUB_TOKEN:
    session.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"


def gh_get(path, params=None):
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def relative_time(dt_str):
    if not dt_str:
        return "unknown"
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    diff = datetime.now(timezone.utc) - dt
    s = int(diff.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def short_digest(digest):
    if digest and digest.startswith("sha256:"):
        return "sha256:" + digest[7:19]
    return digest or ""


def is_generated_tag(tag):
    """Filter out auto-generated tags that clutter the display."""
    # Cosign / sigstore SHA-based tags
    if re.match(r"^sha256-[a-f0-9]{64}", tag):
        return True
    # Long numeric timestamps (e.g. 20240218060000)
    if re.match(r"^\d{14,}$", tag):
        return True
    return False


def recipe_to_image(filename):
    """'vauxite-base.yml' → 'vauxite-base'"""
    for ext in (".yml", ".yaml"):
        if filename.endswith(ext):
            return filename[: -len(ext)]
    return filename


def parse_job_recipe(job_name):
    """'Build image (vauxite-base.yml)' → 'vauxite-base.yml'"""
    m = re.search(r"\(([^)]+)\)", job_name)
    return m.group(1) if m else None


def get_recipe_image_name(org, repo, recipe_filename):
    """Fetch the recipe file and return the image name from its 'name:' field.
    Falls back to stripping the .yml extension if the fetch fails."""
    try:
        data = gh_get(f"/repos/{org}/{repo}/contents/recipes/{recipe_filename}")
        content = base64.b64decode(data["content"]).decode("utf-8")
        recipe = yaml.safe_load(content)
        if recipe and recipe.get("name"):
            return recipe["name"]
    except Exception as e:
        print(f"  Warning: could not read recipe {recipe_filename}: {e}", file=sys.stderr)
    return recipe_to_image(recipe_filename)


def get_latest_run(org, repo, workflow):
    data = gh_get(
        f"/repos/{org}/{repo}/actions/workflows/{workflow}/runs",
        params={"per_page": 1, "exclude_pull_requests": "true"},
    )
    runs = data.get("workflow_runs", [])
    return runs[0] if runs else None


def get_run_jobs(org, repo, run_id):
    data = gh_get(
        f"/repos/{org}/{repo}/actions/runs/{run_id}/jobs",
        params={"per_page": 100},
    )
    return data.get("jobs", [])


def get_package_versions(org, image):
    encoded = requests.utils.quote(image, safe="")
    try:
        return gh_get(
            f"/orgs/{org}/packages/container/{encoded}/versions",
            params={"per_page": 100},
        )
    except requests.HTTPError as e:
        print(f"  Warning: GHCR fetch failed for {org}/{image}: {e}", file=sys.stderr)
        return []


def collect_tags(versions, limit=20):
    """Collect meaningful tags from the most recent versions, preserving order."""
    tags = {}
    for v in versions[:limit]:
        for tag in v.get("metadata", {}).get("container", {}).get("tags", []):
            if tag not in tags and not is_generated_tag(tag):
                tags[tag] = {
                    "tag": tag,
                    "updated_at_relative": relative_time(v.get("updated_at")),
                    "digest": short_digest(v.get("name", "")),
                }
    return tags


def fetch_repo_data(cfg):
    org, repo, workflow = cfg["org"], cfg["repo"], cfg["workflow"]
    print(f"  {org}/{repo}")

    result = {
        "org": org,
        "repo": repo,
        "workflow": workflow,
        "run": None,
        "images": [],
    }

    try:
        run = get_latest_run(org, repo, workflow)
    except Exception as e:
        print(f"  Warning: failed to fetch run for {org}/{repo}: {e}", file=sys.stderr)
        return result

    if not run:
        return result

    result["run"] = {
        "id": run["id"],
        "status": run["status"],
        "conclusion": run["conclusion"],
        "created_at_relative": relative_time(run["created_at"]),
        "html_url": run["html_url"],
    }

    try:
        jobs = get_run_jobs(org, repo, run["id"])
    except Exception as e:
        print(f"  Warning: failed to fetch jobs: {e}", file=sys.stderr)
        jobs = []

    # Build per-image entries from matrix jobs
    images = {}
    for job in jobs:
        recipe_filename = parse_job_recipe(job["name"])
        if recipe_filename:
            image_name = get_recipe_image_name(org, repo, recipe_filename)
            images[image_name] = {
                "name": image_name,
                "conclusion": job["conclusion"],
                "status": job["status"],
                "html_url": job["html_url"],
                "tags": {},
            }

    # Enrich with GHCR tag data
    for image_name, image in images.items():
        versions = get_package_versions(org, image_name)
        image["tags"] = collect_tags(versions)

    result["images"] = sorted(images.values(), key=lambda x: x["name"])
    return result


def fetch_pinned_image_data(cfg):
    org, image = cfg["org"], cfg["image"]
    requested_tags = cfg.get("tags", [])
    print(f"  {org}/{image}")

    versions = get_package_versions(org, image)

    # Build a tag → version map (first occurrence wins — most recent)
    tag_map = {}
    for v in versions:
        for tag in v.get("metadata", {}).get("container", {}).get("tags", []):
            if tag not in tag_map:
                tag_map[tag] = v

    tags = []
    for tag in requested_tags:
        v = tag_map.get(tag)
        if v:
            tags.append({
                "tag": tag,
                "found": True,
                "updated_at_relative": relative_time(v.get("updated_at")),
                "digest": short_digest(v.get("name", "")),
                "html_url": v.get("html_url", ""),
            })
        else:
            tags.append({
                "tag": tag,
                "found": False,
                "updated_at_relative": "not found",
                "digest": None,
                "html_url": None,
            })

    return {"org": org, "image": image, "tags": tags}


def main():
    config_path = Path(__file__).parent / "config.yml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    print("Fetching tracked repo data...")
    repos = [fetch_repo_data(c) for c in config.get("repos", [])]

    print("Fetching pinned image data...")
    pinned = [fetch_pinned_image_data(c) for c in config.get("images", [])]

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("dashboard.html.j2")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = template.render(repos=repos, pinned_images=pinned, generated_at=generated_at)

    out = Path(__file__).parent / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    print(f"Generated → {out}")


if __name__ == "__main__":
    main()
