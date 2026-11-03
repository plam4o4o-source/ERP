# -*- coding: utf-8 -*-
"""Пази само последните N публикувани издания в GitHub и трие по-старите.

Заявка (03.11.2026): „Да се махнат старите резултати и да останат последните
3. GitHub предупреждава за хранилища над 5 GB.“ — всяко издание носи четири
файла по ~40 MB (PHLogistics.exe/-Setup.exe и копията със старите имена),
98 издания бяха 8 GB.

Вика се от release.yml след публикуването. Етикетите (git tag-овете) НЕ се
трият — не заемат място и пазят историята. Чернови и предварителни издания
не се пипат. Автоматичното обновяване в програмата чете само последното
издание (/releases/latest), затова триенето на старите не му влияе.

Употреба: python scripts/prune_releases.py --repo owner/name [--keep 3] [--dry-run]
Токенът е в GITHUB_TOKEN (или GH_TOKEN).
"""
import argparse
import json
import os
import sys
import urllib.request

API = "https://api.github.com"
#: Предпазител: никога по-малко от едно останало издание.
MIN_KEEP = 1


def _request(method, url, token):
    req = urllib.request.Request(url, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ph-logistics-release-prune",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 -- само https://api.github.com
        body = resp.read()
        return json.loads(body.decode("utf-8")) if body else None


def list_releases(repo, token, request=_request):
    releases, page = [], 1
    while True:
        batch = request("GET", "%s/repos/%s/releases?per_page=100&page=%d" % (API, repo, page), token)
        if not batch:
            return releases
        releases.extend(batch)
        page += 1


def plan(releases, keep):
    """(запазени, за триене): само публикувани, не-чернови, не-предварителни
    издания, подредени по дата на публикуване — най-новите keep остават."""
    keep = max(MIN_KEEP, int(keep))
    published = [r for r in releases
                 if not r.get("draft") and not r.get("prerelease") and r.get("published_at")]
    published.sort(key=lambda r: r["published_at"], reverse=True)
    return published[:keep], published[keep:]


def prune(repo, token, keep=3, dry_run=False, request=_request, out=sys.stdout):
    kept, drop = plan(list_releases(repo, token, request), keep)
    size = sum(a.get("size", 0) for r in drop for a in r.get("assets", []))
    print("Остават: %s" % ", ".join(r["tag_name"] for r in kept), file=out)
    print("За триене: %d издания, %.2f GB%s" % (len(drop), size / 1e9,
                                                " (само проверка)" if dry_run else ""), file=out)
    failed = 0
    for r in drop:
        if dry_run:
            continue
        try:
            request("DELETE", "%s/repos/%s/releases/%d" % (API, repo, r["id"]), token)
            print("  изтрито %s" % r["tag_name"], file=out)
        except Exception as exc:  # едно неуспешно не спира останалите
            failed += 1
            print("  НЕУСПЕХ %s: %s" % (r["tag_name"], exc), file=out)
    return len(drop) - failed if not dry_run else 0, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--keep", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("Няма GITHUB_TOKEN/GH_TOKEN.", file=sys.stderr)
        return 2
    _deleted, failed = prune(args.repo, token, args.keep, args.dry_run)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
