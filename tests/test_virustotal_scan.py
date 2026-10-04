# -*- coding: utf-8 -*-
"""scripts/virustotal_scan.py — проверка на изданието с VirusTotal (без мрежа)."""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def vt(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "virustotal_scan", os.path.join(ROOT, "scripts", "virustotal_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    return mod


class FakeApi:
    """Записва заявките и отговаря по ред от зададените отговори."""

    def __init__(self, analyses):
        self.calls = []
        self.analyses = list(analyses)

    def __call__(self, method, url, api_key, body=None, content_type=None):
        self.calls.append((method, url, content_type))
        if url.endswith("/files/upload_url"):
            return 200, {"data": "https://upload.example/big"}
        if method == "POST":
            return 200, {"data": {"type": "analysis", "id": "an-1"}}
        return self.analyses.pop(0)


def _completed(**stats):
    return 200, {"data": {"attributes": {"status": "completed", "stats": stats}}}


QUEUED = (200, {"data": {"attributes": {"status": "queued"}}})


def _exe(tmp_path, name="PachoLogistic.exe", data=b"MZ fake exe"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_small_file_uploaded_directly_and_polled_until_completed(vt, tmp_path, monkeypatch):
    api = FakeApi([QUEUED, QUEUED, _completed(malicious=0, undetected=70, harmless=0)])
    monkeypatch.setattr(vt, "_request", api)
    result = vt.scan(_exe(tmp_path), "key")
    assert result["error"] is None
    assert result["stats"]["undetected"] == 70
    assert api.calls[0][:2] == ("POST", vt.API + "/files")
    assert api.calls[0][2].startswith("multipart/form-data; boundary=")
    assert [c[1] for c in api.calls[1:]] == [vt.API + "/analyses/an-1"] * 3
    assert result["url"] == vt.GUI_FILE_URL % vt.sha256_of(_exe(tmp_path))


def test_large_file_uses_upload_url(vt, tmp_path, monkeypatch):
    api = FakeApi([_completed(undetected=1)])
    monkeypatch.setattr(vt, "_request", api)
    monkeypatch.setattr(vt, "DIRECT_UPLOAD_LIMIT", 4)
    vt.scan(_exe(tmp_path), "key")
    assert api.calls[0][:2] == ("GET", vt.API + "/files/upload_url")
    assert api.calls[1][:2] == ("POST", "https://upload.example/big")


def test_analysis_timeout_gives_pending_row(vt, tmp_path, monkeypatch):
    api = FakeApi([QUEUED])
    monkeypatch.setattr(vt, "_request", api)
    monkeypatch.setattr(vt, "ANALYSIS_TIMEOUT", 0)
    result = vt.scan(_exe(tmp_path), "key")
    assert result["stats"] is None and result["error"] is None
    assert "⏳ анализът още тече" in vt.summary_markdown([result])


def test_upload_error_is_reported_not_raised(vt, tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "_request", lambda *a, **k: (401, {"error": {"message": "Wrong key"}}))
    result = vt.scan(_exe(tmp_path), "bad")
    assert "HTTP 401" in result["error"] and "Wrong key" in result["error"]
    assert "⚠ проверката не завърши" in vt.summary_markdown([result])


def test_summary_counts_engines_and_flags(vt):
    clean = {"name": "a.exe", "url": "u1", "error": None,
             "stats": {"malicious": 0, "suspicious": 0, "undetected": 68, "harmless": 2,
                       "type-unsupported": 5}}
    flagged = {"name": "b.exe", "url": "u2", "error": None,
               "stats": {"malicious": 2, "suspicious": 1, "undetected": 67, "harmless": 0}}
    text = vt.summary_markdown([clean, flagged])
    assert text.startswith(vt.SECTION_TITLE)
    assert "| a.exe | ✅ 0 / 70 антивирусни програми | [доклад](u1) |" in text
    assert "⚠ 3 / 70 антивирусни програми отбелязват файла" in text


def test_merge_replaces_previous_section(vt):
    body = "## Промени\n- нещо\n"
    once = vt.merge_into_body(body, vt.SECTION_TITLE + "\nстар\n")
    twice = vt.merge_into_body(once, vt.SECTION_TITLE + "\nнов\n")
    assert twice == "## Промени\n- нещо\n\n" + vt.SECTION_TITLE + "\nнов\n"
    assert twice.count(vt.SECTION_TITLE) == 1


def test_without_api_key_skips_and_succeeds(vt, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VT_API_KEY", raising=False)
    monkeypatch.setattr(vt, "_request", lambda *a, **k: pytest.fail("няма заявки без ключ"))
    summary = tmp_path / "vt.md"
    assert vt.main([_exe(tmp_path), "--summary", str(summary)]) == 0
    assert not summary.exists()
    assert "VT_API_KEY" in capsys.readouterr().out


def test_main_writes_summary_body_and_step_summary(vt, tmp_path, monkeypatch):
    monkeypatch.setenv("VT_API_KEY", "key")
    step = tmp_path / "step.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step))
    monkeypatch.setattr(vt, "_request", FakeApi([_completed(undetected=70)] * 2))
    body = tmp_path / "body.md"
    body.write_text("Бележки към изданието\n", encoding="utf-8")
    summary = tmp_path / "vt.md"
    files = [_exe(tmp_path), _exe(tmp_path, "PachoLogistic-Setup.exe", b"MZ setup")]
    assert vt.main(files + ["--summary", str(summary), "--body-file", str(body)]) == 0
    text = summary.read_text(encoding="utf-8")
    assert "PachoLogistic.exe" in text and "PachoLogistic-Setup.exe" in text
    assert body.read_text(encoding="utf-8").startswith("Бележки към изданието\n\n" + vt.SECTION_TITLE)
    assert step.read_text(encoding="utf-8") == text


def test_request_spacing_respects_free_key_limit(vt, monkeypatch):
    slept = []
    monkeypatch.setattr(vt, "_sleep", slept.append)

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(vt.urllib.request, "urlopen", lambda req, timeout: Resp())
    vt._request("GET", vt.API + "/x", "key")
    vt._request("GET", vt.API + "/x", "key")
    assert len(slept) == 1 and slept[0] > vt.REQUEST_INTERVAL - 1


def _load(name):
    # PyYAML идва в CI покрай bandit, но не и в билда на изданието.
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_release_workflow_scans_after_publish_without_blocking():
    steps = _load("release.yml")["jobs"]
    steps = next(iter(steps.values()))["steps"]
    names = [s.get("name") for s in steps]
    vt_step = steps[names.index("VirusTotal scan")]
    assert names.index("VirusTotal scan") > names.index("Publish release")
    assert vt_step["continue-on-error"] is True
    assert vt_step["env"]["VT_API_KEY"] == "${{ secrets.VT_API_KEY }}"
    assert "scripts/virustotal_scan.py" in vt_step["run"]


def test_manual_virustotal_workflow():
    wf = _load("virustotal.yml")
    # PyYAML чете ключа „on“ като True.
    trigger = wf.get("on", wf.get(True))
    assert "workflow_dispatch" in trigger
    assert wf["permissions"]["contents"] == "write"
    run = "\n".join(s.get("run", "") for s in next(iter(wf["jobs"].values()))["steps"])
    assert "scripts/virustotal_scan.py" in run and "gh release edit" in run
