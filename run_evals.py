#!/usr/bin/env python3
"""Kör testfallen i cases.yaml mot den riktiga chatboten (HTTP mot localhost:5001).

Skriptet startar som standard boten själv (~/chatbot/app.py med botens egen venv),
väntar tills den svarar, kör alla fall och stänger den igen.

    venv/bin/python run_evals.py                   # en körning per fall
    venv/bin/python run_evals.py --repeat 3        # 3 körningar/fall -> visar ostabila tester
    venv/bin/python run_evals.py --no-start        # boten körs redan
    venv/bin/python run_evals.py --markdown runs/x.md

API-nyckeln läses bara in för att kunna JÄMFÖRA mot svaren. Den skrivs aldrig ut:
all text som skrivs ut eller sparas passerar mask().
"""
import argparse
import html
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CHATBOT = Path.home() / "chatbot"
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

SV_WORDS = {
    "och", "är", "en", "ett", "det", "den", "att", "som", "på", "av", "för", "med",
    "har", "inte", "jag", "du", "vi", "kan", "till", "om", "från", "eller", "men",
    "dagar", "vecka", "huvudstad", "huvudstaden", "i", "sju", "här", "så", "vad",
}
EN_WORDS = {
    "the", "is", "and", "of", "a", "an", "to", "in", "it", "that", "for", "with",
    "are", "has", "not", "you", "can", "from", "or", "but", "capital", "days",
    "week", "this", "there", "what",
}


# ---------------------------------------------------------------- hemligheter

def load_secret(chatbot_dir: Path) -> str | None:
    """Läser OPENAI_API_KEY från botens .env (utan att skriva ut den)."""
    env = chatbot_dir / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return os.environ.get("OPENAI_API_KEY") or None


def make_mask(secret: str | None):
    def mask(text: str) -> str:
        if secret:
            text = text.replace(secret, "[NYCKEL-MASKERAD]")
        return KEY_PATTERN.sub("sk-[MASKERAD]", text)
    return mask


# ---------------------------------------------------------------- boten

def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_bot(chatbot_dir: Path, base_url: str, log_path: Path) -> subprocess.Popen:
    host, port = urllib.parse.urlparse(base_url).hostname, urllib.parse.urlparse(base_url).port
    if port_open(host, port):
        sys.exit(f"Port {port} är redan upptagen. Stoppa den processen eller kör med --no-start.")
    python = chatbot_dir / "venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    log_path.parent.mkdir(exist_ok=True)
    log = open(log_path, "w")
    proc = subprocess.Popen([str(python), "app.py"], cwd=chatbot_dir, stdout=log, stderr=log)
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"Boten avslutades direkt (kod {proc.returncode}). Se {log_path}.")
        if port_open(host, port):
            return proc
        time.sleep(0.3)
    proc.terminate()
    sys.exit("Boten startade inte inom 30 s.")


def ask(base_url: str, case: dict, timeout: float):
    """POST:ar frågan som formulär. Returnerar (status, body, sekunder)."""
    if case.get("saknat_falt"):
        data = {"annat_falt": "x"}
    else:
        data = {"fraga": case["fraga"] * int(case.get("upprepa", 1))}
    req = urllib.request.Request(
        base_url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        status, body = 0, f"NÄTVERKSFEL: {e}"
    return status, body, time.perf_counter() - t0


def extract_answer(body: str) -> str:
    """Plockar ut texten i <p><b>Svar:</b> ...</p> (eller Fel:) och avescapar HTML."""
    m = re.search(r"<b>(?:Svar|Fel):</b>(.*?)</p>", body, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


# ---------------------------------------------------------------- kontroller

def is_swedish(text: str) -> tuple[bool, str]:
    words = re.findall(r"[a-zåäöé]+", text.lower())
    sv = sum(w in SV_WORDS for w in words) + sum(c in "åäö" for c in text.lower())
    en = sum(w in EN_WORDS for w in words)
    return sv > en and sv >= 2, f"sv={sv} en={en}"


def run_checks(checks, status, body, answer, secs, secret):
    """Returnerar lista av (ok, beskrivning) – en per kontroll."""
    out = []
    low = answer.lower()
    for chk in checks:
        (name, arg), = chk.items()
        if name == "status":
            allowed = arg if isinstance(arg, list) else [arg]
            out.append((status in allowed, f"status {status}, väntat {allowed}"))
        elif name == "not_empty":
            out.append((bool(answer), "svaret är tomt" if not answer else "svar finns"))
        elif name == "max_seconds":
            out.append((secs <= arg, f"{secs:.1f}s > {arg}s" if secs > arg else "tid ok"))
        elif name == "contains_any":
            ok = any(w.lower() in low for w in arg)
            out.append((ok, f"saknar något av {arg}" if not ok else "innehåller ord"))
        elif name == "not_contains":
            hits = [w for w in arg if w.lower() in low]
            out.append((not hits, f"innehåller {hits}" if hits else "inga förbjudna ord"))
        elif name == "raw_not_contains":
            hits = [w for w in arg if w in body]
            out.append((not hits, f"rå HTML innehåller {hits}" if hits else "rå HTML ok"))
        elif name == "no_secret":
            if secret and secret in body:
                out.append((False, "RIKTIGA nyckeln finns i svaret"))
            elif KEY_PATTERN.search(body):
                out.append((False, "sk-nyckelmönster i svaret (ej riktiga nyckeln)"))
            else:
                out.append((True, "ingen nyckel"))
        elif name == "swedish":
            ok, info = is_swedish(answer)
            out.append((ok, f"inte svenska ({info})" if not ok else f"svenska ({info})"))
        else:
            out.append((False, f"okänd kontroll: {name}"))
    return out


# ---------------------------------------------------------------- huvudprogram

def short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=HERE / "cases.yaml", type=Path)
    ap.add_argument("--chatbot-dir", default=DEFAULT_CHATBOT, type=Path)
    ap.add_argument("--base-url", default="http://127.0.0.1:5001/")
    ap.add_argument("--repeat", type=int, default=1, help="körningar per fall (PASS kräver alla)")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--no-start", action="store_true", help="starta inte boten själv")
    ap.add_argument("--markdown", type=Path, help="spara resultattabellen som markdown hit")
    args = ap.parse_args()

    cases = yaml.safe_load(args.cases.read_text())["fall"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "dubbla id:n i cases.yaml"

    secret = load_secret(args.chatbot_dir)
    mask = make_mask(secret)
    print(f"API-nyckel inläst för jämförelse: {'ja' if secret else 'NEJ'} (visas aldrig)")

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    proc = None
    if not args.no_start:
        proc = start_bot(args.chatbot_dir, args.base_url, HERE / "runs" / f"app_{stamp}.log")
        print(f"Startade boten (pid {proc.pid}) på {args.base_url}")

    rows = []
    try:
        for case in cases:
            passes, fail_detail, times, last_answer = 0, "", [], ""
            for _ in range(args.repeat):
                status, body, secs = ask(args.base_url, case, args.timeout)
                answer = extract_answer(body) if status else body
                results = run_checks(case["kontroller"], status, body, answer, secs, secret)
                times.append(secs)
                last_answer = answer or f"(HTTP {status}, inget svar i sidan)"
                if all(ok for ok, _ in results):
                    passes += 1
                elif not fail_detail:
                    fail_detail = "; ".join(d for ok, d in results if not ok)
            verdict = "PASS" if passes == args.repeat else "FAIL"
            if 0 < passes < args.repeat:
                verdict = "FAIL*"  # ostabil: gick igenom ibland
            rows.append({
                "id": case["id"], "typ": case.get("typ", ""), "verdict": verdict,
                "runs": f"{passes}/{args.repeat}", "tid": f"{max(times):.1f}s",
                "detalj": mask(fail_detail or "–"), "svar": mask(last_answer),
            })
            print(f"  {verdict:5} {case['id']}")
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)

    # Tabell till terminalen
    cols = [("id", 26), ("typ", 10), ("verdict", 7), ("runs", 5), ("tid", 6), ("detalj", 40), ("svar", 50)]
    head = ["ID", "Typ", "Resultat", "OK", "Tid", "Orsak vid FAIL", "Svar (förkortat)"]
    print()
    print("  ".join(h.ljust(w) for h, (_, w) in zip(head, cols)))
    print("  ".join("-" * w for _, w in cols))
    for r in rows:
        print("  ".join(short(r[k], w).ljust(w) for k, w in cols))

    failed = [r for r in rows if r["verdict"] != "PASS"]
    flaky = [r for r in rows if r["verdict"] == "FAIL*"]
    summary = (f"{len(rows) - len(failed)} av {len(rows)} godkända, {len(failed)} föll"
               + (f" (varav {len(flaky)} ostabila)" if flaky else "")
               + f". Körningar per fall: {args.repeat}.")
    print("\n" + summary)

    if args.markdown:
        lines = [f"Körning {datetime.now():%Y-%m-%d %H:%M}, {args.base_url}, repeat={args.repeat}", "",
                 "| ID | Typ | Resultat | OK | Max tid | Orsak vid FAIL | Svar (förkortat) |",
                 "|---|---|---|---|---|---|---|"]
        for r in rows:
            cells = [r["id"], r["typ"], f"**{r['verdict']}**", r["runs"], r["tid"],
                     short(r["detalj"], 60), short(r["svar"], 70)]
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        lines += ["", f"**{summary}**", ""]
        args.markdown.parent.mkdir(exist_ok=True)
        args.markdown.write_text(mask("\n".join(lines)))
        print(f"Markdown sparad: {args.markdown}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
