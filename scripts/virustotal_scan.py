# -*- coding: utf-8 -*-
"""Качва файловете на изданието във VirusTotal и пише кратък отчет (Markdown).

Ползва се от .github/workflows/release.yml след публикуването на изданието:

    python scripts/virustotal_scan.py --summary vt.md dist/PHLogistics.exe ...

API ключът идва от променливата VT_API_KEY (GitHub secret). Без ключ
скриптът само съобщава, че пропуска проверката, и завършва успешно.
Безплатният ключ позволява 4 заявки в минута, затова между заявките има пауза.
Отчетът никога не спира изданието — само показва резултата и линк към доклада.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "https://www.virustotal.com/api/v3"
GUI_FILE_URL = "https://www.virustotal.com/gui/file/%s"
#: Над този размер VirusTotal изисква отделен адрес за качване.
DIRECT_UPLOAD_LIMIT = 32 * 1024 * 1024
#: Безплатен ключ: 4 заявки в минута.
REQUEST_INTERVAL = 16.0
ANALYSIS_TIMEOUT = 20 * 60

_last_request = [0.0]


def _sleep(seconds):
    time.sleep(seconds)


def _request(method, url, api_key, body=None, content_type=None):
    """Една заявка към API-то с изчакване заради лимита. Връща (статус, JSON)."""
    wait = _last_request[0] + REQUEST_INTERVAL - time.monotonic()
    if _last_request[0] and wait > 0:
        _sleep(wait)
    _last_request[0] = time.monotonic()
    headers = {"x-apikey": api_key, "accept": "application/json"}
    if content_type:
        headers["content-type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310 -- фиксиран https адрес на VirusTotal
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def _multipart(path):
    boundary = "----ph%s" % uuid.uuid4().hex
    with open(path, "rb") as fh:
        data = fh.read()
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, os.path.basename(path))).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
    return head + data + tail, "multipart/form-data; boundary=%s" % boundary


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload(path, api_key):
    """Качва файла и връща ID на анализа."""
    url = API + "/files"
    if os.path.getsize(path) > DIRECT_UPLOAD_LIMIT:
        status, payload = _request("GET", API + "/files/upload_url", api_key)
        if status != 200 or not payload.get("data"):
            raise RuntimeError("VirusTotal не даде адрес за качване (HTTP %s)" % status)
        url = payload["data"]
    body, content_type = _multipart(path)
    status, payload = _request("POST", url, api_key, body=body, content_type=content_type)
    analysis_id = (payload.get("data") or {}).get("id")
    if status != 200 or not analysis_id:
        raise RuntimeError("качването във VirusTotal не успя (HTTP %s): %s"
                           % (status, (payload.get("error") or {}).get("message", "")))
    return analysis_id


def wait_for_analysis(analysis_id, api_key, timeout=None):
    """Статистиката на завършения анализ или None, ако не е завършил навреме."""
    deadline = time.monotonic() + (ANALYSIS_TIMEOUT if timeout is None else timeout)
    while True:
        status, payload = _request("GET", API + "/analyses/" + analysis_id, api_key)
        attrs = (payload.get("data") or {}).get("attributes") or {}
        if status == 200 and attrs.get("status") == "completed":
            return attrs.get("stats") or {}
        if time.monotonic() >= deadline:
            return None


def scan(path, api_key):
    """Резултат за един файл: {name, sha256, url, stats или None, error}."""
    sha = sha256_of(path)
    result = {"name": os.path.basename(path), "sha256": sha,
              "url": GUI_FILE_URL % sha, "stats": None, "error": None}
    try:
        result["stats"] = wait_for_analysis(upload(path, api_key), api_key)
    except Exception as exc:  # мрежа/API — отчитаме, без да спираме изданието
        result["error"] = str(exc)
    return result


def summary_markdown(results):
    lines = [SECTION_TITLE, "",
             "| Файл | Резултат | Доклад |", "|---|---|---|"]
    for r in results:
        stats = r["stats"]
        if r["error"]:
            verdict = "⚠ проверката не завърши: %s" % r["error"]
        elif stats is None:
            verdict = "⏳ анализът още тече — вижте доклада"
        else:
            flagged = int(stats.get("malicious", 0)) + int(stats.get("suspicious", 0))
            engines = sum(int(stats.get(k, 0)) for k in
                          ("malicious", "suspicious", "undetected", "harmless"))
            verdict = ("✅ 0 / %d антивирусни програми" % engines if not flagged else
                       "⚠ %d / %d антивирусни програми отбелязват файла — вижте доклада"
                       % (flagged, engines))
        lines.append("| %s | %s | [доклад](%s) |" % (r["name"], verdict, r["url"]))
    lines += ["", "SHA-256 на файловете е в SHA256SUMS.txt към изданието."]
    return "\n".join(lines) + "\n"


SECTION_TITLE = "### 🛡 Проверка с VirusTotal"


def merge_into_body(body, summary):
    """Описанието на изданието с нов отчет; предишен отчет (при повторна
    проверка) се заменя, вместо да се трупат няколко."""
    body = body or ""
    cut = body.find(SECTION_TITLE)
    if cut != -1:
        body = body[:cut]
    return body.rstrip() + "\n\n" + summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--summary", required=True, help="къде да се запише отчетът (Markdown)")
    parser.add_argument("--body-file", help="описание на изданието, към което да се добави отчетът")
    args = parser.parse_args(argv)
    api_key = (os.environ.get("VT_API_KEY") or "").strip()
    if not api_key:
        print("::notice::VT_API_KEY не е зададен — проверката с VirusTotal е пропусната.")
        return 0
    results = [scan(path, api_key) for path in args.files]
    text = summary_markdown(results)
    with open(args.summary, "w", encoding="utf-8") as fh:
        fh.write(text)
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as fh:
            body = fh.read()
        with open(args.body_file, "w", encoding="utf-8") as fh:
            fh.write(merge_into_body(body, text))
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
