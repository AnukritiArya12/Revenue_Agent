import csv, json, re, sys, time
from config import REPORTS_DIR
from agent.revenue_agent import DeepRevenueAgent

GROUND_TRUTH = "grouth_truth.jsonl"
RESULTS = "eval_results.csv"
HEADER = ["company", "file", "expected", "got", "value_ok", "page_ok",
          "scope_ok", "currency_ok", "error"]


def norm(t):
    return re.sub(r"[^a-z0-9]+", "", str(t).casefold())


def num(x):
    s = re.sub(r"[^\d.]", "", str(x or ""))
    try:
        return float(s)
    except ValueError:
        return None


def find_file(rec):
    if rec.get("file"):
        return rec["file"]
    files = [p for p in REPORTS_DIR.iterdir() if p.suffix.lower() in {".pdf", ".docx"}]
    hits = [p for p in files if norm(rec["company"]) in norm(p.stem)]
    if len(hits) > 1:
        hits = [p for p in hits if norm(rec.get("report", "")) in norm(p.stem)] or hits
    return hits[0].name if len(hits) == 1 else None


def save(row):
    """Insert or update this company's row; keep all other rows."""
    data = {}
    try:
        with open(RESULTS, encoding="utf-8", newline="") as f:
            for r in csv.reader(f):
                if r and r[0] != "company":
                    data[r[0]] = r
    except FileNotFoundError:
        pass
    data[row[0]] = [str(x) for x in row]
    with open(RESULTS, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(data.values())


def main():
    only = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    recs, seen = [], set()
    for line in open(GROUND_TRUTH, encoding="utf-8-sig"):
        if not line.strip():
            continue
        r = json.loads(line)
        key = (r["company"], r.get("report"))
        if key in seen or (only and only not in r["company"].lower()):
            continue
        seen.add(key)
        recs.append(r)

    if not recs:
        print(f"No company matched '{only}' in {GROUND_TRUTH}")
        return

    agent = DeepRevenueAgent()
    v_ok = p_ok = s_ok = c_ok = 0
    for r in recs:
        exp = float(r["expected_value"])
        fname = find_file(r)
        if not fname:
            print(f"[skip] {r['company']}: file not found in data/reports", flush=True)
            save([r["company"], "", exp, "", False, False, False, False, "no file"])
            continue

        t0, err, res = time.time(), "", None
        try:
            res = agent.extract_revenue(fname)
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:120]}"

        got = num(res.revenue_from_operations) if res else None
        value = got is not None and abs(got - exp) < 0.005
        page = bool(res) and r["source_page"] in res.source_pages
        scope = bool(res) and (r["scope"] == "unknown" or res.scope == r["scope"])
        cur = bool(res) and norm(res.currency) == norm(r["currency"])
        v_ok += value; p_ok += page; s_ok += scope; c_ok += cur

        row = [r["company"], fname, exp, got, value, page, scope, cur, err]
        print(row, f"{int(time.time() - t0)}s", flush=True)
        save(row)

    n = len(recs)
    print(f"\nTHIS RUN  -> Value: {v_ok}/{n} | Page: {p_ok}/{n} | "
          f"Scope: {s_ok}/{n} | Currency: {c_ok}/{n}")

    try:
        with open(RESULTS, encoding="utf-8", newline="") as f:
            stored = [r for r in csv.reader(f) if r][1:]
        t = len(stored)
        if t:
            print(f"ALL SAVED ({t}) -> "
                  f"Value: {sum(r[4] == 'True' for r in stored)}/{t} | "
                  f"Page: {sum(r[5] == 'True' for r in stored)}/{t}")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()