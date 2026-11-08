"""Run a QA benchmark against search APIs and measure accuracy and latency.

Datasets (--dataset):
  simpleqa     SimpleQA Verified, 1,000 questions (default)
  verticalrtk  Tako VerticalRTK fast set, commit f3f8628. The 17 rows with a
               capture timestamp or a forecast are dropped (their answers drift
               in hours), so 138 rows remain.
  freshqa      FreshQA, April 21, 2026 version, TEST split. Fast-changing rows
               and rows past their next_review date are dropped (the gold
               answers are older than the run), so 358 rows remain.

Setup, the same for every provider: one search call per question (the
question text is the query, unchanged), 10 results, the provider's default
search mode. A reader LLM answers from the results only. The OpenAI
simple-evals grader prompt grades each answer.

When you give more than one provider, the calls are interleaved: each
question goes to every provider before the next question starts. Thus time
of day and network changes affect all providers equally.

Providers and keys:
  cloro     CLORO_API_KEY     Google results + AI Overview
  cloro-noaio CLORO_API_KEY   Google results only (no AI Overview requested or read)
  serpapi   SERPAPI_KEY       Google results + AI Overview (follows page_token)
  tavily    TAVILY_API_KEY    search_depth=basic
  exa       EXA_API_KEY       type=auto, highlights
  parallel  PARALLEL_API_KEY  default mode
  brave     BRAVE_API_KEY     extra_snippets=true

Reader and grader: OPENAI_API_KEY, READER_MODEL, GRADER_MODEL.
With --search-only, no LLM is called: the run measures latency only.

Usage (from the repo root):
  python3 benchmark.py --providers cloro,serpapi [--n 1000] [--workers 4] [--search-only]
  python3 benchmark.py --summary --out <run folder>

Output, in outputs/search-benchmark-<dataset>-<date>/ (or --out):
results/<provider>.jsonl (one line per question; with --search-only,
results/<provider>.search-only.jsonl), raw/<provider>/<id>.json (the raw
search response, for the leak review) and data/simpleqa_verified.csv.
A re-run with the same --out skips (provider, question) pairs that are done.
"""
import argparse
import concurrent.futures as cf
import csv
import glob
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE
OUT = None  # outputs/search-benchmark-<dataset>-<date>, or --out
DATASET_URLS = {
    "simpleqa": "https://huggingface.co/datasets/google/simpleqa-verified/resolve/main/simpleqa_verified.csv",
    "verticalrtk": "https://raw.githubusercontent.com/TakoData/VerticalRTK/f3f8628/datasets/fast/verticalrtk_fast.jsonl",
    "freshqa": "https://docs.google.com/spreadsheets/d/1_8mi-yuK30mvoDJu1KQXD6ODem7MKMcIgVAwDSzJkjM/export?format=csv",
}
GRADER_TEMPLATE = open(os.path.join(HERE, "grader_template.txt")).read().strip()
READER_MODEL = os.environ.get("READER_MODEL", "gpt-5.6-luna")
GRADER_MODEL = os.environ.get("GRADER_MODEL", "gpt-5.6-sol")
NUM_RESULTS = 10
MAX_CONTEXT_CHARS = 30000  # same cap for every provider
TIMEOUT_S = 120

READER_PROMPT = """Answer the question using only the search results below.
Give only the short answer (a name, number, date or phrase), with no explanation.
If the results do not contain the answer, reply exactly: NOT FOUND

Question: {question}

Search results:
{context}"""

# VerticalRTK and FreshQA ask about "current" and "this year" facts, so the
# reader gets the date. FreshQA also has false-premise questions, where the
# correct answer says that the premise is false. The SimpleQA prompt is not changed.
DATED_PREFIX = "Today's date is {today}.\n"
FALSE_PREMISE_LINE = "If the question rests on a false premise, say so in one short sentence.\n"


class Timed:
    """Collects the duration of each HTTP attempt for one question."""

    def __init__(self):
        self.ok_s = 0.0  # sum of the successful attempts (a user waits this long)
        self.calls = 0

    def http(self, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        for attempt in range(3):
            self.calls += 1
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                    payload = r.read()
                self.ok_s += time.perf_counter() - t0
                return json.loads(payload)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))


def block(title, url, text):
    return f"{title or ''}\n{url or ''}\n{text or ''}".strip()


# Each search function returns (raw_response, list_of_text_blocks, aio_present).


def search_cloro(t, q, with_aio=True):
    body = {"query": q, "country": "US"}
    if with_aio:
        body["include"] = {"aioverview": {"markdown": True}}
    d = t.http("https://api.cloro.dev/v1/monitor/google", body, {"Authorization": f"Bearer {os.environ['CLORO_API_KEY']}"})
    r = d.get("result") or {}
    aio = with_aio and ((r.get("aioverview") or {}).get("text") or (r.get("aioverview") or {}).get("markdown"))
    parts = ["[AI Overview]\n" + aio] if aio else []
    kg = r.get("knowledgeGraph") or {}
    if kg.get("description") or kg.get("attributes"):
        attrs = "; ".join(f"{a.get('key')}: {a.get('value')}" for a in kg.get("attributes") or [])
        parts.append("[Knowledge graph]\n" + " ".join(filter(None, [kg.get("title"), kg.get("description"), attrs])))
    parts += [block(o.get("title"), o.get("link"), o.get("snippet")) for o in (r.get("organicResults") or [])[:NUM_RESULTS]]
    parts += [block(p.get("question"), None, p.get("answer") or p.get("snippet")) for p in r.get("peopleAlsoAsk") or [] if p.get("answer") or p.get("snippet")]
    return d, parts, bool(aio)


def serpapi_aio_text(aio):
    lines = []
    for b in aio.get("text_blocks") or []:
        if b.get("snippet"):
            lines.append(b["snippet"])
        for item in b.get("list") or []:
            lines.append("- " + " ".join(filter(None, [item.get("title"), item.get("snippet")])))
    return "\n".join(lines)


def search_serpapi(t, q):
    key = os.environ["SERPAPI_KEY"]
    params = {"engine": "google", "q": q, "gl": "us", "hl": "en", "no_cache": "true", "api_key": key}
    d = t.http("https://serpapi.com/search?" + urllib.parse.urlencode(params))
    aio = d.get("ai_overview") or {}
    if aio.get("page_token") and not aio.get("text_blocks"):
        # Google loads some AI Overviews in a second request. A user also waits for it.
        extra = t.http("https://serpapi.com/search?" + urllib.parse.urlencode(
            {"engine": "google_ai_overview", "page_token": aio["page_token"], "api_key": key}))
        aio = extra.get("ai_overview") or {}
        d["_ai_overview_followup"] = extra
    aio_text = serpapi_aio_text(aio)
    parts = ["[AI Overview]\n" + aio_text] if aio_text else []
    ab = d.get("answer_box") or {}
    if ab.get("answer") or ab.get("snippet"):
        parts.append("[Answer box]\n" + " ".join(filter(None, [ab.get("title"), ab.get("answer"), ab.get("snippet")])))
    kg = d.get("knowledge_graph") or {}
    if kg.get("description"):
        parts.append("[Knowledge graph]\n" + " ".join(filter(None, [kg.get("title"), kg.get("description")])))
    parts += [block(o.get("title"), o.get("link"), o.get("snippet")) for o in (d.get("organic_results") or [])[:NUM_RESULTS]]
    parts += [block(p.get("question"), None, p.get("snippet")) for p in d.get("related_questions") or [] if p.get("snippet")]
    return d, parts, bool(aio_text)


def search_tavily(t, q):
    d = t.http(
        "https://api.tavily.com/search",
        {"query": q, "search_depth": "basic", "max_results": NUM_RESULTS},
        {"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
    )
    return d, [block(r.get("title"), r.get("url"), r.get("content")) for r in d.get("results") or []], False


def search_exa(t, q):
    d = t.http(
        "https://api.exa.ai/search",
        {"query": q, "type": "auto", "numResults": NUM_RESULTS, "contents": {"highlights": True}},
        {"x-api-key": os.environ["EXA_API_KEY"]},
    )
    return d, [block(r.get("title"), r.get("url"), "\n".join(r.get("highlights") or [])) for r in d.get("results") or []], False


def search_parallel(t, q):
    d = t.http(
        "https://api.parallel.ai/v1/search",
        {"objective": q, "search_queries": [q], "advanced_settings": {"max_results": NUM_RESULTS}},
        {"x-api-key": os.environ["PARALLEL_API_KEY"]},
    )
    return d, [block(r.get("title"), r.get("url"), "\n".join(r.get("excerpts") or [])) for r in d.get("results") or []], False


def search_brave(t, q):
    params = {"q": q, "count": NUM_RESULTS, "extra_snippets": "true"}
    d = t.http(
        "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params),
        headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
    )
    results = (d.get("web") or {}).get("results") or []
    return d, [block(r.get("title"), r.get("url"), "\n".join([r.get("description") or ""] + (r.get("extra_snippets") or []))) for r in results], False


def search_specstream(t, q, with_answer=True):
    params = {"q": q, "num": NUM_RESULTS}
    d = t.http(
        "https://api.specstream.ai/v1/web-search?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {os.environ['SPECSTREAM_API_KEY']}"},
    )
    r = d.get("data") or {}
    answer = (r.get("answer") or {}) if with_answer else {}
    parts = ["[Answer]\n" + block(None, answer.get("page"), answer.get("value"))] if answer.get("value") else []
    parts += [block(s.get("title"), s.get("url"), s.get("snippet")) for s in (r.get("sources") or [])[:NUM_RESULTS]]
    return d, parts, False


PROVIDERS = {
    "cloro": search_cloro,
    "cloro-noaio": lambda t, q: search_cloro(t, q, with_aio=False),
    "serpapi": search_serpapi,
    "tavily": search_tavily,
    "exa": search_exa,
    "parallel": search_parallel,
    "brave": search_brave,
    "specstream": search_specstream,
    "specstream-noanswer": lambda t, q: search_specstream(t, q, with_answer=False),
}


def llm(model, prompt):
    d = Timed().http(
        "https://api.openai.com/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": prompt}]},
        {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    )
    return d["choices"][0]["message"]["content"].strip(), (d.get("usage") or {}).get("prompt_tokens")


def grade(question, target, predicted):
    out, _ = llm(GRADER_MODEL, GRADER_TEMPLATE.format(question=question, target=target, predicted_answer=predicted))
    m = re.search(r"(A|B|C)", out)
    return {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}[m.group(0) if m else "C"]


def run_one(provider, row, search_only, reader_prompt):
    rec = {"id": row["original_index"], "provider": provider, "problem": row["problem"], "gold": row["answer"], "topic": row["topic"]}
    t = Timed()
    try:
        raw, parts, aio_present = PROVIDERS[provider](t, row["problem"])
    except Exception as e:
        rec.update(search_ok=False, http_calls=t.calls, error=repr(e), grade="ERROR")
        return rec
    rec.update(search_ok=True, search_latency_s=round(t.ok_s, 3), http_calls=t.calls, aio_present=aio_present, n_blocks=len(parts))
    raw_dir = os.path.join(OUT, "raw", provider)
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, f"{row['original_index']}.json"), "w") as f:
        json.dump(raw, f, ensure_ascii=False)
    if search_only:
        return rec
    context = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(parts, 1))[:MAX_CONTEXT_CHARS]
    try:
        answer, prompt_tokens = llm(READER_MODEL, reader_prompt.format(question=row["problem"], context=context))
        rec.update(answer=answer, grade=grade(row["problem"], row["answer"], answer), context_chars=len(context), reader_prompt_tokens=prompt_tokens, error=None)
    except Exception as e:  # an LLM failure is ours, not the provider's
        rec.update(answer=None, grade="LLM_ERROR", error=repr(e))
    return rec


def pct(xs, p):
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def summarize():
    print("| Provider | n | Accuracy | Acc. given attempted | Search errors | Latency p50 | p90 | p99 | AI Overview |")
    print("|---|---|---|---|---|---|---|---|---|")
    for path in sorted(glob.glob(os.path.join(OUT, "results", "*.jsonl"))):
        rows = list({r["id"]: r for r in map(json.loads, open(path))}.values())
        name = os.path.basename(path)[:-6]
        n = len(rows)
        graded = [r for r in rows if r.get("grade") in ("CORRECT", "INCORRECT", "NOT_ATTEMPTED", "ERROR")]
        c = sum(r["grade"] == "CORRECT" for r in graded)
        i = sum(r["grade"] == "INCORRECT" for r in graded)
        acc = f"{c / len(graded):.1%}" if graded and (c + i) else "-"
        acc_att = f"{c / (c + i):.1%}" if c + i else "-"
        lat = sorted(r["search_latency_s"] for r in rows if r.get("search_ok"))
        errs = sum(not r.get("search_ok") for r in rows)
        aio = sum(bool(r.get("aio_present")) for r in rows)
        l50, l90, l99 = (f"{pct(lat, p):.2f}s" for p in (0.5, 0.9, 0.99)) if lat else ("-",) * 3
        print(f"| {name} | {n} | {acc} | {acc_att} | {errs} | {l50} | {l90} | {l99} | {aio}/{n} |")


def load_questions(name, n, seed):
    """Return rows with the keys original_index, problem, answer, topic."""
    url = DATASET_URLS[name]
    dataset = os.path.join(OUT, "data", os.path.basename(urllib.parse.urlparse(url).path) if name != "freshqa" else "freshqa.csv")
    if not os.path.exists(dataset):
        os.makedirs(os.path.dirname(dataset), exist_ok=True)
        urllib.request.urlretrieve(url, dataset)
    if name == "simpleqa":
        rows = list(csv.DictReader(open(dataset)))
    elif name == "verticalrtk":
        rows = [
            {"original_index": x["id"], "problem": x["query"], "answer": x["answer"], "topic": x["vertical"]}
            for x in map(json.loads, open(dataset))
            if not re.search(r"\d{4}-\d\d-\d\dT|captured|forecast", x["as_of"])
        ]
    else:  # freshqa: two banner lines before the header
        def reviewed_by(r):
            m = re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", r["next_review"])
            return m and date(int(m[3]), int(m[1]), int(m[2])) <= date.today()
        rows = [
            {"original_index": r["id"], "problem": r["question"],
             "answer": " OR ".join(r[f"answer_{i}"].strip() for i in range(10) if r[f"answer_{i}"].strip()),
             "topic": f"{r['fact_type']}{' false-premise' if r['false_premise'] == 'TRUE' else ''}"}
            for r in csv.DictReader(open(dataset).readlines()[2:])
            if r["split"] == "TEST" and r["fact_type"] != "fast-changing" and not reviewed_by(r)
        ]
    random.Random(seed).shuffle(rows)
    return rows[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", help="comma-separated: " + ",".join(PROVIDERS))
    ap.add_argument("--dataset", choices=list(DATASET_URLS), default="simpleqa")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--search-only", action="store_true", help="measure latency only, no LLM calls")
    ap.add_argument("--summary", action="store_true", help="print the results table and exit")
    ap.add_argument("--out", help="run folder (default: outputs/search-benchmark-<dataset>-<today>)")
    args = ap.parse_args()
    global OUT
    OUT = os.path.abspath(args.out) if args.out else os.path.join(REPO, "outputs", f"search-benchmark-{args.dataset}-{date.today()}")
    reader_prompt = READER_PROMPT
    if args.dataset != "simpleqa":
        reader_prompt = DATED_PREFIX.format(today=date.today()) + (FALSE_PREMISE_LINE if args.dataset == "freshqa" else "") + READER_PROMPT
    if args.summary:
        return summarize()
    providers = args.providers.split(",") if args.providers else []
    if not providers or any(p not in PROVIDERS for p in providers):
        ap.error("--providers must name one or more of: " + ",".join(PROVIDERS))

    os.makedirs(os.path.join(OUT, "results"), exist_ok=True)
    done, files = {}, {}
    for p in providers:
        path = os.path.join(OUT, "results", f"{p}{'.search-only' if args.search_only else ''}.jsonl")
        # A row with a search or LLM error runs again; summarize() keeps the last row per id.
        done[p] = {r["id"] for r in map(json.loads, open(path)) if r.get("grade") not in ("ERROR", "LLM_ERROR")} if os.path.exists(path) else set()
        files[p] = open(path, "a")
    # Interleave: question 1 on every provider, then question 2, and so on.
    tasks = [(p, row) for row in load_questions(args.dataset, args.n, args.seed) for p in providers if row["original_index"] not in done[p]]
    print(f"{len(tasks)} calls to run", file=sys.stderr)

    with cf.ThreadPoolExecutor(args.workers) as ex:
        for k, rec in enumerate(ex.map(lambda t: run_one(t[0], t[1], args.search_only, reader_prompt), tasks), 1):
            files[rec["provider"]].write(json.dumps(rec, ensure_ascii=False) + "\n")
            files[rec["provider"]].flush()
            if k % 25 == 0:
                print(f"{k}/{len(tasks)}", file=sys.stderr)
    summarize()


if __name__ == "__main__":
    main()
