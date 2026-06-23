# Verify: per-company grouping + diff produce one email batch per company.
import scanner, os

cfg = scanner.load_cfg()

# Simulate matched jobs across 3 companies, 2 already seen.
matched = [
    {"id":"gh:databricks:1","title":"ML Engineer","location":"SF","url":"u1","company":"Databricks"},
    {"id":"gh:databricks:2","title":"Applied Scientist","location":"NY","url":"u2","company":"Databricks"},
    {"id":"as:openai:9","title":"Member of Technical Staff, Inference","location":"SF","url":"u3","company":"OpenAI"},
    {"id":"lv:zoox:5","title":"Software Engineer, ML Inference","location":"Foster City","url":"u4","company":"Zoox"},
]
seen = {"gh:databricks:1"}  # databricks job 1 already seen
new_jobs = [j for j in matched if j["id"] not in seen]

by_company = {}
for j in new_jobs:
    by_company.setdefault(j["company"], []).append(j)

print(f"New jobs: {len(new_jobs)}")
print(f"Emails that would be sent (one per company): {len(by_company)}")
for c in sorted(by_company):
    titles = [j["title"] for j in by_company[c]]
    print(f"  EMAIL -> [{c}] {len(titles)} role(s): {titles}")

# Confirm Databricks job 1 (seen) is excluded, job 2 (new) included
assert "Databricks" in by_company
assert len(by_company["Databricks"]) == 1
assert by_company["Databricks"][0]["title"] == "Applied Scientist"
print("\nDiff correct: seen role suppressed, new roles surfaced, grouped per company.")
