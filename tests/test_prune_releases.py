# -*- coding: utf-8 -*-
"""scripts/prune_releases.py — пазят се последните 3 издания (заявка 03.11.2026)."""
import io
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import prune_releases  # noqa: E402

from conftest import read_source  # noqa: E402


def _rel(i, tag, day, draft=False, pre=False, size=40_000_000):
    return {"id": i, "tag_name": tag, "draft": draft, "prerelease": pre,
            "published_at": None if draft else "2026-10-%02dT10:00:00Z" % day,
            "assets": [{"size": size}] * 4}


def _fake(releases):
    calls = []

    def request(method, url, token):
        calls.append((method, url))
        if method == "GET":
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return releases[(page - 1) * 100: page * 100]
        return None
    return request, calls


def test_keeps_the_three_newest_and_deletes_the_rest():
    rels = [_rel(i, "v3.%d" % i, i) for i in range(1, 11)]
    request, calls = _fake(rels)
    out = io.StringIO()
    deleted, failed = prune_releases.prune("o/r", "t", 3, request=request, out=out)
    assert (deleted, failed) == (7, 0)
    gone = sorted(int(u.rsplit("/", 1)[1]) for m, u in calls if m == "DELETE")
    assert gone == [1, 2, 3, 4, 5, 6, 7]
    assert "Остават: v3.10, v3.9, v3.8" in out.getvalue()


def test_order_is_by_publish_date_not_list_order():
    rels = [_rel(5, "v5", 25), _rel(1, "v1", 1), _rel(9, "v9", 29), _rel(3, "v3", 3)]
    kept, drop = prune_releases.plan(rels, 2)
    assert [r["tag_name"] for r in kept] == ["v9", "v5"]
    assert {r["tag_name"] for r in drop} == {"v1", "v3"}


def test_drafts_and_prereleases_are_never_touched():
    rels = [_rel(1, "v1", 1), _rel(2, "draft", 2, draft=True), _rel(3, "rc", 3, pre=True),
            _rel(4, "v4", 4)]
    kept, drop = prune_releases.plan(rels, 1)
    assert [r["tag_name"] for r in kept] == ["v4"] and [r["tag_name"] for r in drop] == ["v1"]


def test_at_least_one_release_always_stays():
    kept, drop = prune_releases.plan([_rel(1, "v1", 1), _rel(2, "v2", 2)], 0)
    assert [r["tag_name"] for r in kept] == ["v2"]


def test_dry_run_deletes_nothing_and_paging_reads_everything():
    rels = [_rel(i, "v%d" % i, 1 + i % 28) for i in range(1, 151)]  # 2 страници
    request, calls = _fake(rels)
    prune_releases.prune("o/r", "t", 3, dry_run=True, request=request, out=io.StringIO())
    assert not [c for c in calls if c[0] == "DELETE"]
    assert len([c for c in calls if c[0] == "GET"]) == 3  # 100 + 50 + празна


def test_one_failed_delete_does_not_stop_the_others():
    rels = [_rel(i, "v%d" % i, i) for i in range(1, 6)]
    request, calls = _fake(rels)

    def flaky(method, url, token):
        if method == "DELETE" and url.endswith("/1"):
            raise OSError("boom")
        return request(method, url, token)
    deleted, failed = prune_releases.prune("o/r", "t", 3, request=flaky, out=io.StringIO())
    assert (deleted, failed) == (1, 1)


def test_release_workflow_prunes_after_publishing_and_also_on_manual_run():
    yml = read_source(".github", "workflows", "release.yml")
    step = yml[yml.index("name: Keep only the last 3 releases"):]
    assert "scripts/prune_releases.py" in step and "--keep 3" in step
    # Без условието „изданието вече съществува“ — ръчното пускане само почиства.
    head = step.split("run:", 1)[0]
    assert "steps.exists.outputs.exists" not in head
    assert yml.index("name: Publish release") < yml.index("name: Keep only the last 3 releases")
